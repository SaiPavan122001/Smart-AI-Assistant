"""TaskRuntime: owns running tasks, concurrency, pause/resume/cancel and the emergency stop."""

from __future__ import annotations

import asyncio
import contextlib

from app.models.enums import EventType, TaskStatus
from app.models.events import Correlation
from app.models.task import Task
from app.observability.logging import get_logger
from app.observability.usage import UsageMeter
from app.orchestration.runner import RunnerDeps, TaskRunner
from app.orchestration.state import TaskHandle
from app.utils.control import CancelToken, KillSwitch, PauseGate
from app.utils.errors import AgentError, ErrorCode, NotFoundError
from app.utils.ids import new_id, utcnow

log = get_logger("runtime")


class TaskRuntime:
    def __init__(self, deps: RunnerDeps, kill_switch: KillSwitch) -> None:
        self.deps = deps
        self.kill_switch = kill_switch
        self._settings = deps.services.settings
        self._runner = TaskRunner(deps)
        self._handles: dict[str, TaskHandle] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._sem = asyncio.Semaphore(max(1, self._settings.limits.max_concurrent_tasks))
        self._closing = False

    # ------------------------------------------------------------------ submit
    async def submit(
        self,
        instruction: str,
        *,
        session_id: str | None = None,
        request_id: str | None = None,
        user_id: str = "local",
    ) -> Task:
        if self._closing:
            raise AgentError("The service is shutting down", code=ErrorCode.INTERNAL)
        if self.kill_switch.engaged:
            raise AgentError(
                "The emergency stop is engaged. Release it before starting new tasks.", code=ErrorCode.KILL_SWITCH
            )  # fmt: skip
        instruction = instruction.strip()
        if not instruction:
            raise AgentError("Instruction must not be empty", code=ErrorCode.INVALID_INPUT)
        task = Task(
            instruction=instruction,
            session_id=session_id,
            request_id=request_id or new_id("req"),
            user_id=user_id,
        )
        meter = UsageMeter(
            self._settings.limits, metrics=self.deps.metrics,
            price_in_per_mtok=self._settings.llm.price_input_per_mtok, price_out_per_mtok=self._settings.llm.price_output_per_mtok,
        )  # fmt: skip
        handle = TaskHandle(
            task_id=task.id, token=CancelToken(self.kill_switch), pause=PauseGate(), meter=meter
        )
        self._handles[task.id] = handle
        task.usage = meter.usage
        await self.deps.repo.save_task(task)
        await self.deps.bus.publish(
            task.id, EventType.TASK_CREATED, task.instruction[:200],
            {"task": task.model_dump(mode="json", include={"id", "instruction", "status", "created_at"})},
            Correlation(request_id=task.request_id, session_id=session_id),
        )  # fmt: skip
        self._tasks[task.id] = asyncio.create_task(self._run(task, handle), name=f"task-{task.id}")
        return task

    async def _run(self, task: Task, handle: TaskHandle) -> None:
        try:
            async with self._sem:
                if handle.token.cancelled:
                    await self._cancel_unstarted(task, handle)
                    return
                await self._runner.run(task, handle)
        except asyncio.CancelledError:  # process shutdown
            handle.token.cancel("service shutdown")
            raise
        except Exception:
            log.exception("task runner failed", extra={"task_id": task.id})
        finally:
            self._handles.pop(task.id, None)
            self._tasks.pop(task.id, None)

    async def _cancel_unstarted(self, task: Task, handle: TaskHandle) -> None:
        task.status = TaskStatus.CANCELLED
        task.finished_at = utcnow()
        await self.deps.repo.save_task(task)
        await self.deps.bus.publish(
            task.id,
            EventType.TASK_CANCELLED,
            handle.token.reason or "cancelled",
            {},
            Correlation(session_id=task.session_id),
        )

    # ------------------------------------------------------------------ control
    def _handle(self, task_id: str) -> TaskHandle:
        h = self._handles.get(task_id)
        if h is None:
            raise NotFoundError(f"Task {task_id} is not running")
        return h

    async def cancel(self, task_id: str, reason: str = "Cancelled by user") -> None:
        h = self._handle(task_id)
        h.token.cancel(reason)
        h.pause.resume()  # let a paused loop observe the cancellation
        self.deps.approvals.cancel_task(task_id)

    async def pause(self, task_id: str) -> None:
        h = self._handle(task_id)
        h.pause.pause()
        task = await self.deps.repo.get_task(task_id)
        if task and not task.status.is_terminal:
            task.paused = True
            await self.deps.repo.save_task(task)
        await self.deps.bus.publish(
            task_id, EventType.TASK_PAUSED, "Paused: no further tools will start until resumed", {}
        )

    async def resume(self, task_id: str) -> None:
        h = self._handle(task_id)
        h.pause.resume()
        task = await self.deps.repo.get_task(task_id)
        if task and not task.status.is_terminal:
            task.paused = False
            await self.deps.repo.save_task(task)
        await self.deps.bus.publish(task_id, EventType.TASK_RESUMED, "Resumed", {})

    async def stop_all(self, reason: str = "STOP AGENT pressed") -> list[str]:
        """Emergency stop: block all tool execution, cancel every task, deny every pending approval."""
        self.kill_switch.engage(reason)
        stopped = list(self._handles)
        for tid, h in list(self._handles.items()):
            h.token.cancel(reason)
            h.pause.resume()
            self.deps.approvals.cancel_task(tid)
            await self.deps.bus.publish(tid, EventType.KILL_SWITCH, reason, {"engaged": True})
        log.warning("emergency stop engaged", extra={"reason": reason, "tasks": stopped})
        return stopped

    def release_kill_switch(self) -> None:
        self.kill_switch.release()

    # ------------------------------------------------------------------ queries / lifecycle
    def active_task_ids(self) -> list[str]:
        return list(self._handles)

    def is_running(self, task_id: str) -> bool:
        return task_id in self._handles

    async def wait(self, task_id: str, timeout: float | None = None) -> None:
        t = self._tasks.get(task_id)
        if t is not None:
            await asyncio.wait_for(asyncio.shield(t), timeout)

    async def recover_orphans(self) -> int:
        """Tasks left non-terminal by a crash/restart can never resume: mark them FAILED, honestly."""
        n = 0
        for task in await self.deps.repo.list_tasks(limit=500):
            if not task.status.is_terminal:
                task.status = TaskStatus.FAILED
                task.finished_at = utcnow()
                task.paused = False
                await self.deps.repo.save_task(task)
                await self.deps.bus.publish(
                    task.id,
                    EventType.TASK_FAILED,
                    "Interrupted by a service restart; actions already performed were not undone",
                    {},
                )
                n += 1
        return n

    async def shutdown(self) -> None:
        self._closing = True
        for h in self._handles.values():
            h.token.cancel("service shutdown")
            h.pause.resume()
        for t in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(t, timeout=10)
