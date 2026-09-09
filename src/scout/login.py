"""Temporary loopback-only remote desktop for manual login over SSH."""

import asyncio
import os
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .login_setup import install_login, remote_tools
from .models import AccessBlocked
from .providers.facebook import Facebook, check_access
from .store import Store
from .worker import worker_lock


def remote_needed(settings, force=False):
    return force or (not os.environ.get("DISPLAY") and not settings.facebook_cdp)


def wait_port(process, port=None, socket_path=None):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("Remote display component exited; inspect login prerequisites")
        try:
            if socket_path is not None:
                with socket.socket(socket.AF_UNIX) as sock:
                    sock.settimeout(0.1)
                    sock.connect(str(socket_path))
                    return
            else:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Remote display did not become ready")


@contextmanager
def remote_display(settings, port=6080, *, socket_path=None):
    if settings.facebook_cdp:
        raise RuntimeError("Unset FACEBOOK_CDP_URL before using --remote")
    root = settings.data_dir / "login-runtime"
    x11vnc, novnc, missing = remote_tools(settings)
    if missing:
        raise RuntimeError(
            f"Remote login is missing {', '.join(missing)}. Run: scout login --setup --remote"
        )
    # Refuse an occupied public-facing bridge port before starting any subprocess.
    if socket_path is None:
        with socket.socket() as check:
            try:
                check.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError(
                    f"Port {port} is busy; use login --port with another port"
                ) from None
    processes = []

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous_signals = {}
    previous = {key: os.environ.get(key) for key in ("DISPLAY", "XAUTHORITY")}
    with tempfile.TemporaryDirectory(prefix="login-", dir=settings.data_dir) as temp:
        temp = Path(temp)
        env = dict(os.environ)
        libs = list((root / "usr/lib").glob("*-linux-gnu"))
        if libs:
            env["LD_LIBRARY_PATH"] = ":".join(str(p) for p in libs) + (
                ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else ""
            )
        display = next(
            (
                f":{n}"
                for n in range(100, 200)
                if not Path(f"/tmp/.X11-unix/X{n}").exists()
                and not Path(f"/tmp/.X{n}-lock").exists()
            ),
            None,
        )
        if display is None:
            raise RuntimeError("No free temporary X display")
        auth = temp / "Xauthority"
        auth.touch(mode=0o600)
        subprocess.run(
            ["xauth", "-f", str(auth), "add", display, ".", secrets.token_hex(16)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        env.update(DISPLAY=display, XAUTHORITY=str(auth))
        with (temp / "components.log").open("w") as log:

            def launch(command):
                process = subprocess.Popen(
                    command, env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL
                )
                processes.append(process)
                return process

            try:
                for sig in (signal.SIGHUP, signal.SIGTERM):
                    previous_signals[sig] = signal.signal(sig, interrupted)
                xvfb = launch(
                    [
                        "Xvfb",
                        display,
                        "-screen",
                        "0",
                        "1440x1000x24",
                        "-nolisten",
                        "tcp",
                        "-auth",
                        str(auth),
                    ]
                )
                for _ in range(100):
                    if xvfb.poll() is not None:
                        raise RuntimeError("Xvfb failed to start")
                    if Path(f"/tmp/.X11-unix/X{display[1:]}").exists():
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Xvfb startup timed out")
                password = secrets.token_hex(4)  # VNC supports eight password characters.
                passwd = temp / "vnc-password"
                subprocess.run(
                    [x11vnc, "-storepasswd", password, str(passwd)],
                    env=env,
                    check=True,
                    stdout=log,
                    stderr=log,
                )
                passwd.chmod(0o600)
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    vnc_port = sock.getsockname()[1]
                vnc = launch(
                    [
                        x11vnc,
                        "-display",
                        display,
                        "-auth",
                        str(auth),
                        "-no6",
                        "-listen",
                        "127.0.0.1",
                        "-rfbport",
                        str(vnc_port),
                        "-rfbauth",
                        str(passwd),
                        "-forever",
                        "-shared",
                        "-noxdamage",
                        "-quiet",
                    ]
                )
                wait_port(vnc, vnc_port)
                listen = (
                    ["--unix-listen", str(socket_path)]
                    if socket_path is not None
                    else [f"127.0.0.1:{port}"]
                )
                bridge = launch(
                    [
                        sys.executable,
                        "-m",
                        "websockify",
                        "--web",
                        str(novnc),
                        *listen,
                        f"127.0.0.1:{vnc_port}",
                    ]
                )
                wait_port(bridge, port, socket_path=socket_path)
                os.environ.update(DISPLAY=display, XAUTHORITY=str(auth))
                yield password
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                for sig, handler in previous_signals.items():
                    signal.signal(sig, handler)
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


MARKETPLACE_URL = "https://www.facebook.com/marketplace/"


def viewer_instructions(port, password, ssh_host=None):
    url = f"http://127.0.0.1:{port}/vnc.html?autoconnect=true&resize=scale"
    if ssh_host or os.environ.get("SSH_CONNECTION"):
        destination = shlex.quote(ssh_host or "YOUR_SSH_HOST")
        print(
            "Keep this terminal open. In another terminal on your own computer, run:\n"
            f"  ssh -N -o ExitOnForwardFailure=yes -L {port}:127.0.0.1:{port} -- {destination}",
            flush=True,
        )
        if not ssh_host:
            print("Replace YOUR_SSH_HOST with the user@host or SSH alias you use to connect.")
    else:
        print("Open the viewer on this computer (over SSH, forward this loopback port first).")
    print(f"Open: {url}\nTemporary viewer password: {password}", flush=True)


def available_port(port=None):
    """Prefer the familiar port, but don't make a default-port collision a setup task."""
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port if port is not None else 6080))
        except OSError:
            if port is not None:
                raise RuntimeError(
                    f"Port {port} is busy; retry with --port and a free port"
                ) from None
            sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def session_ready(page):
    """Require an authenticated Marketplace surface, never just absence of a login form."""
    parsed = urlsplit(page.url)
    if parsed.scheme != "https" or parsed.hostname != "www.facebook.com":
        return False
    if not parsed.path.startswith("/marketplace/"):
        return False
    await check_access(page)
    cookies = {c["name"]: c["value"] for c in await page.context.cookies([MARKETPLACE_URL])}
    if not cookies.get("c_user") or not cookies.get("xs"):
        return False
    # A blank page, generic Facebook home, or unavailable-Marketplace screen is not success.
    links = page.locator(
        'a[href*="/marketplace/item/"], a[href*="/marketplace/create"], '
        'a[href*="/marketplace/categories/"]'
    )
    for link in await links.all():
        if await link.is_visible():
            return True
    return False


