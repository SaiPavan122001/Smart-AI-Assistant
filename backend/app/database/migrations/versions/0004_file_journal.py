"""file journal: every change a task makes to files, written before the change, for undo/rollback

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-12 10:00:00.000000

A new table only; existing rows are untouched. Tasks stored before this migration have no journal
and cannot be rolled back.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "file_journal",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("op", sa.String(length=24), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("src", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.String(length=100), nullable=True),
        sa.Column("backup", sa.Text(), nullable=True),
        sa.Column("backup_fingerprint", sa.String(length=100), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "seq", name="uq_file_journal_task_seq"),
    )
    with op.batch_alter_table("file_journal", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_file_journal_task_id"), ["task_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("file_journal", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_file_journal_task_id"))
    op.drop_table("file_journal")
