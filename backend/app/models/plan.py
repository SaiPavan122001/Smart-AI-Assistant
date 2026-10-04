"""Plan and PlanStep contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.models.enums import StepKind, StepStatus
from app.models.verification import VerificationSpec
from app.utils.errors import ErrorInfo
from app.utils.ids import new_id, utcnow


class PlanStep(BaseModel):
    id: str  # unique within the plan, e.g. "s1"
    title: str  # human-readable, shown in the activity timeline
    kind: StepKind = StepKind.TOOL
    tool: str | None = None  # kind == TOOL
    args: dict[str, Any] = Field(default_factory=dict)  # may contain {{steps.sN.data.x}} refs
    goal: str | None = None  # kind == ADAPTIVE
    allowed_tools: list[str] = Field(default_factory=list)  # kind == ADAPTIVE (empty = all exposed)
    depends_on: list[str] = Field(default_factory=list)
    rationale: str = ""  # why this step exists; surfaced in approvals
    verify: list[VerificationSpec] = Field(default_factory=list)
    optional: bool = False

    # --- runtime state (persisted to task_steps) ---
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    summary: str = ""
    error: ErrorInfo | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_kind(self) -> PlanStep:
        if self.kind is StepKind.TOOL and not self.tool:
            raise ValueError(f"step {self.id}: tool steps require 'tool'")
        if self.kind is StepKind.ADAPTIVE and not self.goal:
            raise ValueError(f"step {self.id}: adaptive steps require 'goal'")
        return self


class Plan(BaseModel):
    id: str = Field(default_factory=lambda: new_id("plan"))
    task_id: str | None = None
    version: int = 1  # incremented on every re-plan
    summary: str = ""
    assumptions: list[str] = Field(default_factory=list)
    steps: list[PlanStep]
    # Conditions that must hold for the *original request* to count as done.
    success_criteria: list[VerificationSpec] = Field(default_factory=list)
    # What to tell the user, resolved deterministically from recorded step outputs, e.g.
    # 'The page title is {{steps.s2.data.title}}'. Used when no adaptive step produced an answer.
    answer_template: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    def step(self, step_id: str) -> PlanStep:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)
