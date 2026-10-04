"""In-memory repository.

Used by unit tests and for explicitly ephemeral runs (``SAA_DATABASE__URL=memory://``).
Nothing is persisted across restarts; production deployments use PostgreSQL.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.models.approval import ApprovalRequest
from app.models.enums import ApprovalStatus, TaskStatus
from app.models.events import ExecutionEvent
from app.models.observation import Observation
from app.models.task import Task
from app.models.verification import VerificationResult
from app.state.repository import (
    AgentActionRecord,
    LogEntry,
    PermissionDecisionRecord,
    ToolCallRecord,
)

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class InMemoryRepository:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._events: dict[str, list[ExecutionEvent]] = defaultdict(list)
        self._tool_calls: dict[str, dict[str, ToolCallRecord]] = defaultdict(dict)
        self._observations: dict[str, list[Observation]] = defaultdict(list)
        self._approvals: dict[str, ApprovalRequest] = {}
        self._verifications: dict[str, list[VerificationResult]] = defaultdict(list)
        self._actions: dict[str, list[AgentActionRecord]] = defaultdict(list)
        self._logs: dict[str, list[LogEntry]] = defaultdict(list)
        self._decisions: dict[str, list[PermissionDecisionRecord]] = defaultdict(list)
        self._policy: dict[str, Any] | None = None
        self._sessions: dict[str, dict[str, Any]] = {}

    async def ensure_session(self, session_id: str, user_id: str, meta: dict[str, Any]) -> None:
        self._sessions.setdefault(session_id, {"user_id": user_id, **meta})

    async def save_task(self, task: Task) -> None:
        self._tasks[task.id] = task.model_copy(deep=True)

    async def get_task(self, task_id: str) -> Task | None:
        t = self._tasks.get(task_id)
        return t.model_copy(deep=True) if t else None

    async def list_tasks(
        self, *, limit: int = 50, offset: int = 0, status: TaskStatus | None = None
    ) -> list[Task]:
        items = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
        if status:
            items = [t for t in items if t.status == status]
        return [t.model_copy(deep=True) for t in items[offset : offset + limit]]

    async def append_event(self, event: ExecutionEvent) -> None:
        self._events[event.task_id].append(event)

    async def list_events(
        self, task_id: str, *, after_seq: int = 0, limit: int = 2000
    ) -> list[ExecutionEvent]:
        return [e for e in self._events[task_id] if e.seq > after_seq][:limit]

    async def save_tool_call(self, task_id: str, record: ToolCallRecord) -> None:
        self._tool_calls[task_id][record.call.id] = record

    async def list_tool_calls(self, task_id: str) -> list[ToolCallRecord]:
        return list(self._tool_calls[task_id].values())

    async def save_observation(self, obs: Observation) -> None:
        if obs.task_id:
            self._observations[obs.task_id].append(obs)

    async def list_observations(self, task_id: str) -> list[Observation]:
        return list(self._observations[task_id])

    async def save_approval(self, req: ApprovalRequest) -> None:
        self._approvals[req.id] = req.model_copy(deep=True)

    async def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        return self._approvals.get(approval_id)

    async def list_approvals(
        self, *, task_id: str | None = None, pending_only: bool = False
    ) -> list[ApprovalRequest]:
        items = list(self._approvals.values())
        if task_id:
            items = [a for a in items if a.task_id == task_id]
        if pending_only:
            items = [a for a in items if a.status == ApprovalStatus.PENDING]
        return sorted(items, key=lambda a: a.created_at)

    async def save_verification(self, result: VerificationResult) -> None:
        if result.task_id:
            self._verifications[result.task_id].append(result)

    async def list_verifications(self, task_id: str) -> list[VerificationResult]:
        return list(self._verifications[task_id])

    async def save_agent_action(self, record: AgentActionRecord) -> None:
        self._actions[record.task_id].append(record)

    async def list_agent_actions(self, task_id: str) -> list[AgentActionRecord]:
        return list(self._actions[task_id])

    async def add_log(self, entry: LogEntry) -> None:
        if entry.task_id:
            self._logs[entry.task_id].append(entry)

    async def list_logs(
        self, task_id: str, *, limit: int = 1000, min_level: str | None = None
    ) -> list[LogEntry]:
        floor = _LEVELS.get((min_level or "DEBUG").upper(), 10)
        return [e for e in self._logs[task_id] if _LEVELS.get(e.level, 20) >= floor][:limit]

    async def record_permission_decision(self, rec: PermissionDecisionRecord) -> None:
        if rec.task_id:
            self._decisions[rec.task_id].append(rec)

    async def list_permission_decisions(self, task_id: str) -> list[PermissionDecisionRecord]:
        return list(self._decisions[task_id])

    async def load_policy(self) -> dict[str, Any] | None:
        return self._policy

    async def save_policy(self, policy: dict[str, Any]) -> None:
        self._policy = policy

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None
