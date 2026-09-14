"""Regression tests for response-driven Facebook search waits."""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from scout.providers.facebook import REGIONS, Facebook
from scout.settings import Settings


def _require_browser(monkeypatch):
    browsers = Path("data/browsers").resolve()
    if not browsers.exists():
        if os.environ.get("CI"):
            pytest.fail("Chromium is required in CI")
        pytest.skip("Install project Chromium to run the browser regression")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))


def _search_document(*, ident, has_next_page):
    return {
        "params": {"filter_radius_km": 500},
        "marketplace_search": {
            "feed_units": {
                "edges": [
                    {
                        "node": {
                            "listing": {
                                "id": str(ident),
                                "marketplace_listing_title": f"Oakley Judge {ident}",
                                "listing_price": {
                                    "amount": "100",
                                    "formatted_amount": "100 $CA",
                                },
                            }
                        }
                    }
                ],
                "page_info": {"has_next_page": has_next_page},
            }
        },
    }


def test_initial_search_waits_for_structured_response(tmp_path, monkeypatch):
    """A delayed GraphQL result is awaited without the former three-second sleep."""
    _require_browser(monkeypatch)
    query = "Oakley Judge"
    variables = {
        "params": {
            "bqf": {"query": query},
            "custom_request_params": {"surface": "SEARCH"},
            "browse_request_params": {"filter_radius_km": 500},
        }
    }
    html = """<html><body><main role="main" style="height:18000px">Loading</main><script>
const body = new URLSearchParams({variables: JSON.stringify(%s)});
setTimeout(() => fetch('/api/graphql', {method: 'POST', body}), 50);
</script></body></html>""" % json.dumps(variables)

    async def exercise():
        fb = Facebook(Settings(tmp_path))
        try:
            await fb.start()

            async def route(route):
                if "/api/graphql" in route.request.url:
                    await asyncio.sleep(0.12)
                    await route.fulfill(json=_search_document(ident=1, has_next_page=False))
                else:
                    await route.fulfill(content_type="text/html", body=html)

            await fb.context.route("**/*", route)
            started = time.monotonic()
            result = await fb._search(query, "CA", REGIONS["CA"][0]["city"])
            elapsed = time.monotonic() - started
            assert [listing.id for listing in result.listings] == ["1"]
            assert result.radius_km == 500
            assert elapsed < 2.5
        finally:
            await fb.close()

    asyncio.run(exercise())


def test_scroll_waits_for_pagination_and_stops_on_explicit_exhaustion(tmp_path, monkeypatch):
    """A page-info false response ends scrolling after its response is captured."""
    _require_browser(monkeypatch)
    initial = _search_document(ident=1, has_next_page=True)
    pagination_variables = {
        "params": {
            "bqf": {"query": "Oakley Judge"},
            "custom_request_params": {"surface": "SEARCH"},
            "browse_request_params": {"filter_radius_km": 500},
        }
    }
    html = (
        '<html><body><main role="main" style="height:18000px">Results</main>'
        '<script type="application/json">'
        + json.dumps(initial)
        + """</script><script>
let last = 0;
addEventListener('scroll', () => {
  const page = Math.floor(scrollY / 1000);
  if (page <= last) return;
  last = page;
  setTimeout(() => {
    const body = new URLSearchParams({variables: JSON.stringify(%s)});
    fetch('/api/graphql', {method: 'POST', body});
  }, 150);
});
</script></body></html>"""
        % json.dumps(pagination_variables)
    )
    pagination_responses = 0

    async def exercise():
        nonlocal pagination_responses
        fb = Facebook(Settings(tmp_path))
        try:
            await fb.start()

            async def route(route):
                nonlocal pagination_responses
                if "/api/graphql" in route.request.url:
                    pagination_responses += 1
                    await asyncio.sleep(0.12)
                    await route.fulfill(json=_search_document(ident=2, has_next_page=False))
                else:
                    await route.fulfill(content_type="text/html", body=html)

            await fb.context.route("**/*", route)
            started = time.monotonic()
            result = await fb._search("Oakley Judge", "CA", REGIONS["CA"][0]["city"])
            elapsed = time.monotonic() - started
            assert {listing.id for listing in result.listings} == {"1", "2"}
            assert 1 <= pagination_responses <= 4
            assert elapsed < 2.5
        finally:
            await fb.close()

    asyncio.run(exercise())
