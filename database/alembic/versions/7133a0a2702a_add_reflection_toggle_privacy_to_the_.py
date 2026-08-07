"""add reflection toggle privacy to the user table

Revision ID: 7133a0a2702a
Revises: 2f01a534b941
Create Date: 2026-08-06 23:03:33.394854

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7133a0a2702a'
down_revision: Union[str, Sequence[str], None] = '2f01a534b941'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('share_reflections', sa.Boolean(), server_default='true', nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'share_reflections')
