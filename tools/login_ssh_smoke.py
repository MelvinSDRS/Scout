"""Real SSH + noVNC + automatic login with temporary keys and synthetic Facebook pages."""

import asyncio
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import async_playwright

from scout.login import available_port, wait_port
from scout.login_client import connect, handoff_path


async def use_viewer(url, ready):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1500, "height": 1100})
            await page.goto(url)
            await page.wait_for_function(
                "document.documentElement.classList.contains('noVNC_connected')", timeout=20000
            )
            assert not await page.locator("#noVNC_password_input").is_visible()
            deadline = time.monotonic() + 20
            while not ready.exists():
                assert time.monotonic() < deadline, "Remote synthetic login did not open"
                await asyncio.sleep(0.1)
            await page.locator("canvas").click(position={"x": 700, "y": 500})
            await page.keyboard.type("synthetic-login")
            await page.keyboard.press("Enter")
            # Keep the viewer open until the server has completed its headless verification.
            await page.wait_for_function(
                "!document.documentElement.classList.contains('noVNC_connected')", timeout=30000
            )
        finally:
            await browser.close()


def main():
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    if not Path(sshd).exists():
        raise RuntimeError("Install openssh-server to run the SSH login smoke test")
    project = Path(__file__).resolve().parents[1]
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(project / "data/browsers")
    with tempfile.TemporaryDirectory(prefix="scout-ssh-smoke-") as directory:
        root = Path(directory)
        for key in ("host", "client"):
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / key)], check=True
            )
        port = available_port(0)
        config = root / "sshd_config"
        # Only this disposable daemon relaxes ancestor-mode checks for keys under /tmp.
        config.write_text(
            f"ListenAddress 127.0.0.1\nPort {port}\nHostKey {root}/host\n"
            f"PidFile {root}/sshd.pid\nAuthorizedKeysFile {root}/client.pub\n"
            "PasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\n"
            "StrictModes no\nPermitRootLogin prohibit-password\nAllowTcpForwarding local\n"
            "AllowStreamLocalForwarding local\n"
        )
        ssh_config = root / "ssh_config"
        ssh_config.write_text(
            f"Host scout-test\n HostName 127.0.0.1\n Port {port}\n User {getpass.getuser()}\n"
            f" IdentityFile {root}/client\n IdentitiesOnly yes\n BatchMode yes\n"
            f" UserKnownHostsFile {root}/known_hosts\n StrictHostKeyChecking yes\n"
        )
        (root / "known_hosts").write_text(f"[127.0.0.1]:{port} " + (root / "host.pub").read_text())
        checkout = root / "Scout with spaces"
        (checkout / ".venv/bin").mkdir(parents=True)
        state = checkout / "data"
        state.mkdir(mode=0o700)
        runtime = project / "data/login-runtime"
        if runtime.is_dir():
            (state / "login-runtime").symlink_to(runtime, target_is_directory=True)
        ready = root / "browser-ready"
        executable = checkout / ".venv/bin/scout"
        executable.write_text(f'''#!{sys.executable}
import os
import sys
from pathlib import Path
from scout.settings import Settings
from scout.providers.facebook import Facebook
import scout.login
from scout.login_handoff import login_from_client
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = {str(project / "data/browsers")!r}
class OfflineFacebook(Facebook):
    async def start(self, headed=False):
        await super().start(headed=headed)
        async def route(request):
            cookies = {{c['name'] for c in await self.context.cookies()}}
            if 'c_user' in cookies:
                html = '<a href="/marketplace/item/123">Synthetic listing</a>'
            else:
                html = """<h1>Synthetic Facebook login</h1><input autofocus onblur="this.focus()"
                onkeydown="if(event.key==='Enter' && this.value==='synthetic-login'){{
                document.cookie='c_user=synthetic; Max-Age=3600; Path=/; Secure';
                document.cookie='xs=synthetic; Max-Age=3600; Path=/; Secure';location.reload();}}">
                <script>fetch('/ready')</script>"""
            if request.request.url.endswith('/ready'):
                Path({str(ready)!r}).touch()
            await request.fulfill(content_type='text/html', body=html)
        await self.context.route('**/*', route)
scout.login.Facebook = OfflineFacebook
settings = Settings(Path({str(state)!r}))
settings.prepare()
login_from_client(settings, sys.argv[sys.argv.index('--handoff') + 1])
''')
        executable.chmod(0o700)
        with (root / "sshd.log").open("w+") as log:
            daemon = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)], stdout=log, stderr=log)
            try:
                wait_port(daemon, port)
                urls = []

                def open_browser(url, new=0):
                    urls.append(url)
                    asyncio.run(use_viewer(url, ready))
                    return True

                with patch("scout.login_client.webbrowser.open", open_browser):
                    connect("scout-test", str(checkout), ssh_config=ssh_config)
                assert len(urls) == 1
                import socket
                from urllib.parse import urlsplit

                with socket.socket() as sock:
                    assert sock.connect_ex(("127.0.0.1", urlsplit(urls[0]).port)) != 0
                print(
                    "SSH login passed: one connection, automatic viewer authentication, "
                    "keyboard input, persisted headless login, tunnel cleanup."
                )
                identifier = "c" * 32
                with (
                    patch("scout.login_client.secrets.token_hex", return_value=identifier),
                    patch("scout.login_client.webbrowser.open", side_effect=KeyboardInterrupt),
                ):
                    try:
                        connect("scout-test", str(checkout), ssh_config=ssh_config)
                    except KeyboardInterrupt:
                        pass
                    else:
                        raise AssertionError("Login cancellation was not propagated")
                deadline = time.monotonic() + 15
                while Path(handoff_path(identifier)).parent.exists():
                    assert time.monotonic() < deadline, "Cancelled server viewer was left running"
                    time.sleep(0.1)
                assert not list(state.glob("login-*/components.log"))
                print("SSH cancellation passed: remote socket and display runtime removed.")
            except BaseException:
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                daemon.terminate()
                daemon.wait(timeout=10)


if __name__ == "__main__":
    main()
