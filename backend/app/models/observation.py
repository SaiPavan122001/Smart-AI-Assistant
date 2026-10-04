from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import PerceptionSource
from app.utils.ids import new_id, utcnow


class Observation(BaseModel):
    """A structured snapshot of environment state."""

    id: str = Field(default_factory=lambda: new_id("obs"))
    task_id: str | None = None
    step_id: str | None = None
    source: PerceptionSource
    kind: str  # "page_state" | "window_tree" | "file_state" | "command_output" | "vision" ...
    subject: str = ""  # url / window title / path
    data: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = ""
    untrusted: bool = False
    approx_tokens: int = 0
    created_at: datetime = Field(default_factory=utcnow)
