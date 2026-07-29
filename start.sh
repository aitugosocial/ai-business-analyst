#!/bin/bash
set -e

# Guard against alembic_version pointing at a revision this codebase no
# longer has a file for (or holding more than one row / competing heads).
# This has broken deploys twice now under two different specific revision
# IDs (see git history for the first incident) — instead of hardcoding
# another one-off ID here, check generically: does every row in
# alembic_version correspond to an actual revision file we ship?
echo "[startup] Verifying alembic_version against known migration files..."
# Handles both styles present in this repo's migration files: the older
# untyped `revision = '...'` and the newer typed `revision: str = "..."`.
KNOWN_REVISIONS=$(grep -h "^revision" database/alembic/versions/*.py | sed -E "s/^revision(: str)? = ['\"]([^'\"]*)['\"].*/\2/")
DB_VERSIONS=$(psql "${DATABASE_URL}" -tAc "SELECT version_num FROM alembic_version;" 2>/dev/null || echo "")
ROW_COUNT=$(printf '%s\n' "${DB_VERSIONS}" | sed '/^$/d' | wc -l)

NEEDS_STAMP=0
if [ "${ROW_COUNT}" -gt 1 ]; then
  echo "[startup] alembic_version has ${ROW_COUNT} rows (competing heads) — will stamp to a single current head."
  NEEDS_STAMP=1
elif [ "${ROW_COUNT}" -eq 1 ]; then
  if ! printf '%s\n' "${KNOWN_REVISIONS}" | grep -qxF "${DB_VERSIONS}"; then
    echo "[startup] alembic_version points to unrecognized revision '${DB_VERSIONS}' (no matching file in this codebase) — will stamp to current head."
    NEEDS_STAMP=1
  fi
fi

if [ "${NEEDS_STAMP}" -eq 1 ]; then
  # Stamp (not upgrade-from-scratch): rewrites only the bookkeeping row to
  # the current head, running no SQL. Safe here because this codebase's
  # actual schema changes are applied defensively via
  # "ALTER TABLE ... ADD COLUMN IF NOT EXISTS" at app startup (api/main.py)
  # specifically because this alembic history has broken before — the live
  # schema is expected to already be current even when alembic's own
  # bookkeeping is confused. Deleting the row and letting a plain
  # `alembic upgrade head` replay the ENTIRE migration history instead would
  # risk erroring on early non-idempotent CREATE TABLE/ADD COLUMN statements
  # against an already-populated production database, or re-applying a data
  # backfill migration a second time.
  echo "[startup] Stamping alembic_version to head (schema assumed already current)..."
  alembic stamp head
fi

echo "[startup] Running alembic upgrade head..."
alembic upgrade head

echo "[startup] Starting server..."
exec uvicorn api.main:app --host 0.0.0.0 --port "${PORT:-8000}"
