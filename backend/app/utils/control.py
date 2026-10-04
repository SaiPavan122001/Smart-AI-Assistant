"""Cancellation, pause and emergency-stop primitives (explicit objects, no globals)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.utils.errors import ErrorCode, TaskCancelled

T = TypeVar("T")


class KillSwitch:
    """Process-wide emergency stop. While engaged, no tool may execute."""

    def __init__(self) -> None:
        self._engaged = False
        self._reason = ""

    @property
    def engaged(self) -> bool:
        return self._engaged

    @property
    def reason(self) -> str:
        return self._reason

    def engage(self, reason: str = "STOP AGENT") -> None:
        self._engaged = True
        self._reason = reason

    def release(self) -> None:
        self._engaged = False
        self._reason = ""


class CancelToken:
    """Cooperative cancellation for one task; also trips when the kill switch engages."""

    def __init__(self, kill_switch: KillSwitch | None = None) -> None:
        self._event = asyncio.Event()
        self._reason = ""
        self._kill = kill_switch

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or bool(self._kill and self._kill.engaged)

    @property
    def reason(self) -> str:
        if self._event.is_set():
            return self._reason
        if self._kill and self._kill.engaged:
            return self._kill.reason
        return ""

    def cancel(self, reason: str = "cancelled") -> None:
        if not self._event.is_set():
            self._reason = reason
            self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise TaskCancelled(self.reason or "cancelled", code=ErrorCode.CANCELLED)

    async def run(self, awaitable: Awaitable[T]) -> T:
        """Await ``awaitable`` but abort promptly (cancelling it) if the token trips."""
        self.raise_if_cancelled()
        work = asyncio.ensure_future(awaitable)
        waiter = asyncio.ensure_future(self._wait_cancelled())
        try:
            done, _ = await asyncio.wait({work, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                return work.result()
            work.cancel()
            try:
                await work
            except (asyncio.CancelledError, Exception):
                pass
            raise TaskCancelled(self.reason or "cancelled", code=ErrorCode.CANCELLED)
        finally:
            waiter.cancel()
            if not work.done():
                work.cancel()

    async def _wait_cancelled(self) -> None:
        # The kill switch is a plain flag, so poll it cheaply alongside the event.
        while not self.cancelled:
            try:
                await asyncio.wait_for(self._event.wait(), timeout=0.05)
            except TimeoutError:
                continue


class PauseGate:
    """Pause/resume gate checked before every step and tool call."""

    def __init__(self) -> None:
        self._open = asyncio.Event()
        self._open.set()
        # Called when a task actually reaches the gate while it is closed (it "parks"). The owner
        # uses it to move the task into the PAUSED state before anything waits.
        self.on_park: Callable[[], Awaitable[None]] | None = None

    @property
    def paused(self) -> bool:
        return not self._open.is_set()

    def pause(self) -> None:
        self._open.clear()

    def resume(self) -> None:
        self._open.set()

    async def wait_if_paused(self, token: CancelToken | None = None) -> None:
        if self._open.is_set():
            return
        if token is not None:
            token.raise_if_cancelled()
        if self.on_park is not None:
            await self.on_park()
        while not self._open.is_set():
            if token is not None:
                token.raise_if_cancelled()
            try:
                await asyncio.wait_for(self._open.wait(), timeout=0.1)
            except TimeoutError:
                continue
