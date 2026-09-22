"""Preserve the user's Marketplace preferences across regional searches."""

import json
import re
from dataclasses import asdict, dataclass, replace
from decimal import Decimal

from .facebook_data import objects

LOCATION_BUTTON = re.compile(
    r"(?:Dans un rayon de|Within)\s+\d+(?:[.,]\d+)?\s*(?:km|kilom[èe]tres?|mi(?:les?)?)\b",
    re.IGNORECASE,
)
APPLY_BUTTON = re.compile(r"^(Appliquer|Apply)$")
PARTNER_DIALOG_TITLE = re.compile(
    r"(?:Explorez plus d['’]articles|Explore more (?:items|listings))", re.IGNORECASE
)
PARTNER_DIALOG_BODY = re.compile(
    r"(?:annonces partenaires|partner (?:ads|listings|offers))", re.IGNORECASE
)
PARTNER_UPDATE_BUTTON = re.compile(r"^(?:Mettre à jour|Update)$", re.IGNORECASE)
PARTNER_CHECKBOX_NAMES = (
    re.compile(r"^buycycle-fr$", re.IGNORECASE),
    re.compile(r"^Catawiki$", re.IGNORECASE),
    re.compile(r"^eBay$", re.IGNORECASE),
    re.compile(r"^Gul&Gratis$", re.IGNORECASE),
    re.compile(r"^Sellpy$", re.IGNORECASE),
)
FACEBOOK_PARTNER_NAME = re.compile(r"^Facebook\s+Marketplace$", re.IGNORECASE)
AUTO_PARTNER_NAME = re.compile(
    r"^(?:Sélectionnez automatiquement de nouveaux partenaires|"
    r"Automatically (?:select|add) new partners)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HomeLocation:
    location: str
    radius: str
    city_id: str | None = None

    def __post_init__(self):
        if self.city_id is not None and not re.fullmatch(r"[0-9]+", self.city_id):
            raise ValueError("Invalid saved Facebook city identifier")


async def open_location(page):
    # Empty-result messages also mention a radius; only the location button opens
    # the controls. US pages abbreviate miles as "mi" in this button.
    await page.get_by_role("button", name=LOCATION_BUTTON).click(timeout=10000)
    dialog = (
        page.get_by_role("dialog")
        .filter(has=page.locator('input:not([type]), input[type="text"], input[type="search"]'))
        .filter(has=page.get_by_role("combobox"))
    )
    # The click may return before React mounts the dialog. Keep Playwright's
    # bounded wait and strictness, while excluding unrelated visible dialogs.
    await dialog.wait_for(state="visible", timeout=10000)
    return dialog


async def configure_partner_selection(page):
    """Restrict Facebook's partner dialog to Marketplace itself when present.

    Facebook may show this consent dialog before the location controls. Only a
    dialog with both known partner markers is actionable; an unknown dialog is
    left untouched and reported as an error rather than receiving generic clicks.
    """
    dialogs = page.get_by_role("dialog")
    candidates = []
    for index in range(await dialogs.count()):
        dialog = dialogs.nth(index)
        if not await dialog.is_visible():
            continue
        text = await dialog.inner_text()
        if PARTNER_DIALOG_TITLE.search(text) and PARTNER_DIALOG_BODY.search(text):
            candidates.append(dialog)
    if not candidates:
        return False
    if len(candidates) != 1:
        raise RuntimeError("Facebook partner selection is ambiguous; restoration stopped")

    dialog = candidates[0]
    checkboxes = dialog.get_by_role("checkbox")
    expected_count = len(PARTNER_CHECKBOX_NAMES) + 2
    if await checkboxes.count() != expected_count:
        raise RuntimeError("Facebook partner selection is unrecognized; restoration stopped")

    controls = []
    for name in PARTNER_CHECKBOX_NAMES:
        control = dialog.get_by_role("checkbox", name=name)
        if await control.count() != 1:
            raise RuntimeError("Facebook partner selection is unrecognized; restoration stopped")
        controls.append((control, False))
    facebook = dialog.get_by_role("checkbox", name=FACEBOOK_PARTNER_NAME)
    automatic = dialog.get_by_role("checkbox", name=AUTO_PARTNER_NAME)
    if await facebook.count() != 1 or await automatic.count() != 1:
        raise RuntimeError("Facebook partner selection is unrecognized; restoration stopped")
    controls.extend(((facebook, True), (automatic, False)))

    update = dialog.get_by_role("button", name=PARTNER_UPDATE_BUTTON)
    if await update.count() != 1:
        raise RuntimeError("Facebook partner selection is unrecognized; restoration stopped")
    for control, should_be_checked in controls:
        if await control.is_checked(timeout=10000) != should_be_checked:
            if should_be_checked:
                await control.check(timeout=10000)
            else:
                await control.uncheck(timeout=10000)
    for control, should_be_checked in controls:
        if await control.is_checked(timeout=10000) != should_be_checked:
            raise RuntimeError(
                "Facebook partner selection could not be verified; restoration stopped"
            )
    await update.click(timeout=10000)
    await dialog.wait_for(state="hidden", timeout=10000)
    return True


def location_input(dialog):
    return dialog.locator('input:not([type]), input[type="text"], input[type="search"]').first


def radius_distance(text):
    """Exact kilometres for preference restoration; do not round unlike scan coverage."""
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(kilom|km|mile)", text.casefold())
    if not match:
        return None
    value = Decimal(match[1].replace(",", "."))
    return value * Decimal("1.609344") if match[2] == "mile" else value


async def read_location(dialog, require_radius=True):
    location = (await location_input(dialog).input_value(timeout=10000)).strip()
    radius_control = dialog.get_by_role("combobox").last
    radius = (await radius_control.inner_text()).strip()
    if not radius and await radius_control.evaluate(
        "el => ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)"
    ):
        radius = (await radius_control.input_value()).strip()
    if not location or (require_radius and radius_distance(radius) is None):
        raise RuntimeError(
            "Could not capture the original Facebook location and radius; scan stopped"
        )
    return HomeLocation(location, radius)


async def marketplace_home(page, check_access, city_id=None):
    response = await page.goto(
        "https://www.facebook.com/marketplace/" + (f"{city_id}/" if city_id else ""),
        wait_until="domcontentloaded",
        timeout=45000,
    )
    if response and response.status in (401, 403, 429):
        # Use the provider's existing access-error semantics.
        from ..models import AccessBlocked

        raise AccessBlocked(f"Facebook HTTP {response.status}; login or rate limit")
    await page.wait_for_timeout(3000)
    await check_access(page)
    await configure_partner_selection(page)


async def capture_home(page, check_access):
    await marketplace_home(page, check_access)
    home = await read_location(await open_location(page))
    await page.keyboard.press("Escape")
    return await identify_city(page, home)


async def identify_city(page, home):
    documents = []
    for text in await page.locator('script[type="application/json"]').all_text_contents():
        try:
            documents.append(json.loads(text))
        except ValueError:
            continue
    identities = set()
    for obj in objects(documents):
        stories = obj.get("marketplace_feed_stories")
        if not isinstance(stories, dict):
            continue
        selected = stories.get("buy_location")
        if not isinstance(selected, dict):
            continue
        ident = selected.get("id")
        if (
            isinstance(ident, str)
            and re.fullmatch(r"[0-9]+", ident)
            and isinstance(selected.get("display_name"), str)
        ):
            identities.add((ident, selected.get("display_name")))
    if len(identities) != 1 or next(iter(identities))[1] != home.location:
        raise RuntimeError("Could not identify the original Facebook city; scan stopped")
    return replace(home, city_id=next(iter(identities))[0])


async def verify_intermediate_city(page, home, check_access):
    # A radius converted from miles may have no label in the metric dropdown.
    # Verify only the city at this intermediate step; the final check stays strict.
    await marketplace_home(page, check_access)
    current = await read_location(await open_location(page), require_radius=False)
    await page.keyboard.press("Escape")
    if not same_city(await identify_city(page, current), home):
        raise RuntimeError("Could not verify restoration of the original Facebook city")


def same_city(actual, expected):
    return (
        actual.city_id == expected.city_id
        if expected.city_id is not None
        else actual.location == expected.location
    )


async def apply_location(dialog, page):
    await dialog.get_by_role("button", name=APPLY_BUTTON).click()
    await dialog.wait_for(state="hidden", timeout=10000)
    await page.wait_for_timeout(3000)


async def restore_home(page, home, check_access):
    if radius_distance(home.radius) is None or radius_distance(home.radius) <= 0:
        raise RuntimeError("The saved Facebook radius is invalid; restoration stopped")
    # A name-only typeahead is relative to the current country. The captured city
    # identifier avoids homonyms and suggestion labels such as "Montréal Ville".
    for attempt in range(2):
        await marketplace_home(page, check_access, home.city_id)
        dialog = await open_location(page)
        current = await read_location(dialog, require_radius=False)
        if home.city_id is None and current.location != home.location:
            # Compatibility for old snapshots. Never guess among same-name cities.
            await location_input(dialog).fill(home.location)
            suggestion = page.get_by_role("option", name=home.location, exact=True).or_(
                dialog.get_by_role("button", name=home.location, exact=True)
            )
            await suggestion.click(timeout=10000)
        await dialog.get_by_role("combobox").last.click()
        options = page.get_by_role("option")
        await options.first.wait_for(state="visible", timeout=10000)
        labels = await options.all_text_contents()
        matches = [label for label in labels if label.strip() == home.radius]
        if not matches:
            matches = [
                label for label in labels if radius_distance(label) == radius_distance(home.radius)
            ]
        if len(matches) > 1:
            raise RuntimeError(
                "The original Facebook radius is ambiguous; restoration needs a retry"
            )
        if matches:
            await page.get_by_role("option", name=matches[0], exact=True).click(timeout=10000)
            await apply_location(dialog, page)
            restored = await capture_home(page, check_access)
            if not same_city(restored, home) or radius_distance(restored.radius) != radius_distance(
                home.radius
            ):
                raise RuntimeError(
                    "Could not verify restoration of the original Facebook location and radius"
                )
            return
        if attempt or home.city_id is None:
            raise RuntimeError(
                "The original Facebook radius is unavailable; restoration needs a retry"
            )
        # US options cannot express every metric radius (e.g. 10 km). Persist the
        # target city with the current radius first, verify its identity, then
        # reload its unit system and apply the original distance. Keep the durable
        # snapshot untouched until BOTH city and radius have been verified.
        existing = [
            label for label in labels if radius_distance(label) == radius_distance(current.radius)
        ]
        if len(existing) != 1:
            raise RuntimeError("Could not preserve the current radius while restoring the city")
        await page.get_by_role("option", name=existing[0], exact=True).click(timeout=10000)
        await apply_location(dialog, page)
        await verify_intermediate_city(page, home, check_access)


def save_home(path, home):
    import json

    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        temporary.chmod(0o600)
        json.dump(asdict(home), handle)
    temporary.replace(path)
