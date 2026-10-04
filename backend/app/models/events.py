from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import EventType
from app.utils.ids import new_id, utcnow


class Correlation(BaseModel):
    request_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    tool_call_id: str | None = None


class ExecutionEvent(BaseModel):
    """One entry of the task timeline; the frontend rebuilds state from these."""

    id: str = Field(default_factory=lambda: new_id("evt"))
    task_id: str
    seq: int = 0  # monotonically increasing per task, assigned by the event bus
    type: EventType
    timestamp: datetime = Field(default_factory=utcnow)
    correlation: Correlation = Field(default_factory=Correlation)
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
