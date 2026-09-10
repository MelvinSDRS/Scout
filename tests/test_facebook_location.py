"""Exercise saved Marketplace preferences using an isolated browser and fake server."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scout.models import AccessBlocked
from scout.providers.facebook import REGIONS, Facebook
from scout.providers.facebook_location import HomeLocation, save_home
from scout.settings import Settings


@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "blocked", "retry"])
def test_search_restores_original_preferences(tmp_path, monkeypatch, outcome):
    browsers = Path("data/browsers").resolve()
    if not browsers.exists():
        if os.environ.get("CI"):
            pytest.fail("Chromium is required in CI")
        pytest.skip("Install project Chromium to run the browser regression")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    original = {"location": "Home City, Quebec", "radius": "25 km"}
    state = dict(original)
    applied = []
    fail_restore = outcome == "retry"

    async def exercise():
        nonlocal fail_restore
        fb = Facebook(Settings(tmp_path))
        snapshot = tmp_path / "facebook-home.json"
        try:
            await fb.start()

            async def route(route):
                if route.request.url.endswith("/preferences"):
                    submitted = json.loads(route.request.post_data)
                    applied.append(submitted)
                    if not fail_restore:
                        state.update(submitted)
                    await route.fulfill(json={"ok": True})
                    return
                if "/search?" in route.request.url:
                    state.update(location="Scan City, Ontario", radius="500 km")
                data = {
                    "params": {"filter_radius_km": 500},
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
 const option=document.createElement('div');
 option.role='option';option.textContent=city.value;
 option.onclick=()=>{city.value=option.textContent;suggestions.replaceChildren();};
 suggestions.append(option);
});
function chooseRadius(option){radius.textContent=option.textContent;document.querySelector('#options').hidden=true;}
async function apply(){
 await fetch('/preferences',{method:'POST',body:JSON.stringify({location:city.value,radius:radius.textContent})});
 document.querySelector('[role=dialog]').hidden=true;
}
addEventListener('keydown',event=>{if(event.key==='Escape')document.querySelector('[role=dialog]').hidden=true;});
"""
                    + "city.value="
                    + json.dumps(state["location"])
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
            assert not snapshot.exists()
            assert len(fb.context.pages) == 1  # Only the initial persistent-context tab remains.
        finally:
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
