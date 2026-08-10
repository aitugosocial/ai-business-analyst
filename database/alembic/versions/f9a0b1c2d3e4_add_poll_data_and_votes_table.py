"""add poll_data and votes table

Revision ID: f9a0b1c2d3e4
Revises: ec89f9b1f529
Create Date: 2026-08-10 01:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f9a0b1c2d3e4'
down_revision: Union[str, None] = 'ec89f9b1f529'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add poll_data column if not exists
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('community_discussions')]
    if 'poll_data' not in columns:
        op.add_column('community_discussions', sa.Column('poll_data', sa.JSON(), nullable=True))

    tables = inspector.get_table_names()
    if 'discussion_poll_votes' not in tables:
        op.create_table(
            'discussion_poll_votes',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('discussion_id', sa.Integer(), sa.ForeignKey('community_discussions.id', ondelete='CASCADE'), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('option_index', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.PrimaryKeyConstraint('id')
        )


def downgrade() -> None:
    op.drop_table('discussion_poll_votes')
    op.drop_column('community_discussions', 'poll_data')
