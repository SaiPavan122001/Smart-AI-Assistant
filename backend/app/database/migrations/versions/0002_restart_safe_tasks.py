"""restart-safe tasks: durable runner state, interruption record, action fingerprints

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10 10:00:00.000000

Adds only nullable columns (and one boolean with a server default), so existing rows stay valid:
a task stored before this migration has no durable state and is offered restart/cancel, not resume.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("idempotency_key", sa.String(length=128), nullable=True))
        batch_op.add_column(
            sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch_op.add_column(sa.Column("durable", _JSON, nullable=True))
        batch_op.add_column(sa.Column("interruption", _JSON, nullable=True))
        batch_op.add_column(sa.Column("restarted_from", sa.String(length=64), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_tasks_idempotency_key"), ["idempotency_key"], unique=False
        )

    with op.batch_alter_table("plans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("goal", _JSON, nullable=True))
        batch_op.add_column(sa.Column("answer_template", sa.Text(), nullable=True))

    with op.batch_alter_table("tool_calls", schema=None) as batch_op:
        batch_op.add_column(sa.Column("fingerprint", sa.String(length=160), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tool_calls", schema=None) as batch_op:
        batch_op.drop_column("fingerprint")

    with op.batch_alter_table("plans", schema=None) as batch_op:
        batch_op.drop_column("answer_template")
        batch_op.drop_column("goal")

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tasks_idempotency_key"))
        batch_op.drop_column("restarted_from")
        batch_op.drop_column("interruption")
        batch_op.drop_column("durable")
        batch_op.drop_column("cancel_requested")
        batch_op.drop_column("idempotency_key")
