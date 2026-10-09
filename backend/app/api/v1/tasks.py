"""Task endpoints: create, inspect, control, history, events, logs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.api.deps import AuthDep, ContainerDep
from app.api.schemas import CreateTaskRequest, TaskList, TaskSummary
from app.models.approval import ApprovalRequest
from app.models.enums import TaskStatus
from app.models.events import ExecutionEvent
from app.models.task import Task
from app.models.verification import VerificationResult
from app.state.repository import (
    AgentActionRecord,
    LogEntry,
    PermissionDecisionRecord,
    ToolCallRecord,
)
from app.utils.errors import AgentError, ErrorCode, NotFoundError

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[AuthDep])


async def _load(c: ContainerDep, task_id: str) -> Task:
    task = await c.repo.get_task(task_id)
    if task is None:
        raise NotFoundError(f"Task {task_id} not found")
    return task


def _require_active(task: Task) -> None:
    if task.status.is_terminal:
        raise AgentError(f"Task already {task.status.value}", code=ErrorCode.INVALID_INPUT)


@router.post("", response_model=TaskSummary, status_code=status.HTTP_201_CREATED)
async def create_task(body: CreateTaskRequest, request: Request, c: ContainerDep) -> TaskSummary:
    task = await c.runtime.submit(
        body.instruction,
        session_id=request.headers.get("x-session-id"),
        request_id=getattr(request.state, "request_id", None),
        idempotency_key=request.headers.get("idempotency-key"),
    )
    return TaskSummary.from_task(task)


@router.get("", response_model=TaskList)
async def list_tasks(
    c: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None,
) -> TaskList:
    tasks = await c.repo.list_tasks(limit=limit, offset=offset, status=status_filter)
    return TaskList(items=[TaskSummary.from_task(t) for t in tasks], limit=limit, offset=offset)


@router.get("/{task_id}", response_model=Task)
async def get_task(task_id: str, c: ContainerDep) -> Task:
    return await _load(c, task_id)


@router.post("/{task_id}/cancel", response_model=TaskSummary, status_code=status.HTTP_202_ACCEPTED)
async def cancel_task(task_id: str, c: ContainerDep) -> TaskSummary:
    task = await _load(c, task_id)
    _require_active(task)
    await c.runtime.cancel(task_id)
    return TaskSummary.from_task(task)


@router.post("/{task_id}/pause", response_model=TaskSummary)
async def pause_task(task_id: str, c: ContainerDep) -> TaskSummary:
    _require_active(await _load(c, task_id))
    await c.runtime.pause(task_id)
    return TaskSummary.from_task(await _load(c, task_id))


@router.post("/{task_id}/resume", response_model=TaskSummary)
async def resume_task(task_id: str, c: ContainerDep) -> TaskSummary:
    _require_active(await _load(c, task_id))
    await c.runtime.resume(task_id)
    return TaskSummary.from_task(await _load(c, task_id))


@router.get("/{task_id}/events", response_model=list[ExecutionEvent])
async def task_events(
    task_id: str,
    c: ContainerDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> list[ExecutionEvent]:
    await _load(c, task_id)
    return await c.repo.list_events(task_id, after_seq=after_seq, limit=limit)


@router.get("/{task_id}/logs", response_model=list[LogEntry])
async def task_logs(
    task_id: str,
    c: ContainerDep,
    level: Annotated[str, Query(pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")] = "INFO",
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> list[LogEntry]:
    await _load(c, task_id)
    return await c.repo.list_logs(task_id, limit=limit, min_level=level)


@router.get("/{task_id}/tool-calls", response_model=list[ToolCallRecord])
async def task_tool_calls(task_id: str, c: ContainerDep) -> list[ToolCallRecord]:
    await _load(c, task_id)
    return await c.repo.list_tool_calls(task_id)


@router.get("/{task_id}/approvals", response_model=list[ApprovalRequest])
async def task_approvals(task_id: str, c: ContainerDep) -> list[ApprovalRequest]:
    await _load(c, task_id)
    return await c.repo.list_approvals(task_id=task_id)


@router.get("/{task_id}/verifications", response_model=list[VerificationResult])
async def task_verifications(task_id: str, c: ContainerDep) -> list[VerificationResult]:
    await _load(c, task_id)
    return await c.repo.list_verifications(task_id)


@router.get("/{task_id}/permission-decisions", response_model=list[PermissionDecisionRecord])
async def task_permission_decisions(
    task_id: str, c: ContainerDep
) -> list[PermissionDecisionRecord]:
    await _load(c, task_id)
    return await c.repo.list_permission_decisions(task_id)


@router.get("/{task_id}/agent-actions", response_model=list[AgentActionRecord])
async def task_agent_actions(task_id: str, c: ContainerDep) -> list[AgentActionRecord]:
    await _load(c, task_id)
    return await c.repo.list_agent_actions(task_id)


@router.get("/{task_id}/checkpoints", response_model=list[AgentActionRecord])
async def task_checkpoints(task_id: str, c: ContainerDep) -> list[AgentActionRecord]:
    """Progress snapshots taken at step boundaries (what is done, what remains, what recovery did).

    Informational only: a task is not resumed from a checkpoint after a process restart.
    """
    await _load(c, task_id)
    return [a for a in await c.repo.list_agent_actions(task_id) if a.kind == "checkpoint"]
