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

## Facebook login

From your checkout after `uv sync --frozen`:

```bash
uv run scout login --setup
```

This installs [Playwright Chromium and its Linux system dependencies](https://playwright.dev/python/docs/browsers#install-system-dependencies),
then opens Facebook. Installation may request sudo. Scout loads `.env` first and installs the
browser into the same location it will use at runtime (`data/browsers` by default, or your
explicit `PLAYWRIGHT_BROWSERS_PATH`). `SCOUT_DATA` in `.env` works without exporting it again.

Sign in, complete any two-factor authentication/checkpoint, and open Marketplace. Scout
waits up to 30 minutes and finishes automatically after detecting an authenticated Marketplace
page and reopening the session headlessly. It only clears scan backoff after verification
succeeds. Checkpoints remain for you to resolve. Ctrl+C cancels and closes the login browser.

For subsequent logins, use `uv run scout login`. To check an existing session without opening
a visible browser or changing scan backoff:

```bash
uv run scout login --check
```

The check opens Marketplace headlessly and exits with status 0 on verified access, or 1 with
an actionable error. It checks session access, not search coverage or collection permissions.
It still requires the worker to be stopped because both use the same private browser profile.

## Headless Facebook login

### Recommended: one command on your own computer

The server needs a Scout checkout and `uv`. Update both the server and the launcher to a
version with this workflow. From the checkout on your laptop/desktop, run:

```bash
python3 tools/login_remote.py user@server
# If your server checkout is elsewhere:
python3 tools/login_remote.py my-ssh-alias --directory /srv/Scout
```

The default server directory is `~/Scout`. `--directory` also accepts paths containing spaces
or `~/`. The helper uses your normal OpenSSH configuration, including keys, aliases and jump
hosts. `--ssh-port` and `--ssh-config` handle custom connection settings. Host-key checks stay
enabled; a first connection may ask you to verify the host, and SSH may ask for your password
or key passphrase.

The launcher creates the server virtual environment with `uv sync --frozen` if needed,
installs missing login prerequisites, sets up forwarding, and opens your local browser at the
viewer. Debian/Ubuntu dependency installation may ask for sudo in the same terminal. On
subsequent runs, installed prerequisites are reused. Add `--setup` to repair/reinstall them.

**You only handle Facebook sign-in and checkpoints.** There is no second terminal, manually
chosen port, viewer password to copy, or Enter confirmation. The helper supplies the temporary
viewer credential using a [noVNC URL fragment](https://novnc.com/noVNC/docs/EMBEDDING.html), which
is not sent in HTTP requests. Treat that temporary link as private; it can remain in local
browser history but stops working when the viewer closes. If browser launch is unavailable,
the helper prints the link for you to open on the same computer.

The local end binds to a free `127.0.0.1` port. A single SSH connection forwards it to an
owner-only Unix socket on the server; no server HTTP port needs to be chosen or exposed.
Normal OpenSSH forwarding permissions must allow local TCP-to-Unix-socket forwarding.
The browser profile and Facebook credentials stay on the server. Closing the helper or
pressing Ctrl+C closes the SSH connection; the server tears down the temporary viewer.

If `scout.service` is already active and its working directory matches this checkout, the
helper pauses it for login and restores it afterward, including after a failed/cancelled
login. It does not start an inactive service or stop another checkout's service. Foreground
`serve`/`worker` processes still need to be stopped manually. If service restoration fails,
Scout prints the recovery command.

The laptop helper uses only the Python standard library and OpenSSH. It needs neither the
Scout dependencies nor a local Chromium download. You can copy just
[`src/scout/login_client.py`](../src/scout/login_client.py) and run
`python3 login_client.py user@server`. An installed wheel also provides `scout-login user@server`.
The server stays Linux; laptop execution is tested on Linux, with no macOS/Windows validation yet.

### Manual server-terminal fallback

On Debian/Ubuntu servers, the same `login --setup` command detects the missing display and
installs Xvfb, xauth, x11vnc and noVNC through apt when needed. To force the viewer and print a
ready-to-copy tunnel command, supply the SSH alias or destination you normally use:

```bash
# On the server; stop the service first only if you installed it:
systemctl --user stop scout
uv run scout login --setup --ssh-host user@server
```

1. Keep that server terminal open.
2. Run the printed SSH command in another terminal on your own computer.
3. Open the printed local URL and enter the temporary viewer password.
4. Sign into Facebook in that browser and open Marketplace. Scout verifies the saved
   session, closes the viewer, and prints the command to start Scout again.

For the default port, the tunnel command is:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 6080:127.0.0.1:6080 -- user@server
```

The viewer URL enables [automatic connection and scaling](https://novnc.com/noVNC/docs/EMBEDDING.html).
In this manual flow the password is printed separately, not embedded in the URL. Both VNC and the viewer bind
only to `127.0.0.1`; no firewall opening, public listener or desktop installation is needed.
`--remote` forces the viewer without an SSH alias. Without an alias Scout prints a placeholder
when it detects an SSH connection. A headless local machine can open the URL directly.

Scout prefers port 6080 and selects a free port if it is busy. Always use the printed URL and
tunnel command. To choose a port yourself, pass `--port 6090`; an explicitly chosen busy port
fails with instructions. If the port is occupied on your own computer, rerun login with a
port free on both machines. Stop the tunnel with Ctrl+C when finished. The viewer closes on
completion, cancellation, SSH disconnect or the 30-minute login timeout.

### Preinstalling and alternative environments

`uv run scout login --setup-only --remote` installs everything without opening Facebook.
This is useful during server provisioning; later run `uv run scout login --remote` as the
same user with the same configuration. Use sudo for system packages, not for running Scout.
The setup implementation ships in the wheel as well as the source release.

Without sudo, ask an administrator to install the prerequisites. Debian/Ubuntu example:

```bash
sudo apt-get update
sudo apt-get install -y xvfb xauth x11vnc novnc
# Playwright may request administrator access for its own system dependencies:
uv run playwright install-deps chromium
# As the Scout user, install Chromium into the configured browser directory:
PLAYWRIGHT_BROWSERS_PATH="$PWD/data/browsers" uv run playwright install chromium
uv run scout login --remote
```

Use your actual browser directory if you changed `SCOUT_DATA` or `PLAYWRIGHT_BROWSERS_PATH`.
On other Linux distributions, install the equivalent display packages and Chromium system
libraries with your package manager. Automatic remote package installation supports apt only;
Playwright's supported Linux distributions define its system-dependency support. If system
libraries are already present, skip `--setup` after installing Chromium with
`PLAYWRIGHT_BROWSERS_PATH="/absolute/path/to/your/data/browsers" uv run playwright install chromium`.
That path must match Scout's data directory or your `PLAYWRIGHT_BROWSERS_PATH` override.

The older `tools/install-login-runtime.sh` remains available to unpack Debian/Ubuntu
x11vnc/noVNC packages without root when Xvfb, xauth and their libraries are already installed.
It is a limited fallback; export `SCOUT_DATA` when using that script with a custom data path.

`FACEBOOK_CDP_URL` is an advanced alternative for an operator-managed browser. Scout checks
that attached session, closes only its own tabs, and leaves your browser open. Unset the
setting before `--remote`, `--ssh-host` or `--setup`; the standard profile workflow is simpler.

### Troubleshooting login

| Symptom | What to do |
| --- | --- |
| Another worker or login is running | Stop foreground `scout serve`/`worker` with Ctrl+C, or `systemctl --user stop scout`; close any other login process. |
| Missing browser or viewer dependencies | Run `uv run scout login --setup` (add `--remote` to force the viewer). |
| Installer fails | Read the installer output, fix package/network/sudo access, then rerun the same command. |
| Viewer will not connect | Keep the server terminal and SSH tunnel open, use the printed port, and check the tunnel terminal for errors. |
| Login completes but Scout keeps waiting | Open Marketplace in Scout's browser tab; finish checkpoints and confirm that your account has Marketplace access. |
| Headless verification fails | Run login again and resolve any challenge. A headed login alone does not prove that Facebook accepts the headless session. |
| Login times out or browser closes | Rerun `uv run scout login`; the private profile is retained. |

After success, use `uv run scout serve` for a foreground instance, or
`systemctl --user start scout` for an installed service. Scout never starts a second worker
automatically. Keep `data/`, `.env`, browser profiles and viewer passwords private.

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
