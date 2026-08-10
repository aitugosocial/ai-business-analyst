"""add unique constraint to poll votes

Revision ID: a9b8c7d6e5f4
Revises: f9a0b1c2d3e4
Create Date: 2026-08-10 02:21:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9b8c7d6e5f4'
down_revision: Union[str, None] = 'f9a0b1c2d3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    constraints = [c['name'] for c in inspector.get_unique_constraints('discussion_poll_votes')]
    if 'uq_discussion_user_vote' not in constraints:
        op.create_unique_constraint('uq_discussion_user_vote', 'discussion_poll_votes', ['discussion_id', 'user_id'])


def downgrade() -> None:
    op.drop_constraint('uq_discussion_user_vote', 'discussion_poll_votes', type_='unique')
