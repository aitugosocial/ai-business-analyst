"""Merge split DB heads: 4a015b12472c and add_parent_reply_id_001

When add_parent_reply_id_001 was first applied with down_revision='80725524ba7b'
(a mid-chain branchpoint) it branched off the existing chain without consuming
4a015b12472c, leaving two rows in alembic_version simultaneously.
This migration declares both as parents so a single upgrade head call can
resolve the DB to one current revision.

Revision ID: merge_split_db_heads_001
Revises: 4a015b12472c, add_parent_reply_id_001
Create Date: 2026-06-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'merge_split_db_heads_001'
down_revision: Union[str, Sequence[str], None] = ('4a015b12472c', 'add_parent_reply_id_001')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
