"""Per-task runtime objects: control handle and mutable run state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.enums import EventType, TaskStatus
from app.models.events import Correlation
from app.models.plan import Plan
from app.models.task import Task
from app.observability.usage import UsageMeter
from app.perception.cache import ObservationCache
from app.permissions.engine import TaskGrants
from app.recovery.engine import RecoveryAttempt
from app.security.injection import InjectionGuard
from app.state.events import EventBus
from app.state.machine import transition
from app.state.repository import Repository
from app.utils.control import CancelToken, PauseGate
from app.utils.ids import utcnow


@dataclass
class TaskHandle:
    """Everything the outside world (API, emergency stop) needs to control a running task."""

    task_id: str
    token: CancelToken
    pause: PauseGate
    meter: UsageMeter
    grants: TaskGrants = field(default_factory=TaskGrants)
    guard: InjectionGuard = field(default_factory=InjectionGuard)
    cache: ObservationCache = field(default_factory=ObservationCache)
    denied: set[str] = field(default_factory=set)
    asyncio_task: Any = None


class RunState:
    """Mutable state of one task execution. Owns status transitions and event emission."""

    def __init__(self, task: Task, handle: TaskHandle, bus: EventBus, repo: Repository) -> None:
        self.task = task
        self.handle = handle
        self._bus = bus
        self._repo = repo
        self.plan: Plan | None = None
        self.step_data: dict[str, dict[str, Any]] = {}
        self.history: list[RecoveryAttempt] = []
        self.replans_used = 0
        self.answer: str | None = None
        self.artifacts: dict[str, Any] = {}
        self.last_verification: list[
            Any
        ] = []  # newest final-verification results (for failure reports)
        self._status_before_wait: TaskStatus | None = None

    # ------------------------------------------------------------------ scope for templates
    @property
    def scope(self) -> dict[str, Any]:
        return {"steps": self.step_data}

    def record_step_data(
        self,
        step_id: str,
        *,
        data: dict[str, Any],
        artifacts: dict[str, Any],
        summary: str,
        ok: bool,
    ) -> None:
        self.step_data[step_id] = {
            "data": data,
            "artifacts": artifacts,
            "summary": summary,
            "ok": ok,
        }

    # ------------------------------------------------------------------ events
    def correlation(
        self, step_id: str | None = None, tool_call_id: str | None = None
    ) -> Correlation:
        return Correlation(
            request_id=self.task.request_id, session_id=self.task.session_id, task_id=self.task.id,
            plan_id=self.plan.id if self.plan else None, step_id=step_id, tool_call_id=tool_call_id,
        )  # fmt: skip

    async def emit(
        self, type_: EventType, message: str = "", data: dict[str, Any] | None = None, *,
        step_id: str | None = None, tool_call_id: str | None = None,
    ) -> None:  # fmt: skip
        if type_ is EventType.TOOL_SELECTED and data:
            self.task.current_tool = data.get("tool")
            self.task.current_application = _app_label(data)
        await self._bus.publish(
            self.task.id, type_, message, data, self.correlation(step_id, tool_call_id)
        )

    # ------------------------------------------------------------------ status
    async def set_status(self, target: TaskStatus, message: str = "") -> None:
        current = self.task.status
        if (
            target is TaskStatus.EXECUTING
            and current is TaskStatus.WAITING_APPROVAL
            and self._status_before_wait
            in (
                TaskStatus.RECOVERING,
                TaskStatus.VERIFYING,
                TaskStatus.PLANNING,
            )
        ):
            target = self._status_before_wait  # approval granted: resume the phase we were in
        if target is TaskStatus.WAITING_APPROVAL and current is not TaskStatus.WAITING_APPROVAL:
            self._status_before_wait = current
        if target == current:
            return
        self.task.status = transition(current, target)
        self.task.updated_at = utcnow()
        if target is TaskStatus.EXECUTING and self.task.started_at is None:
            self.task.started_at = utcnow()
        await self.save()
        await self.emit(
            EventType.STATUS_CHANGED,
            message or target.value,
            {"status": target.value, "previous": current.value},
        )

    async def save(self) -> None:
        self.task.updated_at = utcnow()
        self.task.plan = self.plan
        self.task.usage = self.handle.meter.usage
        await self._repo.save_task(self.task)


def _app_label(data: dict[str, Any]) -> str | None:
    cap = data.get("capability")
    resource = str(data.get("resource") or "")
    return {
        "browser": f"Browser · {resource[:60]}" if resource else "Browser",
        "desktop": f"Desktop · {resource[:60]}" if resource else "Desktop",
        "filesystem": "File system",
        "terminal": "Terminal",
        "vision": "Screen capture",
        "verification": "Verification",
    }.get(cap or "")
