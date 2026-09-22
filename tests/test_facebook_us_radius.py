"""Browser regressions for Facebook's US and localized radius controls."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from scout.providers.facebook import Facebook
from scout.providers.facebook_location import open_location
from scout.settings import Settings


def _require_browser(monkeypatch):
    browsers = Path("data/browsers").resolve()
    if not browsers.exists():
        if os.environ.get("CI"):
            pytest.fail("Chromium is required in CI")
        pytest.skip("Install project Chromium to run the browser regression")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))


def _location_page(label, radius):
    return f"""<html><head><meta charset="utf-8"></head><body>
<div id="no-results">No listings found for &quot;Oakley Judge&quot; within 311 miles</div>
<div id="notification" role="dialog" style="pointer-events: none">
  <h2>Notifications</h2>
  <input type="text" aria-label="Notification filter" value="">
  <button>Dismiss</button>
</div>
<button id="location-button" type="button">{label}</button>
<div id="location-dialog" role="dialog" hidden>
  <input id="location" type="text" aria-label="Location" value="Unalaska, Alaska">
  <button id="radius" role="combobox" type="button">{radius}</button>
  <div id="options" hidden>
    <div role="option">{radius}</div>
  </div>
  <button id="apply" type="button">Apply</button>
</div>
<main role="main" style="height: 18000px">No results</main>
<script>
const dialog = document.querySelector('#location-dialog');
const options = document.querySelector('#options');
document.querySelector('#location-button').onclick = () => dialog.hidden = false;
document.querySelector('#radius').onclick = () => options.hidden = false;
document.querySelector('#apply').onclick = () => {{ options.hidden = true; dialog.hidden = true; }};
</script>
</body></html>"""


@pytest.mark.parametrize(
    ("label", "radius"),
    [
        ("Unalaska, Alaska · Within 500 mi", "500 miles"),
        ("Unalaska, Alaska · Within 500 miles", "500 miles"),
        ("Unalaska, Alaska · Within 500 km", "500 kilometres"),
        ("Montréal · Dans un rayon de 500 km", "500 kilomètres"),
    ],
)
def test_open_location_uses_the_location_button_and_matching_dialog(
    tmp_path, monkeypatch, label, radius
):
    """Radius text in a no-results banner must never open a notification dialog."""

    _require_browser(monkeypatch)

    async def exercise():
        fb = Facebook(Settings(tmp_path))
        try:
            await fb.start()

            async def route(request):
                await request.fulfill(content_type="text/html", body=_location_page(label, radius))

            await fb.context.route("**/*", route)
            page = await fb.context.new_page()
            await page.goto("https://www.facebook.com/marketplace/", wait_until="domcontentloaded")

            dialog = await open_location(page)

            assert await dialog.get_attribute("id") == "location-dialog"
            assert await page.locator("#notification").is_visible()
            assert await page.locator("#no-results").is_visible()
            assert await dialog.get_by_role("combobox").inner_text() == radius
        finally:
            await fb.close()

    asyncio.run(exercise())


def _search_payload(radius):
    return {
        "params": {"filter_radius_km": radius},
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


def test_search_radius_adjustment_handles_us_mi_location_button(tmp_path, monkeypatch):
    """A US ``Within ... mi`` button still permits the normal radius adjustment."""

    _require_browser(monkeypatch)
    navigations = []

    async def exercise():
        fb = Facebook(Settings(tmp_path))
        try:
            await fb.start()

            async def route(request):
                url = request.request.url
                if "/api/graphql" in url:
                    await request.fulfill(json=_search_payload(805))
                    return
                navigations.append(url)
                applied = 500 if len(navigations) == 1 else 805
                html = _location_page("Unalaska, Alaska · Within 500 mi", "500 miles")
                html = html.replace(
                    "document.querySelector('#apply').onclick = () => { options.hidden = true; dialog.hidden = true; };",
                    "document.querySelector('#apply').onclick = async () => {"
                    " options.hidden = true;"
                    " await fetch('/api/graphql', {method: 'POST'});"
                    " dialog.hidden = true;"
                    "};",
                )
                await request.fulfill(
                    content_type="text/html",
                    body=html.replace(
                        "</body>",
                        '<script type="application/json">'
                        + json.dumps(_search_payload(applied))
                        + "</script></body>",
                    ),
                )

            await fb.context.route("**/*", route)
            result = await fb._search("Oakley Judge", "US", "portland")

            assert result.listings == []
            assert result.radius_km == 805
            assert len(navigations) == 2
        finally:
            await fb.close()

    asyncio.run(exercise())
