from __future__ import annotations

from enum import IntEnum, StrEnum


class RiskLevel(IntEnum):
    """Ordered risk classes. Comparison operators are meaningful."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @property
    def label(self) -> str:
        return self.name.lower()


class Capability(StrEnum):
    API = "api"
    BROWSER = "browser"
    DESKTOP = "desktop"
    FILESYSTEM = "filesystem"
    TERMINAL = "terminal"
    VISION = "vision"
    VERIFICATION = "verification"


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    RECOVERING = "RECOVERING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT}
)


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class StepKind(StrEnum):
    TOOL = "tool"  # deterministic: fixed tool + args, no LLM involved at run time
    ADAPTIVE = "adaptive"  # goal-directed: bounded LLM tool-calling loop


class ApprovalRequirement(StrEnum):
    NEVER = "never"  # tool never asks; still subject to deny rules
    POLICY = "policy"  # decided by the permission engine from risk + policy
    ALWAYS = "always"  # always requires explicit approval


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class ApprovalScope(StrEnum):
    ONCE = "once"
    TASK = "task"


class PerceptionSource(StrEnum):
    """Perception hierarchy, ordered from preferred to last resort."""

    API = "api"
    STRUCTURED = "structured"
    DOM = "dom"
    ACCESSIBILITY = "accessibility"
    UIA = "uia"
    DETERMINISTIC = "deterministic"
    VISION = "vision"
    OCR = "ocr"


PERCEPTION_PRIORITY: tuple[PerceptionSource, ...] = tuple(PerceptionSource)


class EventType(StrEnum):
    TASK_CREATED = "task_created"
    PLANNING_STARTED = "planning_started"
    PLAN_CREATED = "plan_created"
    PLAN_REVISED = "plan_revised"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    TOOL_SELECTED = "tool_selected"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    ACTION_STARTED = "action_started"
    ACTION_COMPLETED = "action_completed"
    ACTION_FAILED = "action_failed"
    OBSERVATION_RECEIVED = "observation_received"
    INJECTION_DETECTED = "injection_detected"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_COMPLETED = "verification_completed"
    USAGE_UPDATED = "usage_updated"
    STATUS_CHANGED = "status_changed"
    TASK_PAUSED = "task_paused"
    TASK_RESUMED = "task_resumed"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TASK_TIMED_OUT = "task_timed_out"
    LIMIT_REACHED = "limit_reached"
    KILL_SWITCH = "kill_switch"
