"""Composition root: builds every long-lived collaborator from settings (no hidden globals)."""

from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config

from app.config.settings import Settings, get_settings
from app.database.repository import SqlRepository
from app.database.session import create_engine, create_session_factory, is_memory_url
from app.llm.base import LLMProvider
from app.llm.capabilities import require_for_agent
from app.llm.factory import build_llm_provider, build_vision_provider
from app.llm.gateway import LLMGateway
from app.observability.logging import configure_logging, get_logger
from app.observability.metrics import Metrics
from app.orchestration.runner import RunnerDeps
from app.orchestration.runtime import TaskRuntime
from app.permissions.approvals import ApprovalManager
from app.permissions.engine import PermissionEngine
from app.permissions.policy import PermissionPolicy
from app.planner.planner import Planner
from app.registry import build_registry
from app.security.paths import PathPolicy
from app.state.events import EventBus
from app.state.memory import InMemoryRepository
from app.state.repository import Repository
from app.state.task_logs import TaskLogSink
from app.tools.context import ToolServices
from app.tools.registry import ToolRegistry
from app.tools.router import ToolRouter
from app.tools.vision.service import LLMVisionProvider, VisionService
from app.utils.control import KillSwitch
from app.utils.errors import LLMError

log = get_logger("container")
BACKEND_DIR = Path(__file__).resolve().parents[1]


@dataclass
class Container:
    settings: Settings
    repo: Repository
    bus: EventBus
    registry: ToolRegistry
    engine: PermissionEngine
    approvals: ApprovalManager
    router: ToolRouter
    metrics: Metrics
    services: ToolServices
    kill_switch: KillSwitch
    runtime: TaskRuntime
    gateway: LLMGateway | None
    api_token: str
    log_sink: TaskLogSink
    warnings: list[str] = field(default_factory=list)
    llm_problem: str | None = None
    llm: LLMProvider | None = None
    _owned_providers: tuple[LLMProvider, ...] = ()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.settings.database.auto_migrate and not is_memory_url(self.settings.database.url):
            await asyncio.to_thread(run_migrations, self.settings)
        problem = await database_problem(self.repo, self.settings)
        if problem:
            raise RuntimeError(problem)
        stored = await self.repo.load_policy()
        if stored:
            try:
                self.engine.update_policy(PermissionPolicy.model_validate(stored))
            except Exception:
                log.warning("stored permission policy is invalid; using defaults")
        self.log_sink.start()
        orphans = await self.runtime.recover_orphans()
        if orphans:
            log.warning("marked interrupted tasks as failed", extra={"count": orphans})

    async def stop(self) -> None:
        await self.runtime.shutdown()
        if self.services.browser is not None:
            await self.services.browser.shutdown()
        if self.services.desktop is not None:
            self.services.desktop.shutdown()
        await self.log_sink.stop()
        for p in self._owned_providers:
            await p.aclose()
        await self.repo.close()


async def database_problem(
    repo: Repository, settings: Settings, *, expect_schema: bool = True
) -> str | None:
    """Return an actionable description of what is wrong with the database, or ``None`` if it is usable."""
    if is_memory_url(settings.database.url):
        return None
    target = settings.database.url.rsplit("@", 1)[-1]  # never print credentials
    if not await repo.ping():
        return (
            f"Cannot connect to the database ({target}). Start PostgreSQL "
            "(docker compose -f infra/docker/docker-compose.yml up -d) or fix SAA_DATABASE__URL; "
            "to try the app without persistence set SAA_DATABASE__URL=memory://"
        )
    if expect_schema:
        try:
            await repo.list_tasks(limit=1)
        except Exception:
            return (
                f"The database ({target}) is reachable but its schema is missing or outdated. "
                "Run `python -m app migrate` (or set SAA_DATABASE__AUTO_MIGRATE=true)."
            )
    return None


def run_migrations(settings: Settings) -> None:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "app" / "database" / "migrations"))
    os.environ["SAA_MIGRATION_URL"] = settings.database.url
    try:
        command.upgrade(cfg, "head")
    finally:
        os.environ.pop("SAA_MIGRATION_URL", None)


def build_path_policy(settings: Settings) -> PathPolicy:
    fs = settings.filesystem
    fs.workspace.mkdir(parents=True, exist_ok=True)
    return PathPolicy(
        allowed_roots=settings.resolved_allowed_roots(),
        read_only_roots=fs.read_only_roots,
        workspace=fs.workspace,
        blocked_write_extensions=fs.blocked_write_extensions,
    )


