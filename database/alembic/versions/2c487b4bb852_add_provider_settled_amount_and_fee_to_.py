"""Add provider_settled_amount and provider_fee to payouts

Revision ID: 2c487b4bb852
Revises: 94956a86226b
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '2c487b4bb852'
down_revision: Union[str, Sequence[str], None] = '94956a86226b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # IF NOT EXISTS, not a plain ADD COLUMN: a prior deploy attempt already
    # applied this exact change directly against the DB (outside Alembic's
    # own bookkeeping getting a chance to record it), so a plain ADD COLUMN
    # here raises DuplicateColumn on the next `alembic upgrade head` and
    # crashes the container before uvicorn ever binds a port, failing the
    # Railway healthcheck. Idempotent, same pattern already used elsewhere
    # in this migration history (see cf2b1944f856).
    op.execute("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS provider_settled_amount NUMERIC(10, 2)")
    op.execute("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS provider_fee NUMERIC(10, 2)")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE payouts DROP COLUMN IF EXISTS provider_fee")
    op.execute("ALTER TABLE payouts DROP COLUMN IF EXISTS provider_settled_amount")
