from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.utils.ids import new_id, utcnow


class VerificationSpec(BaseModel):
    """A declarative, machine-checkable condition.

    ``kind`` selects a checker registered in ``app.verification.checks``.
    """

    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class VerificationResult(BaseModel):
    id: str = Field(default_factory=lambda: new_id("vr"))
    task_id: str | None = None
    step_id: str | None = None
    kind: str
    passed: bool
    description: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    method: str = "deterministic"  # or "llm_judgement" (lower assurance)
    polls: int = 1  # checker invocations (1 = decided on the immediate first check)
    duration_ms: float = 0.0
    terminal: bool = False  # failed in a way that waiting/retrying cannot change (contract error)
    created_at: datetime = Field(default_factory=utcnow)
