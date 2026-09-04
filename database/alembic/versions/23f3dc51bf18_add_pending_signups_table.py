"""Add pending_signups table

Revision ID: 23f3dc51bf18
Revises: 2c487b4bb852
Create Date: 2026-09-10 00:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '23f3dc51bf18'
down_revision: Union[str, Sequence[str], None] = '2c487b4bb852'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'pending_signups' not in tables:
        op.create_table(
            'pending_signups',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('email', sa.String(), nullable=False),
            sa.Column('name', sa.String(), nullable=False),
            sa.Column('password_hash', sa.String(), nullable=False),
            sa.Column('confirm_password_hash', sa.String(), nullable=False),
            sa.Column('company_name', sa.String(), nullable=True),
            sa.Column('referrer_code', sa.String(), nullable=True),
            sa.Column('verification_code', sa.String(length=6), nullable=False),
            sa.Column('code_expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('attempts', sa.Integer(), server_default='0'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        )
        op.create_index(op.f('ix_pending_signups_email'), 'pending_signups', ['email'], unique=True)
    else:
        existing_indexes = [idx['name'] for idx in inspector.get_indexes('pending_signups')]
        if 'ix_pending_signups_email' not in existing_indexes:
            try:
                op.create_index(op.f('ix_pending_signups_email'), 'pending_signups', ['email'], unique=True)
            except Exception:
                pass


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    if 'pending_signups' in tables:
        existing_indexes = [idx['name'] for idx in inspector.get_indexes('pending_signups')]
        if 'ix_pending_signups_email' in existing_indexes:
            op.drop_index(op.f('ix_pending_signups_email'), table_name='pending_signups')
        op.drop_table('pending_signups')
