# Security policy

Scout is a single-operator, self-hosted application. Keep it on loopback or behind an
operator-managed HTTPS proxy with session/API authentication. Treat the runtime directory as
sensitive: it contains browser login state, access tokens and listing history.

Dashboard launchers issue five-minute, single-use sign-in links. The browser exchanges one
for a revocable 30-day HttpOnly, SameSite=Strict cookie (Secure over HTTPS). Only keyed hashes
of sign-in codes and sessions are stored in SQLite. Mutating cookie requests require a matching
Origin; bearer API authentication remains available. Changing the API secret invalidates
existing sessions and sign-in codes. Treat unused sign-in links and database backups as private.

## Reporting a vulnerability

Use **Security → Report a vulnerability** on the repository if private vulnerability
reporting is enabled. If that option is unavailable, open an issue containing only a request
for a private reporting channel; do not post exploit details, credentials or personal data.
This repository has no published dedicated security email address yet.

Include the affected version/commit, impact, reproduction with synthetic data and a suggested
fix if known. Do not test on another operator's installation or include Facebook/Telegram
credentials, cookies, live listings, screenshots or database copies. No response-time SLA
or bounty is offered. Fixes target the latest version; older versions have no support promise.

## Maintainer release procedure

Enable private vulnerability reporting on the hosting repository before public launch.
Review authentication, origin/host validation, local browser access, image download limits,
subprocess limits and accidental secret disclosure when these areas change.

Run the source scanner and archive checks described in CONTRIBUTING.md. These checks do not
prove absence of secrets or vulnerabilities. Before publishing an existing repository, audit
its complete Git history with a dedicated history scanner and rotate any exposed credentials.
Ignoring or deleting a sensitive file from the current tree does not remove it from history.
Use synthetic regression fixtures; never publish `.env`, runtime state, browser profiles,
image archives or personal deployment notes.
