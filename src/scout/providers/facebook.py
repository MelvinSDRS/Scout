"""Read rendered Marketplace search data; stop at login/checkpoint walls."""

import json
import re
from importlib.resources import files
from urllib.parse import parse_qs, urlencode

from ..models import AccessBlocked, SearchResult
from .facebook_data import (
    applied_radius,
    choose_radius,
    confirmed_empty,
    extract_listings,
    more_results,
)

REGIONS = json.loads(files("scout").joinpath("regions.json").read_text())


def search_url(query, country, city):
    region = next(r for r in REGIONS[country] if r["city"] == city)
    # Newest-first can bury exact model matches beneath Facebook's relaxed
    # suggestions. Use its default relevance order; alerts still deduplicate IDs.
    return f"https://www.facebook.com/marketplace/{city}/search?" + urlencode(
        {
            "query": query,
            "radius": region["radius"],
            "exact": "true",
        }
    )


def json_documents(text):
    text = re.sub(r"^for\s*\(;;\);", "", text.strip())
    try:
        return [json.loads(text)]
    except (ValueError, TypeError):
        docs = []
        for line in text.splitlines():
            try:
                docs.append(json.loads(line))
            except ValueError:
                pass
        return docs


class Facebook:
    def __init__(self, settings):
        self.settings = settings
        self.playwright = None
        self.context = None
        self.browser = None

    async def start(self, headed=False):
        if self.context:
            return
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        if self.settings.facebook_cdp:
            self.browser = await self.playwright.chromium.connect_over_cdp(
                self.settings.facebook_cdp
            )
            self.context = self.browser.contexts[0]
        else:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.settings.data_dir / "facebook-profile"),
                headless=not headed,
                locale="en-US",
                viewport={"width": 1440, "height": 1000},
            )

    async def close(self):
        if self.context and not self.settings.facebook_cdp:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        self.context = self.playwright = self.browser = None

    async def search(self, query, country, anchor):
        await self.start()
        page = await self.context.new_page()
        documents = []
        requests = []
        import asyncio

        pending = set()

        async def capture(response):
            if "/api/graphql" in response.url:
                try:
                    documents.extend(json_documents(await response.text()))
                except Exception:
                    pass

        def on_response(response):
            task = asyncio.create_task(capture(response))
            pending.add(task)
            task.add_done_callback(pending.discard)

        def on_request(request):
            if "/api/graphql" in request.url and request.post_data:
                try:
                    variables = json.loads(parse_qs(request.post_data).get("variables", ["{}"])[0])
                    search = variables.get("params", {})
                    params = search.get("browse_request_params", {})
                    if (
                        search.get("bqf", {}).get("query") == query
                        and search.get("custom_request_params", {}).get("surface") == "SEARCH"
                        and "filter_radius_km" in params
                    ):
                        requests.append(params)
                except (ValueError, AttributeError):
                    pass

        page.on("response", on_response)
        page.on("request", on_request)
        try:
            response = await page.goto(
                search_url(query, country, anchor), wait_until="domcontentloaded", timeout=45000
            )
            if response and response.status in (401, 403, 429):
                raise AccessBlocked(f"Facebook HTTP {response.status}; login or rate limit")
            await page.wait_for_timeout(3000)
            await check_access(page)
            initial = []
            for script in await page.locator('script[type="application/json"]').all_text_contents():
                initial.extend(json_documents(script))
            actual = applied_radius(initial) or applied_radius(requests)
            desired = next(r["radius"] for r in REGIONS[country] if r["city"] == anchor)
            if actual != desired:
                await page.get_by_text(
                    re.compile(r"Dans un rayon de|Within .* (?:km|miles)|within .* (?:km|miles)")
                ).first.click(timeout=10000)
                dialog = page.get_by_role("dialog")
                await dialog.get_by_role("combobox").last.click()
                options = await page.get_by_role("option").all_text_contents()
                selected, label = choose_radius(options, desired)
                await page.get_by_role("option", name=label, exact=True).click()
                # Discard the pre-change search; only ingest the radius-adjusted response.
                if pending:
                    await asyncio.gather(*list(pending))
                changed = abs(selected - actual) > 1 if actual is not None else True
                if changed:
                    documents.clear()
                    requests.clear()
                    initial = []
                await dialog.get_by_role("button", name=re.compile(r"^(Appliquer|Apply)$")).click()
                await page.wait_for_timeout(3000)
                actual = applied_radius(requests) or (actual if not changed else None)
                if actual is None or abs(actual - selected) > 1:
                    # Facebook may reuse a cached route and issue no new search request.
                    # Reload the selected URL to verify fresh server search parameters.
                    if pending:
                        await asyncio.gather(*list(pending))
                    documents.clear()
                    requests.clear()
                    await page.reload(wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(3000)
                    await check_access(page)
                    initial = []
                    for script in await page.locator(
                        'script[type="application/json"]'
                    ).all_text_contents():
                        initial.extend(json_documents(script))
                    actual = applied_radius(initial) or applied_radius(requests)
                    if actual is None or abs(actual - selected) > 1:
                        raise RuntimeError("Could not verify Facebook's applied search radius")
            if actual is None:
                raise RuntimeError("Facebook search radius is unverified")
            # Hover once. Re-hovering a tall pane can scroll it back to the top.
            main = page.get_by_role("main").first
            await main.hover(position={"x": 100, "y": 200})
            for _ in range(4):
                await check_access(page)
                await page.mouse.wheel(0, 2000)
                await page.wait_for_timeout(1500)
            if pending:
                await asyncio.gather(*list(pending))
            combined = initial + documents
            listings = extract_listings(combined, country, scoped=True)
            if not listings and not confirmed_empty(combined):
                raise RuntimeError(
                    "No structured search result or confirmed empty result; Facebook layout is unverified"
                )
            return SearchResult(
                listings,
                saturated=bool(listings) and more_results(combined),
                radius_km=actual,
                coverage_warning=f"Facebook applied {actual} km (requested {desired} km)"
                if abs(actual - desired) > 1
                else None,
            )
        finally:
            await page.close()
            if pending:
                await asyncio.gather(*list(pending), return_exceptions=True)


async def check_access(page):
    if any(marker in page.url for marker in ("/login", "/checkpoint", "/captcha")):
        raise AccessBlocked("Facebook requires interactive login/checkpoint resolution")
    if await page.locator('input[type="password"]').count():
        raise AccessBlocked("Facebook login wall; run scout login")
    text = (await page.locator("body").inner_text()).casefold()
    if any(
        marker in text
        for marker in (
            "you're temporarily blocked",
            "you’re temporarily blocked",
            "confirm you're human",
            "marketplace isn't available to you",
            "marketplace isn’t available to you",
        )
    ):
        raise AccessBlocked("Facebook access restricted; inspect the browser")
