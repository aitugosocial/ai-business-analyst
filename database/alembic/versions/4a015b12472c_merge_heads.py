"""merge heads

Revision ID: 4a015b12472c
Revises: b3d9f2a47c81, cf2b1944f856
Create Date: 2026-06-15 15:16:39.944913

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4a015b12472c'
down_revision: Union[str, Sequence[str], None] = ('b3d9f2a47c81', 'cf2b1944f856')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
