"""Add is_partner column to users table

Revision ID: a1c3e5f7b9d2
Revises: fbd856f8ccd8
Create Date: 2026-05-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1c3e5f7b9d2'
down_revision: Union[str, Sequence[str], None] = 'fbd856f8ccd8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    existing_columns = {col['name'] for col in inspector.get_columns('users')}

    if 'is_partner' not in existing_columns:
        op.add_column(
            'users',
            sa.Column('is_partner', sa.Boolean(), nullable=False, server_default='false'),
        )


def downgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    existing_columns = {col['name'] for col in inspector.get_columns('users')}

    if 'is_partner' in existing_columns:
        op.drop_column('users', 'is_partner')
