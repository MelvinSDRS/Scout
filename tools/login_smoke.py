"""Exercise real Xvfb/VNC/noVNC with a disposable browser profile and no external sites."""

import asyncio
import os
import signal
import tempfile
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from scout.login import available_port, remote_display
from scout.providers.facebook import Facebook
from scout.settings import Settings


async def exercise(settings, password, port):
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
                await page.goto(f"http://127.0.0.1:{port}/vnc.html?autoconnect=true&resize=scale")
                await page.locator("#noVNC_password_input").fill(password)
                await page.locator("#noVNC_credentials_button").click()
                await page.wait_for_function(
                    "document.documentElement.classList.contains('noVNC_connected')", timeout=20000
                )
                assert await page.locator("canvas").count() == 1
                await page.locator("canvas").click(position={"x": 700, "y": 500})
                # The remote input retains autofocus; prove keyboard events cross the viewer.
                await remote_page.locator("input").focus()
                await page.keyboard.type("scout-viewer-test")
                await remote_page.wait_for_function(
                    "document.querySelector('input').value === 'scout-viewer-test'"
                )
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
        if (root / "login-runtime").is_dir():
            (settings.data_dir / "login-runtime").symlink_to(
                root / "login-runtime", target_is_directory=True
            )
        port = available_port(0)
        url = f"http://127.0.0.1:{port}/vnc.html"
        with remote_display(settings, port) as password:
            assert httpx.get(url).status_code == 200
            asyncio.run(exercise(settings, password, port))
        assert os.environ.get("DISPLAY") == previous
        try:
            httpx.get(url, timeout=1)
        except httpx.ConnectError:
            print("Remote display processes stopped and environment restored")
        else:
            raise AssertionError("Remote display left running")
        # The previous HTTP connection can leave its port in TIME_WAIT after shutdown.
        port = available_port(0)
        url = f"http://127.0.0.1:{port}/vnc.html"
        previous_handler = signal.getsignal(signal.SIGTERM)
        try:
            with remote_display(settings, port):
                os.kill(os.getpid(), signal.SIGTERM)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("Termination signal did not cancel the viewer")
        assert os.environ.get("DISPLAY") == previous
        assert signal.getsignal(signal.SIGTERM) == previous_handler
        assert not list(settings.data_dir.glob("login-*/components.log"))
        try:
            httpx.get(url, timeout=1)
        except httpx.ConnectError:
            print("Termination signal removed the viewer and restored signal handlers")
        else:
            raise AssertionError("Cancelled remote display left running")


if __name__ == "__main__":
    main()
