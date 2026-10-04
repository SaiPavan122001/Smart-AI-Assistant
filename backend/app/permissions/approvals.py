"""Approval workflow: create a specific request, wait for the human, record the decision."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

from app.models.approval import ApprovalDecision, ApprovalRequest
from app.models.enums import ApprovalScope, ApprovalStatus
from app.utils.control import CancelToken
from app.utils.errors import AgentError, ErrorCode, NotFoundError, TaskCancelled
from app.utils.ids import utcnow

Notifier = Callable[[ApprovalRequest], Awaitable[None]]


class ApprovalManager:
    def __init__(self, *, timeout_s: float = 600.0, on_requested: Notifier | None = None,
                 on_resolved: Notifier | None = None) -> None:  # fmt: skip
        self._timeout_s = timeout_s
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalDecision]]] = {}
        self._history: dict[str, ApprovalRequest] = {}
        self._on_requested = on_requested
        self._on_resolved = on_resolved

    def bind(
        self, *, on_requested: Notifier | None = None, on_resolved: Notifier | None = None
    ) -> None:
        self._on_requested = on_requested or self._on_requested
        self._on_resolved = on_resolved or self._on_resolved

    # ------------------------------------------------------------------ agent side
    async def request(self, req: ApprovalRequest, token: CancelToken) -> ApprovalRequest:
        """Block until the request is decided, expires, or the task is cancelled."""
        token.raise_if_cancelled()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ApprovalDecision] = loop.create_future()
        req.expires_at = utcnow() + timedelta(seconds=self._timeout_s)
        self._pending[req.id] = (req, fut)
        self._history[req.id] = req
        try:
            if self._on_requested:
                await self._on_requested(req)
            try:
                decision = await token.run(asyncio.wait_for(fut, timeout=self._timeout_s))
            except TimeoutError:
                self._finish(
                    req, ApprovalStatus.EXPIRED, "No decision before the approval timed out"
                )
                await self._notify_resolved(req)
                return req
            except TaskCancelled:
                self._finish(req, ApprovalStatus.CANCELLED, "Task was cancelled while waiting")
                await self._notify_resolved(req)
                raise
            # resolve() already updated the request object
            _ = decision
            return req
        finally:
            self._pending.pop(req.id, None)

    # ------------------------------------------------------------------ human side
    async def resolve(self, approval_id: str, decision: ApprovalDecision) -> ApprovalRequest:
        entry = self._pending.get(approval_id)
        if entry is None:
            if approval_id in self._history:
                raise AgentError(
                    f"Approval {approval_id} is already {self._history[approval_id].status.value}",
                    code=ErrorCode.INVALID_INPUT,
                )
            raise NotFoundError(f"Approval {approval_id} not found")
        req, fut = entry
        scope = decision.scope if decision.scope in req.allowed_scopes else ApprovalScope.ONCE
        status = ApprovalStatus.APPROVED if decision.approved else ApprovalStatus.DENIED
        self._finish(req, status, decision.note, decided_by=decision.decided_by,
                     scope=scope if decision.approved else None)  # fmt: skip
        if not fut.done():
            fut.set_result(decision)
        await self._notify_resolved(req)
        return req

    def cancel_task(self, task_id: str) -> int:
        """Deny every pending approval of a task (used by cancel / emergency stop)."""
        n = 0
        for req, fut in list(self._pending.values()):
            if req.task_id == task_id and not fut.done():
                self._finish(req, ApprovalStatus.CANCELLED, "Task cancelled")
                fut.set_result(
                    ApprovalDecision(approved=False, note="Task cancelled", decided_by="system")
                )
                n += 1
        return n

    def pending(self, task_id: str | None = None) -> list[ApprovalRequest]:
        return [r for r, _ in self._pending.values() if task_id is None or r.task_id == task_id]

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self._history.get(approval_id)

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _finish(
        req: ApprovalRequest, status: ApprovalStatus, note: str | None, *,
        decided_by: str = "system", scope: ApprovalScope | None = None,
    ) -> None:  # fmt: skip
        req.status = status
        req.decided_at = utcnow()
        req.decided_by = decided_by
        req.decision_note = note
        req.scope_granted = scope

    async def _notify_resolved(self, req: ApprovalRequest) -> None:
        if self._on_resolved:
            await self._on_resolved(req)
