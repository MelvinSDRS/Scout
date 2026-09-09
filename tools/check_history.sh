#!/usr/bin/env bash
# Audit all available Git refs with a pinned, checksum-verified Gitleaks binary.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$(git rev-parse --is-shallow-repository)" == true ]]; then
    printf '%s\n' 'History audit requires a full clone. Run git fetch --unshallow first.' >&2
    exit 1
fi
audit_tmp="$(mktemp -d -t scout-history.XXXXXXXX)"
trap 'rm -rf "$audit_tmp"' EXIT
# This distribution is for Linux x86_64 (the supported CI runner).
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
    printf '%s\n' 'Install Gitleaks 8.30.1 for your platform and run: gitleaks git --log-opts=--all --redact=100' >&2
    exit 1
fi
curl --fail --location --silent --show-error \
    https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz \
    --output "$audit_tmp/gitleaks.tar.gz"
# Published release checksum, not a credential.
gitleaks_sha256='551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb' # pragma: allowlist secret
printf '%s  %s\n' "$gitleaks_sha256" "$audit_tmp/gitleaks.tar.gz" | sha256sum --check --status
tar -xzf "$audit_tmp/gitleaks.tar.gz" -C "$audit_tmp" gitleaks
"$audit_tmp/gitleaks" git --log-opts=--all --redact=100 --no-banner .
