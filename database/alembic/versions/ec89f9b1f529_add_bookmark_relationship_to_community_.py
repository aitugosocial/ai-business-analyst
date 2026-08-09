"""add bookmark relationship to community discusion

Revision ID: ec89f9b1f529
Revises: 5f0460f10984
Create Date: 2026-08-09 21:21:27.051018

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ec89f9b1f529'
down_revision: Union[str, Sequence[str], None] = '5f0460f10984'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - relationship addition is pure Python ORM, no DDL required."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
