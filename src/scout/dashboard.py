"""Open the dashboard without copying the long-lived API token."""

import json
import sys
import webbrowser
from urllib.parse import urlsplit

from .dashboard_auth import DashboardAuth
from .login_client import READY_PREFIX


def dashboard_link(store, settings, url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        raise RuntimeError("Use an http:// or https:// dashboard URL without credentials")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise RuntimeError("Use the dashboard root URL without a query or fragment")
    if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("Use HTTPS for a remote dashboard, or a local SSH tunnel")
    return url.rstrip("/") + "/#signin=" + DashboardAuth(store, settings).issue_ticket()


def open_dashboard(store, settings, url="http://127.0.0.1:8765", *, handoff=False):
    link = dashboard_link(store, settings, url)
    if handoff:
        print(
            "\n"
            + READY_PREFIX
            + json.dumps({"kind": "dashboard", "ticket": link.split("#signin=")[1]}),
            flush=True,
        )
        print("Dashboard connected. Keep this terminal open; Ctrl+C closes the tunnel.", flush=True)
        try:
            while sys.stdin.read(1):
                pass
        except OSError:
            pass  # The SSH PTY has closed.
    else:
        try:
            opened = webbrowser.open(link, new=2)
        except (webbrowser.Error, OSError):
            opened = False
        if opened:
            print("Dashboard opened. This browser will stay signed in for 30 days.")
        else:
            print(f"Open this one-time sign-in link (valid for 5 minutes):\n{link}", flush=True)
