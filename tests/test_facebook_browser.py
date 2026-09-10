"""Real browser regression: scroll forward through results instead of revisiting page one."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from scout.providers.facebook import REGIONS, Facebook
from scout.settings import Settings


@pytest.mark.parametrize("empty", [False, True])
def test_scroll_discovers_later_result_pages(tmp_path, monkeypatch, empty):
    browsers = Path("data/browsers").resolve()
    if not browsers.exists():
        if os.environ.get("CI"):
            pytest.fail("Chromium is required in CI")
        pytest.skip("Install project Chromium to run the browser regression")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))

    def response(ident):
        return {
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
                    "page_info": {"has_next_page": True},
                }
            }
        }

    if empty:

        def response(ident):
            return {
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
                        "page_info": {"has_next_page": True},
                    }
                }
            }

    initial = {"params": {"filter_radius_km": 500}, **response(1)}
    html = '<html><body><main role="main" style="height:18000px">Results</main>'
    html += '<script type="application/json">' + json.dumps(initial) + "</script>"
    html += """<script>
let last=0;
addEventListener('scroll',()=>{
 const page=Math.floor(scrollY/1000);
 if(page>last){last=page;fetch('/api/graphql',{method:'POST',body:JSON.stringify({page})});}
});
</script></body></html>"""

    async def exercise():
        fb = Facebook(Settings(tmp_path))
        try:
            await fb.start()

            async def route(request):
                if "/api/graphql" in request.request.url:
                    ident = json.loads(request.request.post_data)["page"] + 10
                    await request.fulfill(json=response(ident))
                else:
                    await request.fulfill(content_type="text/html", body=html)

            await fb.context.route("**/*", route)
            result = await fb._search("Oakley Judge", "CA", REGIONS["CA"][0]["city"])
            if empty:
                assert result.listings == []
                assert result.saturated is False
            else:
                assert len(result.listings) >= 4
                assert max(int(item.id) for item in result.listings) >= 16
        finally:
            await fb.close()

    asyncio.run(exercise())


def test_cached_radius_change_reloads_to_verify_server_parameters(tmp_path, monkeypatch):
    browsers = Path("data/browsers").resolve()
    if not browsers.exists():
        if os.environ.get("CI"):
            pytest.fail("Chromium is required in CI")
        pytest.skip("Install project Chromium to run the browser regression")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    navigations = []

    async def exercise():
        fb = Facebook(Settings(tmp_path))
        try:
            await fb.start()

            async def route(request):
                navigations.append(request.request.url)
                radius = 805 if len(navigations) == 1 else 500
                data = {
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
                            "page_info": {"has_next_page": True},
                        }
                    },
                }
                html = (
                    """<html><body>
<button onclick="document.querySelector('[role=dialog]').style.display='block'">Within 500 km</button>
<div role="dialog" style="display:none">
<button role="combobox" onclick="document.querySelector('[role=option]').style.display='block'">Radius</button>
<div role="option" style="display:none" onclick="this.style.display='none'">500 kilometres</div>
<button onclick="document.querySelector('[role=dialog]').style.display='none'">Apply</button>
</div><main role="main" style="height:18000px">No results</main>
<script type="application/json">"""
                    + json.dumps(data)
                    + """</script></body></html>"""
                )
                await request.fulfill(content_type="text/html", body=html)

            await fb.context.route("**/*", route)
            result = await fb._search("Oakley Judge", "CA", REGIONS["CA"][0]["city"])
            assert len(navigations) == 2
            assert result.radius_km == 500
            assert result.listings == []
        finally:
            await fb.close()

    asyncio.run(exercise())
