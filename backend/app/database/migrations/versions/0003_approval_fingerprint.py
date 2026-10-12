"""approvals record the exact action they were given for

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-10 22:30:00.000000

Nullable: approvals stored before this migration keep working (they have no fingerprint).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.add_column(sa.Column("fingerprint", sa.String(length=160), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.drop_column("fingerprint")
