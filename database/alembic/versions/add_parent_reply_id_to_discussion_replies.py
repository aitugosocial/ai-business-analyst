"""Add parent_reply_id to discussion_replies for threaded replies

Revision ID: add_parent_reply_id_001
Revises: add_community_marketplace_001
Create Date: 2026-06-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'add_parent_reply_id_001'
down_revision: Union[str, Sequence[str], None] = '80725524ba7b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'discussion_replies',
        sa.Column('parent_reply_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'fk_discussion_replies_parent_reply_id',
        'discussion_replies',
        'discussion_replies',
        ['parent_reply_id'],
        ['id'],
        ondelete='CASCADE',
    )


def downgrade() -> None:
    op.drop_constraint('fk_discussion_replies_parent_reply_id', 'discussion_replies', type_='foreignkey')
    op.drop_column('discussion_replies', 'parent_reply_id')
