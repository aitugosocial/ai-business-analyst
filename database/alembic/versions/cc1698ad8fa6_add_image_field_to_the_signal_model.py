"""add image field to the signal model

Revision ID: cc1698ad8fa6
Revises: 0305ed083049
Create Date: 2026-07-09 09:44:25.932825

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'cc1698ad8fa6'
down_revision: Union[str, Sequence[str], None] = '0305ed083049'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('signals', sa.Column('cover_image_data', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('signals', 'cover_image_data')