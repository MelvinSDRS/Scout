"""Exercise search, country results and watch conversion with isolated fixture data."""

import asyncio
import os
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import uvicorn
from PIL import Image
from playwright.async_api import async_playwright

from scout.api import create_app
from scout.dashboard_auth import DashboardAuth
from scout.image_filter import PhotoFilter
from scout.models import Listing, SearchResult, SearchSpec
from scout.searches import Searches
from scout.settings import Settings
from scout.store import Store


async def exercise(settings, searches):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        auth = DashboardAuth(searches.store, settings)
        ticket = auth.issue_ticket()
        urls = []
        page.on("request", lambda request: urls.append(request.url))
        await page.goto("http://127.0.0.1:8766/#signin=" + ticket)
        try:
            await page.locator("#app").wait_for(state="visible", timeout=10000)
        except Exception:
            print(
                "Dashboard sign-in failed:", await page.locator("#login-error").inner_text(), errors
            )
            raise
        assert "#signin=" not in page.url
        assert page.url.endswith("/explore")
        assert all(ticket not in url for url in urls)
        cookies = await page.context.cookies()
        session = next(cookie for cookie in cookies if cookie["name"] == auth.cookie_name)
        assert session["httpOnly"] and session["expires"] > time.time() + 29 * 86400
        await page.reload()
        await page.locator("#app").wait_for(state="visible")
        ignored_controls = page.locator(
            "#app input, #app select, #app textarea, "
            "#watch-dialog input, #watch-dialog select, #watch-dialog textarea"
        )
        assert await ignored_controls.count() > 0
        assert await ignored_controls.evaluate_all(
            "controls => controls.every(control => "
            "['data-1p-ignore', 'data-op-ignore', 'data-lpignore', 'data-bwignore']"
            ".every(attribute => control.getAttribute(attribute) === 'true'))"
        )
        assert await page.locator("#token").get_attribute("data-1p-ignore") is None
        await page.goto("http://127.0.0.1:8766/status")
        await page.locator("#view-status").wait_for(state="visible")
        await page.locator("[data-view=pricing]").click()
        assert page.url.endswith("/sell-price")
        await page.go_back()
        await page.locator("#view-status").wait_for(state="visible")
        await page.go_forward()
        await page.locator("#view-pricing").wait_for(state="visible")
        await page.locator("[data-view=search]").click()
        assert page.url.endswith("/explore")
        await page.screenshot(path="/tmp/scout-explore.png", full_page=True)
        await page.locator("#query").fill("Oakley Judge")
        await page.locator("#filters summary").click()
        await page.evaluate("updateImageProfiles([])")
        assert await page.locator("#photo-profile-field").is_hidden()
        await page.evaluate('selectImageProfile("missing-reference")')
        assert await page.locator("#photo-profile-field").is_visible()
        assert await page.locator("#image-profile").input_value() == "missing-reference"
        await page.evaluate('selectImageProfile("")')
        await page.evaluate("refreshBasics()")
        await page.locator("#exclude").fill("glasses, sunglasses, lunettes")
        await page.locator("#image-profile").select_option("example-bag")
        await page.locator("#search-submit").click()
        await page.locator("#results").wait_for(state="visible")
        while job := searches.claim():
            c = job["country"]
            matches = [
                Listing(
                    "facebook",
                    str(n),
                    f"Oakley Judge watch {n}",
                    f"https://www.facebook.com/marketplace/item/{n}/",
                    c,
                    str(100 + n),
                    {"US": "USD", "CA": "CAD", "FR": "EUR"}[c],
                    {"US": "Chicago, IL", "CA": "Montreal, QC", "FR": "Paris, France"}[c],
                )
                for n in range(65)
            ]
            matches.append(
                Listing(
                    "facebook",
                    "999",
                    "Oakley Judge glasses",
                    "https://www.facebook.com/marketplace/item/999/",
                    c,
                )
            )
            searches.record(job, SearchResult(matches, radius_km=500))
        await page.wait_for_function("document.querySelectorAll('.listing').length===60")
        assert await page.get_by_role("tab").count() == 3
        regular_id = searches.recent()[0]["id"]
        await page.locator("#load-more").click()
        await page.wait_for_function("document.querySelectorAll('.listing').length===65")
        await page.get_by_role("tab", name="🇫🇷 France").click()
        await page.wait_for_function(
            "document.querySelector('#listing-count').textContent.includes('65 title matches')"
        )
        await page.locator("#suggestions").check()
        await page.wait_for_function(
            "document.querySelector('#listing-count').textContent.includes('66 collected listings')"
        )
        await page.locator("#filters summary").click()
        await page.screenshot(path="/tmp/scout-results.png", full_page=False)

        # Pricing uses the same local fixture queue as the dashboard, so this
        # exercises the complete desktop flow without contacting Facebook.
        await page.locator("[data-view=pricing]").click()
        assert await page.locator("#pricing-query").get_attribute("type") == "search"
        assert await page.locator("#pricing-query").get_attribute("autocomplete") == "off"
        assert await page.locator("#pricing-query").get_attribute("data-1p-ignore") == "true"
        assert await page.locator("#pricing-city").get_attribute("type") == "search"
        for attribute in ("data-1p-ignore", "data-op-ignore", "data-lpignore", "data-bwignore"):
            assert await page.locator("#pricing-city").get_attribute(attribute) == "true"
        assert await page.locator("#pricing-radius").input_value() == "20"
        assert await page.locator("#pricing-city-options option").count() >= 100
        await page.locator("#pricing-query").fill("Oakley Judge")
        await page.locator("#pricing-city").fill("Montpellier, France")
        await page.locator("#pricing-radius").click()
        assert await page.locator("#pricing-country").input_value() == "FR"
        assert await page.evaluate("pricingSpecFromForm().pricing.city") == "115100621840245"
        await page.locator("#pricing-city").fill("Burlington, VT")
        await page.locator("#pricing-country").select_option("US")
        assert await page.evaluate("pricingSpecFromForm().pricing.city") == "burlington"
        await page.locator("#pricing-city").fill("Montréal, QC")
        await page.locator("#pricing-radius").click()
        assert await page.locator("#pricing-country").input_value() == "CA"
        assert await page.evaluate("pricingSpecFromForm().pricing.city") == "montreal"
        await page.locator("#pricing-radius").fill("40")
        await page.locator("#pricing-filters summary").click()
        await page.locator("#pricing-include").fill("Oakley")
        await page.locator("#pricing-exclude").fill("case")
        blocked_by = searches.submit(SearchSpec(query="other search", countries=["FR"]))
        await page.locator("#pricing-submit").click()
        await page.wait_for_function(
            "document.querySelector('#pricing-state').textContent.includes('already running')"
        )
        assert await page.locator("#pricing-state").is_visible()
        searches.cancel(blocked_by)
        await page.locator("#pricing-submit").click()
        await page.locator("#pricing-results").wait_for(state="visible")
        pricing_job = searches.claim()
        assert pricing_job and pricing_job["spec"]
        pricing_matches = [
            Listing(
                "facebook",
                str(n),
                "Oakley Judge watch " + str(n),
                f"https://www.facebook.com/marketplace/item/{n}/",
                "CA",
                str(100 + n * 25),
                "CAD",
                "Montreal, QC",
                status="active",
                distance_km=5,
            )
            for n in range(1, 7)
        ]
        outside = replace(
            pricing_matches[0], id="8001", price="1", location="Ottawa, ON", distance_km=165
        )
        unknown = replace(
            pricing_matches[0], id="8002", price="1", location="Unknown", distance_km=None
        )
        searches.record(
            pricing_job, SearchResult([*pricing_matches, outside, unknown], radius_km=40)
        )
        await page.wait_for_function(
            "document.querySelectorAll('#pricing-histogram .histogram-bar').length > 0"
        )
        assert await page.locator("#pricing-cards .pricing-card").count() == 5
        assert await page.locator("#pricing-items .listing").count() == 6
        assert "Ottawa" not in await page.locator("#pricing-items").inner_text()
        assert "5.0 km from search center" in await page.locator("#pricing-items").inner_text()
        assert await page.locator("#pricing-state").is_hidden()

        # Rerun with one comparable now marked sold to populate the observed-
        # days graph and sold-history cards. This models provider fixture data.
        await page.locator("#rerun-pricing").click()
        await page.locator("#pricing-results").wait_for(state="visible")
        rerun_job = searches.claim()
        assert rerun_job and rerun_job["spec"]
        rerun_matches = pricing_matches[:5] + [
            Listing(
                "facebook",
                "6",
                "Oakley Judge watch 6",
                "https://www.facebook.com/marketplace/item/6/",
                "CA",
                "240",
                "CAD",
                "Montreal, QC",
                status="sold",
                distance_km=5,
            )
        ]
        searches.record(rerun_job, SearchResult(rerun_matches, radius_km=40))
        await page.wait_for_function(
            "document.querySelectorAll('#pricing-speed .speed-point').length > 0"
        )
        await page.locator("#pricing-sold-section").wait_for(state="visible")
        assert await page.locator("#pricing-sold-items .listing").count() == 1
        await page.wait_for_function(
            "document.querySelector('#pricing-progress-text').textContent.includes('Complete')"
        )
        await page.locator("[data-view=search]").click()
        assert await page.locator("#results").is_hidden()
        assert await page.locator("#welcome").is_visible()
        await page.locator("[data-view=pricing]").click()
        assert await page.locator("#pricing-report").is_visible()
        await page.screenshot(path="/tmp/scout-pricing.png", full_page=False)
        await page.locator(".pricing-charts").screenshot(path="/tmp/scout-pricing-charts.png")
        await page.evaluate("(id) => openSearch(id)", regular_id)
        await page.locator("#results").wait_for(state="visible")

        await page.locator("#create-watch").click()
        await page.locator("#watch-name").fill("Browser smoke watch")
        await page.locator("#save-watch").click()
        await page.wait_for_function("document.querySelector('#create-watch').disabled")
        assert len(searches.store.watches()) == 1
        assert searches.store.health()["pending_alerts"] == 0
        assert len(searches.store.health()["scans"]) == 24
        await page.locator("[data-view=watches]").click()
        await page.locator(".watch-card").wait_for()
        ident = searches.store.watches()[0]["id"]
        assert searches.store.watches()[0]["image_profile"] == "example-bag"
        PhotoFilter(searches.store, settings).save(
            ident,
            Listing(
                "facebook",
                "900",
                "Oakley Judge watch",
                "https://facebook.com/marketplace/item/900/",
                "FR",
            ),
            "test-photo",
            "review",
            0,
            "Photo not confidently matched",
        )
        await page.get_by_role("button", name="Review photos", exact=True).click()
        await page.locator("#photo-review-grid .listing").wait_for()
        await page.get_by_role("button", name="Send this to Telegram", exact=True).click()
        await page.locator("#photo-review-grid .listing").wait_for(state="detached")
        assert searches.store.health()["pending_alerts"] == 1

        await page.set_viewport_size({"width": 390, "height": 844})
        await page.locator("[data-view=pricing]").click()
        await page.screenshot(path="/tmp/scout-pricing-mobile.png", full_page=False)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            "Mobile pricing overflow"
        )
        await page.locator("[data-view=search]").click()
        await page.screenshot(path="/tmp/scout-mobile.png", full_page=False)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            "Mobile overflow"
        )
        await page.locator("[data-view=watches]").click()
        page.on("dialog", lambda d: d.accept())
        await page.get_by_role("button", name="Delete", exact=True).click()
        await page.locator(".watch-card").wait_for(state="detached")
        await page.locator("#disconnect").click()
        await page.locator("#login").wait_for(state="visible")
        await page.reload()
        assert await page.locator("#app").is_hidden()
        assert not auth.valid_session(session["value"])
        # Compatibility fallback is tucked away; the normal path above never needs a token.
        await page.locator("#login details summary").click()
        await page.locator("#token").fill(settings.api_token)
        await page.get_by_role("button", name="Connect", exact=True).click()
        await page.locator("#app").wait_for(state="visible")
        assert await page.locator("#token").input_value() == ""
        assert not errors, errors
        await browser.close()


def main():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(Path("data/browsers").resolve())
    with tempfile.TemporaryDirectory() as directory:
        settings = Settings(Path(directory), api_token="local-smoke-token-" + "x" * 24)
        references = settings.reference_root / "example-bag"
        references.mkdir(parents=True)
        Image.new("RGB", (16, 16), "blue").save(references / "front.jpg")
        store = Store(settings.data_dir / "smoke.sqlite3")
        searches = Searches(store)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(store, settings, start_worker=False),
                host="127.0.0.1",
                port=8766,
                log_level="warning",
            )
        )
        thread = threading.Thread(target=server.run)
        thread.start()
        try:
            for _ in range(100):
                try:
                    if httpx.get("http://127.0.0.1:8766", timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    time.sleep(0.1)
            asyncio.run(exercise(settings, searches))
            print(
                "Browser passed: authentication, search, pricing statistics/charts/history, errors, navigation, watch baseline, deletion, mobile layout; no JavaScript errors."
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
