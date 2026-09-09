# Scout

Scout is a self-hosted Facebook Marketplace explorer with saved searches, scheduled
watches and optional Telegram alerts. Search regional samples in the USA, Canada and
mainland France; browse photos, filter titles/prices, and turn a search into a watch.

Scout is an early-stage project. Facebook can change its layout, limit results, ignore
location filters or require a login checkpoint. Country tabs identify the search area,
not a verified seller location. Alerts report newly discovered listings, not guaranteed
publication times. See [architecture and limitations](docs/architecture.md).

## Requirements

- Linux with Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).
- Chromium installed through Playwright (commands below).
- A Facebook session with access to Marketplace and permission for the intended collection.
- Optional: your own Telegram bot token and destination.

Linux is the supported platform. Worker locking, resource limits and the optional headless
login viewer use Unix/Linux facilities; Windows and macOS are not currently validated.

## Quick start

From a checkout or extracted source release:

```bash
cd Scout
uv sync --frozen
cp .env.example .env
PLAYWRIGHT_BROWSERS_PATH="$PWD/data/browsers" uv run playwright install --with-deps chromium
uv run scout login
uv run scout serve
```

The Playwright system-dependency step may require administrator access. On a headless
server, use the [remote login instructions](docs/deployment.md#headless-facebook-login).
Complete Facebook checkpoints yourself; Scout does not bypass them. Stop a running
worker before opening the login browser.

Open [the local dashboard](http://127.0.0.1:8765). In another terminal, run `uv run scout token`
and enter that access token. It stays in browser-tab memory. All `/api/` routes require it.
Runtime data, tokens and browser credentials are stored in private `data/` by default.

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
