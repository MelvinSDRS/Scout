"""Server half of the one-command SSH login workflow."""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from .login_client import READY_PREFIX, handoff_path
from .login_setup import install_login, remote_tools


@contextmanager
def cancellation_signals():
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGHUP, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


async def login_until_disconnect(settings, parent_pid=None):
    from .login import browser_login

    loop = asyncio.get_running_loop()
    disconnected = loop.create_future()
    parent_pid = os.getppid() if parent_pid is None else parent_pid

    def disconnect():
        if not disconnected.done():
            disconnected.set_result(None)

    # Raising KeyboardInterrupt inside a Playwright callback makes asyncio.run cancel
    # the driver's initialization tasks before they can close their transport. Let our
    # coroutine cancel and await the browser work instead, then tear down the event loop.
    signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
    previous = {sig: signal.getsignal(sig) for sig in signals}
    for sig in signals:
        loop.add_signal_handler(sig, disconnect)

    async def watch_parent():
        while not disconnected.done():
            # Some SSH/PTY implementations don't deliver EOF while descendant processes
            # retain the terminal. Reparenting also proves that our SSH session has ended.
            if os.getppid() != parent_pid:
                disconnected.set_result(None)
                return
            await asyncio.sleep(0.2)

    def read_stdin():
        try:
            closed = not os.read(sys.stdin.fileno(), 1024)
        except OSError:
            closed = True  # Linux PTYs can report EIO instead of EOF after SSH disconnects.
        if closed:
            disconnect()

    loop.add_reader(sys.stdin.fileno(), read_stdin)
    session = asyncio.create_task(browser_login(settings))
    parent = asyncio.create_task(watch_parent())
    try:
        done, _ = await asyncio.wait((session, disconnected), return_when=asyncio.FIRST_COMPLETED)
        if disconnected in done:
            raise RuntimeError("SSH disconnected; login cancelled.")
        await session
    finally:
        loop.remove_reader(sys.stdin.fileno())
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)
        if not session.done():
            session.cancel()
        try:
            await asyncio.gather(session, return_exceptions=True)
        finally:
            for sig, handler in previous.items():
                loop.remove_signal_handler(sig)
                signal.signal(sig, handler)


@contextmanager
def handoff_socket(identifier):
    path = Path(handoff_path(identifier))
    # Exclusive creation prevents reuse of another login's socket or a pre-existing path.
    path.parent.mkdir(mode=0o700)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
        path.parent.rmdir()


def announce(password):
    print("\n" + READY_PREFIX + json.dumps({"password": password}), flush=True)


def ensure_runtime(settings, force=False):
    async def browser_missing():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            return not Path(playwright.chromium.executable_path).is_file()

    if force or remote_tools(settings)[2] or asyncio.run(browser_missing()):
        install_login(settings, remote=True)


@contextmanager
def pause_service():
    """Restore only a running user service belonging to this exact checkout."""
    paused = False
    if shutil.which("systemctl"):
        try:
            state = subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    "scout.service",
                    "--property=WorkingDirectory",
                    "--property=ActiveState",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            properties = dict(
                line.split("=", 1) for line in state.stdout.splitlines() if "=" in line
            )
            directory = properties.get("WorkingDirectory", "")
            paused = (
                state.returncode == 0
                and properties.get("ActiveState") == "active"
                and bool(directory)
                and Path(directory).resolve() == Path.cwd().resolve()
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        if paused:
            print("Pausing this checkout's Scout service for login...", flush=True)
            service_command("stop")
        yield
    finally:
        if paused:
            try:
                print("Restoring the Scout service...", flush=True)
            except OSError:
                pass  # The SSH terminal may already be gone; restoration still must happen.
            service_command("start")


def service_command(action):
    try:
        subprocess.run(["systemctl", "--user", action, "scout.service"], check=True, timeout=45)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError(
            f"Could not {action} Scout's user service. Run: systemctl --user {action} scout"
        ) from None


def login_from_client(settings, identifier, setup=False):
    from .login import remote_display
    from .worker import worker_lock

    handoff_path(identifier)  # Validate before installation or service state changes.
    if settings.facebook_cdp:
        raise RuntimeError("Unset FACEBOOK_CDP_URL on the server before using the laptop launcher.")
    with cancellation_signals():
        parent_pid = os.getppid()
        ensure_runtime(settings, force=setup)
        with pause_service(), worker_lock(settings.data_dir), handoff_socket(identifier) as path:
            with remote_display(settings, socket_path=path) as password:
                announce(password)
                asyncio.run(login_until_disconnect(settings, parent_pid))
