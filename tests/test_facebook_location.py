"""Exercise saved Marketplace preferences using an isolated browser and fake server."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scout.models import AccessBlocked
from scout.providers.facebook import REGIONS, Facebook
from scout.providers.facebook_location import (
    HomeLocation,
    capture_home,
    restore_home,
    save_home,
)
from scout.settings import Settings

HOME_CITY_ID = "101010"
SAME_NAME_CITY_ID = "202020"
WRONG_CITY_ID = "303030"
SAME_NAME_CITY = "Springfield"


@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "blocked", "retry"])
def test_search_restores_original_preferences(tmp_path, monkeypatch, outcome):
    browsers = Path("data/browsers").resolve()
    if not browsers.exists():
        if os.environ.get("CI"):
            pytest.fail("Chromium is required in CI")
        pytest.skip("Install project Chromium to run the browser regression")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    original = {
        "location": SAME_NAME_CITY,
        "radius": "25 km",
        "city_id": HOME_CITY_ID,
    }
    state = dict(original)
    route_locations = {
        HOME_CITY_ID: SAME_NAME_CITY,
        SAME_NAME_CITY_ID: SAME_NAME_CITY,
        WRONG_CITY_ID: SAME_NAME_CITY,
    }
    applied = []
    navigations = []
    suggestion_selections = []
    fail_restore = outcome == "retry"

    async def exercise():
        nonlocal fail_restore
        fb = Facebook(Settings(tmp_path))
        snapshot = tmp_path / "facebook-home.json"
        try:
            await fb.start()

            async def route(route):
                url = route.request.url
                navigations.append(url)
                if url.endswith("/preferences"):
                    submitted = json.loads(route.request.post_data)
                    applied.append(submitted)
                    if not fail_restore:
                        state.update(submitted)
                    await route.fulfill(json={"ok": True})
                    return
                if url.endswith("/suggestion-selected"):
                    suggestion_selections.append(url)
                    await route.fulfill(json={"ok": True})
                    return
                if "/search?" in url:
                    # The scan route uses a different canonical city with the
                    # same display name, which a typeahead cannot disambiguate.
                    state.update(city_id=SAME_NAME_CITY_ID, radius="500 km")
                route_city_id = state["city_id"]
                marker = "/marketplace/"
                if marker in url:
                    suffix = url.split(marker, 1)[1]
                    candidate = suffix.split("/", 1)[0]
                    if candidate.isdigit():
                        route_city_id = candidate
                dialog_location = route_locations.get(route_city_id, state["location"])
                data = {
                    "params": {"filter_radius_km": 500},
                    "viewer": {
                        "marketplace_feed_stories": {
                            "buy_location": {
                                "id": state["city_id"],
                                "display_name": state["location"],
                            }
                        }
                    },
                    "payload": {
                        "marketplace_feed_stories": {
                            "buy_location": {
                                "id": state["city_id"],
                                "display_name": state["location"],
                            }
                        }
                    },
                    "marketplace_search": {
                        "feed_units": {
                            "edges": [
                                {
                                    "node": {
                                        "__typename": "MarketplaceSearchFeedNoResults",
                                        "is_main_results_empty": True,
                                        "is_relaxation_results_empty": True,
                                    }
                                }
                            ],
                            "page_info": {"has_next_page": False},
                        }
                    },
                }
                html = (
                    """<html><body>
