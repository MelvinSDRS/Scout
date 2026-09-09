"""Exercise search, country results and watch conversion with isolated fixture data."""

import asyncio
import os
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from PIL import Image
from playwright.async_api import async_playwright

from scout.api import create_app
from scout.image_filter import PhotoFilter
from scout.models import Listing, SearchResult
from scout.searches import Searches
from scout.settings import Settings
from scout.store import Store


async def exercise(settings, searches):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        await page.goto("http://127.0.0.1:8766")
        await page.locator("#token").fill(settings.api_token)
        await page.get_by_role("button", name="Connect", exact=True).click()
        await page.locator("#app").wait_for(state="visible")
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
        await page.locator("[data-view=search]").click()
        await page.screenshot(path="/tmp/scout-mobile.png", full_page=False)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            "Mobile overflow"
        )
        await page.locator("[data-view=watches]").click()
        page.on("dialog", lambda d: d.accept())
        await page.get_by_role("button", name="Delete", exact=True).click()
        await page.locator(".watch-card").wait_for(state="detached")
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
                "Browser passed: authentication, search, country tabs, pagination, suggestions, watch baseline, deletion, mobile layout; no JavaScript errors."
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
