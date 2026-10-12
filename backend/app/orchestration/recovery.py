"""Restart recovery: what an unfinished task looks like after the process stopped, and what a
resumed task may reuse.

Nothing here executes an action. ``assess_interruption`` only reads the persisted records and the
observable state (through each tool's read-only ``reconcile``) and classifies every action that had
started but has no recorded result:

* ``SAFE_TO_RETRY``      the tool is declared idempotent: repeating it leaves the same state
* ``CONFIRMED_DONE``     the state shows it happened: its result is replayed, it is not run again
* ``CONFIRMED_NOT_DONE`` the state shows it did not happen: it runs again through the normal checks
* ``UNCERTAIN``          anything else: it is never repeated without the user's approval

There is no exactly-once guarantee for arbitrary side effects. A process can die after an effect and
before its result is written; the write-ahead record makes that window *visible*, not impossible.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.agent.answer import is_superseded
from app.models.enums import Capability, StepStatus, TaskStatus
from app.models.recovery import ActionCertainty, InFlightAction, Interruption, RecoveryOption
from app.models.task import Task
from app.models.tool import ToolResult
from app.observability.logging import get_logger
from app.state.repository import Repository, ToolCallRecord
from app.tools.context import ExecutionContext, ToolServices
from app.tools.registry import ToolRegistry
from app.tools.resume import ResumeLedger
from app.tools.router import IN_FLIGHT
from app.utils.errors import ErrorCode, ErrorInfo
from app.utils.ids import utcnow
from app.utils.redact import MASK

log = get_logger("recovery")

_OPEN_STEP = (StepStatus.PENDING, StepStatus.RUNNING, StepStatus.WAITING_APPROVAL)
REPLAYED = "replayed"


def is_in_flight(record: ToolCallRecord) -> bool:
    return record.result is None and record.decision == IN_FLIGHT


def classify(
    record: ToolCallRecord, registry: ToolRegistry, services: ToolServices
) -> InFlightAction:
    """How certain is the outcome of an action that had started when the process stopped?"""
    call = record.call
    base: dict[str, Any] = {
        "tool_call_id": call.id, "tool": call.tool, "step_id": call.step_id,
        "fingerprint": call.fingerprint, "arguments": call.args,
    }  # fmt: skip

    def result(certainty: ActionCertainty, detail: str, **extra: Any) -> InFlightAction:
        return InFlightAction(**base, certainty=certainty, detail=detail, **extra)

    tool = registry.get(call.tool)
    if tool is None:
        return result(ActionCertainty.UNCERTAIN, "the tool is no longer available")
    if tool.read_only or tool.idempotent:
        return result(
            ActionCertainty.SAFE_TO_RETRY,
            "declared idempotent: running it again leaves the same state",
        )
    stored = json.dumps(call.args, default=str)
    if MASK in stored or "***MASKED***" in stored:
        return result(
            ActionCertainty.UNCERTAIN,
            "its arguments contained sensitive values that were not stored, so the state cannot be checked",
        )
    try:
        args = tool.Input.model_validate(call.args)
        found = tool.reconcile(args, services)
    except ValidationError:
        return result(ActionCertainty.UNCERTAIN, "its recorded arguments can no longer be read")
    except Exception:  # a reconcile hook must never break startup
        log.exception("reconcile failed", extra={"tool": call.tool})
        return result(ActionCertainty.UNCERTAIN, "the current state could not be inspected")
    if found.done is True:
        out = found.output
        return result(
            ActionCertainty.CONFIRMED_DONE, found.detail,
            data=out.data if out else {}, artifacts=out.artifacts if out else {},
        )  # fmt: skip
    if found.done is False:
        return result(ActionCertainty.CONFIRMED_NOT_DONE, found.detail)
    return result(
        ActionCertainty.UNCERTAIN,
        found.detail or "it cannot be determined from the current state whether it took effect",
    )


async def assess_interruption(
    task: Task, repo: Repository, registry: ToolRegistry, services: ToolServices,
    *, reason: str = "The service stopped while this task was not finished",
) -> Interruption:  # fmt: skip
    """Describe an unfinished task. Read-only; safe to call again (state may have changed since)."""
    records = await repo.list_tool_calls(task.id)
    previous = task.interruption
    in_flight = [classify(r, registry, services) for r in records if is_in_flight(r)]
    steps = [s for s in (task.plan.steps if task.plan else []) if not is_superseded(s)]
    running = [s.id for s in steps if s.status in (StepStatus.RUNNING, StepStatus.WAITING_APPROVAL)]
    uncertain = [a for a in in_flight if a.certainty is ActionCertainty.UNCERTAIN]
    status = previous.status_at_interruption if previous else task.status
    notes: list[str] = []
    resumable = True
    if task.plan is None:
        notes.append("It stopped before a plan existed: nothing had been done on the computer.")
    elif task.durable is None:
        resumable = False
        notes.append(
            "This task was stored before restart recovery existed, so the outputs of its completed "
            "steps were not kept. It can be restarted or cancelled, not resumed."
        )
    if status is TaskStatus.WAITING_APPROVAL or any(
        s.status is StepStatus.WAITING_APPROVAL for s in steps
    ):
        notes.append(
            "It was waiting for an approval. That request is void; the action is asked again if the "
            "task is resumed."
        )
    for a in in_flight:
        label = {
            ActionCertainty.SAFE_TO_RETRY: "can be repeated safely",
            ActionCertainty.CONFIRMED_DONE: "was completed and will not be repeated",
            ActionCertainty.CONFIRMED_NOT_DONE: "did not happen and will be run again",
            ActionCertainty.UNCERTAIN: "may or may not have happened; it will need your approval before it is repeated",
        }[a.certainty]  # fmt: skip
        notes.append(f"{a.tool} was in progress: {label} ({a.detail}).")
    if any(_capability(registry, r.call.tool) is Capability.BROWSER for r in records):
        notes.append(
            "The browser session did not survive: pages are opened again if the task is resumed."
        )
    if any(_capability(registry, r.call.tool) is Capability.DESKTOP for r in records):
        notes.append(
            "Desktop windows may have changed since the interruption; they are looked up again."
        )
    notes.append("Actions completed before the interruption were not undone.")
    options = [RecoveryOption.RESTART, RecoveryOption.CANCEL, RecoveryOption.INSPECT]
    if resumable:
        options.insert(0, RecoveryOption.RESUME)
    return Interruption(
        at=previous.at if previous else utcnow(),
        reason=previous.reason if previous else reason,
        status_at_interruption=status,
        was_paused=previous.was_paused if previous else (task.paused or status is TaskStatus.PAUSED),
        awaiting_approval=status is TaskStatus.WAITING_APPROVAL,
        interrupted_step=running[0] if running else task.current_step_id if in_flight else None,
        in_flight=in_flight,
        completed_steps=[s.id for s in steps if s.status is StepStatus.SUCCEEDED],
        remaining_steps=[s.id for s in steps if s.status in _OPEN_STEP],
        resumable=resumable,
        needs_attention=bool(uncertain),
        notes=notes,
        options=options,
    )  # fmt: skip


def _capability(registry: ToolRegistry, name: str) -> Capability | None:
    tool = registry.get(name)
    return tool.capability if tool else None


def build_ledger(
    task: Task, records: list[ToolCallRecord], interruption: Interruption, registry: ToolRegistry
) -> ResumeLedger:
    """Which actions of the unfinished step(s) must not run again, and which need the user first."""
    ledger = ResumeLedger()
    open_steps = {
        s.id for s in (task.plan.steps if task.plan else []) if s.status in _OPEN_STEP
    }  # fmt: skip
    for r in records:
        tool = registry.get(r.call.tool)
        if (
            tool is None or tool.read_only or not r.call.fingerprint
            or r.call.step_id not in open_steps or r.result is None or not r.result.ok
            or r.decision == REPLAYED or r.result.cached
        ):  # fmt: skip
            continue
        ledger.add_done(r.call.fingerprint, r.result)
    for a in interruption.in_flight:
        if not a.fingerprint:
            continue
        if a.certainty is ActionCertainty.CONFIRMED_DONE:
            ledger.add_done(
                a.fingerprint,
                ToolResult(
                    tool_call_id=a.tool_call_id, tool=a.tool, ok=True, data=a.data,
                    artifacts=a.artifacts, summary=f"{a.tool}: {a.detail}",
                ),
            )  # fmt: skip
        elif a.certainty is ActionCertainty.UNCERTAIN:
            ledger.uncertain[a.fingerprint] = a.detail
            ledger.uncertain_records.setdefault(a.fingerprint, []).extend(
                r for r in records if r.call.id == a.tool_call_id
            )
    return ledger


async def close_reconciled(
    repo: Repository, task_id: str, records: list[ToolCallRecord], interruption: Interruption
) -> None:
    """Give every reconciled in-flight record a result, so it is not reconsidered after another restart.

    ``UNCERTAIN`` records stay open: the uncertainty must survive until the user has decided.
    """
    verdict = {a.tool_call_id: a for a in interruption.in_flight}
    for r in records:
        a = verdict.get(r.call.id)
        if a is None or not is_in_flight(r) or a.certainty is ActionCertainty.UNCERTAIN:
            continue
        if a.certainty is ActionCertainty.CONFIRMED_DONE:
            r.result = ToolResult(
                tool_call_id=r.call.id, tool=r.call.tool, ok=True, data=a.data,
                artifacts=a.artifacts, summary=f"Confirmed after a restart: {a.detail}",
            )  # fmt: skip
            r.decision = "reconciled"
        else:
            r.result = interrupted_result(r.call.id, r.call.tool, a.detail)
            r.decision = "interrupted"
        r.decision_reason = a.detail
        await repo.save_tool_call(task_id, r)


def interrupted_result(tool_call_id: str, tool: str, detail: str) -> ToolResult:
    return ToolResult(
        tool_call_id=tool_call_id, tool=tool, ok=False,
        error=ErrorInfo(
            code=ErrorCode.INTERRUPTED,
            message=f"Interrupted by a service restart before a result was recorded: {detail}",
        ),
    )  # fmt: skip


def replay_observations(
    ctx: ExecutionContext, records: list[ToolCallRecord], registry: ToolRegistry
) -> list[str]:
    """Rebuild what the task had seen and written (and whether it was tainted) from the recorded
    results, so goal evaluation and injection protection behave as if the process had never stopped.

    Returns the names of the browser/desktop tools that had been used (their state may be gone).
    """
    ephemeral: list[str] = []
    for r in records:
        tool = registry.get(r.call.tool)
        if tool is None or r.result is None:
            continue
        if tool.capability in (Capability.BROWSER, Capability.DESKTOP) and r.result.ok:
            ephemeral.append(r.call.tool)
        if r.decision == REPLAYED:
            continue  # already counted when it really ran
        if r.result.untrusted:
            text = r.result.summary + "\n" + json.dumps(
                r.result.data or (r.result.error.details if r.result.error else {}),
                default=str, ensure_ascii=False,
            )  # fmt: skip
            ctx.guard.observe(r.call.tool, text)
        if r.result.ok:
            ctx.observed.note(
                tool=r.call.tool, read_only=tool.read_only, external=tool.returns_external_content,
                filesystem=tool.capability == Capability.FILESYSTEM, data=r.result.data,
                artifacts=r.result.artifacts,
            )  # fmt: skip
    return ephemeral


def done_before(records: list[ToolCallRecord], step_id: str, registry: ToolRegistry) -> list[str]:
    """Side-effecting calls of one step that completed before the interruption (for the model's context)."""
    lines: list[str] = []
    for r in records:
        tool = registry.get(r.call.tool)
        if (
            tool is not None and not tool.read_only and r.call.step_id == step_id
            and r.result is not None and r.result.ok and r.decision != REPLAYED
        ):  # fmt: skip
            lines.append(f"{r.call.tool}: {r.result.summary[:160]}")
    return lines[-12:]
