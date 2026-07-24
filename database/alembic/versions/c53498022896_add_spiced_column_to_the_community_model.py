"""add spiced column to the community model

Revision ID: c53498022896
Revises: a3b93c4aab25
Create Date: 2026-07-24 09:47:38.806894

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c53498022896'
down_revision: Union[str, Sequence[str], None] = 'a3b93c4aab25'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'community_discussions',
        sa.Column('spice_count', sa.Integer(), nullable=True)
    )
    op.add_column(
        'community_discussions',
        sa.Column('quoted_discussion_id', sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        'fk_community_discussions_quoted_discussion_id',
        'community_discussions',
        'community_discussions',
        ['quoted_discussion_id'],
        ['id'],
        ondelete='SET NULL'
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        'fk_community_discussions_quoted_discussion_id',
        'community_discussions',
        type_='foreignkey'
    )
    op.drop_column('community_discussions', 'quoted_discussion_id')
    op.drop_column('community_discussions', 'spice_count')