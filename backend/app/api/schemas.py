"""Versioned API request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import ApprovalScope, TaskStatus
from app.models.task import Task, UsageStats


class CreateTaskRequest(BaseModel):
    instruction: str = Field(
        min_length=1, max_length=4000, description="What you want done, in plain language"
    )


class ApproveRequest(BaseModel):
    scope: ApprovalScope = ApprovalScope.ONCE
    note: str | None = Field(default=None, max_length=500)


class DenyRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class TaskSummary(BaseModel):
    id: str
    instruction: str
    status: TaskStatus
    paused: bool
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current_step_id: str | None = None
    current_tool: str | None = None
    current_application: str | None = None
    usage: UsageStats
    summary: str | None = None
    verified: bool | None = None

    @classmethod
    def from_task(cls, t: Task) -> TaskSummary:
        return cls(
            id=t.id,
            instruction=t.instruction,
            status=t.status,
            paused=t.paused,
            created_at=t.created_at,
            updated_at=t.updated_at,
            started_at=t.started_at,
            finished_at=t.finished_at,
            current_step_id=t.current_step_id,
            current_tool=t.current_tool,
            current_application=t.current_application,
            usage=t.usage,
            summary=t.result.summary if t.result else None,
            verified=t.result.verified if t.result else None,
        )


class TaskList(BaseModel):
    items: list[TaskSummary]
    limit: int
    offset: int


class AgentStatus(BaseModel):
    kill_switch_engaged: bool
    kill_switch_reason: str
    active_tasks: list[str]
    pending_approvals: int


class StopResult(BaseModel):
    stopped_tasks: list[str]
    kill_switch_engaged: bool


class Health(BaseModel):
    status: str
    version: str


class Readiness(BaseModel):
    ready: bool
    checks: dict[str, Any]


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody
