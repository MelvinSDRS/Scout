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

GitHub-hosted CI execution, repository history review, private-reporting setup and registry
publication require an actual hosting repository. Adding a workflow file does not establish
that GitHub has run it. Publication itself is a separate action.

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

These are local results. GitHub-hosted execution and complete Git-history scanning have not
been verified because no accessible hosting repository/history is configured in this workspace.
