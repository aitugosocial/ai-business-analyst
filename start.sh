#!/bin/bash
set -e

# The DB's alembic_version table can accumulate multiple rows (stale parent
# revision alongside the actual current head) if a prior deploy partially
# succeeded. alembic upgrade head rejects this with an "overlaps" error.
# Fix: delete any rows that are NOT a file-level head before upgrading.
python3 - <<'PYEOF'
import os, sys
try:
    import sqlalchemy as sa
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    file_heads = set(script.get_heads())

    engine = sa.create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchall()
        db_versions = [r[0] for r in rows]
        if len(db_versions) > 1:
            stale = [v for v in db_versions if v not in file_heads]
            if stale:
                print(f"Removing stale alembic versions: {stale}", flush=True)
                for v in stale:
                    conn.execute(
                        sa.text("DELETE FROM alembic_version WHERE version_num = :v"),
                        {"v": v},
                    )
                conn.commit()
except Exception as e:
    print(f"Warning (alembic cleanup): {e}", file=sys.stderr, flush=True)
PYEOF

alembic upgrade head
exec uvicorn api.main:app --host 0.0.0.0 --port "${PORT:-8000}"
