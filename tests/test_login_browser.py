"""Login evidence and profile persistence against synthetic, offline Facebook pages."""

import asyncio
import os
from pathlib import Path

import pytest

from scout.login import MARKETPLACE_URL, session_ready
from scout.models import AccessBlocked
from scout.providers.facebook import Facebook
from scout.settings import Settings


def test_login_requires_positive_evidence_and_persists(tmp_path, monkeypatch):
    browsers = Path("data/browsers").resolve()
    if not browsers.exists():
        if os.environ.get("CI"):
            pytest.fail("Chromium is required in CI")
        pytest.skip("Install project Chromium to run login browser tests")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))

    async def exercise():
        settings = Settings(tmp_path)
        fb = Facebook(settings)
        html = '<main><a href="/marketplace/item/123">Synthetic listing</a></main>'

        async def route(request):
            await request.fulfill(content_type="text/html", body=html)

        try:
            await fb.start()
            await fb.context.route("**/*", route)
            page = await fb.context.new_page()
            await page.goto(MARKETPLACE_URL)
            assert not await session_ready(page)  # Anonymous browsing isn't authentication.
            await fb.context.add_cookies(
                [
                    {
                        "name": name,
                        "value": "synthetic",
                        "url": MARKETPLACE_URL,
                        "expires": 2000000000,
                    }
                    for name in ("c_user", "xs")
                ]
            )
            assert await session_ready(page)
            await page.goto("https://www.facebook.com/")
            assert not await session_ready(page)
            await page.goto(MARKETPLACE_URL)
            await page.set_content("<main>Loading...</main>")
            assert not await session_ready(page)
            await page.set_content('<a hidden href="/marketplace/item/123">Hidden</a>')
            assert not await session_ready(page)
            await page.set_content(html + '<input type="password">')
            with pytest.raises(AccessBlocked):
                await session_ready(page)
            await page.set_content(html + "Marketplace isn't available to you")
            with pytest.raises(AccessBlocked):
                await session_ready(page)
            await page.goto("https://www.facebook.com/checkpoint/")
            assert not await session_ready(page)
            await page.goto("https://example.org/marketplace/")
            assert not await session_ready(page)
        finally:
            await fb.close()
        # New browser process and context must read the saved cookies from disk.
        reopened = Facebook(settings)
        try:
            await reopened.start()
            await reopened.context.route("**/*", route)
            page = await reopened.context.new_page()
            await page.goto(MARKETPLACE_URL)
            assert await session_ready(page)
        finally:
            await reopened.close()

    asyncio.run(exercise())


def test_automatic_login_reopens_saved_profile_before_resuming(tmp_path, monkeypatch):
    from scout.login import browser_login
    from scout.store import Store

    browsers = Path("data/browsers").resolve()
    if not browsers.exists():
        if os.environ.get("CI"):
            pytest.fail("Chromium is required in CI")
        pytest.skip("Install project Chromium to run login browser tests")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    starts = []
    contexts = []

    class OfflineFacebook(Facebook):
        async def start(self, headed=False):
            starts.append(headed)
            await (
                super().start()
            )  # Real headless Chromium; headed rendering has its own smoke test.
            contexts.append(self.context)

            async def route(request):
                cookies = {c["name"] for c in await self.context.cookies([MARKETPLACE_URL])}
                if "c_user" in cookies:
                    html = '<a href="/marketplace/item/123">Synthetic listing</a>'
                else:
                    html = """<input type="password"><button onclick="
                    document.cookie='c_user=synthetic; Max-Age=3600; Path=/; Secure';
                    document.cookie='xs=synthetic; Max-Age=3600; Path=/; Secure';
                    location.reload();">Synthetic sign in</button>"""
                await request.fulfill(content_type="text/html", body=html)

            await self.context.route("**/*", route)

    monkeypatch.setattr("scout.login.Facebook", OfflineFacebook)
    store = Store(tmp_path / "scout.sqlite3")
    with store.connect() as db:
        db.execute("INSERT INTO meta VALUES ('cooldown:facebook', '2000000000')")

    async def exercise():
        task = asyncio.create_task(browser_login(Settings(tmp_path)))
        try:
            async with asyncio.timeout(10):
                while not contexts or not any(p.url == MARKETPLACE_URL for p in contexts[0].pages):
                    if task.done():
                        await task
                    await asyncio.sleep(0.05)
                page = next(p for p in contexts[0].pages if p.url == MARKETPLACE_URL)
                await page.get_by_role("button", name="Synthetic sign in").click()
            await asyncio.wait_for(task, timeout=20)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert starts == [True, False]
    with store.connect() as db:
        assert not db.execute("SELECT * FROM meta WHERE key='cooldown:facebook'").fetchone()
