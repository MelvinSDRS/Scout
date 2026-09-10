"""Preserve the user's Marketplace preferences across regional searches."""

import re
from dataclasses import asdict, dataclass

from .facebook_data import radius_option

LOCATION_BUTTON = re.compile(r"Dans un rayon de|Within .* (?:km|miles)|within .* (?:km|miles)")
APPLY_BUTTON = re.compile(r"^(Appliquer|Apply)$")


@dataclass(frozen=True)
class HomeLocation:
    location: str
    radius: str


async def open_location(page):
    await page.get_by_text(LOCATION_BUTTON).first.click(timeout=10000)
    dialog = page.get_by_role("dialog")
    await dialog.wait_for(state="visible", timeout=10000)
    return dialog


def location_input(dialog):
    return dialog.locator('input:not([type]), input[type="text"], input[type="search"]').first


async def read_location(dialog):
    location = (await location_input(dialog).input_value(timeout=10000)).strip()
    radius_control = dialog.get_by_role("combobox").last
    radius = (await radius_control.inner_text()).strip()
    if not radius:
        radius = (await radius_control.input_value()).strip()
    if not location or radius_option(radius) is None:
        raise RuntimeError(
            "Could not capture the original Facebook location and radius; scan stopped"
        )
    return HomeLocation(location, radius)


async def marketplace_home(page, check_access):
    response = await page.goto(
        "https://www.facebook.com/marketplace/", wait_until="domcontentloaded", timeout=45000
    )
    if response and response.status in (401, 403, 429):
        # Use the provider's existing access-error semantics.
        from ..models import AccessBlocked

        raise AccessBlocked(f"Facebook HTTP {response.status}; login or rate limit")
    await page.wait_for_timeout(3000)
    await check_access(page)


async def capture_home(page, check_access):
    await marketplace_home(page, check_access)
    home = await read_location(await open_location(page))
    await page.keyboard.press("Escape")
    return home


async def restore_home(page, home, check_access):
    await marketplace_home(page, check_access)
    dialog = await open_location(page)
    current = await read_location(dialog)
    if current.location != home.location:
        await location_input(dialog).fill(home.location)
        # Never guess among cities with the same name or choose the first suggestion.
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
        # A different country can switch units. Only accept the same distance.
        matches = [label for label in labels if radius_option(label) == radius_option(home.radius)]
    if len(matches) != 1:
        raise RuntimeError("The original Facebook radius is unavailable; restoration needs a retry")
    await page.get_by_role("option", name=matches[0], exact=True).click(timeout=10000)
    await dialog.get_by_role("button", name=APPLY_BUTTON).click()
    await dialog.wait_for(state="hidden", timeout=10000)
    await page.wait_for_timeout(3000)
    # Read a fresh page so optimistic dialog state is not mistaken for saved preferences.
    restored = await capture_home(page, check_access)
    if restored.location != home.location or radius_option(restored.radius) != radius_option(
        home.radius
    ):
        raise RuntimeError(
            "Could not verify restoration of the original Facebook location and radius"
        )


def save_home(path, home):
    import json

    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        temporary.chmod(0o600)
        json.dump(asdict(home), handle)
    temporary.replace(path)
