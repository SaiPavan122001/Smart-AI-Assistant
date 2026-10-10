from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import ApprovalScope, ApprovalStatus, RiskLevel
from app.utils.ids import new_id, utcnow


class ApprovalRequest(BaseModel):
    """A specific, human-readable request. Never a vague 'Allow action?'."""

    id: str = Field(default_factory=lambda: new_id("apr"))
    task_id: str
    step_id: str | None = None
    tool_call_id: str
    # Hash of the tool and its exact, original arguments: what this approval is for and nothing else.
    # (``arguments`` below are redacted for display and cannot identify the call.)
    fingerprint: str = ""
    tool: str
    action: str  # what the agent wants to do
    reason: str  # why it wants to do it
    resource: str  # what is affected
    risk: RiskLevel
    risk_explanation: list[str] = Field(default_factory=list)  # what could go wrong
    arguments: dict[str, Any] = Field(default_factory=dict)  # redacted
    allowed_scopes: list[ApprovalScope] = Field(
        default_factory=lambda: [ApprovalScope.ONCE, ApprovalScope.TASK]
    )
    tainted: bool = False  # task has consumed external content flagged as prompt injection
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    scope_granted: ApprovalScope | None = None
    decision_note: str | None = None
    category: str = "general"


class ApprovalDecision(BaseModel):
    approved: bool
    scope: ApprovalScope = ApprovalScope.ONCE
    note: str | None = None
    decided_by: str = "user"
