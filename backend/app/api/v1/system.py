from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app import __version__
from app.api.deps import AuthDep, ContainerDep
from app.api.schemas import Health, Readiness
from app.models.tool import ToolSpec

public = APIRouter(tags=["system"])
secured = APIRouter(tags=["system"], dependencies=[AuthDep])


@public.get("/health", response_model=Health)
async def health() -> Health:
    return Health(status="ok", version=__version__)


@public.get("/ready", response_model=Readiness)
async def ready(c: ContainerDep, response: Response) -> Readiness:
    db_ok = await c.repo.ping()
    checks: dict[str, Any] = {
        "database": db_ok,
        "llm_configured": c.gateway is not None,
        "llm_usable": c.gateway is not None and c.llm_problem is None,
        "llm_problem": c.llm_problem,
        "warnings": list(c.warnings),
        "browser": c.services.browser is not None,
        "desktop": c.services.desktop is not None,
        "vision": c.services.vision is not None,
        "kill_switch_engaged": c.kill_switch.engaged,
    }
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Readiness(ready=db_ok, checks=checks)


@secured.get("/tools", response_model=list[ToolSpec])
async def tools(c: ContainerDep) -> list[ToolSpec]:
    return c.registry.specs()


@secured.get("/settings")
async def effective_settings(c: ContainerDep) -> dict[str, Any]:
    """Effective, non-secret configuration (read-only; change it via environment variables)."""
    s = c.settings
    return {
        "env": s.env,
        "llm": {
            "provider": s.llm.provider,
            "model": s.llm.model,
            "temperature": s.llm.temperature,
            "configured": c.gateway is not None,
            "capabilities": c.llm.capabilities.model_dump() if c.llm is not None else None,
            "problem": c.llm_problem,
        },
        "vision": {
            "enabled": s.vision.enabled,
            "provider": s.vision.provider,
            "model": s.vision.model,
            "configured": c.services.vision is not None,
            "min_confidence": s.vision.min_confidence,
        },
        "browser": {
            "headless": s.browser.headless,
            "channel": s.browser.channel,
            "max_tabs": s.browser.max_tabs,
            "allow_upload": s.browser.allow_upload,
            "allow_private_network": s.browser.allow_private_network,
        },
        "filesystem": {
            "allowed_roots": [str(r) for r in c.services.path_policy.writable_roots],
            "workspace": str(c.services.path_policy.workspace),
        },
        "terminal": {
            "allow_shell_interpreters": s.terminal.allow_shell_interpreters,
            "default_timeout_s": s.terminal.default_timeout_s,
        },
        "desktop": {
            "enabled": c.services.desktop is not None,
            "launchable_apps": sorted(s.desktop.apps),
        },
        "limits": s.limits.model_dump(),
        "database": {
            "kind": "memory"
            if s.database.url.startswith("memory://")
            else s.database.url.split(":", 1)[0]
        },
    }


@secured.get("/metrics/summary")
async def metrics_summary(c: ContainerDep) -> dict[str, float | int]:
    return c.metrics.summary()
