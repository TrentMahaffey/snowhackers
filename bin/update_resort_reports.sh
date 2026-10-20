#!/usr/bin/env bash
# Fetch resort-published snow reports and store them in Postgres (resort_snow_reports).
# Fetch runs on the host: the Vail Resorts sites (Akamai) reject the runner container's
# TLS fingerprint. The runner then loads the JSON into the DB it can reach.
set -euo pipefail
IFS=$'\n\t'

if docker compose version >/dev/null 2>&1; then
  COMPOSE=("docker" "compose")
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=("docker-compose")
else
  echo "ERROR: Neither 'docker compose' nor 'docker-compose' found on PATH."
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOCK_DIR="${REPO_ROOT}/.update_resort_reports.lockdir"
LOG_DIR="${REPO_ROOT}/logs"
OUT_JSON="${LOG_DIR}/resort_reports_latest.json"
mkdir -p "$LOG_DIR"
TS() { date +"%Y-%m-%dT%H:%M:%S%z"; }

if mkdir "$LOCK_DIR" 2>/dev/null; then
  trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM
else
  echo "[$(TS)] another resort-report update is running, exiting"
  exit 0
fi

# Feed token lives in .env (gitignored; this repo is public)
export ALTERRA_FEED_TOKEN="$(grep -E "^ALTERRA_FEED_TOKEN=" .env | cut -d= -f2-)"
echo "==== [$(TS)] resort reports: fetch ===="
# Exit 2 = some sources failed but others produced rows; still load what we have.
set +e
python3 -u bin/fetch_resort_reports.py --json-out "$OUT_JSON"
rc=$?
set -e
if [[ $rc -ne 0 && $rc -ne 2 ]]; then
  echo "[$(TS)] fetch failed (exit $rc); nothing loaded"
  exit $rc
fi

echo "==== [$(TS)] resort reports: load ===="
"${COMPOSE[@]}" run --rm -T runner python -u /app/bin/fetch_resort_reports.py --load /app/logs/resort_reports_latest.json

echo "==== [$(TS)] resort reports: done (fetch exit $rc) ===="
exit $rc