def build_container(
    settings: Settings | None = None,
    *,
    repo: Repository | None = None,
    llm: LLMProvider | None = None,
    vision_llm: LLMProvider | None = None,
    desktop_backend: Any | None = None,
    enable_browser: bool | None = None,
    enable_desktop: bool | None = None,
) -> Container:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    # ---- persistence ----
    if repo is None:
        if is_memory_url(settings.database.url):
            log.warning("using the in-memory repository: nothing is persisted")
            repo = InMemoryRepository()
        else:
            engine = create_engine(settings.database)
            repo = SqlRepository(engine, create_session_factory(engine))

    # ---- security primitives ----
    path_policy = build_path_policy(settings)
    kill = KillSwitch()
    metrics = Metrics()
    bus = EventBus(repo)

    token = settings.security.api_token.get_secret_value() if settings.security.api_token else ""
    if not token:
        if settings.env == "prod":
            raise RuntimeError("SAA_SECURITY__API_TOKEN must be set when SAA_ENV=prod")
        token = secrets.token_urlsafe(32)
        log.warning(
            "no API token configured; generated an ephemeral one",
            extra={"hint": "set SAA_SECURITY__API_TOKEN"},
        )

    # ---- perception / actuators ----
    browser = None
    if enable_browser if enable_browser is not None else True:
        from app.tools.browser.manager import BrowserManager

        browser = BrowserManager(settings.browser, path_policy)

    desktop = None
    if settings.desktop.enabled and (enable_desktop if enable_desktop is not None else True):
        from app.tools.desktop.service import DesktopService
        from app.tools.desktop.uia import UIABackend

        backend = desktop_backend or UIABackend()
        svc = DesktopService(backend, settings.desktop)
        if svc.available():
            desktop = svc
        else:
            svc.shutdown()
            log.info("windows UI automation unavailable; desktop tools are disabled")

    llm_provider = llm if llm is not None else build_llm_provider(settings)
    owned: list[LLMProvider] = [llm_provider] if (llm_provider is not None and llm is None) else []
    gateway = LLMGateway(llm_provider) if llm_provider is not None else None
    warnings: list[str] = []
    llm_problem: str | None = None
    if llm_provider is not None:
        try:
            warnings += require_for_agent(llm_provider.capabilities, model=llm_provider.model)
        except LLMError as exc:  # unusable model: tasks fail early with this explanation
            llm_problem = exc.message

    vision = None
    if settings.vision.enabled:
        v_provider = vision_llm
        if v_provider is None and settings.vision.provider != "none":
            v_provider = build_vision_provider(settings)
            if v_provider is not None and v_provider is not llm_provider:
                owned.append(v_provider)
        if v_provider is not None and not v_provider.capabilities.supports_vision:
            warnings.append(
                f"Vision model '{v_provider.model}' ({v_provider.name}) does not support images; vision tools are disabled. "
                "Set SAA_VISION__PROVIDER/MODEL to a vision-capable model."
            )
            v_provider = None
        if v_provider is not None:
            vision = VisionService(
                LLMVisionProvider(LLMGateway(v_provider)),
                min_confidence=settings.vision.min_confidence,
            )

    services = ToolServices(
        settings=settings,
        path_policy=path_policy,
        metrics=metrics,
        browser=browser,
        desktop=desktop,
        vision=vision,
    )
    registry = build_registry(
        include_browser=browser is not None,
        include_desktop=desktop is not None,
        include_vision=vision is not None,
    )

    perm_engine = PermissionEngine(PermissionPolicy())
    approvals = ApprovalManager(timeout_s=settings.limits.approval_timeout_s)
    router = ToolRouter(
        registry,
        perm_engine,
        approvals,
        repo,
        block_on_injection=settings.security.block_on_injection,
    )
    planner = Planner(gateway, registry, services) if gateway is not None else None
    deps = RunnerDeps(
        repo=repo,
        bus=bus,
        registry=registry,
        router=router,
        approvals=approvals,
        services=services,
        metrics=metrics,
        gateway=gateway,
        planner=planner,
    )
    runtime = TaskRuntime(deps, kill)
    return Container(
        settings=settings, repo=repo, bus=bus, registry=registry, engine=perm_engine, approvals=approvals, router=router,
        metrics=metrics, services=services, kill_switch=kill, runtime=runtime, gateway=gateway, api_token=token,
        log_sink=TaskLogSink(repo),
        warnings=warnings,
        llm_problem=llm_problem, llm=llm_provider, _owned_providers=tuple(owned),
    )  # fmt: skip
