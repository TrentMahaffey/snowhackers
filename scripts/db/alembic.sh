#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname "${BASH_SOURCE[0]}")/.."

# Location of Alembic config (we'll place it at db/alembic.ini)
export ALEMBIC_CONFIG=${ALEMBIC_CONFIG:-db/alembic.ini}

# Optional custom rev id seed (useful in CI). Example: ALEMBIC_REV_ID=20250926
if [[ ${ALEMBIC_REV_ID:-} ]]; then
  export REVISION_ENV=AUTO
  # Alembic reads REVISION_ID via env var only if using script.py.mako template; we'll pass via -x
  XOPTS=(-x revision_id="$ALEMBIC_REV_ID")
else
  XOPTS=()
fi

MSG="${1a:-change}"

# Create a new (empty) migration. Use `alembic revision --autogenerate` once models/metadata are wired.
alembic -c "$ALEMBIC_CONFIG" revision -m "$MSG" "${XOPTS[@]}"

echo "Created new Alembic revision with message: $MSG"