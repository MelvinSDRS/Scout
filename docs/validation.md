# Validation and remaining limits

The project has a local regression suite for matching, currency handling, API authentication
and origins, persistence, deduplication, scheduling, cancellation, transactional watch
conversion, login lifecycle and photo review. Real-browser tests use synthetic Facebook
responses and page markup. They do not establish live Facebook permission or completeness.

Run the commands in [CONTRIBUTING.md](../CONTRIBUTING.md). CI installs Chromium explicitly,
runs the browser tests and dashboard smoke workflow, scans publishable source for secrets,
and checks source/wheel contents and license metadata.

Operational notes, destination IDs, private listing examples and deployment-specific logs
are intentionally kept outside the public source. A healthy worker or successful test suite
does not prove upstream availability, exhaustive coverage or guaranteed notification latency.
The local source scan does not replace a complete repository-history audit.

## Publication preparation

The MIT license covers Scout's original code, and the upstream regional-data MIT notice is
packaged with both release formats. Configuration and service installation use operator
paths. Photo profiles require operator-supplied valid references; unavailable profiles cannot
be selected for new watches. The existing runtime can retain its own private configuration,
reference images and database while the source becomes portable.

Hosted CI runs in [MelvinSDRS/Scout](https://github.com/MelvinSDRS/Scout). Repository visibility,
private-reporting configuration and registry publication are separate administrative actions.

## Local publication-preparation checks — 2026-09-08

The existing regression suite and added cases cover opt-in shared configuration, configurable
reference folders, missing/invalid profiles, unavailable saved-search conversion, reference
cache invalidation, actual photo subprocess selection and release-notice/private-file guards.
The isolated Chromium workflow verifies both available and unavailable photo-profile UI states,
search, pagination, watch conversion and manual photo review without external messages.

Wheel and source archives build successfully, include both MIT notices and pass the release
content/credential scan. A separate environment with only locked runtime dependencies installs
the wheel and passes CLI, packaged-resource, API authentication and empty-state health checks.
The portable user-service unit passes systemd validation. Local source lint/format, JavaScript
syntax, workflow YAML and the current-file secret scan pass. Secret verification over the
network is disabled; one documented exception covers synthetic env fixture field names.

## Hosted validation — 2026-09-09

[CI run 34309483861](https://github.com/MelvinSDRS/Scout/actions/runs/34309483861) passed on
Ubuntu 24.04 for initial commit `8ec74d233faa2da3e846027c7d4c67f9af9930bd`. It verified the
locked dependency installation, Chromium/browser checks, lint/format, JavaScript syntax,
regression tests, source secret scan, full-history Gitleaks scan, wheel/source builds, release
archive contents and isolated wheel installation/API checks.

There was no preceding Git repository in the workspace. The new repository begins with the
reviewed source snapshot; private runtime data and archived operator notes were never committed.
Gitleaks 8.30.1 scanned the full initial history with no findings. Git object integrity checks
also passed. CI now fetches full history and runs the checksum-verified history scanner on
subsequent changes. This is evidence of the scanner results, not a guarantee that all possible
secrets or vulnerabilities have been ruled out.