async def wait_for_session(page, timeout=1800):
    from playwright.async_api import Error

    async def wait():
        consecutive = 0
        while True:
            if page.is_closed():
                raise RuntimeError("Login browser closed. Run scout login again to continue.")
            try:
                ready = await session_ready(page)
            except AccessBlocked:
                ready = False  # Leave checkpoints open for the person signing in.
            except Error:
                if page.is_closed():
                    raise RuntimeError("Login browser closed. Run scout login again.") from None
                ready = False  # Navigation can replace the execution context during login.
            consecutive = consecutive + 1 if ready else 0
            if consecutive >= 2:
                return
            await asyncio.sleep(2)

    try:
        await asyncio.wait_for(wait(), timeout=timeout)
    except TimeoutError:
        raise RuntimeError(
            "Login timed out. Open Marketplace after completing login/checkpoints, "
            "then retry scout login."
        ) from None


async def open_marketplace(fb, headed=False):
    from playwright.async_api import Error

    try:
        await fb.start(headed=headed)
    except Error:
        if fb.settings.facebook_cdp:
            message = (
                "Cannot connect to FACEBOOK_CDP_URL. Start your CDP browser or unset that setting."
            )
        else:
            message = (
                "Cannot start Chromium. Run scout login --setup; if installed, stop other "
                "browsers using Scout's profile and check that the display is available."
            )
        raise RuntimeError(message) from None
    page = await fb.context.new_page()
    try:
        response = await page.goto(MARKETPLACE_URL, wait_until="domcontentloaded", timeout=60000)
    except Error:
        await page.close()
        raise RuntimeError(
            "Cannot load Facebook Marketplace. Check your connection and retry."
        ) from None
    if response and response.status >= 400:
        await page.close()
        raise RuntimeError(
            f"Facebook returned HTTP {response.status}. Retry later or check access in your browser."
        )
    return page


async def check_session(settings):
    fb = Facebook(settings)
    page = None
    try:
        page = await open_marketplace(fb)
        try:
            await wait_for_session(page, timeout=20)
        except RuntimeError:
            raise RuntimeError(
                "Saved session could not be verified in Marketplace. Run scout login and "
                "complete any checkpoint; Marketplace must be available to your account."
            ) from None
    finally:
        try:
            if page is not None and not page.is_closed():
                await page.close()
        finally:
            await fb.close()


async def browser_login(settings):
    fb = Facebook(settings)
    page = None
    try:
        print("Opening Facebook. Sign in and complete any checkpoints in the browser.", flush=True)
        page = await open_marketplace(fb, headed=True)
        print(
            "Open Marketplace when done. Scout detects login automatically; no Enter needed.\n"
            "You have 30 minutes. Press Ctrl+C to cancel.",
            flush=True,
        )
        await wait_for_session(page)
        print(
            "Login detected. Checking the attached browser session..."
            if settings.facebook_cdp
            else "Login detected. Checking that the saved session works headlessly...",
            flush=True,
        )
    finally:
        try:
            if page is not None and not page.is_closed():
                await page.close()
        finally:
            await fb.close()
    # Closing the persistent context flushes the profile before reopening it headlessly.
    await check_session(settings)
    Store(settings.data_dir / "scout.sqlite3").resume_source("facebook")
    print(
        "Facebook session verified. Login complete.\n"
        "Start Scout: scout serve\n"
        "If you installed the user service: systemctl --user start scout",
        flush=True,
    )


def login(
    settings,
    force_remote=False,
    port=None,
    *,
    setup=False,
    setup_only=False,
    check=False,
    ssh_host=None,
):
    remote = remote_needed(settings, force_remote)
    if force_remote and settings.facebook_cdp:
        raise RuntimeError("Unset FACEBOOK_CDP_URL before using --remote")
    with worker_lock(settings.data_dir):
        if setup or setup_only:
            install_login(settings, remote=remote and not check)
        if setup_only:
            return
        if check:
            asyncio.run(check_session(settings))
            print("Saved Facebook session verified in Marketplace.")
        elif remote:
            port = available_port(port)
            with remote_display(settings, port) as password:
                viewer_instructions(port, password, ssh_host)
                asyncio.run(browser_login(settings))
        else:
            asyncio.run(browser_login(settings))
