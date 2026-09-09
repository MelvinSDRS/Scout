"""Install login prerequisites using the same interpreter and paths as Scout."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def remote_tools(settings):
    root = settings.data_dir / "login-runtime"
    x11vnc = shutil.which("x11vnc") or str(root / "usr/bin/x11vnc")
    novnc = Path("/usr/share/novnc")
    if not (novnc / "vnc.html").is_file():
        novnc = root / "usr/share/novnc"
    missing = [cmd for cmd in ("Xvfb", "xauth") if not shutil.which(cmd)]
    if not os.access(x11vnc, os.X_OK):
        missing.append("x11vnc")
    if not (novnc / "vnc.html").is_file():
        missing.append("novnc")
    return x11vnc, novnc, missing


def install_login(settings, remote=False):
    if settings.facebook_cdp:
        raise RuntimeError(
            "FACEBOOK_CDP_URL uses your own browser; unset it to install Scout's runtime"
        )
    if remote and remote_tools(settings)[2]:
        if not shutil.which("apt-get"):
            raise RuntimeError(
                "Automatic remote setup needs Debian/Ubuntu. Install Xvfb, xauth, x11vnc and "
                "noVNC with your package manager, then run scout login --setup again."
            )
        prefix = []
        if os.geteuid() != 0:
            if not shutil.which("sudo"):
                raise RuntimeError(
                    "Ask an administrator to run: apt-get update && "
                    "apt-get install -y xvfb xauth x11vnc novnc; then retry scout login --setup"
                )
            prefix = ["sudo"]
        print("Installing the remote viewer's system packages (may ask for sudo).", flush=True)
        run_install([*prefix, "apt-get", "update"])
        run_install([*prefix, "apt-get", "install", "-y", "xvfb", "xauth", "x11vnc", "novnc"])
    print("Installing Chromium and its system dependencies (may ask for sudo).", flush=True)
    run_install([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"])
    print("Login setup complete.", flush=True)


def run_install(command):
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError):
        raise RuntimeError(
            "Login setup failed. See the installer output above, fix the reported problem, "
            "then rerun scout login --setup."
        ) from None
