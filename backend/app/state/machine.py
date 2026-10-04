"""Explicit task state machine. Every status change goes through ``transition``."""

from __future__ import annotations

from app.models.enums import TERMINAL_STATUSES, TaskStatus
from app.utils.errors import AgentError, ErrorCode

S = TaskStatus

_ALLOWED: dict[TaskStatus, frozenset[TaskStatus]] = {
    S.CREATED: frozenset({S.PLANNING, S.CANCELLED, S.FAILED}),
    S.PLANNING: frozenset({S.EXECUTING, S.FAILED, S.CANCELLED, S.TIMED_OUT, S.WAITING_APPROVAL}),
    S.EXECUTING: frozenset(
        {
            S.WAITING_APPROVAL,
            S.RECOVERING,
            S.PLANNING,
            S.VERIFYING,
            S.FAILED,
            S.CANCELLED,
            S.TIMED_OUT,
        }
    ),
    S.WAITING_APPROVAL: frozenset(
        {S.EXECUTING, S.RECOVERING, S.VERIFYING, S.PLANNING, S.FAILED, S.CANCELLED, S.TIMED_OUT}
    ),
    S.RECOVERING: frozenset(
        {S.EXECUTING, S.PLANNING, S.WAITING_APPROVAL, S.FAILED, S.CANCELLED, S.TIMED_OUT}
    ),
    S.VERIFYING: frozenset(
        {S.COMPLETED, S.EXECUTING, S.RECOVERING, S.PLANNING, S.FAILED, S.CANCELLED, S.TIMED_OUT}
    ),
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
    S.TIMED_OUT: frozenset(),
}


class InvalidTransition(AgentError):
    code = ErrorCode.INTERNAL


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return current == target or target in _ALLOWED[current]


def transition(current: TaskStatus, target: TaskStatus) -> TaskStatus:
    """Validate and return the new status. Terminal states are final."""
    if current in TERMINAL_STATUSES and current != target:
        raise InvalidTransition(f"Task is already {current.value}; cannot move to {target.value}")
    if not can_transition(current, target):
        raise InvalidTransition(f"Illegal task transition {current.value} -> {target.value}")
    return target
