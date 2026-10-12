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
    PAUSED = "PAUSED"  # parked at the pause gate: no tool call, LLM call, recovery or re-plan runs
    # The process stopped while the task was unfinished. Nothing runs; the user chooses to resume,
    # restart or cancel. Entered only by startup recovery, never by a running task.
    INTERRUPTED = "INTERRUPTED"
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


class VerificationStatus(StrEnum):
    """Outcome of the task's final verification contract (authoritative for ``verified``)."""

    VERIFIED = "VERIFIED"  # every deterministic criterion passed and all required state is complete
    JUDGED = "JUDGED"  # no machine-checkable criteria: lower-assurance model judgement passed
    INCOMPLETE = "INCOMPLETE"  # criteria passed, but required steps / answer data are missing
    FAILED = "FAILED"  # at least one criterion did not hold
    NOT_RUN = "NOT_RUN"  # the task ended before final verification


class AnswerStatus(StrEnum):
    """What the ``answer`` text is. Only ``ANSWERED`` is a success answer."""

    ANSWERED = "ANSWERED"  # rendered from verified outputs of a COMPLETED task
    NOT_REQUESTED = "NOT_REQUESTED"  # the task completed and asked for no information back
    UNAVAILABLE = "answer_unavailable"  # completed, but a required answer field was never produced
    WITHHELD = "WITHHELD"  # the task did not succeed: the text describes the failure, not a result


class TaskOutcome(StrEnum):
    """Why a task ended. Finer-grained than ``TaskStatus`` (several map to FAILED)."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    DENIED = "DENIED"
    LOOP_DETECTED = "LOOP_DETECTED"
    PLAN_INVALID = "PLAN_INVALID"
    PARTIAL = "PARTIAL"  # failed, but some requested work was completed and is still in place


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
    PAUSE_REQUESTED = "pause_requested"
    PAUSE_COMPLETED = "pause_completed"  # the task is parked: status is PAUSED
    RESUME_REQUESTED = "resume_requested"
    RESUME_COMPLETED = "resume_completed"  # status restored, execution gate released
    TASK_PAUSED = "task_paused"  # legacy (events stored before the PAUSED state); no longer emitted
    TASK_RESUMED = "task_resumed"  # legacy; no longer emitted
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TASK_TIMED_OUT = "task_timed_out"
    LIMIT_REACHED = "limit_reached"
    KILL_SWITCH = "kill_switch"
    CHECKPOINT_SAVED = (
        "checkpoint_saved"  # progress snapshot at a step boundary (not a resume point)
    )
    TASK_INTERRUPTED = "task_interrupted"  # found unfinished at startup; waits for the user
    TASK_RESUMING = "task_resuming"  # the user chose to continue an interrupted task
    ROLLBACK_STARTED = "rollback_started"  # the user asked to undo a finished task's file changes
    ROLLBACK_COMPLETED = "rollback_completed"  # finished or stopped; counts per journal state
