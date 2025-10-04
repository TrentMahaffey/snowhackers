#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

# --- choose docker compose command dynamically ---
if [[ -n "${COMPOSE:-}" ]]; then
  : # respect user-provided COMPOSE
elif docker compose version >/dev/null 2>&1; then
  COMPOSE=("docker" "compose")
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=("docker-compose")
else
  echo "ERROR: Neither 'docker compose' nor 'docker-compose' found on PATH."
  exit 1
fi

# --- config (override via env if you want) ---
DAYS_BACK="${DAYS_BACK:-14}"
MODELS="${MODELS:-gfs,icon,ecmwf}"
STATES="${STATES:-CO,UT,WY,ID,MT,WA,OR,CA,NV,NM,AZ}"
FORECAST_DAYS_AHEAD="${FORECAST_DAYS_AHEAD:-14}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOCK_DIR="${REPO_ROOT}/.update_snow.lockdir"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "$LOG_DIR"
TS() { date +"%Y-%m-%dT%H:%M:%S%z"; }
LOG_FILE="${LOG_DIR}/update_$(date +%Y%m%d_%H%M%S).log"

# acquire lock
if mkdir "$LOCK_DIR" 2>/dev/null; then
  trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM
else
  echo "[$(TS)] another update is running, exiting"
  exit 0
fi

echo "==== [$(TS)] starting update ====" | tee -a "$LOG_FILE"

run() {
  echo "[$(TS)] $*" | tee -a "$LOG_FILE"
  "$@" 2>&1 | tee -a "$LOG_FILE"
}

# pipeline
run "${COMPOSE[@]}" run --rm -T runner \
  python -u /app/main.py -v update-all \
  --days-back "$DAYS_BACK" \
  --models "$MODELS" \
  --states "$STATES" \
  --forecast-days-ahead "$FORECAST_DAYS_AHEAD"

run "${COMPOSE[@]}" run --rm -T runner \
  python -u /app/main.py init-views

echo "==== [$(TS)] update complete ====" | tee -a "$LOG_FILE"