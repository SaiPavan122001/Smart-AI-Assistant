"""WebSocket event streaming with replay (reconnect with ?after_seq=N)."""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.deps import origin_allowed, verify_token, ws_token

router = APIRouter()

_POLICY_VIOLATION = 1008


async def _authorise(ws: WebSocket) -> bool:
    c = ws.app.state.container
    if not origin_allowed(ws.headers.get("origin"), c.settings.security.allowed_origins):
        await ws.close(code=_POLICY_VIOLATION)
        return False
    token, subprotocol = ws_token(ws)
    if not verify_token(token, c.api_token):
        await ws.close(code=_POLICY_VIOLATION)
        return False
    await ws.accept(subprotocol=subprotocol)
    return True


async def _pump(ws: WebSocket, task_id: str | None, after_seq: int) -> None:
    c = ws.app.state.container
    sub = c.bus.subscribe(task_id)  # subscribe BEFORE replay so nothing is missed in between
    try:
        last = after_seq
        if task_id is not None:
            for ev in await c.repo.list_events(task_id, after_seq=after_seq, limit=5000):
                await ws.send_json(ev.model_dump(mode="json"))
                last = ev.seq
        async for ev in sub:
            if task_id is not None and ev.seq <= last:
                continue  # already delivered via replay
            await ws.send_json(ev.model_dump(mode="json"))
            if task_id is not None:
                last = ev.seq
        if sub.overflowed:
            await ws.close(code=1013)  # try again later: the client reconnects and replays by seq
    finally:
        sub.unsubscribe()


async def _serve(ws: WebSocket, task_id: str | None, after_seq: int) -> None:
    if not await _authorise(ws):
        return
    pump = asyncio.create_task(_pump(ws, task_id, after_seq))
    try:
        while True:  # we only read to notice disconnects (and answer pings)
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump


@router.websocket("/ws/tasks/{task_id}")
async def ws_task(ws: WebSocket, task_id: str, after_seq: int = 0) -> None:
    await _serve(ws, task_id, max(0, after_seq))


@router.websocket("/ws/events")
async def ws_all(ws: WebSocket) -> None:
    await _serve(ws, None, 0)
