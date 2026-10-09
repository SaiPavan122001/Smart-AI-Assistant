"""Task-level checkpoints: a small, safe snapshot of progress taken at every step boundary.

A checkpoint answers "what is done, what is left, what did recovery and the user decide" for a task that is
still running (or has just ended). It is stored as an agent-action record and announced on the timeline.

It is NOT a resume mechanism: nothing reads a checkpoint to continue a task after a process restart. It holds
no step outputs, page text, credentials or secrets: titles and short summaries are redacted, and file paths
are the only references to state.
"""

from __future__ import annotations

import time
from typing import Any

from app.agent.answer import is_superseded
from app.agent.state import RunState
from app.models.enums import EventType, StepStatus
from app.state.repository import AgentActionRecord, Repository
from app.tools.context import ExecutionContext
from app.utils.redact import redact, redact_text

KIND = "checkpoint"


def build_checkpoint(rs: RunState, ctx: ExecutionContext, reason: str) -> dict[str, Any]:
    plan = rs.plan
    steps = [s for s in (plan.steps if plan else []) if not is_superseded(s)]
    usage = rs.handle.meter.usage
    return {
        "reason": reason,
        "task_id": rs.task.id,
        "plan_id": plan.id if plan else None,
        "plan_version": plan.version if plan else None,
        "task_status": rs.task.status.value,
        "current_step": rs.task.current_step_id,
        "steps": [
            {
                "id": s.id, "title": redact_text(s.title)[:100], "status": s.status.value,
                "attempts": s.attempts, "error": s.error.code.value if s.error else None,
                "summary": redact_text(s.summary or "")[:140],
            }
            for s in steps
        ],  # fmt: skip
        "completed": [s.id for s in steps if s.status is StepStatus.SUCCEEDED],
        "remaining": [
            s.id for s in steps if s.status in (StepStatus.PENDING, StepStatus.RUNNING, StepStatus.WAITING_APPROVAL)
        ],  # fmt: skip
        "requirements": [
            {"id": r.id, "text": r.text, "status": r.status.value} for r in rs.requirement_results
        ],
        "outstanding_requirements": [
            r.id for r in rs.requirement_results if r.status.value != "met"
        ],
        "recovery_history": [
            {"step": h.step_id, "strategy": h.strategy.value, "code": h.error_code, "phase": h.phase}
            for h in rs.history[-20:]
        ],  # fmt: skip
        "approvals": {"requested": usage.approvals, "refused_actions": len(ctx.denied)},
        "state": {
            "files_written": ctx.observed.written[-20:],
            "side_effects": ctx.observed.effects[-40:],
            "urls_seen": len(ctx.observed.urls),
        },
        "usage": {
            "llm_calls": usage.llm_calls, "tool_calls": usage.tool_calls, "replans": usage.replans,
            "recoveries": usage.recoveries, "elapsed_s": round(time.monotonic() - rs.started_monotonic, 1),
        },
    }  # fmt: skip


async def save_checkpoint(
    repo: Repository, rs: RunState, ctx: ExecutionContext, reason: str
) -> None:
    payload = redact(build_checkpoint(rs, ctx, reason))
    await repo.save_agent_action(
        AgentActionRecord(
            task_id=rs.task.id, step_id=rs.task.current_step_id, kind=KIND, payload=payload
        )
    )
    await rs.emit(
        EventType.CHECKPOINT_SAVED,
        f"Checkpoint: {len(payload['completed'])} done, {len(payload['remaining'])} remaining",
        {
            "reason": reason,
            "completed": payload["completed"],
            "remaining": payload["remaining"],
            "outstanding_requirements": payload["outstanding_requirements"],
        },
    )
