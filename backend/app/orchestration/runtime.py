"""TaskRuntime: owns running tasks, concurrency, pause/resume/cancel and the emergency stop."""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict

from app.agent.answer import failure_statement
from app.agent.report import build_report
from app.agent.state import TaskHandle
from app.models.enums import (
    TERMINAL_STATUSES,
    AnswerStatus,
    ApprovalStatus,
    EventType,
    TaskOutcome,
    TaskStatus,
)
from app.models.events import Correlation
from app.models.task import Task, TaskResult
from app.observability.logging import get_logger
from app.observability.usage import UsageMeter
from app.orchestration.recovery import assess_interruption
from app.orchestration.runner import RunnerDeps, TaskRunner
from app.state.machine import transition
from app.state.repository import AgentActionRecord
from app.tools.loop_guard import LoopGuard
from app.utils.control import CancelToken, KillSwitch, PauseGate
from app.utils.errors import AgentError, ErrorCode, ErrorInfo, NotFoundError
from app.utils.ids import new_id, utcnow
from app.utils.redact import redact

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
        # Idempotent task start: a repeated delivery of the same request must not start a second execution.
        self._live: dict[str, Task] = {}  # task_id -> task, registered synchronously at submit
        self._active_by_text: dict[
            str, str
        ] = {}  # normalised instruction -> task_id (while not finished)
        self._by_key: OrderedDict[str, str] = OrderedDict()  # Idempotency-Key -> task_id (bounded)
        # Interrupted tasks waiting for the user's decision: the same request submitted again is
        # answered with the interrupted task, never run a second time next to it.
        self._interrupted_by_text: dict[str, str] = {}
        self._resuming: set[str] = set()

    # ------------------------------------------------------------------ submit
    async def submit(
        self,
        instruction: str,
        *,
        session_id: str | None = None,
        request_id: str | None = None,
        user_id: str = "local",
        idempotency_key: str | None = None,
        restarted_from: str | None = None,
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
        # ---- idempotency: decided and registered BEFORE the first await, so concurrent duplicates collapse ----
        key = (idempotency_key or "").strip()[:128] or None
        text = " ".join(instruction.split()).lower()
        known = self._by_key.get(key) if key else None
        if known is not None:
            existing = self._live.get(known) or await self.deps.repo.get_task(known)
            if existing is not None:
                return existing
        running = self._live.get(self._active_by_text.get(text, ""))
        if running is not None and not running.status.is_terminal:
            return running  # the same request is already executing: never run it twice
        waiting_id = self._interrupted_by_text.get(text)
        if waiting_id is not None:
            waiting = await self.deps.repo.get_task(waiting_id)
            if waiting is not None and waiting.status is TaskStatus.INTERRUPTED:
                # Part of this request may already be done. Starting it again from scratch must be
                # an explicit choice (resume / restart / cancel), not a side effect of re-sending it.
                return waiting
            self._interrupted_by_text.pop(text, None)
            running = self._live.get(self._active_by_text.get(text, ""))
            if running is not None and not running.status.is_terminal:
                return running  # a concurrent duplicate registered while we looked
        task = Task(
            instruction=instruction,
            session_id=session_id,
            request_id=request_id or new_id("req"),
            user_id=user_id,
            idempotency_key=key,
            restarted_from=restarted_from,
        )
        self._live[task.id] = task
        self._active_by_text[text] = task.id
        if key:
            self._by_key[key] = task.id
            while len(self._by_key) > 512:
                self._by_key.popitem(last=False)
        try:
            return await self._start(task, session_id)
        except BaseException:
            self._forget(task.id)
            raise

    def _forget(self, task_id: str) -> None:
        self._live.pop(task_id, None)
        for k in [k for k, v in self._active_by_text.items() if v == task_id]:
            del self._active_by_text[k]

    def _new_handle(self, task: Task, *, keep_usage: bool = False) -> TaskHandle:
        meter = UsageMeter(
            self._settings.limits, metrics=self.deps.metrics,
            price_in_per_mtok=self._settings.llm.price_input_per_mtok, price_out_per_mtok=self._settings.llm.price_output_per_mtok,
        )  # fmt: skip
        if keep_usage:
            meter.usage = task.usage  # budgets and limits continue where the task stopped
        lim = self._settings.limits
        handle = TaskHandle(
            task_id=task.id, token=CancelToken(self.kill_switch), pause=PauseGate(), meter=meter,
            loop_guard=LoopGuard(
                max_identical=lim.max_identical_actions, max_cycle_repeats=lim.max_cycle_repeats,
                max_no_progress=lim.max_no_progress,
            ),
        )  # fmt: skip
        self._handles[task.id] = handle
        task.usage = meter.usage
        return handle

    async def _start(self, task: Task, session_id: str | None) -> Task:
        handle = self._new_handle(task)
        await self.deps.repo.save_task(task)
        await self.deps.bus.publish(
            task.id, EventType.TASK_CREATED, task.instruction[:200],
            {"task": task.model_dump(mode="json", include={"id", "instruction", "status", "created_at"})},
            Correlation(request_id=task.request_id, session_id=session_id),
        )  # fmt: skip
        self._tasks[task.id] = asyncio.create_task(self._run(task, handle), name=f"task-{task.id}")
        return task

    async def _run(self, task: Task, handle: TaskHandle, *, resume: bool = False) -> None:
        try:
            async with self._sem:
                if handle.token.cancelled:
                    if not handle.token.suspended:  # a service stop leaves a queued task as it is
                        await self._cancel_unstarted(task, handle)
                    return
                if resume:
                    await self._runner.run(task, handle, resume=True)
                else:
                    await self._runner.run(task, handle)
        except asyncio.CancelledError:  # process shutdown
            handle.token.suspend("service shutdown")
            raise
        except Exception:
            log.exception("task runner failed", extra={"task_id": task.id})
        finally:
            self._handles.pop(task.id, None)
            self._tasks.pop(task.id, None)
            self._forget(task.id)

    async def _cancel_unstarted(self, task: Task, handle: TaskHandle) -> None:
        if task.status is TaskStatus.INTERRUPTED:  # a resume that never got to run
            await self._finish_idle(task, handle.token.reason or "Cancelled by user")
            return
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

    async def _finish_idle(self, task: Task, reason: str) -> Task:
        """End a task that is not executing (interrupted, or found cancelled at startup) as CANCELLED,
        with a truthful account of what had been done. Nothing is executed and nothing is undone."""
        report = build_report(
            task.plan, [], effects=[], replans=task.usage.replans, recoveries=task.usage.recoveries
        )
        error = ErrorInfo(code=ErrorCode.CANCELLED, message=reason)
        text = failure_statement(TaskOutcome.CANCELLED, error, report)
        task.result = TaskResult(
            status=TaskStatus.CANCELLED, summary=f"{reason}\n{text}", error=error,
            outcome=TaskOutcome.CANCELLED, answer_status=AnswerStatus.WITHHELD, answer=text,
            report=report, usage=task.usage,
        )  # fmt: skip
        task.status = transition(task.status, TaskStatus.CANCELLED)
        task.finished_at = task.updated_at = utcnow()
        task.paused = False
        task.interruption = None
        await self.deps.repo.save_task(task)
        await self.deps.bus.publish(
            task.id, EventType.TASK_CANCELLED, reason,
            {"result": task.result.model_dump(mode="json")},
            Correlation(session_id=task.session_id, task_id=task.id),
        )  # fmt: skip
        self._forget_interrupted(task.id)
        return task

    def _forget_interrupted(self, task_id: str) -> None:
        for k in [k for k, v in self._interrupted_by_text.items() if v == task_id]:
            del self._interrupted_by_text[k]

    # ------------------------------------------------------------------ control
    def _handle(self, task_id: str) -> TaskHandle:
        h = self._handles.get(task_id)
        if h is None:
            raise NotFoundError(f"Task {task_id} is not running")
        return h

    async def cancel(self, task_id: str, reason: str = "Cancelled by user") -> None:
        h = self._handles.get(task_id)
        if h is None:
            stored = await self.deps.repo.get_task(task_id)
            if stored is None or stored.status.is_terminal:
                raise NotFoundError(f"Task {task_id} is not running")
            stored.cancel_requested = True  # unfinished but not executing (interrupted)
            await self._finish_idle(stored, reason)
            return
        await self._record_cancel(task_id)
        h.token.cancel(reason)
        h.pause.resume()  # let a paused loop observe the cancellation
        self.deps.approvals.cancel_task(task_id)

    async def pause(self, task_id: str) -> None:
        """pause_requested -> (task reaches the gate) status PAUSED -> pause_completed.

        An in-flight tool or model call cannot be interrupted safely; it finishes, then the task
        parks at the next gate and only then becomes PAUSED. Nothing new starts in between.
        """
        h = self._handle(task_id)
        # Under the lock the task cannot park (and announce it) until the request is recorded.
        async with h.pause_lock:
            if h.pause.paused:
                return  # already requested / paused: idempotent
            h.pause.pause()
            task = self._live.get(task_id)
            await self.deps.bus.publish(
                task_id,
                EventType.PAUSE_REQUESTED,
                "Pause requested: no further tool or model call will start",
                {"status": task.status.value if task else None},
                Correlation(session_id=task.session_id if task else None, task_id=task_id),
            )
            if task is not None and not task.status.is_terminal:
                task.paused = True
                await self.deps.repo.save_task(task)

    async def resume(self, task_id: str) -> None:
        """resume_requested -> status restored (persisted) -> gate released -> resume_completed."""
        h = self._handle(task_id)
        if not h.pause.paused:
            return
        task = self._live.get(task_id)
        corr = Correlation(session_id=task.session_id if task else None, task_id=task_id)
        await self.deps.bus.publish(
            task_id, EventType.RESUME_REQUESTED, "Resume requested", {}, corr
        )
        status = None
        if h.run_state is not None:
            status = await h.run_state.leave_pause()  # state first: never RUNNING-while-parked
        elif task is not None and not task.status.is_terminal:
            task.paused = False
            await self.deps.repo.save_task(task)
            status = task.status
        h.meter.usage.resume_count += 1
        h.pause.resume()  # only now may the task continue
        await self.deps.bus.publish(
            task_id,
            EventType.RESUME_COMPLETED,
            "Resumed",
            {"status": status.value if status else None},
            corr,
        )

    async def stop_all(self, reason: str = "STOP AGENT pressed") -> list[str]:
        """Emergency stop: block all tool execution, cancel every task, deny every pending approval."""
        self.kill_switch.engage(reason)  # from here on no tool executes, whatever else happens
        stopped = list(self._handles)
        for tid, h in list(self._handles.items()):
            await self._record_cancel(tid)
            h.token.cancel(reason)
            h.pause.resume()
            self.deps.approvals.cancel_task(tid)
            await self.deps.bus.publish(tid, EventType.KILL_SWITCH, reason, {"engaged": True})
        log.warning("emergency stop engaged", extra={"reason": reason, "tasks": stopped})
        return stopped

    async def _record_cancel(self, task_id: str) -> None:
        """Persist the user's decision to stop BEFORE acting on it: if the process dies right after,
        startup finds the task cancelled, not interrupted, and never offers to resume it."""
        task = self._live.get(task_id)
        if task is None or task.status.is_terminal or task.cancel_requested:
            return
        task.cancel_requested = True
        try:
            await self.deps.repo.save_task(task)
        except Exception:  # the in-memory cancellation below still takes effect
            log.exception("could not persist the cancel request", extra={"task_id": task_id})

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

    # ------------------------------------------------------------------ restart recovery
    async def recover_interrupted(self) -> int:
        """Startup: find tasks the previous process left unfinished and classify them.

        Nothing is executed or resumed here. A task the user had cancelled (or stopped) is finished
        as CANCELLED; every other one becomes INTERRUPTED and waits for the user to resume, restart
        or cancel it. Its in-flight action, if any, is reconciled against the observable state.
        """
        repo = self.deps.repo
        for stub in reversed(await repo.list_tasks(limit=512)):  # newest last, so it wins
            if stub.idempotency_key:
                self._by_key[stub.idempotency_key] = stub.id
        n = 0
        for status in [s for s in TaskStatus if s not in TERMINAL_STATUSES]:
            for stub in await repo.list_tasks(limit=500, status=status):
                if stub.id in self._handles:
                    continue  # running in THIS process
                task = await repo.get_task(stub.id)
                if task is None:
                    continue
                await self._void_pending_approvals(task.id)
                if task.cancel_requested:
                    await self._finish_idle(
                        task, "Stopped by the user before the service restarted"
                    )
                    continue
                first = task.status is not TaskStatus.INTERRUPTED
                task.interruption = await assess_interruption(
                    task, repo, self.deps.registry, self.deps.services
                )
                task.status = transition(task.status, TaskStatus.INTERRUPTED)
                task.paused = False
                task.updated_at = utcnow()
                await repo.save_task(task)
                self._interrupted_by_text[" ".join(task.instruction.split()).lower()] = task.id
                if first:
                    found = task.interruption
                    payload = redact(found.model_dump(mode="json"))
                    await repo.save_agent_action(
                        AgentActionRecord(
                            task_id=task.id, step_id=found.interrupted_step, kind="interruption",
                            payload=payload,
                        )
                    )  # fmt: skip
                    await self.deps.bus.publish(
                        task.id, EventType.TASK_INTERRUPTED,
                        "Interrupted by a service restart. Nothing runs until you choose to resume, "
                        "restart or cancel. Actions already performed were not undone.",
                        {"status": TaskStatus.INTERRUPTED.value, "interruption": payload},
                        Correlation(session_id=task.session_id, task_id=task.id),
                    )  # fmt: skip
                n += 1
        return n

    async def _void_pending_approvals(self, task_id: str) -> None:
        """An approval is bound to the process that asked for it: after a restart it is void and
        the action is asked again (with its exact arguments) if the task continues."""
        for req in await self.deps.repo.list_approvals(task_id=task_id, pending_only=True):
            req.status = ApprovalStatus.EXPIRED
            req.decided_at = utcnow()
            req.decided_by = "system"
            req.decision_note = "The service restarted before a decision; asked again on resume"
            await self.deps.repo.save_approval(req)

    async def interruption(self, task_id: str) -> Task:
        """The interrupted task with a fresh assessment (the files may have changed meanwhile)."""
        task = await self._interrupted(task_id)
        task.interruption = await assess_interruption(
            task, self.deps.repo, self.deps.registry, self.deps.services
        )
        return task

    async def _interrupted(self, task_id: str) -> Task:
        task = await self.deps.repo.get_task(task_id)
        if task is None:
            raise NotFoundError(f"Task {task_id} not found")
        if task.status is not TaskStatus.INTERRUPTED or task_id in self._handles:
            raise AgentError(
                f"Task is {task.status.value}; only an interrupted task can be recovered",
                code=ErrorCode.INVALID_INPUT,
            )
        return task

    async def resume_interrupted(self, task_id: str) -> Task:
        """Continue an interrupted task from its persisted state (the user's explicit choice).

        Idempotent: a second request while the first is being started or is running returns the same
        task. The resumed run goes through the same router, permission engine, approvals, limits,
        loop guard and STOP as any task; task-scoped approvals from before the restart do not apply.
        """
        if self._closing:
            raise AgentError("The service is shutting down", code=ErrorCode.INTERNAL)
        if self.kill_switch.engaged:
            raise AgentError(
                "The emergency stop is engaged. Release it before resuming a task.", code=ErrorCode.KILL_SWITCH
            )  # fmt: skip
        live = self._live.get(task_id)
        if live is not None:
            return live
        if task_id in self._resuming:
            raise AgentError("This task is already being resumed", code=ErrorCode.INVALID_INPUT)
        self._resuming.add(task_id)  # before the first await: concurrent requests collapse
        try:
            task = await self._interrupted(task_id)
            if task.cancel_requested:
                raise AgentError("This task was cancelled", code=ErrorCode.INVALID_INPUT)
            if task.durable is None and task.plan is not None:
                raise AgentError(
                    "This task cannot be resumed (its step outputs were not kept). Restart or cancel it.",
                    code=ErrorCode.INVALID_INPUT,
                )
            self._live[task.id] = task
            self._active_by_text[" ".join(task.instruction.split()).lower()] = task.id
            self._forget_interrupted(task.id)
            handle = self._new_handle(task, keep_usage=True)
            self._tasks[task.id] = asyncio.create_task(
                self._run(task, handle, resume=True), name=f"task-{task.id}"
            )
            return task
        finally:
            self._resuming.discard(task_id)

    async def restart_interrupted(self, task_id: str) -> Task:
        """End the interrupted task and run the same request again as a NEW task, from the start.

        Explicit by design: what the interrupted task already did stays in place and the new task
        does not know about it, so it may find files already moved or created.
        """
        if self._closing:
            raise AgentError("The service is shutting down", code=ErrorCode.INTERNAL)
        if self.kill_switch.engaged:
            raise AgentError(
                "The emergency stop is engaged. Release it before starting new tasks.", code=ErrorCode.KILL_SWITCH
            )  # fmt: skip
        old = await self._interrupted(task_id)
        old.cancel_requested = True
        await self._finish_idle(old, "Restarted as a new task at the user's request")
        return await self.submit(
            old.instruction, session_id=old.session_id, user_id=old.user_id, restarted_from=old.id
        )

    async def shutdown(self) -> None:
        self._closing = True
        for h in self._handles.values():
            # Not a cancellation: the tasks are left unfinished and are offered for resume at startup.
            h.token.suspend("service shutdown")
            h.pause.resume()
        for t in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(t, timeout=10)
