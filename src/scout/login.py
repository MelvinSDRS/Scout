"""Temporary loopback-only remote desktop for manual login over SSH."""

import asyncio
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .providers.facebook import Facebook
from .store import Store
from .worker import worker_lock


def remote_needed(settings, force=False):
    return force or (not os.environ.get("DISPLAY") and not settings.facebook_cdp)


def wait_port(process, port):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("Remote display component exited; inspect login prerequisites")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Remote display did not become ready")


@contextmanager
def remote_display(settings, port=6080):
    if settings.facebook_cdp:
        raise RuntimeError("Unset FACEBOOK_CDP_URL before using --remote")
    root = settings.data_dir / "login-runtime"
    x11vnc = shutil.which("x11vnc") or str(root / "usr/bin/x11vnc")
    novnc = Path("/usr/share/novnc")
    if not (novnc / "vnc.html").exists():
        novnc = root / "usr/share/novnc"
    if (
        not all(shutil.which(cmd) for cmd in ("Xvfb", "xauth"))
        or not Path(x11vnc).exists()
        or not (novnc / "vnc.html").exists()
    ):
        raise RuntimeError(
            "Remote login needs Xvfb, xauth, x11vnc and noVNC. Run tools/install-login-runtime.sh"
        )
    # Refuse an occupied public-facing bridge port before starting any subprocess.
    with socket.socket() as check:
        try:
            check.bind(("127.0.0.1", port))
        except OSError:
            raise RuntimeError(f"Port {port} is busy; use login --port with another port") from None
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
                bridge = launch(
                    [
                        sys.executable,
                        "-m",
                        "websockify",
                        "--web",
                        str(novnc),
                        f"127.0.0.1:{port}",
                        f"127.0.0.1:{vnc_port}",
                    ]
                )
                wait_port(bridge, port)
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


async def wait_for_enter():
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def ready():
        line = sys.stdin.readline()
        if not future.done():
            if line:
                future.set_result(None)
            else:
                future.set_exception(RuntimeError("Login input closed; session ended"))

    loop.add_reader(sys.stdin.fileno(), ready)
    try:
        await asyncio.wait_for(future, timeout=1800)
    except TimeoutError:
        raise RuntimeError("Login timed out after 30 minutes; run login again") from None
    finally:
        loop.remove_reader(sys.stdin.fileno())


async def browser_login(settings):
    fb = Facebook(settings)
    try:
        await fb.start(headed=True)
        page = await fb.context.new_page()
        await page.goto(
            "https://www.facebook.com/marketplace/", wait_until="domcontentloaded", timeout=60000
        )
        print(
            "Complete login in the browser, then press Enter here (30-minute timeout).", flush=True
        )
        await wait_for_enter()
        if await page.locator('input[type="password"]').count() or any(
            part in page.url for part in ("/login", "/checkpoint")
        ):
            raise RuntimeError(
                "Facebook still shows login/checkpoint; authentication is not complete"
            )
        await page.close()
        Store(settings.data_dir / "scout.sqlite3").resume_source("facebook")
        print("Browser session saved. Restart with: systemctl --user start scout")
    finally:
        await fb.close()


def login(settings, force_remote=False, port=6080):
    with worker_lock(settings.data_dir):
        if remote_needed(settings, force_remote):
            with remote_display(settings, port) as password:
                print(
                    f"On your own computer run: ssh -N -L {port}:127.0.0.1:{port} user@server\n"
                    f"Then open http://127.0.0.1:{port}/vnc.html\nTemporary VNC password: {password}",
                    flush=True,
                )
                asyncio.run(browser_login(settings))
        else:
            asyncio.run(browser_login(settings))
