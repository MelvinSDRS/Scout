#!/usr/bin/env bash
# Deploy application source to the mounted Scout runtime without rebuilding.
set -euo pipefail

cd "$(dirname "$0")/.."
source_dir="$(pwd -P)/.deploy/src"
mkdir -p "$source_dir"
rsync -a --delete --delay-updates \
  --exclude='__pycache__/' --exclude='*.pyc' \
  src/ "$source_dir/"

container_id="$(docker compose ps -q scout)"
mounted_source=""
if [[ -n "$container_id" ]]; then
  mounted_source="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/opt/scout/src"}}{{.Source}}{{end}}{{end}}' "$container_id")"
fi

if [[ "$mounted_source" == "$source_dir" ]]; then
  docker compose restart scout
else
  docker compose up -d --no-deps scout
fi

container_id="$(docker compose ps -q scout)"
actual_source="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/opt/scout/src"}}{{.Source}}{{end}}{{end}}' "$container_id")"
if [[ "$actual_source" != "$source_dir" ]]; then
  echo "Scout source mount is missing or points elsewhere: $actual_source" >&2
  exit 1
fi

for ((attempt = 0; attempt < 45; attempt++)); do
  health="$(docker inspect --format '{{.State.Health.Status}}' "$container_id")"
  if [[ "$health" == "healthy" ]]; then
    echo "Scout is healthy and serving source from $source_dir"
    exit 0
  fi
  if [[ "$health" == "unhealthy" ]]; then
    echo "Scout failed its health check" >&2
    exit 1
  fi
  sleep 2
done
echo "Scout did not become healthy within 90 seconds" >&2
exit 1
