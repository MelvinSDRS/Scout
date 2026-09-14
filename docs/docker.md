# Docker deployment

All hostnames and network addresses below are examples. Keep your real deployment
in an ignored `compose.private.yaml`, and set `COMPOSE_FILE=compose.private.yaml`
in your private `.env`. Keep host-specific Nginx configuration under private
`data/` and point that Compose file at it. Never commit these private files.


This deployment keeps Scout and its Facebook browser inside the existing healthy
`gluetun` container's network namespace. `gluetun` must already be connected to
the ProtonVPN Canada route and to the external `torrent_default` network. Scout
does not create, configure, or copy credentials into a VPN container.

The Nginx gateway is the only container with published ports. It joins the
existing `torrent_default` network at `172.29.0.250` and forwards to
`gluetun:8765` through Docker DNS. It also joins `nginx_default` at
`172.28.0.250`; NPM's Scout proxy host points directly to that address on port
8765 so the gateway sees NPM's source address without host NAT. Confirm both
addresses are unused before the first start. The local host bindings are
`127.0.0.1:8765` and `172.28.0.1:8765`.

## Prepare the host

Run these checks without changing the existing torrent stack:

```sh
docker inspect gluetun --format '{{.State.Health.Status}}'
docker network inspect torrent_default
docker network inspect nginx_default
```

The network must already exist, and `gluetun` must be healthy. If the Gluetun
container is recreated, recreate Scout afterward so its `container:` network
namespace points at the current container:

```sh
docker compose up -d --force-recreate scout gateway
```

Create the persistent data directory and the runtime-only environment file. Keep
the file owner-readable and fill in the real values locally:

```sh
mkdir -p data
chmod 700 data
touch .env.docker
chmod 600 .env.docker
${EDITOR:-vi} .env.docker
```

`.env.docker` is read only at runtime and is excluded from the image build
context. It should contain the Telegram settings and, if desired, a long-lived
`SCOUT_API_TOKEN`; it must not contain `FACEBOOK_CDP_URL`. Scout generates
`/data/api-token` when no token is supplied. The browser profile, SQLite state,
and image references remain in `./data`.

For a dedicated Facebook account, set `SCOUT_FACEBOOK_RESTORE_HOME=false` in
`.env.docker` to leave Marketplace at the last searched location instead of
capturing and restoring its initial location and radius. The default is `true`
for accounts also used manually. Recreate Scout after changing this setting.

## Build and start

The image uses Python 3.12 on Debian Bookworm, installs the locked production
dependencies with `uv sync --frozen --no-dev`, and installs the Chromium version
selected by the locked Playwright package into `/ms-playwright`. The runtime
user is UID/GID 1000, the root filesystem is read-only, `/tmp` is a tmpfs, and
Chromium receives a 256 MiB shared-memory mount.

Build and start the two services after the existing VPN prerequisites are ready:

```sh
docker compose build --pull
docker compose up -d
docker compose ps
```

The Scout command is `scout serve --no-open`. No host Chrome, host Playwright
installation, or external CDP endpoint is used. The gateway preserves the
dashboard authority `scout.example.com` and allows `localhost` for local
access. NPM at `172.28.0.2` may supply the external `X-Forwarded-Proto`; other
callers have all forwarded headers overwritten by the gateway. Scout trusts
only the gateway address `172.29.0.250`.

Use the normal HTTPS NPM URL for the dashboard. For a local check, open
`http://127.0.0.1:8765/` or `http://localhost:8765/`. The root page is an
unauthenticated liveness check; API and dashboard state remain authenticated.
The API health endpoint must be checked through an authenticated dashboard
session or bearer token, so no health check prints or embeds a token.

To create the one-use dashboard sign-in link inside the container:

```sh
docker compose exec scout scout open --url https://scout.example.com
```

Open the printed link in the browser that will use Scout. For local access use
`--url http://localhost:8765`; the one-use ticket is still stored only in the
Scout data directory.

## Facebook login through the VPN

The image includes Chromium and a temporary private browser viewer. Use the
[Docker login helper](../tools/login-docker.sh), which pauses Scout, starts the
login browser in Gluetun's network namespace and restarts Scout afterward.

**On the server**, from the Scout checkout:

```bash
bash tools/login-docker.sh
```

Keep that terminal open. It prints a temporary viewer password. **On your own
computer**, open another terminal and forward a local port to the viewer socket:

```bash
ssh -N -S none -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:16080:/absolute/path/to/Scout/data/login-viewer.sock \
  user@server
```

Replace the checkout path and SSH destination with your own. This example uses
`./data` from the supplied Compose file; adjust the socket path if you change the
volume mount. Open **http://127.0.0.1:16080/vnc.html**, click **Connect**, and enter
the password printed on the server. Use this forwarded address even if the helper
prints port 6080. The viewer is available only while login is running.

Sign into Facebook, complete any checkpoints and open Marketplace. Scout detects
completion, verifies the saved session headlessly, closes the viewer and restarts
the normal service. Watches that you explicitly paused remain paused; re-enable
them in the dashboard when ready. Close the SSH tunnel with Ctrl+C afterward.

Keep both terminals open until login completes. If the browser reports a reset,
check that the address uses **HTTP**, then test on your computer:

```bash
curl --noproxy '*' -v http://127.0.0.1:16080/vnc.html -o /dev/null
```

A `200 OK` response confirms the tunnel and viewer work; check your browser's
HTTPS upgrade or proxy settings, or try another browser. A forwarding error in
the SSH terminal points to the socket path, permissions or SSH configuration.

Do not use a host Chrome browser or set `FACEBOOK_CDP_URL` for this deployment;
the Facebook browser must stay inside Gluetun's namespace. Its persistent
profile lives under `./data`. The temporary viewer socket is owner-only and
removed when the helper exits normally.

## Lifecycle and troubleshooting

```sh
docker compose logs -f scout
docker compose logs -f gateway
docker compose restart scout gateway
docker compose down
```

`docker compose down` removes only this Compose project's containers; it does
not remove the external Docker networks or recreate Gluetun. Do not
remove `./data` while the service is stopped unless you intend to discard the
Scout database, token, browser session, and photo-review state. Back it up with
the service stopped. If a Gluetun recreation leaves Scout unable to start,
recreate Scout and gateway after Gluetun becomes healthy.
