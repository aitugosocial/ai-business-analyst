"""add signals model

Revision ID: 0305ed083049
Revises: add_parent_reply_id_001
Create Date: 2026-07-06 00:08:31.058971

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0305ed083049'
down_revision: Union[str, Sequence[str], None] = 'add_parent_reply_id_001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- Signal (blog) tables ---
    op.create_table('signals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('author_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('slug', sa.String(length=300), nullable=False),
    sa.Column('excerpt', sa.Text(), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('cover_image_url', sa.String(length=500), nullable=True),
    sa.Column('category', sa.String(length=100), nullable=True),
    sa.Column('tags', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='draft', nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('read_time', sa.String(length=20), nullable=True),
    sa.Column('is_featured', sa.Boolean(), nullable=True),
    sa.Column('is_pinned', sa.Boolean(), nullable=True),
    sa.Column('view_count', sa.Integer(), nullable=True),
    sa.Column('like_count', sa.Integer(), nullable=True),
    sa.Column('comment_count', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_signals_author', 'signals', ['author_id'], unique=False)
    op.create_index('idx_signals_status_published', 'signals', ['status', 'published_at'], unique=False)
    op.create_index(op.f('ix_signals_author_id'), 'signals', ['author_id'], unique=False)
    op.create_index(op.f('ix_signals_category'), 'signals', ['category'], unique=False)
    op.create_index(op.f('ix_signals_id'), 'signals', ['id'], unique=False)
    op.create_index(op.f('ix_signals_slug'), 'signals', ['slug'], unique=True)

    op.create_table('signal_comments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('signal_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('parent_comment_id', sa.Integer(), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('chops_awarded', sa.Integer(), nullable=True),
    sa.Column('is_edited', sa.Boolean(), nullable=True),
    sa.Column('edited_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_deleted', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['parent_comment_id'], ['signal_comments.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['signal_id'], ['signals.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_signal_comments_signal', 'signal_comments', ['signal_id'], unique=False)
    op.create_index('idx_signal_comments_user', 'signal_comments', ['user_id'], unique=False)
    op.create_index(op.f('ix_signal_comments_id'), 'signal_comments', ['id'], unique=False)
    op.create_index(op.f('ix_signal_comments_is_deleted'), 'signal_comments', ['is_deleted'], unique=False)
    op.create_index(op.f('ix_signal_comments_signal_id'), 'signal_comments', ['signal_id'], unique=False)
    op.create_index(op.f('ix_signal_comments_user_id'), 'signal_comments', ['user_id'], unique=False)

    op.create_table('signal_likes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('signal_id', sa.Integer(), nullable=False),
    sa.Column('chops_awarded', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['signal_id'], ['signals.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_signal_likes_user_signal', 'signal_likes', ['user_id', 'signal_id'], unique=True)
    op.create_index(op.f('ix_signal_likes_id'), 'signal_likes', ['id'], unique=False)

    # --- New chop-tracking columns on users ---
    op.add_column('users', sa.Column('signal_like_chops', sa.Integer(), nullable=True))
    op.add_column('users', sa.Column('signal_comment_chops', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'signal_comment_chops')
    op.drop_column('users', 'signal_like_chops')

    op.drop_index(op.f('ix_signal_likes_id'), table_name='signal_likes')
    op.drop_index('idx_signal_likes_user_signal', table_name='signal_likes')
    op.drop_table('signal_likes')

    op.drop_index(op.f('ix_signal_comments_user_id'), table_name='signal_comments')
    op.drop_index(op.f('ix_signal_comments_signal_id'), table_name='signal_comments')
    op.drop_index(op.f('ix_signal_comments_is_deleted'), table_name='signal_comments')
    op.drop_index(op.f('ix_signal_comments_id'), table_name='signal_comments')
    op.drop_index('idx_signal_comments_user', table_name='signal_comments')
    op.drop_index('idx_signal_comments_signal', table_name='signal_comments')
    op.drop_table('signal_comments')

    op.drop_index(op.f('ix_signals_slug'), table_name='signals')
    op.drop_index(op.f('ix_signals_id'), table_name='signals')
    op.drop_index(op.f('ix_signals_category'), table_name='signals')
    op.drop_index(op.f('ix_signals_author_id'), table_name='signals')
    op.drop_index('idx_signals_status_published', table_name='signals')
    op.drop_index('idx_signals_author', table_name='signals')
    op.drop_table('signals')