#!/usr/bin/env bash
set -euo pipefail

# cd to repo root (works regardless of where you run it from)
cd -- "$(dirname "${BASH_SOURCE[0]}")/.."

# Where to write the dump (init scripts run automatically on first boot)
OUT="db/init/01_schema.sql"

# Ensure the output dir exists
mkdir -p "$(dirname "$OUT")"

# Optional flags to make the dump init-friendly
DUMP_FLAGS=(--schema-only --no-owner --no-privileges)

# If you want data too, uncomment:
# DUMP_FLAGS=(--schema-only --no-owner --no-privileges)  # schema only
# DUMP_FLAGS=(--data-only --inserts)                     # data only (example)

echo "→ Dumping schema from docker 'db' container to $OUT ..."
# Use env inside the container; redirect happens on host
docker compose exec db sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" '"${DUMP_FLAGS[*]}" \
  > "$OUT"

echo "✓ Wrote $OUT"