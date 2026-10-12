"""FastAPI dependencies: container access and bearer-token authentication."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, WebSocket, status

from app.container import Container


def get_container(request: Request) -> Container:
    return request.app.state.container  # type: ignore[no-any-return]


ContainerDep = Annotated[Container, Depends(get_container)]


def _extract_bearer(header: str | None) -> str:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def verify_token(provided: str, expected: str) -> bool:
    return bool(provided) and secrets.compare_digest(provided.encode(), expected.encode())


async def require_auth(request: Request, container: ContainerDep) -> None:
    token = _extract_bearer(request.headers.get("authorization"))
    if not verify_token(token, container.api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


AuthDep = Depends(require_auth)


def ws_token(ws: WebSocket) -> tuple[str, str | None]:
    """Token from the ``bearer.<token>`` subprotocol. A token in the URL (``?token=``) is NOT accepted:
    URLs end up in browser history, proxy logs and crash reports."""
    offered = [
        p.strip() for p in (ws.headers.get("sec-websocket-protocol") or "").split(",") if p.strip()
    ]
    selected = "saa.v1" if "saa.v1" in offered else None
    for p in offered:
        if p.startswith("bearer."):
            return p[len("bearer.") :], selected
    return "", selected


def origin_allowed(origin: str | None, allowed: list[str]) -> bool:
    """Browsers always send Origin on WebSocket handshakes; non-browser clients may omit it."""
    return origin is None or origin in allowed
