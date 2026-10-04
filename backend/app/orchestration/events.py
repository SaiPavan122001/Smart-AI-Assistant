"""Event bus: sequencing, redaction, persistence and fan-out of ExecutionEvents."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from app.models.enums import EventType
from app.models.events import Correlation, ExecutionEvent
from app.observability.logging import get_logger
from app.state.repository import Repository
from app.utils.redact import redact

log = get_logger("events")


class Subscription:
    def __init__(self, bus: EventBus, task_id: str | None, maxsize: int = 1000) -> None:
        self._bus = bus
        self.task_id = task_id
        self.queue: asyncio.Queue[ExecutionEvent | None] = asyncio.Queue(maxsize=maxsize)
        self.overflowed = False

    def push(self, event: ExecutionEvent) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # A client that cannot keep up is dropped; it reconnects and replays by seq.
            self.overflowed = True
            self.close()

    def close(self) -> None:
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            _ = self.queue.get_nowait()
            self.queue.put_nowait(None)

    async def __aiter__(self) -> AsyncIterator[ExecutionEvent]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    def unsubscribe(self) -> None:
        self._bus._remove(self)


class EventBus:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self._seq: dict[str, int] = defaultdict(int)
        self._seeded: set[str] = set()
        self._subs: list[Subscription] = []

    async def publish(
        self,
        task_id: str,
        type_: EventType,
        message: str = "",
        data: dict[str, Any] | None = None,
        correlation: Correlation | None = None,
    ) -> ExecutionEvent:
        await self._seed(task_id)
        self._seq[task_id] += 1
        corr = (correlation or Correlation()).model_copy(update={"task_id": task_id})
        event = ExecutionEvent(
            task_id=task_id,
            seq=self._seq[task_id],
            type=type_,
            message=message,
            data=redact(data or {}),
            correlation=corr,
        )
        try:
            await self._repo.append_event(event)
        except Exception:  # persistence failure must not kill the run, but must be loud
            log.exception("failed to persist event", extra={"event_type": type_.value})
        for sub in list(self._subs):
            if sub.task_id in (None, task_id):
                sub.push(event)
        return event

    def subscribe(self, task_id: str | None = None) -> Subscription:
        sub = Subscription(self, task_id)
        self._subs.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    async def _seed(self, task_id: str) -> None:
        if task_id in self._seeded:
            return
        self._seeded.add(task_id)
        existing = await self._repo.list_events(task_id, after_seq=0, limit=100000)
        if existing:
            self._seq[task_id] = max(e.seq for e in existing)
