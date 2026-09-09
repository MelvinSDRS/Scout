# Deployment

## User service

Use a Linux checkout with `uv sync --frozen` completed. Run:

```bash
uv run python tools/install_service.py
systemctl --user daemon-reload
systemctl --user enable --now scout
journalctl --user -u scout -f
```

`deploy/scout.service` is a template, not a unit to copy directly. The installer renders
absolute paths for the current checkout. To inspect it first, pass
`--output /tmp/scout.service`. The installer does not start or restart anything.
For startup without an interactive login, configure systemd user lingering with your system
administrator. Do not run `serve`, `worker` or `once` concurrently; all use the browser lock.

## Headless Facebook login

On Debian/Ubuntu, install Xvfb and xauth using your package manager. Then:

```bash
uv run python --version
bash tools/install-login-runtime.sh
systemctl --user stop scout
uv run scout login --remote
```

The helper downloads and unpacks x11vnc/noVNC packages locally into `data/login-runtime`;
it does not run their package installation scripts. Xvfb and xauth must be on PATH.
Set `SCOUT_DATA` in the shell as well if you chose a custom data directory for this helper.

Keep that terminal open. From your own computer:

```bash
ssh -N -L 6080:127.0.0.1:6080 user@server
```

Open `http://127.0.0.1:6080/vnc.html`, enter the temporary VNC password printed on the server,
and sign into Facebook inside that browser. Complete any checkpoint yourself. When
Marketplace opens, press Enter in the server terminal, then `systemctl --user start scout`.
The temporary viewer is removed on completion, interruption or the 30-minute input timeout.
Never publish its port or password. `FACEBOOK_CDP_URL` can instead use an operator-managed
local browser; unset it before using the dedicated remote-login workflow.

## Remote dashboard and proxy

For occasional remote access, tunnel the dashboard:

```bash
ssh -N -L 8765:127.0.0.1:8765 user@server
```

For a permanent HTTPS reverse proxy, configure `.env` with your own values:

```dotenv
SCOUT_BIND_HOSTS=127.0.0.1
SCOUT_ALLOWED_HOSTS=localhost,127.0.0.1,scout.example.com
SCOUT_TRUSTED_PROXIES=127.0.0.1
```

The proxy forwards to `127.0.0.1:8765` and preserves the external Host and HTTPS scheme.
A proxy container may need an explicitly selected private bridge listener and trusted
source address; adapt all three settings to your network. Do not use wildcard listeners,
allowed hosts or trusted proxies. Disable caching for authenticated responses and retain
Scout's token authentication. TLS termination and proxy administration are operator tasks.

## State and backups

`SCOUT_DATA` defaults to `data/` relative to the working directory. It includes the SQLite
database, generated access token, browser session and optional image references. Keep it
owner-only and outside any published source archive.

Stop the service before a full directory backup or restore. For an online database-only
backup, use SQLite's `Connection.backup()` API; copying just the main database while WAL
writes are active is unsafe. Reference folders outside `SCOUT_DATA` need separate backups.
Restoring a database does not restore Facebook authentication unless the profile is restored.

Older mixed-source databases undergo a backed-up, one-time Marketplace-only migration.
The backup is `scout.sqlite3.before-marketplace-only-*.sqlite3`. Legacy installations using
`marketwatch.sqlite3` must stop the service, checkpoint/back up SQLite, and rename that
database to `scout.sqlite3` before starting this version. Preserve the browser profile and
access token; rename `MARKETWATCH_*` configuration keys to `SCOUT_*`. Sharing a Telegram env
file now requires an explicit `SCOUT_TELEGRAM_ENV_FILE` (or legacy `HOMEBOT_ENV_FILE`) setting.

Worker health and an HTTP 200 do not guarantee successful regional collection. Monitor
per-region errors, pending alerts and Facebook checkpoints. Facebook access errors trigger
a one-hour cooldown and regional backoff. Never run automated probes against another
operator's installation without authorization.
