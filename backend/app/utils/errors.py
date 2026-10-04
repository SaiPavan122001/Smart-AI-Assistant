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
    BLOCKED_BY_SITE = "blocked_by_site"  # bot check / CAPTCHA: never bypassed
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
