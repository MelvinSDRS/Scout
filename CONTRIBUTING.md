# Contributing

Scout is licensed under MIT. Contributions are submitted under that license. Preserve the
separate upstream MIT notice for the regional definitions. Use synthetic examples and data
that you have permission to contribute; never include live credentials or private listings.

## Local checks (Linux)

```bash
uv sync --frozen
PLAYWRIGHT_BROWSERS_PATH="$PWD/data/browsers" uv run playwright install --with-deps chromium
uv run ruff check src tests tools
uv run ruff format --check src tests tools
node --check src/scout/app.js
uv run pytest -q
uv run python tools/browser_smoke.py
uv run python tools/check_secrets.py
bash tools/check_history.sh
uv build --out-dir dist
uv run python tools/check_release.py dist
```

Browser tests use isolated local HTML and synthetic records, not live Facebook searches.
Some require Chromium and local sockets; install Chromium first. No Telegram credentials
are needed, and test notifications are intercepted. The browser smoke test uses port 8766;
production defaults to 8765. Browser tests must not be silently skipped in CI.

Keep changes focused and describe the problem, resulting behavior and relevant validation.
Include regression coverage for persistence, scheduling, collection or API contract changes.
Document configuration changes and preserve existing saved state during migrations.
Avoid adding tests that simply repeat presentation text or implementation structure.

## Release review

CI runs on Linux and checks the source, fixtures, browser workflow and build archives. It
uses read-only repository permissions and does not publish artifacts automatically.
Before a public release, review the source archive contents and the license notices, run
an installation check from the built wheel, and confirm private vulnerability reporting is
enabled. Audit the full Git history separately; the normal scanner checks current source.

Private runtime files are excluded by an explicit source-distribution allowlist and by
`.gitignore`. Both wheel and source archives are checked for credentials and private paths.
Do not add blanket secret-scan exclusions or accept a generated baseline without review.
False-positive fixture exceptions must be narrow and explain why the value is synthetic.

The repository is [MelvinSDRS/Scout](https://github.com/MelvinSDRS/Scout).
Verify any registry package name before publishing there; installing from this source checkout
or its wheel does not reserve a public package name.
