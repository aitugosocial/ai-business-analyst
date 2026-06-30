#!/bin/bash
set -e

# If the DB's alembic_version table has multiple rows (e.g. stale parent revision
# left alongside the actual current head), alembic upgrade head fails with an
# "overlaps" error. Detect this and stamp the single file-level head before upgrading.
HEADS_IN_DB=$(alembic current 2>/dev/null | grep -c "(head)" || true)

if [ "$HEADS_IN_DB" -gt 1 ]; then
    FILE_HEAD=$(alembic heads 2>/dev/null | grep "(head)" | awk '{print $1}')
    echo "Multiple heads in alembic_version ($HEADS_IN_DB). Stamping to $FILE_HEAD..."
    alembic stamp "$FILE_HEAD"
fi

alembic upgrade head
exec uvicorn api.main:app --host 0.0.0.0 --port "${PORT:-8000}"
