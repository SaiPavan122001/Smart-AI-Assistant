"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api import errors
from app.api.deps import AuthDep
from app.api.spa import mount_frontend
from app.api.v1 import agent, approvals, permissions, system, tasks, ws
from app.config.settings import get_settings
from app.container import Container, build_container
from app.observability.logging import bind, get_logger
from app.utils.ids import new_id

log = get_logger("api")


def create_app(container: Container | None = None) -> FastAPI:
    """Create the API. Pass a pre-built container (tests, embedding) or let the lifespan build one."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        c = container or build_container()
        app.state.container = c
        await c.start()
        if container is None:
            log.warning(
                "API token in use (set SAA_SECURITY__API_TOKEN to make it stable)",
                extra={"token_prefix": c.api_token[:4]},
            )
        try:
            yield
        finally:
            await c.stop()

    app = FastAPI(
        title="Smart AI Assistant API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    settings = container.settings if container else get_settings()

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.security.allowed_hosts)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.security.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Session-Id", "X-Request-ID"],
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = (request.headers.get("x-request-id") or new_id("req"))[:64]
        request.state.request_id = rid
        with bind(request_id=rid, session_id=request.headers.get("x-session-id")):
            response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    errors.install(app)
    prefix = "/api/v1"
    app.include_router(system.public, prefix=prefix)
    app.include_router(system.secured, prefix=prefix)
    for r in (tasks.router, approvals.router, permissions.router, agent.router):
        app.include_router(r, prefix=prefix)
    app.include_router(ws.router, prefix=prefix)

    @app.get("/metrics", include_in_schema=False, dependencies=[AuthDep])
    async def prometheus(request: Request) -> Response:
        return Response(
            request.app.state.container.metrics.render(), media_type="text/plain; version=0.0.4"
        )

    if settings.serve_frontend:
        mount_frontend(app)  # last: API routes win; unknown non-API paths fall back to the SPA

    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn app.api.main:app_factory --factory``."""
    return create_app()
