"""SQLAlchemy ORM schema (PostgreSQL in production; SQLite supported for tests).

Normalised across several tables, each with timestamps, ids and correlation columns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False)


class UserRow(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = _ts()


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    client_meta: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = _ts()
    last_seen_at: Mapped[datetime] = _ts()


class TaskRow(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    instruction: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    current_step_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_tool: Mapped[str | None] = mapped_column(String(100), nullable=True)
    current_application: Mapped[str | None] = mapped_column(String(300), nullable=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    usage: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    # restart safety (migration 0002)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    durable: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    interruption: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    restarted_from: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (Index("ix_tasks_created_at", "created_at"),)


class PlanRow(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str] = mapped_column(Text, default="")
    assumptions: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    success_criteria: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    # needed to continue a task in a new process (migration 0002)
    goal: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    answer_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()


class TaskStepRow(Base):
    __tablename__ = "task_steps"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)  # "<plan_id>:<step_key>"
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    step_key: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16))
    tool: Mapped[str | None] = mapped_column(String(100), nullable=True)
    args: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    allowed_tools: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    depends_on: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    rationale: Mapped[str] = mapped_column(Text, default="")
    verify: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    optional: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    output: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("plan_id", "step_key", name="uq_task_steps_plan_key"),)


class AgentActionRow(Base):
    __tablename__ = "agent_actions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    step_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = _ts()


class ToolCallRow(Base):
    __tablename__ = "tool_calls"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    step_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool: Mapped[str] = mapped_column(String(100), index=True)
    args: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)  # redacted
    # hash of the tool and its original arguments: recognises an action after a restart (migration 0002)
    fingerprint: Mapped[str | None] = mapped_column(String(160), nullable=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    assessment: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    decision: Mapped[str] = mapped_column(String(24), default="")
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    result: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = _ts()


class FileJournalRow(Base):
    """One change a task made to a file or folder, and what is needed to undo it (migration 0004)."""

    __tablename__ = "file_journal"
    __table_args__ = (UniqueConstraint("task_id", "seq", name="uq_file_journal_task_seq"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seq: Mapped[int] = mapped_column(Integer)
    op: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(Text)
    src: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(100), nullable=True)
    backup: Mapped[str | None] = mapped_column(Text, nullable=True)
    backup_fingerprint: Mapped[str | None] = mapped_column(String(100), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class ObservationRow(Base):
    __tablename__ = "observations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    step_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    untrusted: Mapped[bool] = mapped_column(Boolean, default=False)
    approx_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts()


class PermissionRow(Base):
    """Permission policy (kind='policy', singleton) and the audit trail of every decision."""

    __tablename__ = "permissions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # policy | decision
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool: Mapped[str | None] = mapped_column(String(100), nullable=True)
    risk: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    via: Mapped[str] = mapped_column(String(32), default="")
    data: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = _ts()


class ApprovalRow(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    step_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_call_id: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str | None] = mapped_column(String(160), nullable=True)  # migration 0003
    tool: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    resource: Mapped[str] = mapped_column(Text)
    risk: Mapped[int] = mapped_column(Integer)
    risk_explanation: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    arguments: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    allowed_scopes: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    tainted: Mapped[bool] = mapped_column(Boolean, default=False)
    category: Mapped[str] = mapped_column(String(64), default="general")
    status: Mapped[str] = mapped_column(String(16), index=True)
    scope_granted: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExecutionLogRow(Base):
    __tablename__ = "execution_logs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    level: Mapped[str] = mapped_column(String(12))
    component: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    correlation: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = _ts()


class VerificationResultRow(Base):
    __tablename__ = "verification_results"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    step_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(64))
    passed: Mapped[bool] = mapped_column(Boolean)
    description: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    message: Mapped[str] = mapped_column(Text, default="")
    method: Mapped[str] = mapped_column(String(24), default="deterministic")
    created_at: Mapped[datetime] = _ts()


class TaskEventRow(Base):
    """Ordered timeline events (the frontend can fully reconstruct a task from these)."""

    __tablename__ = "task_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    correlation: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    timestamp: Mapped[datetime] = _ts()
    __table_args__ = (UniqueConstraint("task_id", "seq", name="uq_task_events_task_seq"),)
