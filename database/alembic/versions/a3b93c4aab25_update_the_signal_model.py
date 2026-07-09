"""update the signal model

Revision ID: a3b93c4aab25
Revises: cc1698ad8fa6
Create Date: 2026-07-09 10:36:33.838252

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3b93c4aab25'
down_revision: Union[str, Sequence[str], None] = 'cc1698ad8fa6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # cover_image_data was already added to signals by migration cc1698ad8fa6.
    # This migration is intentionally a no-op to advance the revision pointer.
    pass


def downgrade() -> None:
    pass