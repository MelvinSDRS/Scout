"""Exercise real Xvfb/VNC/noVNC with a disposable browser profile and no external sites."""

import asyncio
import os
import tempfile
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from scout.login import remote_display
from scout.providers.facebook import Facebook
from scout.settings import Settings


async def exercise(settings, password):
    fb = Facebook(settings)
    try:
        await fb.start(headed=True)
        remote_page = await fb.context.new_page()
        await remote_page.goto(
            'data:text/html,<h1>Remote login smoke test</h1><input autofocus placeholder="Test input">'
        )
        async with async_playwright() as p:
            viewer = await p.chromium.launch(headless=True)
            try:
                page = await viewer.new_page(viewport={"width": 1500, "height": 1100})
                await page.goto("http://127.0.0.1:6089/vnc.html?autoconnect=true&resize=scale")
                await page.locator("#noVNC_password_input").fill(password)
                await page.locator("#noVNC_credentials_button").click()
                await page.wait_for_function(
                    "document.documentElement.classList.contains('noVNC_connected')", timeout=20000
                )
                assert await page.locator("canvas").count() == 1
                await page.screenshot(path="/tmp/scout-login-smoke.png")
                print("Remote browser viewer authenticated and connected")
            finally:
                await viewer.close()
    finally:
        await fb.close()


def main():
    root = Path("data").resolve()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root / "browsers")
    previous = os.environ.get("DISPLAY")
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(Path(tmp))
        (settings.data_dir / "login-runtime").symlink_to(
            root / "login-runtime", target_is_directory=True
        )
        with remote_display(settings, 6089) as password:
            assert httpx.get("http://127.0.0.1:6089/vnc.html").status_code == 200
            asyncio.run(exercise(settings, password))
        assert os.environ.get("DISPLAY") == previous
        try:
            httpx.get("http://127.0.0.1:6089/vnc.html", timeout=1)
        except httpx.ConnectError:
            print("Remote display processes stopped and environment restored")
        else:
            raise AssertionError("Remote display left running")


if __name__ == "__main__":
    main()
