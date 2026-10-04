"""Persistence port. The runtime depends on this protocol, never on a database."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.models.approval import ApprovalRequest
from app.models.enums import TaskStatus
from app.models.events import ExecutionEvent
from app.models.observation import Observation
from app.models.task import Task
from app.models.tool import RiskAssessment, ToolCall, ToolResult
from app.models.verification import VerificationResult
from app.utils.ids import new_id, utcnow


class LogEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("log"))
    task_id: str | None = None
    level: str = "INFO"
    component: str = ""
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    correlation: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class ToolCallRecord(BaseModel):
    call: ToolCall
    result: ToolResult | None = None
    assessment: RiskAssessment | None = None
    decision: str = ""
    decision_reason: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class PermissionDecisionRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("pd"))
    task_id: str | None
    tool_call_id: str | None
    tool: str
    risk: int
    action: str
    reason: str
    via: str = "policy"
    created_at: datetime = Field(default_factory=utcnow)


class AgentActionRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("act"))
    task_id: str
    step_id: str | None = None
    kind: str  # plan | adaptive_decision | replan | recovery | verify_judgement
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class Repository(Protocol):
    async def ensure_session(self, session_id: str, user_id: str, meta: dict[str, Any]) -> None: ...

    async def save_task(self, task: Task) -> None: ...
    async def get_task(self, task_id: str) -> Task | None: ...
    async def list_tasks(
        self, *, limit: int = 50, offset: int = 0, status: TaskStatus | None = None
    ) -> list[Task]: ...

    async def append_event(self, event: ExecutionEvent) -> None: ...
    async def list_events(
        self, task_id: str, *, after_seq: int = 0, limit: int = 2000
    ) -> list[ExecutionEvent]: ...

    async def save_tool_call(self, task_id: str, record: ToolCallRecord) -> None: ...
    async def list_tool_calls(self, task_id: str) -> list[ToolCallRecord]: ...

    async def save_observation(self, obs: Observation) -> None: ...
    async def list_observations(self, task_id: str) -> list[Observation]: ...

    async def save_approval(self, req: ApprovalRequest) -> None: ...
    async def get_approval(self, approval_id: str) -> ApprovalRequest | None: ...
    async def list_approvals(
        self, *, task_id: str | None = None, pending_only: bool = False
    ) -> list[ApprovalRequest]: ...

    async def save_verification(self, result: VerificationResult) -> None: ...
    async def list_verifications(self, task_id: str) -> list[VerificationResult]: ...

    async def save_agent_action(self, record: AgentActionRecord) -> None: ...
    async def list_agent_actions(self, task_id: str) -> list[AgentActionRecord]: ...

    async def add_log(self, entry: LogEntry) -> None: ...
    async def list_logs(
        self, task_id: str, *, limit: int = 1000, min_level: str | None = None
    ) -> list[LogEntry]: ...

    async def record_permission_decision(self, rec: PermissionDecisionRecord) -> None: ...
    async def list_permission_decisions(self, task_id: str) -> list[PermissionDecisionRecord]: ...
    async def load_policy(self) -> dict[str, Any] | None: ...
    async def save_policy(self, policy: dict[str, Any]) -> None: ...

    async def ping(self) -> bool: ...
    async def close(self) -> None: ...