<button onclick="document.querySelector('[role=dialog]').hidden=false">Within 500 km</button>
<div role="dialog" hidden>
<input type="text" role="combobox" id="city">
<div id="suggestions"></div>
<button role="combobox" id="radius" onclick="document.querySelector('#options').hidden=false"></button>
<div id="options" hidden>
<div role="option" onclick="chooseRadius(this)">25 km</div>
<div role="option" onclick="chooseRadius(this)">500 km</div>
</div>
<button onclick="apply()">Apply</button>
</div><main role="main" style="height:18000px">No results</main>
<script>
const city=document.querySelector('#city'), radius=document.querySelector('#radius');
const suggestions=document.querySelector('#suggestions');
city.addEventListener('input',()=>{
 suggestions.replaceChildren();
 ["""
                    + json.dumps(SAME_NAME_CITY)
                    + """, """
                    + json.dumps(SAME_NAME_CITY)
                    + """].forEach(label=>{
  const option=document.createElement('div');
  option.role='option';option.textContent=label;
  option.onclick=async()=>{
   await fetch('/suggestion-selected');
   city.value=option.textContent;suggestions.replaceChildren();
  };
  suggestions.append(option);
 });
});
function chooseRadius(option){radius.textContent=option.textContent;document.querySelector('#options').hidden=true;}
async function apply(){
 await fetch('/preferences',{method:'POST',body:JSON.stringify({location:city.value,radius:radius.textContent,city_id:"""
                    + json.dumps(route_city_id)
                    + """})});
 document.querySelector('[role=dialog]').hidden=true;
}
addEventListener('keydown',event=>{if(event.key==='Escape')document.querySelector('[role=dialog]').hidden=true;});
"""
                    + "city.value="
                    + json.dumps(dialog_location)
                    + ";radius.textContent="
                    + json.dumps(state["radius"])
                    + ";</script>"
                )
                html += (
                    '<script type="application/json">'
                    + json.dumps(data)
                    + "</script></body></html>"
                )
                await route.fulfill(content_type="text/html", body=html)

            await fb.context.route("**/*", route)
            # Keep all DOM interaction/navigation real; shorten only fixed scan pacing.
            from playwright.async_api import Page

            async def settle(page, milliseconds):
                await page.wait_for_load_state("networkidle")

            monkeypatch.setattr(Page, "wait_for_timeout", settle)
            if outcome in ("error", "cancel", "blocked"):

                async def interrupted(*args):
                    state.update(location="Scan City, Ontario", radius="500 km")
                    if outcome == "cancel":
                        asyncio.current_task().cancel()
                        await asyncio.sleep(0)
                    if outcome == "blocked":
                        raise AccessBlocked("Facebook access restricted")
                    raise RuntimeError("Search failed")

                monkeypatch.setattr(fb, "_search", interrupted)
                expected = {
                    "error": RuntimeError,
                    "cancel": asyncio.CancelledError,
                    "blocked": AccessBlocked,
                }[outcome]
                with pytest.raises(expected):
                    await fb.search("chair", "CA", REGIONS["CA"][0]["city"])
            elif outcome == "retry":
                with pytest.raises(RuntimeError, match="Could not restore"):
                    await fb.search("chair", "CA", REGIONS["CA"][0]["city"])
                assert json.loads(snapshot.read_text()) == original
                assert state != original
                fail_restore = False
                # A new provider instance recovers the durable snapshot before scanning.
                await fb.close()
                fb = Facebook(Settings(tmp_path))
                await fb.start()
                await fb.context.route("**/*", route)
                result = await fb.search("chair", "CA", REGIONS["CA"][0]["city"])
                assert result.listings == []
            else:
                result = await fb.search("chair", "CA", REGIONS["CA"][0]["city"])
                assert result.listings == []
                assert result.radius_km == 500
            assert applied[-1] == original
            assert state == original
            assert any(f"/marketplace/{HOME_CITY_ID}/" in url for url in navigations)
            assert not suggestion_selections
            assert not snapshot.exists()
            assert len(fb.context.pages) == 1  # Only the initial persistent-context tab remains.
        finally:
            await fb.close()

    asyncio.run(exercise())


def _location_browser_html(
    data, location=SAME_NAME_CITY, radius="25 km", options=("25 km", "500 km")
):
    """Build the small location dialog used by capture and restore tests."""
    option_markup = "".join(
        f'<div role="option" onclick="chooseRadius(this)">{option}</div>' for option in options
    )
    return (
        """<html><body>
<button onclick="document.querySelector('[role=dialog]').hidden=false">Within 25 km</button>
<div role="dialog" hidden>
<input type="text" role="combobox" id="city">
<div id="suggestions"></div>
<button role="combobox" id="radius" onclick="document.querySelector('#options').hidden=false"></button>
<div id="options" hidden>
"""
        + option_markup
        + """
</div>
<button onclick="apply()">Apply</button>
</div><main role="main">No results</main>
<script>
const city=document.querySelector('#city'), radius=document.querySelector('#radius');
const suggestions=document.querySelector('#suggestions');
city.addEventListener('input',()=>{
 suggestions.replaceChildren();
 ["""
        + json.dumps(SAME_NAME_CITY)
        + """, """
        + json.dumps(SAME_NAME_CITY)
        + """].forEach(label=>{
  const option=document.createElement('div');
  option.role='option';option.textContent=label;
  option.onclick=async()=>{
   await fetch('/suggestion-selected');
   city.value=option.textContent;suggestions.replaceChildren();
  };
  suggestions.append(option);
 });
});
function chooseRadius(option){radius.textContent=option.textContent;document.querySelector('#options').hidden=true;}
async function apply(){
 await fetch('/preferences',{method:'POST',body:JSON.stringify({location:city.value,radius:radius.textContent})});
 document.querySelector('[role=dialog]').hidden=true;
}
addEventListener('keydown',event=>{if(event.key==='Escape')document.querySelector('[role=dialog]').hidden=true;});
"""
        + "city.value="
        + json.dumps(location)
        + ";radius.textContent="
        + json.dumps(radius)
        + ";</script>"
        + '<script type="application/json">'
        + json.dumps(data)
        + "</script></body></html>"
    )


def _fast_browser_waits(monkeypatch):
    from playwright.async_api import Page

    async def settle(page, milliseconds):
        await page.wait_for_load_state("networkidle")

    monkeypatch.setattr(Page, "wait_for_timeout", settle)


def _partner_page_html(
    data,
    *,
    partner_names=(
        "buycycle-fr",
        "Catawiki",
        "eBay",
        "Facebook Marketplace",
        "Gul&Gratis",
        "Sellpy",
    ),
    unrelated=False,
):
    if unrelated:
        partner_markup = """<div role="dialog" id="unrelated-dialog">
<h2>Unrelated settings</h2><button>Do not touch</button></div>"""
    else:
        checkboxes = "".join(
            f'<label><input type="checkbox" checked>{name}</label>' for name in partner_names
        )
        checkboxes += (
            '<label><input type="checkbox" checked>'
            "Sélectionnez automatiquement de nouveaux partenaires</label>"
        )
        partner_markup = """<div role="dialog" id="partner-dialog">
