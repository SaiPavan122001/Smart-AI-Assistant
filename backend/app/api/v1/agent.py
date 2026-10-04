"""Emergency controls and agent status."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import AuthDep, ContainerDep
from app.api.schemas import AgentStatus, StopResult

router = APIRouter(prefix="/agent", tags=["agent"], dependencies=[AuthDep])


@router.get("/status", response_model=AgentStatus)
async def status(c: ContainerDep) -> AgentStatus:
    return AgentStatus(
        kill_switch_engaged=c.kill_switch.engaged,
        kill_switch_reason=c.kill_switch.reason,
        active_tasks=c.runtime.active_task_ids(),
        pending_approvals=len(c.approvals.pending()),
    )


@router.post("/stop", response_model=StopResult)
async def stop_agent(c: ContainerDep) -> StopResult:
    """STOP AGENT: block all tool execution, cancel every task, deny every pending approval."""
    stopped = await c.runtime.stop_all("STOP AGENT pressed by the user")
    return StopResult(stopped_tasks=stopped, kill_switch_engaged=True)


@router.post("/release", response_model=StopResult)
async def release(c: ContainerDep) -> StopResult:
    """Clear the emergency stop so new tasks can run again."""
    c.runtime.release_kill_switch()
    return StopResult(stopped_tasks=[], kill_switch_engaged=False)
