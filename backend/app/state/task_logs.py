"""Persist per-task execution logs (redacted) from the standard logging stream."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from app.observability.logging import _RESERVED, current_context
from app.state.repository import LogEntry, Repository
from app.utils.redact import redact, redact_text


class TaskLogSink(logging.Handler):
    """Logging handler that stores records carrying a ``task_id`` into ``execution_logs``."""

    def __init__(self, repo: Repository, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._repo = repo
        self._queue: asyncio.Queue[LogEntry] = asyncio.Queue(maxsize=5000)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._writer: asyncio.Task[None] | None = None
        self.dropped = 0

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._writer = asyncio.create_task(self._drain(), name="task-log-writer")
        logging.getLogger("saa").addHandler(self)

    async def stop(self) -> None:
        logging.getLogger("saa").removeHandler(self)
        if self._writer:
            await self._flush()
            self._writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer

    def emit(self, record: logging.LogRecord) -> None:
        ctx = current_context()
        task_id = ctx.get("task_id")
        if not task_id or self._loop is None:
            return
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        entry = LogEntry(
            task_id=task_id,
            level=record.levelname,
            component=record.name.removeprefix("saa."),
            message=redact_text(record.getMessage())[:2000],
            data=redact(extras),
            correlation=ctx,
        )
        try:
            self._loop.call_soon_threadsafe(self._offer, entry)
        except RuntimeError:  # loop closed during shutdown
            self.dropped += 1

    def _offer(self, entry: LogEntry) -> None:
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            self.dropped += 1

    async def _drain(self) -> None:
        while True:
            entry = await self._queue.get()
            try:
                await self._repo.add_log(entry)
            except Exception:  # never let log persistence break the agent
                self.dropped += 1

    async def _flush(self) -> None:
        for _ in range(100):
            if self._queue.empty():
                return
            await asyncio.sleep(0.02)
