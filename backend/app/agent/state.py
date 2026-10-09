"""Per-task runtime objects: control handle and mutable run state."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.models.enums import EventType, TaskStatus
from app.models.events import Correlation
from app.models.goal import GoalSpec, RequirementResult
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
from app.tools.loop_guard import LoopGuard
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
    loop_guard: LoopGuard = field(
        default_factory=LoopGuard
    )  # action ledger + loop detection for this task
    asyncio_task: Any = None
    run_state: Any = None  # RunState once the runner started (owns status transitions)
    # Serialises pause/resume requests with the task parking itself, so their events and the
    # persisted status can never interleave (a slow database write must not reorder them).
    pause_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RunState:
    """Mutable state of one task execution. Owns status transitions and event emission."""

    def __init__(self, task: Task, handle: TaskHandle, bus: EventBus, repo: Repository) -> None:
        self.task = task
        self.handle = handle
        self._bus = bus
        self._repo = repo
        self.started_monotonic = time.monotonic()
        self.plan: Plan | None = None
        self.goal: GoalSpec | None = None  # extracted from the request by code, fixed for the task
        self.requirement_results: list[
            RequirementResult
        ] = []  # latest evaluation against real state
        self.step_data: dict[str, dict[str, Any]] = {}
        self.history: list[RecoveryAttempt] = []
        self.replans_used = 0
        # Non-retryable conditions that already got their one re-plan (a second one is final).
        self.replanned_codes: set[Any] = set()
        # Answers reported by adaptive steps, keyed by step id. Nothing here is "the answer" of the
        # task: the final answer is decided once, from final state, in app.agent.answer.
        self.step_answers: dict[str, str] = {}
        self.artifacts: dict[str, Any] = {}
        self.last_verification: list[
            Any
        ] = []  # newest final-verification results (for failure reports)
        self._status_before_wait: TaskStatus | None = None
        self._status_before_pause: TaskStatus | None = None
        self._pause_lock = handle.pause_lock
        handle.run_state = self
        handle.pause.on_park = self.enter_pause

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

    # ------------------------------------------------------------------ pause
    async def gate(self) -> None:
        """Park here while paused. Call before anything that costs or acts (LLM call, re-plan)."""
        self.handle.token.raise_if_cancelled()
        await self.handle.pause.wait_if_paused(self.handle.token)

    async def enter_pause(self) -> None:
        """The task reached the gate while paused: persist PAUSED, then announce it."""
        async with self._pause_lock:
            current = self.task.status
            if not self.handle.pause.paused or current is TaskStatus.PAUSED or current.is_terminal:
                return
            self._status_before_pause = current
            self.handle.meter.usage.pause_count += 1
            await self.set_status(TaskStatus.PAUSED, "Paused: nothing runs until resumed")
            await self.emit(
                EventType.PAUSE_COMPLETED,
                "Paused: no tool, model call, recovery or re-plan runs until resumed",
                {"status": TaskStatus.PAUSED.value, "previous": current.value},
            )

    async def leave_pause(self) -> TaskStatus:
        """Restore the interrupted phase. Called BEFORE the execution gate is released."""
        async with self._pause_lock:
            self.task.paused = False
            if self.task.status is TaskStatus.PAUSED:
                await self.set_status(self._status_before_pause or TaskStatus.EXECUTING, "Resumed")
            else:
                await self.save()
            return self.task.status

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
