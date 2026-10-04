"""Structured error taxonomy.

Every failure that crosses a module boundary is expressed as an ``ErrorCode``
plus a human message. Recovery strategies and the UI key off the code, never
off message text.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    # generic
    INVALID_INPUT = "invalid_input"
    UNKNOWN_TOOL = "unknown_tool"
    INTERNAL = "internal_error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    LIMIT_EXCEEDED = "limit_exceeded"
    # search / browser-automation contract (provider-specific reasons live in ``details``)
    SEARCH_UNAVAILABLE = "SEARCH_UNAVAILABLE"  # no search provider could answer
    SEARCH_AUTOMATION_BLOCKED = (
        "SEARCH_AUTOMATION_BLOCKED"  # bot check / CAPTCHA: never bypassed, never retried
    )
    LOOP_DETECTED = "loop_detected"  # the agent is repeating actions without progress
    # security
    POLICY_DENIED = "policy_denied"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_TIMEOUT = "approval_timeout"
    KILL_SWITCH = "kill_switch_active"
    PATH_BLOCKED = "path_blocked"
    COMMAND_BLOCKED = "command_blocked"
    URL_BLOCKED = "url_blocked"
    INJECTION_BLOCKED = "prompt_injection_blocked"
    # environment
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    ELEMENT_NOT_FOUND = "element_not_found"
    STALE_REFERENCE = "stale_reference"
    AMBIGUOUS_TARGET = "ambiguous_target"
    NOT_AVAILABLE = "capability_not_available"
    LOW_CONFIDENCE = "low_confidence"
    COMMAND_FAILED = "command_failed"
    VERIFICATION_FAILED = "verification_failed"
    NAVIGATION_FAILED = "navigation_failed"
    IO_ERROR = "io_error"
    LLM_ERROR = "llm_error"
    PLAN_INVALID = "plan_invalid"

    @classmethod
    def _missing_(cls, value: object) -> ErrorCode | None:
        """Records persisted before the search-error normalisation still load."""
        return _LEGACY_CODES.get(str(value))


_LEGACY_CODES: dict[str, ErrorCode] = {"blocked_by_site": ErrorCode.SEARCH_AUTOMATION_BLOCKED}

# ---- retry classification: the single place that says whether trying again can help -----------
# Transient: the same action may succeed if repeated (subject to the per-step recovery budget).
RETRYABLE_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.TIMEOUT,
        ErrorCode.NAVIGATION_FAILED,
        ErrorCode.IO_ERROR,
        ErrorCode.STALE_REFERENCE,
        ErrorCode.LLM_ERROR,
    }
)
# Final: a human decision, a hard limit, a protected resource or an automation block ended it.
# These never enter retry / refresh / adaptive recovery and never trigger a re-plan.
NON_RETRYABLE_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.APPROVAL_DENIED,
        ErrorCode.APPROVAL_TIMEOUT,
        ErrorCode.INJECTION_BLOCKED,
        ErrorCode.CANCELLED,
        ErrorCode.KILL_SWITCH,
        ErrorCode.LIMIT_EXCEEDED,
        ErrorCode.LOOP_DETECTED,
        ErrorCode.NOT_AVAILABLE,
        ErrorCode.UNKNOWN_TOOL,
    }
)
# Not retryable either, but ONE re-plan may find a compliant alternative (another source, another
# path). A second occurrence of the same condition in the same task is final.
SINGLE_REPLAN_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.SEARCH_AUTOMATION_BLOCKED,
        ErrorCode.SEARCH_UNAVAILABLE,
        ErrorCode.PLAN_INVALID,
        ErrorCode.POLICY_DENIED,
        ErrorCode.PATH_BLOCKED,
        ErrorCode.COMMAND_BLOCKED,
        ErrorCode.URL_BLOCKED,
    }
)


def is_retryable(code: ErrorCode) -> bool:
    return code in RETRYABLE_CODES


class ErrorInfo(BaseModel):
    """Serializable error attached to ToolResults, events and task results."""

    code: ErrorCode
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class AgentError(Exception):
    """Base exception for expected, structured failures."""

    code: ErrorCode = ErrorCode.INTERNAL
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.details = details or {}

    def to_info(self) -> ErrorInfo:
        return ErrorInfo(
            code=self.code, message=self.message, retryable=self.retryable, details=self.details
        )


class PolicyViolation(AgentError):
    code = ErrorCode.POLICY_DENIED


class PathViolation(PolicyViolation):
    code = ErrorCode.PATH_BLOCKED


class CommandViolation(PolicyViolation):
    code = ErrorCode.COMMAND_BLOCKED


class UrlViolation(PolicyViolation):
    code = ErrorCode.URL_BLOCKED


class ToolInputError(AgentError):
    code = ErrorCode.INVALID_INPUT


class NotFoundError(AgentError):
    code = ErrorCode.NOT_FOUND


class TaskCancelled(AgentError):
    code = ErrorCode.CANCELLED


class LimitExceeded(AgentError):
    code = ErrorCode.LIMIT_EXCEEDED


class LLMError(AgentError):
    code = ErrorCode.LLM_ERROR
    retryable = True
