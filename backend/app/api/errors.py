"""Uniform error responses (no stack traces or internals leak to clients)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.observability.logging import get_logger
from app.utils.errors import AgentError, ErrorCode

log = get_logger("api")

_STATUS = {
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.INVALID_INPUT: 422,
    ErrorCode.UNKNOWN_TOOL: 422,
    ErrorCode.KILL_SWITCH: 409,
    ErrorCode.ALREADY_EXISTS: 409,
    ErrorCode.POLICY_DENIED: 403,
    ErrorCode.LIMIT_EXCEEDED: 429,
}


def _body(
    request: Request, code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", None),
            "details": details or {},
        }
    }


def install(app: FastAPI) -> None:
    @app.exception_handler(AgentError)
    async def agent_error(request: Request, exc: AgentError) -> JSONResponse:
        return JSONResponse(
            _body(request, exc.code.value, exc.message, exc.details),
            status_code=_STATUS.get(exc.code, 400),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [{"loc": [str(p) for p in e["loc"]], "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse(
            _body(request, "invalid_input", "Request validation failed", {"problems": problems}),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthorized", 404: "not_found", 405: "method_not_allowed"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(
            _body(request, code, str(exc.detail)),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse(
            _body(request, "internal_error", "Internal server error"), status_code=500
        )
