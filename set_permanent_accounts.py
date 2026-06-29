"""
set_permanent_accounts.py
─────────────────────────
Grants permanent, unrestricted subscription access to specific admin/owner
email accounts.  Run once from the repo root:

    python set_permanent_accounts.py

What this script does
─────────────────────
USERS table (per account):
  • subscription_status  → 'active'       (bypasses ALL subscription guards)
  • subscription_plan    → 'yearly'       (most inclusive plan)
  • subscription_expires_at → 2099-12-31  (never expires in practice)
  • grace_period_ends_at    → 2099-12-31  (never triggers beta-expiry logic)
  • is_beta_user         → False          (disables the individual grace-expiry
                                           block in beta_service.get_user_status)

WHY this is sufficient
─────────────────────
beta_service.get_user_status() checks subscription_status == 'active' FIRST
(line ~181) and returns immediately with status='active', skipping every other
condition — grace period, beta mode, card checks, etc.

The frontend subscription guard reads betaStatus.status and user.subscription_status
from the Zustand store.  When both equal 'active', showModal is never set to true,
so no restriction modal ever fires.

SUBSCRIPTIONS table (per account):
  Creates one 'admin_permanent' record (idempotent — skips if already present).
  This satisfies any dashboard queries that look for subscription history.
"""

import os
import sys
import hashlib
from datetime import datetime, timezone

# If project dependencies (psycopg2, etc.) are missing, transparently re-exec
# this script using the repo's venv Python so it always works with `python3`.
def _ensure_venv():
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        _root = os.path.dirname(os.path.abspath(__file__))
        _venv_py = os.path.join(_root, "venv", "bin", "python")
        if os.path.exists(_venv_py) and os.path.abspath(sys.executable) != os.path.abspath(_venv_py):
            os.execv(_venv_py, [_venv_py] + sys.argv)
        sys.exit("❌  psycopg2 not found and no venv/bin/python available. Run: venv/bin/python set_permanent_accounts.py")

_ensure_venv()

# Load .env / .env.local before any project imports so DATABASE_URL is available
# regardless of whether this is run with system python or venv python.
def _load_env():
    _root = os.path.dirname(os.path.abspath(__file__))
    for _name in (".env.local", ".env"):
        _path = os.path.join(_root, _name)
        if not os.path.exists(_path):
            continue
        try:
            from dotenv import load_dotenv
            load_dotenv(_path, override=False)
        except ImportError:
            # dotenv not available — parse the file ourselves (simple k=v only)
            with open(_path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _, _v = _line.partition("=")
                    _k = _k.strip()
                    _v = _v.strip().strip("'\"")
                    os.environ.setdefault(_k, _v)
        break

_load_env()

from database.pg_connections import get_db
from database.pg_models import User, Subscriptions

# ── Permanent accounts ────────────────────────────────────────────────────────
PERMANENT_EMAILS = [
    "wealththecreator01@gmail.com",
    "clinton@gmail.com",
    "oparaudochukwu277@gmail.com",
    "tfred59@gmail.com",
]

# Date far enough in the future that it will never naturally expire
FOREVER = datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def make_tx_id(email: str) -> str:
    """Deterministic unique transaction reference based on email — idempotent."""
    digest = hashlib.sha256(email.encode()).hexdigest()[:16]
    return f"ADMIN_PERMANENT_{digest.upper()}"


def activate_account(db, email: str) -> None:
    user = db.query(User).filter(User.email == email).first()

    if not user:
        print(f"  ⚠️  User not found: {email}  (skipping)")
        return

    print(f"\n  📧  {email}  (id={user.id})")

    # ── 1. Update users table ──────────────────────────────────────────────────
    user.subscription_status = "active"
    user.subscription_plan   = "yearly"
    user.subscription_expires_at = FOREVER   # Never expires
    user.grace_period_ends_at    = FOREVER   # Grace expiry block never fires
    user.is_beta_user            = False     # Not a beta user → no beta checks

    print("     ✅  users table updated")
    print(f"         subscription_status  = 'active'")
    print(f"         subscription_plan    = 'yearly'")
    print(f"         subscription_expires_at = {FOREVER.date()}  (permanent)")
    print(f"         grace_period_ends_at    = {FOREVER.date()}  (permanent)")
    print(f"         is_beta_user            = False")

    # ── 2. Upsert subscriptions table ─────────────────────────────────────────
    tx_id = make_tx_id(email)
    existing = db.query(Subscriptions).filter(
        Subscriptions.transaction_id == tx_id
    ).first()

    if existing:
        # Already exists — just make sure it's still marked active
        existing.subscription_status = "active"
        existing.end_date = FOREVER
        print(f"     ✅  subscriptions record already exists — refreshed to active")
    else:
        now = datetime.now(timezone.utc)
        record = Subscriptions(
            user_id             = user.id,
            subscription_plan   = "yearly",
            transaction_id      = tx_id,
            tx_ref              = tx_id,          # same value — both unique cols
            amount              = 0.00,
            currency            = "USD",
            status              = "succeeded",    # original payment status column
            subscription_status = "active",       # lifecycle status column
            payment_provider    = "admin",
            start_date          = now,
            end_date            = FOREVER,
        )
        db.add(record)
        print(f"     ✅  subscriptions record created  (tx_id={tx_id})")


def main():
    db = next(get_db())
    try:
        print("=" * 60)
        print("  Lavoo — Permanent Account Activation")
        print("=" * 60)

        for email in PERMANENT_EMAILS:
            activate_account(db, email)

        db.commit()
        print("\n" + "=" * 60)
        print("  ✅  All changes committed successfully.")
        print("=" * 60)

    except Exception as exc:
        db.rollback()
        print(f"\n❌  Error — rolled back all changes: {exc}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
