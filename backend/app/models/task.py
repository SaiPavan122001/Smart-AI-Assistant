from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.models.enums import AnswerStatus, TaskOutcome, TaskStatus, VerificationStatus
from app.models.plan import Plan
from app.models.report import CompletionReport
from app.models.verification import VerificationResult
from app.utils.errors import ErrorInfo
from app.utils.ids import new_id, utcnow


class UsageStats(BaseModel):
    llm_calls: int = 0
    # llm_calls broken down by purpose (vision is counted separately in vision_calls)
    planner_calls: int = 0
    replanner_calls: int = 0
    repair_calls: int = 0  # plan-repair rounds after a plan failed deterministic validation
    step_calls: int = 0  # adaptive-step iterations
    judge_calls: int = 0  # LLM judgement when a task has no machine-checkable criteria
    answer_generation_calls: int = 0  # answers are rendered deterministically; stays 0 by design
    llm_budget: int = 0  # task-aware cap derived from the plan (0 = only the global limit applies)
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
    verification_attempts: int = 0  # verification contracts evaluated
    verification_polls: int = 0  # checker invocations (>= attempts when a check polled)
    verification_duration_ms: float = 0.0
    verification_success_ms: float = 0.0  # time spent in checks that ended up passing
    pause_count: int = 0
    resume_count: int = 0


class TaskMetrics(BaseModel):
    """One flat, secret-free record per finished task (logged and stored with the result)."""

    task_id: str
    provider: str | None = None
    model: str | None = None
    llm_calls: int = 0
    planner_calls: int = 0
    replanner_calls: int = 0
    repair_calls: int = 0
    answer_generation_calls: int = 0
    tool_calls: int = 0
    recovery_count: int = 0
    verification_attempts: int = 0
    verification_duration: float = 0.0  # seconds
    pause_count: int = 0
    resume_count: int = 0
    vision_calls: int = 0
    task_duration: float = 0.0  # seconds
    final_status: str = ""
    verified: bool = False
    answer_status: str = ""


class TaskResult(BaseModel):
    status: TaskStatus
    summary: str
    # True only if the task COMPLETED, every criterion passed deterministically, every required
    # step finished and the requested answer data is complete. See ``_invariants``.
    verified: bool = False
    verification: list[VerificationResult] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    error: ErrorInfo | None = None
    # Task state, verification state and answer state are separate facts: "there is some text in
    # ``answer``" never means "the task succeeded". Read ``answer_status`` to interpret ``answer``.
    outcome: TaskOutcome = TaskOutcome.FAILED
    verification_status: VerificationStatus = VerificationStatus.NOT_RUN
    answer_status: AnswerStatus = AnswerStatus.NOT_REQUESTED
    answer: str | None = None
    report: CompletionReport | None = (
        None  # steps done / failed / not run, requirements met / unmet
    )
    duration_s: float = 0.0
    usage: UsageStats = Field(default_factory=UsageStats)
    metrics: TaskMetrics | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, raw: Any) -> Any:
        """Results stored before the state split: derive the new fields, never upgrade a claim."""
        if not isinstance(raw, dict):
            return raw
        raw = dict(raw)
        status = str(getattr(raw.get("status"), "value", raw.get("status")))
        done = status == TaskStatus.COMPLETED.value
        raw.setdefault("outcome", status if status in TaskOutcome.__members__ else "FAILED")
        if "answer_status" in raw:
            return raw
        if not done:
            raw["verified"] = False
            raw["answer_status"] = AnswerStatus.WITHHELD
            raw.setdefault("verification_status", VerificationStatus.NOT_RUN)
            raw["answer"] = None  # an answer recorded for a failed task was never trustworthy
        else:
            raw["answer_status"] = (
                AnswerStatus.ANSWERED if raw.get("answer") else AnswerStatus.NOT_REQUESTED
            )
            raw.setdefault(
                "verification_status",
                VerificationStatus.VERIFIED if raw.get("verified") else VerificationStatus.JUDGED,
            )
        return raw

    @model_validator(mode="after")
    def _invariants(self) -> TaskResult:
        done = self.status is TaskStatus.COMPLETED
        if self.verified and not done:
            raise ValueError(f"verified=true is impossible for a {self.status.value} task")
        if self.verified != (self.verification_status is VerificationStatus.VERIFIED):
            raise ValueError("verified must equal (verification_status == VERIFIED)")
        if self.verified and self.answer_status is AnswerStatus.UNAVAILABLE:
            raise ValueError("verified=true is impossible while required answer data is missing")
        if self.answer_status is AnswerStatus.ANSWERED and not (done and self.answer):
            raise ValueError("a success answer requires a COMPLETED task and a non-empty answer")
        if not done and self.answer_status is not AnswerStatus.WITHHELD:
            raise ValueError(f"a {self.status.value} task cannot carry a success answer state")
        if done and self.answer_status is AnswerStatus.WITHHELD:
            raise ValueError("a COMPLETED task does not withhold its answer")
        if (self.outcome is TaskOutcome.COMPLETED) != done:
            raise ValueError("outcome must be COMPLETED exactly when status is COMPLETED")
        return self


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
    paused: bool = False  # a pause was requested and not yet resumed (status PAUSED once parked)
    current_step_id: str | None = None
    current_tool: str | None = None
    current_application: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
