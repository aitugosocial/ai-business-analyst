"""add voo to the community model

Revision ID: 94956a86226b
Revises: a9b8c7d6e5f4
Create Date: 2026-08-25 12:42:26.883193

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '94956a86226b'
down_revision: Union[str, Sequence[str], None] = 'a9b8c7d6e5f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add is_bot to users table
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_bot BOOLEAN DEFAULT FALSE;")

    # 2. Add Voo tracking columns to community_discussions
    op.execute("ALTER TABLE community_discussions ADD COLUMN IF NOT EXISTS voo_status VARCHAR(30) DEFAULT 'untracked';")
    op.execute("ALTER TABLE community_discussions ADD COLUMN IF NOT EXISTS voo_scheduled_for TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE community_discussions ADD COLUMN IF NOT EXISTS voo_reply_id INTEGER;")
    op.execute("ALTER TABLE community_discussions ADD COLUMN IF NOT EXISTS is_resolved BOOLEAN DEFAULT FALSE;")

    # 3. Add foreign key constraint safely if not exists
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_community_discussions_voo_reply_id'
            ) THEN
                ALTER TABLE community_discussions
                ADD CONSTRAINT fk_community_discussions_voo_reply_id
                FOREIGN KEY (voo_reply_id) REFERENCES discussion_replies(id)
                ON DELETE SET NULL;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE community_discussions DROP CONSTRAINT IF EXISTS fk_community_discussions_voo_reply_id;")
    op.execute("ALTER TABLE community_discussions DROP COLUMN IF EXISTS is_resolved;")
    op.execute("ALTER TABLE community_discussions DROP COLUMN IF EXISTS voo_reply_id;")
    op.execute("ALTER TABLE community_discussions DROP COLUMN IF EXISTS voo_scheduled_for;")
    op.execute("ALTER TABLE community_discussions DROP COLUMN IF EXISTS voo_status;")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_bot;")
