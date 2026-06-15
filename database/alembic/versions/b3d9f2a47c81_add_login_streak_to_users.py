"""Add login_streak and login_streak_date columns to users table

Revision ID: b3d9f2a47c81
Revises: a1c3e5f7b9d2
Create Date: 2026-06-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b3d9f2a47c81'
down_revision: Union[str, Sequence[str], None] = 'a1c3e5f7b9d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    existing_columns = {col['name'] for col in inspector.get_columns('users')}

    if 'login_streak' not in existing_columns:
        op.add_column(
            'users',
            sa.Column('login_streak', sa.Integer(), nullable=False, server_default='0'),
        )

    if 'login_streak_date' not in existing_columns:
        op.add_column(
            'users',
            sa.Column('login_streak_date', sa.Date(), nullable=True),
        )


def downgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    existing_columns = {col['name'] for col in inspector.get_columns('users')}

    if 'login_streak_date' in existing_columns:
        op.drop_column('users', 'login_streak_date')

    if 'login_streak' in existing_columns:
        op.drop_column('users', 'login_streak')
