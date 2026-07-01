#!/bin/bash
set -e

# Remove the stale 4a015b12472c row from alembic_version.
# If both 4a015b12472c and its child add_parent_reply_id_001 are present,
# alembic upgrade head fails with an "overlaps" error. Deleting the parent
# row leaves only the head row so upgrade head becomes a no-op.
echo "[startup] Removing stale alembic_version row (if present)..."
psql "${DATABASE_URL}" -c "DELETE FROM alembic_version WHERE version_num = '4a015b12472c';" 2>&1 || true

echo "[startup] Running alembic upgrade head..."
alembic upgrade head

echo "[startup] Starting server..."
exec uvicorn api.main:app --host 0.0.0.0 --port "${PORT:-8000}"
