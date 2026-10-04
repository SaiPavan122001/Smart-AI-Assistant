from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import TaskStatus
from app.models.plan import Plan
from app.models.verification import VerificationResult
from app.utils.errors import ErrorInfo
from app.utils.ids import new_id, utcnow


class UsageStats(BaseModel):
    llm_calls: int = 0
    vision_calls: int = 0
    tool_calls: int = 0
    actions: int = 0
    shell_operations: int = 0
    retries: int = 0
    recoveries: int = 0
    replans: int = 0
    approvals: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    llm_latency_ms: float = 0.0
    estimated_cost_usd: float = 0.0
    cache_hits: int = 0
    verification_failures: int = 0


class TaskResult(BaseModel):
    status: TaskStatus
    summary: str
    verified: bool = False  # True only if every success criterion passed deterministically
    verification: list[VerificationResult] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    error: ErrorInfo | None = None
    answer: str | None = None  # free-form answer for question/summarization tasks
    duration_s: float = 0.0
    usage: UsageStats = Field(default_factory=UsageStats)


class Task(BaseModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    user_id: str = "local"
    session_id: str | None = None
    request_id: str | None = None
    instruction: str
    status: TaskStatus = TaskStatus.CREATED
    plan: Plan | None = None
    result: TaskResult | None = None
    usage: UsageStats = Field(default_factory=UsageStats)
    paused: bool = False
    current_step_id: str | None = None
    current_tool: str | None = None
    current_application: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
