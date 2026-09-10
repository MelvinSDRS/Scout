# Scout

Scout is a self-hosted Facebook Marketplace explorer with saved searches, scheduled
watches and optional Telegram alerts. Search regional samples in the USA, Canada and
mainland France; browse photos, filter titles/prices, and turn a search into a watch.

Scout is an early-stage project. Facebook can change its layout, limit results, ignore
location filters or require a login checkpoint. Country tabs identify the search area,
not a verified seller location. Alerts report newly discovered listings, not guaranteed
publication times. See [architecture and limitations](docs/architecture.md).

Before each regional scan, Scout saves your current Marketplace location and radius,
then restores and verifies them when the scan ends, including after errors or cancellation.
If restoration fails, the scan reports an error and keeps the original settings locally
for recovery before another scan. If older scans already changed your preferences, set
your home location and radius in Facebook once before starting the next scan.

## Requirements

- Linux with Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).
- Chromium installed through Playwright (commands below).
- A Facebook session with access to Marketplace and permission for the intended collection.
- Optional: your own Telegram bot token and destination.

Linux is the supported platform. Worker locking, resource limits and the optional headless
login viewer use Unix/Linux facilities. The separate laptop launcher uses Python and OpenSSH;
its end-to-end tests run on Linux. Windows and macOS execution is not currently validated.

## Quick start

From a checkout or extracted source release:

```bash
cd Scout
uv sync --frozen
uv run scout login --setup
uv run scout serve
```

`login --setup` installs Chromium and its system dependencies, then opens the login browser.
On a Debian/Ubuntu server without a display it also installs a temporary browser viewer and
prints SSH tunnel instructions. Installation may ask for your sudo password. Subsequent
logins only need `uv run scout login`.

Sign into Facebook, complete any checkpoints yourself, and open Marketplace. Scout detects
completion automatically and checks that the saved session works headlessly before exiting.
You do not need to press Enter or copy cookies. Stop a running Scout worker before logging in.
See [login and troubleshooting](docs/deployment.md#facebook-login) for remote servers,
preinstalled dependencies and session checks.

**Using a headless server?** From the checkout on your own computer, run:

```bash
python3 tools/login_remote.py user@server
```

This assumes Scout is in `~/Scout` on the server; use `--directory /path/to/Scout` otherwise.
The launcher installs missing server prerequisites, creates the SSH tunnel, and opens the
login viewer with its temporary password supplied automatically. Just complete Facebook login.
A running Scout user service for that checkout is paused and restored automatically.

Your computer only needs Python 3.10+, OpenSSH and a browser; no local Scout installation or
Playwright download is needed. You can also copy the standalone
[login launcher](src/scout/login_client.py) to your computer and run
`python3 login_client.py user@server`. See [the remote guide](docs/deployment.md#headless-facebook-login).

No configuration file is needed for the default local setup. To change settings or enable
Telegram, copy `.env.example` to `.env` and edit it. Browser installation and login use the
same configured data directory, including `SCOUT_DATA` in `.env`.

On a local desktop, `scout serve` opens the dashboard and signs you in automatically.
To reopen a running dashboard, use:

```bash
uv run scout open
```

For a running server, use the laptop launcher instead:

```bash
python3 tools/login_remote.py user@server --dashboard
```

Both open a one-time sign-in link; **there is no token to copy**. Your browser stays signed in
for 30 days, including across reloads and server restarts. **Sign out** revokes that browser's
session. The SSH launcher also creates the dashboard tunnel; keep its terminal open while
using it. Add `--directory /path/to/Scout` if the server checkout is not `~/Scout`.

API scripts can still use `uv run scout token`; the dashboard's advanced token option is a
fallback. Runtime state and sign-in credentials stay in private `data/`. See
[dashboard access](docs/deployment.md#dashboard-access) for HTTPS proxies and recovery.

Start in **Explore**, enter an item and select countries. **Refine search** supports required
and excluded phrases, alternative phrase groups and per-currency price limits. Results
arrive by region. **Include broader suggestions** shows collected items that failed title
filters; **Load more results** paginates the stored results.

**Create watch** preserves the search filters and baselines existing matches so they are not
sent as newly found alerts. Manage watches and review uncertain photos under **My watches**.
Only one interactive search runs at a time. Interactive and scheduled checks share a worker.

## Telegram alerts

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. An optional
`TELEGRAM_MESSAGE_THREAD_ID` routes messages to a forum topic. Use values for a bot and
chat you control; the bot needs permission to post there. Restart the service after changes.

```bash
uv run scout test-notification
```

That command sends one setup message. Scout uses outbound `sendMessage` only and never
polls Telegram updates. No other project is required. If you intentionally share an existing
bot configuration, set `SCOUT_TELEGRAM_ENV_FILE` to its env-file path; only token and chat
are read. The older `HOMEBOT_ENV_FILE` name is accepted as an explicit compatibility alias.

## CLI

```bash
uv run scout add "vintage camera" --countries US CA FR --interval 60
uv run scout add "vintage camera" --countries CA --max-price CAD=200 --exclude "parts only"
uv run scout list
uv run scout status
uv run scout probe "vintage camera" --country CA
uv run scout delete 1
```

Examples are not created automatically. `probe` reads one region without saving results or
sending alerts. By default every query word must occur in the title. Unknown prices are
excluded when price limits are active; currencies are never converted implicitly.

## Photo checks

Optional photo profiles use your own reference JPEGs; no product or seller images are
shipped with Scout. Only valid configured profiles appear in the dashboard. See
[photo profiles](docs/photo-filter.md) for setup, cache behavior and limitations.

## Deployment and maintenance

See [deployment](docs/deployment.md) for systemd, headless login, reverse proxies and backups.
Defaults bind only to loopback. Do not expose a bare HTTP API or a remote login viewer publicly.

```bash
uv run python tools/install_service.py
systemctl --user daemon-reload
systemctl --user enable --now scout
```

The installer generates paths for your checkout; moving it requires regenerating the unit
and virtual environment. The database is `data/scout.sqlite3`. Never publish `data/`, `.env`,
browser profiles, screenshots, response archives or backups.

## Development and licensing

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) and
[validation](docs/validation.md). CI runs tests, browser checks, secret scanning and release
archive checks without production credentials. It does not publish releases automatically.

Scout's original code is licensed under [MIT](LICENSE). Regional definitions come from
[facebook-marketplace-nationwide](https://github.com/gmoz22/facebook-marketplace-nationwide);
its [MIT notice](docs/nationwide-LICENSE.txt) is included in source and wheel distributions.
Dependencies keep their own licenses. Facebook access remains subject to its terms;
[Meta states](https://about.fb.com/news/2021/04/how-we-combat-scraping/) that collecting data
with automation without its permission violates those terms.
