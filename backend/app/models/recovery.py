"""Durable task state and the description of an interrupted task.

``DurableState`` is what the runner needs, beyond the plan and its steps, to continue a task in a new
process. ``Interruption`` is what startup recovery found for a task that was not finished when the
process stopped, including how certain the outcome of every action that was in flight is.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import TaskStatus
from app.utils.ids import utcnow


class DurableState(BaseModel):
    """Runner state persisted with the task row (same transaction as the plan and its steps)."""

    version: int = 1
    step_data: dict[str, dict[str, Any]] = Field(
        default_factory=dict
    )  # outputs later steps reference
    step_answers: dict[str, str] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    replans_used: int = 0
    replanned_codes: list[str] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)  # recovery attempts
    denied: list[str] = Field(
        default_factory=list
    )  # call keys the user refused: final, also after a restart
    status_before_pause: TaskStatus | None = None
    resumes: int = 0  # how many times this task was continued after an interruption


class ActionCertainty(StrEnum):
    """What is known about an action that had started but had no recorded result."""

    SAFE_TO_RETRY = "safe_to_retry"  # repeating it cannot do harm (declared idempotent)
    CONFIRMED_DONE = "confirmed_done"  # the observable state shows it happened
    CONFIRMED_NOT_DONE = "confirmed_not_done"  # the observable state shows it did not happen
    UNCERTAIN = "uncertain"  # cannot be determined: never repeated without the user's approval


class InFlightAction(BaseModel):
    tool_call_id: str
    tool: str
    step_id: str | None = None
    fingerprint: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)  # redacted, as recorded
    certainty: ActionCertainty
    detail: str = ""
    # For CONFIRMED_DONE: the result reconstructed from the observable state (replayed, not re-executed).
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)


class RecoveryOption(StrEnum):
    RESUME = "resume"  # continue from the recorded state; nothing confirmed done is repeated
    RESTART = "restart"  # cancel this task and start the same request again as a new task
    CANCEL = "cancel"  # end the task; completed actions stay in place
    INSPECT = "inspect"  # read-only: what was done and what was not


class Interruption(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    reason: str = "The service stopped while this task was not finished"
    status_at_interruption: TaskStatus
    was_paused: bool = False
    awaiting_approval: bool = False
    interrupted_step: str | None = None
    in_flight: list[InFlightAction] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)
    remaining_steps: list[str] = Field(default_factory=list)
    resumable: bool = True
    needs_attention: bool = False  # an action's outcome is uncertain
    notes: list[str] = Field(default_factory=list)
    options: list[RecoveryOption] = Field(default_factory=list)
