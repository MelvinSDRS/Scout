#!/usr/bin/env bash
# Private Facebook login through Scout's existing Gluetun network namespace.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
docker compose stop scout
trap 'docker compose up -d --no-build scout' EXIT
docker compose run --rm --no-deps -T scout python - <<'PY'
import asyncio
import os
from scout.login import remote_display, browser_login
from scout.settings import Settings
from scout.worker import worker_lock

os.umask(0o077)
settings = Settings.load()
socket_path = settings.data_dir / "login-viewer.sock"
with worker_lock(settings.data_dir):
    if socket_path.exists():
        raise RuntimeError("A login socket already exists; check for another login process.")
    try:
        with remote_display(settings, socket_path=socket_path) as password:
            print("Open http://127.0.0.1:6080/vnc.html after starting your SSH tunnel.", flush=True)
            print(f"Temporary viewer password: {password}", flush=True)
            asyncio.run(browser_login(settings))
    finally:
        socket_path.unlink(missing_ok=True)
PY
