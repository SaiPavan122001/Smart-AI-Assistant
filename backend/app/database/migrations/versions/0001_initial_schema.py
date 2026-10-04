"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-04 08:41:16.299159
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "client_meta", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sessions_user_id"), ["user_id"], unique=False)

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_step_id", sa.String(length=64), nullable=True),
        sa.Column("current_tool", sa.String(length=100), nullable=True),
        sa.Column("current_application", sa.String(length=300), nullable=True),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column(
            "usage", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column(
            "result", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
        ),
        sa.Column("error", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.create_index("ix_tasks_created_at", ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_tasks_request_id"), ["request_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tasks_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_tasks_user_id"), ["user_id"], unique=False)

    op.create_table(
        "agent_actions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("step_key", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column(
            "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("agent_actions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_agent_actions_task_id"), ["task_id"], unique=False)

    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("step_key", sa.String(length=64), nullable=True),
        sa.Column("tool_call_id", sa.String(length=64), nullable=False),
        sa.Column("tool", sa.String(length=100), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("risk", sa.Integer(), nullable=False),
        sa.Column(
            "risk_explanation",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "arguments", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column(
            "allowed_scopes",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("tainted", sa.Boolean(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("scope_granted", sa.String(length=16), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_approvals_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_approvals_task_id"), ["task_id"], unique=False)

    op.create_table(
        "execution_logs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("level", sa.String(length=12), nullable=False),
        sa.Column("component", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column(
            "correlation", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("execution_logs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_execution_logs_task_id"), ["task_id"], unique=False)

    op.create_table(
        "observations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("step_key", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("untrusted", sa.Boolean(), nullable=False),
        sa.Column("approx_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("observations", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_observations_task_id"), ["task_id"], unique=False)

    op.create_table(
        "permissions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("tool", sa.String(length=100), nullable=True),
        sa.Column("risk", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("via", sa.String(length=32), nullable=False),
        sa.Column("data", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("permissions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_permissions_kind"), ["kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_permissions_task_id"), ["task_id"], unique=False)

    op.create_table(
        "plans",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "assumptions", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column(
            "success_criteria",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("plans", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_plans_task_id"), ["task_id"], unique=False)

    op.create_table(
        "task_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column(
            "correlation", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "seq", name="uq_task_events_task_seq"),
    )
    with op.batch_alter_table("task_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_task_events_task_id"), ["task_id"], unique=False)

    op.create_table(
        "tool_calls",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("step_key", sa.String(length=64), nullable=True),
        sa.Column("tool", sa.String(length=100), nullable=False),
        sa.Column("args", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "assessment", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
        ),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column(
            "result", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("tool_calls", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tool_calls_task_id"), ["task_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tool_calls_tool"), ["tool"], unique=False)

    op.create_table(
        "verification_results",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("step_key", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "evidence", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("method", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("verification_results", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_verification_results_task_id"), ["task_id"], unique=False
        )

    op.create_table(
        "task_steps",
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("step_key", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("tool", sa.String(length=100), nullable=True),
        sa.Column("args", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column(
            "allowed_tools",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "depends_on", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "verify", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("optional", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("error", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True),
        sa.Column(
            "output", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "step_key", name="uq_task_steps_plan_key"),
    )
    with op.batch_alter_table("task_steps", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_task_steps_plan_id"), ["plan_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_task_steps_task_id"), ["task_id"], unique=False)


def downgrade() -> None:

    with op.batch_alter_table("task_steps", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_task_steps_task_id"))
        batch_op.drop_index(batch_op.f("ix_task_steps_plan_id"))

    op.drop_table("task_steps")
    with op.batch_alter_table("verification_results", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_verification_results_task_id"))

    op.drop_table("verification_results")
    with op.batch_alter_table("tool_calls", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tool_calls_tool"))
        batch_op.drop_index(batch_op.f("ix_tool_calls_task_id"))

    op.drop_table("tool_calls")
    with op.batch_alter_table("task_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_task_events_task_id"))

    op.drop_table("task_events")
    with op.batch_alter_table("plans", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_plans_task_id"))

    op.drop_table("plans")
    with op.batch_alter_table("permissions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_permissions_task_id"))
        batch_op.drop_index(batch_op.f("ix_permissions_kind"))

    op.drop_table("permissions")
    with op.batch_alter_table("observations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_observations_task_id"))

    op.drop_table("observations")
    with op.batch_alter_table("execution_logs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_execution_logs_task_id"))

    op.drop_table("execution_logs")
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_approvals_task_id"))
        batch_op.drop_index(batch_op.f("ix_approvals_status"))

    op.drop_table("approvals")
    with op.batch_alter_table("agent_actions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_agent_actions_task_id"))

    op.drop_table("agent_actions")
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tasks_user_id"))
        batch_op.drop_index(batch_op.f("ix_tasks_status"))
        batch_op.drop_index(batch_op.f("ix_tasks_request_id"))
        batch_op.drop_index("ix_tasks_created_at")

    op.drop_table("tasks")
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sessions_user_id"))

    op.drop_table("sessions")
    op.drop_table("users")
