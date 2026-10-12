"""Rollback of a finished task's file changes (NEXT-D), started only by the user.

The journal is walked newest first. Each open entry is undone by one ``fs_undo`` call through the
``ToolRouter``, so the permission engine, the path policy, the write-ahead record, STOP and the audit
apply exactly as to any other action. Nothing here touches a file directly.

Idempotent and resumable: an entry that is already settled is not looked at again, an undo that
stopped half-way is continued from the observed state, and running the rollback again only handles
what is left. Details: ``docs/file-transactions.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from app.models.enums import EventType, TaskStatus
from app.models.events import Correlation
from app.models.journal import OPEN_STATES, JournalEntry, JournalState
from app.models.tool import ToolCall
from app.observability.usage import UsageMeter
from app.perception.cache import ObservationCache
from app.permissions.engine import TaskGrants
from app.security.injection import InjectionGuard
from app.tools.context import ExecutionContext
from app.utils.control import CancelToken, KillSwitch, PauseGate
from app.utils.errors import ErrorCode, ErrorInfo

if TYPE_CHECKING:
    from app.orchestration.runner import RunnerDeps


class JournalView(BaseModel):
    """A task's file journal and what a rollback could still do with it."""

    task_id: str
    entries: list[JournalEntry]
    counts: dict[str, int] = Field(description="number of entries per state")
    open: int = Field(description="entries a rollback would still look at")
    can_roll_back: bool
    reason: str = Field(default="", description="why a rollback is not possible now")


class RollbackResult(BaseModel):
    view: JournalView
    attempted: int
    errors: list[ErrorInfo] = Field(default_factory=list)
    stopped: str = Field(default="", description="why the rollback stopped before the end")


def journal_view(task_id: str, entries: list[JournalEntry], blocked: str = "") -> JournalView:
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.state.value] = counts.get(e.state.value, 0) + 1
    open_ = sum(1 for e in entries if e.state in OPEN_STATES)
    reason = blocked or ("" if open_ else "nothing left to undo")
    return JournalView(
        task_id=task_id, entries=entries, counts=counts, open=open_, can_roll_back=not reason,
        reason=reason,
    )  # fmt: skip


async def run_rollback(
    deps: RunnerDeps, kill_switch: KillSwitch, task_id: str, status: TaskStatus
) -> RollbackResult:
    """Undo every open entry of the task, newest first. The caller has checked that the task is
    finished and that nothing else is running."""
    journal = deps.services.journal
    assert journal is not None
    entries = await journal.entries(task_id)
    todo = sorted((e for e in entries if e.state in OPEN_STATES), key=lambda e: e.seq, reverse=True)
    limits = deps.services.settings.limits.model_copy(update={"max_actions": len(todo) + 1})

    async def emit(
        type_: EventType, message: str = "", data: dict[str, Any] | None = None, *,
        step_id: str | None = None, tool_call_id: str | None = None,
    ) -> None:  # fmt: skip
        await deps.bus.publish(
            task_id, type_, message, data, Correlation(step_id=step_id, tool_call_id=tool_call_id)
        )

    async def keep_status(
        _status: TaskStatus,
    ) -> None:  # a rollback never changes the task's status
        return None

    ctx = ExecutionContext(
        task_id=task_id, services=deps.services, token=CancelToken(kill_switch), pause=PauseGate(),
        meter=UsageMeter(limits, metrics=deps.metrics), guard=InjectionGuard(), grants=TaskGrants(),
        cache=ObservationCache(), cwd=Path(deps.services.settings.filesystem.workspace), emit=emit,
        set_status=keep_status, rationale="rollback requested by the user", extras={"rollback": True},
    )  # fmt: skip
    await emit(
        EventType.ROLLBACK_STARTED,
        f"Rollback requested: {len(todo)} change(s) to look at, newest first",
        {"open": len(todo), "task_status": status.value},
    )
    errors: list[ErrorInfo] = []
    stopped = ""
    attempted = 0
    for e in todo:
        if ctx.token.cancelled:
            stopped = ctx.token.reason or "stopped"
            break
        attempted += 1
        result = await deps.router.execute(
            ToolCall(tool="fs_undo", args={"entry_id": e.id}, rationale=ctx.rationale), ctx
        )
        if result.error is not None:
            errors.append(result.error)
            if result.error.code in (ErrorCode.CANCELLED, ErrorCode.KILL_SWITCH):
                stopped = result.error.message
                break
    after = await journal.entries(task_id)
    view = journal_view(task_id, after)
    undone = view.counts.get(JournalState.UNDONE.value, 0)
    await emit(
        EventType.ROLLBACK_COMPLETED,
        f"Rollback {'stopped' if stopped else 'finished'}: {undone} undone, "
        f"{view.counts.get(JournalState.SKIPPED.value, 0)} left as they are, "
        f"{view.counts.get(JournalState.MANUAL.value, 0)} need the user, {view.open} still open",
        {"counts": view.counts, "open": view.open, "errors": len(errors), "stopped": stopped},
    )
    return RollbackResult(view=view, attempted=attempted, errors=errors, stopped=stopped)
