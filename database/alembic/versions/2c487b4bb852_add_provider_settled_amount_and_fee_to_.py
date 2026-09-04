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
    op.add_column('payouts', sa.Column('provider_settled_amount', sa.Numeric(precision=10, scale=2), nullable=True))
    op.add_column('payouts', sa.Column('provider_fee', sa.Numeric(precision=10, scale=2), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('payouts', 'provider_fee')
    op.drop_column('payouts', 'provider_settled_amount')