<h2>Explorez plus d’articles</h2>
<p>annonces partenaires</p>
%s
<button id="partner-update">Mettre à jour</button>
<script>
document.querySelector('#partner-update').onclick = async () => {
  const selected = [...document.querySelectorAll('#partner-dialog input')]
    .filter(input => input.checked).map(input => input.parentElement.textContent.trim());
  const automatic = document.querySelectorAll('#partner-dialog input')[%d].checked;
  await fetch('/partner-preferences', {method:'POST', body:JSON.stringify({selected, automatic})});
  document.querySelector('#partner-dialog').hidden = true;
};
</script></div>""" % (checkboxes, len(partner_names))
    return _location_browser_html(data).replace(
        "</body></html>", partner_markup + "</body></html>", 1
    )


def _partner_browser_setup(tmp_path, monkeypatch, html, updates):
    browsers = Path("data/browsers").resolve()
    if not browsers.exists():
        if os.environ.get("CI"):
            pytest.fail("Chromium is required in CI")
        pytest.skip("Install project Chromium to run the browser regression")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))

    async def route(route):
        if route.request.url.endswith("/partner-preferences"):
            updates.append(json.loads(route.request.post_data))
            await route.fulfill(json={"ok": True})
            return
        await route.fulfill(content_type="text/html; charset=utf-8", body=html)

    return route


def _partner_data():
    return {
        "viewer": {
            "marketplace_feed_stories": {
                "buy_location": {"id": HOME_CITY_ID, "display_name": SAME_NAME_CITY}
            }
        }
    }


def test_partner_selection_keeps_only_facebook_marketplace(tmp_path, monkeypatch):
    updates = []
    html = _partner_page_html(_partner_data())

    async def exercise():
        route = _partner_browser_setup(tmp_path, monkeypatch, html, updates)
        _fast_browser_waits(monkeypatch)
        fb = Facebook(Settings(tmp_path))
        page = None
        try:
            await fb.start()
            await fb.context.route("**/*", route)
            page = await fb.context.new_page()
            home = await capture_home(page, lambda _page: asyncio.sleep(0))
            assert home == HomeLocation(SAME_NAME_CITY, "25 km", HOME_CITY_ID)
            assert updates == [{"selected": ["Facebook Marketplace"], "automatic": False}]
        finally:
            if page is not None:
                await page.close()
            await fb.close()

    asyncio.run(exercise())


def test_location_selection_waits_for_delayed_dialog_render(tmp_path, monkeypatch):
    updates = []
    html = _partner_page_html(_partner_data(), unrelated=True).replace(
        "document.querySelector('[role=dialog]').hidden=false",
        "setTimeout(() => document.querySelector('[role=dialog]').hidden=false, 150)",
        1,
    )

    async def exercise():
        route = _partner_browser_setup(tmp_path, monkeypatch, html, updates)
        _fast_browser_waits(monkeypatch)
        fb = Facebook(Settings(tmp_path))
        try:
            await fb.start()
            await fb.context.route("**/*", route)
            page = await fb.context.new_page()
            home = await capture_home(page, lambda _page: asyncio.sleep(0))
            assert home == HomeLocation(SAME_NAME_CITY, "25 km", HOME_CITY_ID)
            assert not updates
        finally:
            await fb.close()

    asyncio.run(exercise())


def test_unrelated_dialog_is_untouched(tmp_path, monkeypatch):
    updates = []
    html = _partner_page_html(_partner_data(), unrelated=True)

    async def exercise():
        route = _partner_browser_setup(tmp_path, monkeypatch, html, updates)
        _fast_browser_waits(monkeypatch)
        fb = Facebook(Settings(tmp_path))
        page = None
        try:
            await fb.start()
            await fb.context.route("**/*", route)
            page = await fb.context.new_page()
            home = await capture_home(page, lambda _page: asyncio.sleep(0))
            assert home == HomeLocation(SAME_NAME_CITY, "25 km", HOME_CITY_ID)
            assert not updates
        finally:
            if page is not None:
                await page.close()
            await fb.close()

    asyncio.run(exercise())


def test_unrecognized_partner_dialog_preserves_snapshot_on_restore_failure(tmp_path, monkeypatch):
    updates = []
    unknown_names = (
        "buycycle-fr",
        "Catawiki",
        "eBay",
        "Facebook Marketplace",
        "Gul&Gratis",
        "Mystery",
    )
    html = _partner_page_html(_partner_data(), partner_names=unknown_names)

    async def exercise():
        route = _partner_browser_setup(tmp_path, monkeypatch, html, updates)
        _fast_browser_waits(monkeypatch)
        fb = Facebook(Settings(tmp_path))
        snapshot = tmp_path / "facebook-home.json"
        home = HomeLocation(SAME_NAME_CITY, "25 km", HOME_CITY_ID)
        save_home(snapshot, home)
        try:
            await fb.start()
            await fb.context.route("**/*", route)
            with pytest.raises(RuntimeError, match="Could not restore"):
                await fb.search("chair", "CA", REGIONS["CA"][0]["city"])
            assert HomeLocation(**json.loads(snapshot.read_text())) == home
            assert not updates
        finally:
            await fb.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("identity_case", ["missing", "conflicting"])
def test_capture_requires_one_matching_canonical_city_id_before_scan(
    tmp_path, monkeypatch, identity_case
):
    browsers = Path("data/browsers").resolve()
    if not browsers.exists():
        if os.environ.get("CI"):
            pytest.fail("Chromium is required in CI")
        pytest.skip("Install project Chromium to run the browser regression")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    if identity_case == "missing":
        locations = [{"id": None, "display_name": SAME_NAME_CITY}]
    else:
        locations = [
            {"id": HOME_CITY_ID, "display_name": SAME_NAME_CITY},
            {"id": SAME_NAME_CITY_ID, "display_name": SAME_NAME_CITY},
        ]
    data = {
        "viewer": {
            "marketplace_feed_stories": {
                "buy_location": locations[0],
            }
        }
    }
    if identity_case == "conflicting":
        data["payload"] = {"marketplace_feed_stories": {"buy_location": locations[1]}}

    async def exercise():
        fb = Facebook(Settings(tmp_path))
        try:
            await fb.start()

            async def route(route):
                await route.fulfill(content_type="text/html", body=_location_browser_html(data))

            await fb.context.route("**/*", route)
            _fast_browser_waits(monkeypatch)
            scan = AsyncMock()
            monkeypatch.setattr(fb, "_search", scan)
            with pytest.raises(RuntimeError, match="Could not identify the original Facebook city"):
                await fb.search("chair", "CA", REGIONS["CA"][0]["city"])
            scan.assert_not_awaited()
            assert not (tmp_path / "facebook-home.json").exists()
        finally:
            await fb.close()

    asyncio.run(exercise())


def test_restore_rejects_wrong_city_id_with_identical_display_name(tmp_path, monkeypatch):
    browsers = Path("data/browsers").resolve()
    if not browsers.exists():
        if os.environ.get("CI"):
            pytest.fail("Chromium is required in CI")
        pytest.skip("Install project Chromium to run the browser regression")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    account = {
        "location": SAME_NAME_CITY,
        "radius": "500 km",
        "city_id": HOME_CITY_ID,
    }
    navigations = []
    suggestion_selections = []

    async def exercise():
        fb = Facebook(Settings(tmp_path))
        page = None
        try:
            await fb.start()

            async def route(route):
                url = route.request.url
                navigations.append(url)
                if url.endswith("/suggestion-selected"):
                    suggestion_selections.append(url)
                    await route.fulfill(json={"ok": True})
                    return
                if url.endswith("/preferences"):
                    submitted = json.loads(route.request.post_data)
                    account["radius"] = submitted["radius"]
                    await route.fulfill(json={"ok": True})
                    return
                data = {
                    "viewer": {
                        "marketplace_feed_stories": {
                            # The dialog has the same label for the wrong route,
                            # while persisted account state remains HOME_CITY_ID.
                            "buy_location": {
                                "id": account["city_id"],
                                "display_name": account["location"],
                            }
                        }
                    }
                }
                await route.fulfill(
                    content_type="text/html",
                    body=_location_browser_html(data, account["location"], account["radius"]),
                )

            await fb.context.route("**/*", route)
            _fast_browser_waits(monkeypatch)
            page = await fb.context.new_page()
            home = HomeLocation(SAME_NAME_CITY, "25 km", WRONG_CITY_ID)

            async def allow_access(_page):
                return None

            with pytest.raises(RuntimeError, match="Could not verify restoration"):
                await restore_home(page, home, allow_access)
            assert any(f"/marketplace/{WRONG_CITY_ID}/" in url for url in navigations)
            assert not suggestion_selections
        finally:
            if page is not None:
                await page.close()
            await fb.close()

    asyncio.run(exercise())


def test_restore_handles_blank_radius_during_city_unit_transition(tmp_path, monkeypatch):
    browsers = Path("data/browsers").resolve()
    if not browsers.exists():
        if os.environ.get("CI"):
            pytest.fail("Chromium is required in CI")
        pytest.skip("Install project Chromium to run the browser regression")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    account = {
        "location": SAME_NAME_CITY,
        "radius": "500 miles",
        "city_id": SAME_NAME_CITY_ID,
    }
    intermediate_pending = False
    blank_dialog_seen = False
    applied = []

    async def exercise():
        nonlocal intermediate_pending, blank_dialog_seen
        fb = Facebook(Settings(tmp_path))
        page = None
        try:
            await fb.start()

            async def route(route):
                nonlocal intermediate_pending, blank_dialog_seen
                url = route.request.url
                if url.endswith("/preferences"):
                    submitted = json.loads(route.request.post_data)
                    applied.append(submitted)
                    account.update(submitted)
                    account["city_id"] = HOME_CITY_ID
                    intermediate_pending = submitted["radius"] == "500 miles"
                    await route.fulfill(json={"ok": True})
                    return
                base_url = "https://www.facebook.com/marketplace/"
                if url == base_url and intermediate_pending:
                    # Facebook briefly renders a non numeric distance while
                    # switching the dialog from imperial to metric units.
                    radius = "Rayon\n \n"
                    blank_dialog_seen = True
                    intermediate_pending = False
                    options = ("10 km", "25 km")
                elif "/marketplace/" in url and HOME_CITY_ID in url:
                    radius = account["radius"]
                    options = ("1 mile", "500 miles")
                    if not intermediate_pending and account["city_id"] == HOME_CITY_ID:
                        radius = "Rayon\n \n"
                        options = ("10 km", "25 km")
                else:
                    radius = account["radius"]
                    options = ("1 mile", "500 miles")
                data = {
                    "viewer": {
                        "marketplace_feed_stories": {
                            "buy_location": {
                                "id": account["city_id"],
                                "display_name": account["location"],
                            }
                        }
                    }
                }
                await route.fulfill(
                    content_type="text/html",
                    body=_location_browser_html(data, SAME_NAME_CITY, radius, options=options),
                )

            await fb.context.route("**/*", route)
            _fast_browser_waits(monkeypatch)
            page = await fb.context.new_page()
            home = HomeLocation(SAME_NAME_CITY, "10 km", HOME_CITY_ID)

            async def allow_access(_page):
                return None

            await restore_home(page, home, allow_access)
            assert blank_dialog_seen
            assert len(applied) == 2
            assert applied[-1]["radius"] == "10 km"
            assert account == {
                "location": SAME_NAME_CITY,
                "radius": "10 km",
                "city_id": HOME_CITY_ID,
            }
        finally:
            if page is not None:
                await page.close()
            await fb.close()

    asyncio.run(exercise())


def test_capture_failure_does_not_start_scan(tmp_path, monkeypatch):
    async def exercise():
        fb = Facebook(Settings(tmp_path))
        page = AsyncMock()
        fb.context = AsyncMock()
        fb.context.new_page.return_value = page
        scan = AsyncMock()
        monkeypatch.setattr(fb, "_search", scan)
        monkeypatch.setattr(
            "scout.providers.facebook.capture_home",
            AsyncMock(side_effect=RuntimeError("Cannot capture preferences")),
        )
        with pytest.raises(RuntimeError, match="Cannot capture"):
            await fb.search("chair", "CA", REGIONS["CA"][0]["city"])
        scan.assert_not_awaited()
        page.close.assert_awaited_once()
        assert not (tmp_path / "facebook-home.json").exists()

    asyncio.run(exercise())


def test_failed_recovery_keeps_original_snapshot_and_stops_scan(tmp_path, monkeypatch):
    async def exercise():
        home = HomeLocation("Home City, Quebec", "25 km")
        snapshot = tmp_path / "facebook-home.json"
        save_home(snapshot, home)
        fb = Facebook(Settings(tmp_path))
        fb.context = AsyncMock()
        scan = AsyncMock()
        monkeypatch.setattr(fb, "_search", scan)
        monkeypatch.setattr(
            "scout.providers.facebook.restore_home",
            AsyncMock(side_effect=RuntimeError("Retry failed")),
        )
        with pytest.raises(RuntimeError, match="Could not restore"):
            await fb.search("chair", "CA", REGIONS["CA"][0]["city"])
        scan.assert_not_awaited()
        assert HomeLocation(**json.loads(snapshot.read_text())) == home
        assert snapshot.stat().st_mode & 0o777 == 0o600

    asyncio.run(exercise())


@pytest.mark.parametrize("restore_fails", [False, True])
def test_cancellation_during_restoration_waits_for_cleanup(tmp_path, monkeypatch, restore_fails):
    async def exercise():
        fb = Facebook(Settings(tmp_path))
        fb.context = AsyncMock()
        home = HomeLocation("Home City, Quebec", "25 km")
        started = asyncio.Event()
        finish = asyncio.Event()
        completed = asyncio.Event()

        async def restore(*args):
            started.set()
            await finish.wait()
            completed.set()
            if restore_fails:
                raise RuntimeError("Restore failed")

        monkeypatch.setattr("scout.providers.facebook.capture_home", AsyncMock(return_value=home))
        monkeypatch.setattr("scout.providers.facebook.restore_home", restore)
        monkeypatch.setattr(fb, "_search", AsyncMock(return_value=None))
        task = asyncio.create_task(fb.search("chair", "CA", REGIONS["CA"][0]["city"]))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed.is_set()
        assert (tmp_path / "facebook-home.json").exists() is restore_fails
        fb.context.new_page.return_value.close.assert_awaited_once()

    asyncio.run(exercise())


def test_scan_session_captures_and_restores_once_for_serialized_batch(tmp_path, monkeypatch):
    async def exercise():
        fb = Facebook(Settings(tmp_path))
        fb.context = AsyncMock()
        page = AsyncMock()
        fb.context.new_page.return_value = page
        home = HomeLocation("Home City, Quebec", "25 km")
        capture = AsyncMock(return_value=home)
        restore = AsyncMock()
        monkeypatch.setattr("scout.providers.facebook.capture_home", capture)
        monkeypatch.setattr("scout.providers.facebook.restore_home", restore)
        active = 0
        maximum = 0
        calls = []

        async def search(query, country, anchor):
            nonlocal active, maximum
            assert fb._search_lock.locked()
            active += 1
            maximum = max(maximum, active)
            calls.append(query)
            await asyncio.sleep(0)
            active -= 1
            return query

        monkeypatch.setattr(fb, "_search", search)
        async with fb.scan_session() as session:
            results = await asyncio.gather(
                session.search("chair", "CA", REGIONS["CA"][0]["city"]),
                session.search("table", "CA", REGIONS["CA"][0]["city"]),
            )

        assert results == ["chair", "table"]
        assert calls == ["chair", "table"]
        assert maximum == 1
        capture.assert_awaited_once()
        restore.assert_awaited_once()
        assert not (tmp_path / "facebook-home.json").exists()
        assert not fb._search_lock.locked()
        page.close.assert_awaited_once()

    asyncio.run(exercise())


def test_scan_session_opt_out_skips_snapshot_io_and_restoration(tmp_path, monkeypatch):
    async def exercise():
        snapshot = tmp_path / "facebook-home.json"
        snapshot.write_text("not json")
        fb = Facebook(Settings(tmp_path, facebook_restore_home=False))
        fb.context = AsyncMock()
        capture = AsyncMock(side_effect=AssertionError("opt-out must not capture"))
        restore = AsyncMock(side_effect=AssertionError("opt-out must not restore"))
        monkeypatch.setattr("scout.providers.facebook.capture_home", capture)
        monkeypatch.setattr("scout.providers.facebook.restore_home", restore)
        search = AsyncMock(side_effect=["first", "second"])
        monkeypatch.setattr(fb, "_search", search)

        async with fb.scan_session() as session:
            assert await session.search("chair", "CA", REGIONS["CA"][0]["city"]) == "first"
            assert await session.search("table", "CA", REGIONS["CA"][0]["city"]) == "second"

        capture.assert_not_awaited()
        restore.assert_not_awaited()
        fb.context.new_page.assert_not_awaited()
        assert snapshot.read_text() == "not json"
        assert not fb._search_lock.locked()

    asyncio.run(exercise())


def test_scan_session_opt_out_still_drains_query_before_cancellation(tmp_path, monkeypatch):
    async def exercise():
        fb = Facebook(Settings(tmp_path, facebook_restore_home=False))
        fb.context = AsyncMock()
        capture = AsyncMock(side_effect=AssertionError("opt-out must not capture"))
        restore = AsyncMock(side_effect=AssertionError("opt-out must not restore"))
        monkeypatch.setattr("scout.providers.facebook.capture_home", capture)
        monkeypatch.setattr("scout.providers.facebook.restore_home", restore)
        started = asyncio.Event()
        finish = asyncio.Event()
        completed = asyncio.Event()

        async def search(*args):
            started.set()
            try:
                await finish.wait()
            finally:
                completed.set()
            return "result"

        monkeypatch.setattr(fb, "_search", search)

        async def run_batch():
            async with fb.scan_session() as session:
                await session.search("chair", "CA", REGIONS["CA"][0]["city"])

        task = asyncio.create_task(run_batch())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert completed.is_set()
        capture.assert_not_awaited()
        restore.assert_not_awaited()
        assert not fb._search_lock.locked()

    asyncio.run(exercise())


def test_scan_session_restores_once_when_a_query_fails(tmp_path, monkeypatch):
    async def exercise():
        fb = Facebook(Settings(tmp_path))
        fb.context = AsyncMock()
        page = AsyncMock()
        fb.context.new_page.return_value = page
        home = HomeLocation("Home City, Quebec", "25 km")
        capture = AsyncMock(return_value=home)
        restore = AsyncMock()
        monkeypatch.setattr("scout.providers.facebook.capture_home", capture)
        monkeypatch.setattr("scout.providers.facebook.restore_home", restore)
        calls = 0

        async def search(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("search failed")
            return "first"

        monkeypatch.setattr(fb, "_search", search)
        with pytest.raises(RuntimeError, match="search failed"):
            async with fb.scan_session() as session:
                assert await session.search("chair", "CA", REGIONS["CA"][0]["city"]) == "first"
                await session.search("table", "CA", REGIONS["CA"][0]["city"])

        assert capture.await_count == 1
        restore.assert_awaited_once()
        assert not (tmp_path / "facebook-home.json").exists()
        assert not fb._search_lock.locked()

    asyncio.run(exercise())


def test_scan_session_restores_once_when_cancelled_during_query(tmp_path, monkeypatch):
    async def exercise():
        fb = Facebook(Settings(tmp_path))
        fb.context = AsyncMock()
        page = AsyncMock()
        fb.context.new_page.return_value = page
        home = HomeLocation("Home City, Quebec", "25 km")
        monkeypatch.setattr("scout.providers.facebook.capture_home", AsyncMock(return_value=home))
        restore = AsyncMock()
        monkeypatch.setattr("scout.providers.facebook.restore_home", restore)
        started = asyncio.Event()
        finish = asyncio.Event()

        async def search(*args):
            started.set()
            await finish.wait()
            return "result"

        monkeypatch.setattr(fb, "_search", search)

        async def run_batch():
            async with fb.scan_session() as session:
                await session.search("chair", "CA", REGIONS["CA"][0]["city"])

        task = asyncio.create_task(run_batch())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        restore.assert_awaited_once()
        assert not (tmp_path / "facebook-home.json").exists()
        assert not fb._search_lock.locked()
        page.close.assert_awaited_once()

    asyncio.run(exercise())


def test_recover_home_restores_persisted_snapshot_without_capture(tmp_path, monkeypatch):
    async def exercise():
        home = HomeLocation("Home City, Quebec", "25 km")
        snapshot = tmp_path / "facebook-home.json"
        save_home(snapshot, home)
        fb = Facebook(Settings(tmp_path))
        fb.context = AsyncMock()
        page = AsyncMock()
        fb.context.new_page.return_value = page
        capture = AsyncMock(side_effect=AssertionError("recovery must not capture"))
        restore = AsyncMock()
        monkeypatch.setattr("scout.providers.facebook.capture_home", capture)
        monkeypatch.setattr("scout.providers.facebook.restore_home", restore)

        assert await fb.recover_home() is True
        capture.assert_not_awaited()
        restore.assert_awaited_once()
        assert not snapshot.exists()
        assert not fb._search_lock.locked()
        page.close.assert_awaited_once()

    asyncio.run(exercise())


def test_standalone_search_remains_a_safe_session_wrapper(tmp_path, monkeypatch):
    async def exercise():
        fb = Facebook(Settings(tmp_path))
        fb.context = AsyncMock()
        page = AsyncMock()
        fb.context.new_page.return_value = page
        home = HomeLocation("Home City, Quebec", "25 km")
        capture = AsyncMock(return_value=home)
        restore = AsyncMock()
        monkeypatch.setattr("scout.providers.facebook.capture_home", capture)
        monkeypatch.setattr("scout.providers.facebook.restore_home", restore)
        search = AsyncMock(return_value="result")
        monkeypatch.setattr(fb, "_search", search)

        result = await fb.search("chair", "CA", REGIONS["CA"][0]["city"])

        assert result == "result"
        search.assert_awaited_once_with("chair", "CA", REGIONS["CA"][0]["city"])
        capture.assert_awaited_once()
        restore.assert_awaited_once()
        assert not fb._search_lock.locked()

    asyncio.run(exercise())


def test_scan_session_entry_timeout_releases_lock_and_closes_page(tmp_path, monkeypatch):
    async def exercise():
        fb = Facebook(Settings(tmp_path))
        fb.context = AsyncMock()
        page = AsyncMock()
        fb.context.new_page.return_value = page
        monkeypatch.setattr("scout.providers.facebook.SESSION_START_TIMEOUT", 0.01)

        async def blocked(*args):
            await asyncio.Future()

        monkeypatch.setattr("scout.providers.facebook.capture_home", blocked)
        with pytest.raises(TimeoutError):
            async with fb.scan_session():
                pass

        assert not fb._search_lock.locked()
        page.close.assert_awaited_once()

    asyncio.run(exercise())


def test_restoration_radius_comparison_does_not_round_units():
    from decimal import Decimal

    from scout.providers.facebook_location import radius_distance

    assert radius_distance("1 mile") == Decimal("1.609344")
    assert radius_distance("1 mile") != radius_distance("2 km")
    assert radius_distance("2 miles") != radius_distance("3 kilomètres")
    assert radius_distance("Rayon\n10 kilomètres") == radius_distance("10 km")
    assert radius_distance("Rayon\n\u00a0") is None
