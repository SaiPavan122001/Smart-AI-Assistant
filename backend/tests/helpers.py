"""Shared test harness: builds a real router/engine/repo stack over temp folders."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config.settings import (
    BrowserSettings,
    FilesystemSettings,
    LimitSettings,
    Settings,
    TerminalSettings,
)
from app.models.enums import EventType, TaskStatus
from app.models.events import Correlation
from app.observability.metrics import Metrics
from app.observability.usage import UsageMeter
from app.orchestration.events import EventBus
from app.perception.cache import ObservationCache
from app.permissions.approvals import ApprovalManager
from app.permissions.engine import PermissionEngine, TaskGrants
from app.permissions.policy import PermissionPolicy
from app.security.injection import InjectionGuard
from app.security.paths import PathPolicy
from app.state.memory import InMemoryRepository
from app.tools.bootstrap import build_registry
from app.tools.context import ExecutionContext, ToolServices
from app.tools.router import ToolRouter
from app.utils.control import CancelToken, KillSwitch, PauseGate


@dataclass
class Env:
    root: Path
    desktop: Path
    downloads: Path
    documents: Path
    workspace: Path
    settings: Settings
    paths: PathPolicy
    repo: InMemoryRepository
    bus: EventBus
    engine: PermissionEngine
    approvals: ApprovalManager
    router: ToolRouter
    metrics: Metrics
    services: ToolServices
    kill: KillSwitch
    statuses: list[TaskStatus] = field(default_factory=list)
    ctx_task: str = "task_test"

    def ctx(self, task_id: str | None = None, **overrides: Any) -> ExecutionContext:
        tid = task_id or self.ctx_task
        meter = UsageMeter(self.settings.limits, metrics=self.metrics)

        async def emit(type_: EventType, message: str = "", data: dict[str, Any] | None = None, *,
                       step_id: str | None = None, tool_call_id: str | None = None) -> None:  # fmt: skip
            await self.bus.publish(
                tid, type_, message, data, Correlation(step_id=step_id, tool_call_id=tool_call_id)
            )

        async def set_status(status: TaskStatus) -> None:
            self.statuses.append(status)

        base = {
            "task_id": tid, "services": self.services, "token": CancelToken(self.kill), "pause": PauseGate(),
            "meter": meter, "guard": InjectionGuard(), "grants": TaskGrants(), "cache": ObservationCache(),
            "cwd": self.workspace, "emit": emit, "set_status": set_status,
        }  # fmt: skip
        base.update(overrides)
        return ExecutionContext(**base)

    async def event_types(self, task_id: str | None = None) -> list[str]:
        return [e.type.value for e in await self.repo.list_events(task_id or self.ctx_task)]


def make_env(
    root: Path,
    *,
    policy: PermissionPolicy | None = None,
    limits: LimitSettings | None = None,
    terminal: TerminalSettings | None = None,
    block_on_injection: bool = False,
    include_browser: bool = False,
    include_desktop: bool = False,
    include_vision: bool = False,
) -> Env:
    desktop, downloads, documents, workspace = (
        root / n for n in ("Desktop", "Downloads", "Documents", "workspace")
    )
    for d in (desktop, downloads, documents, workspace):
        d.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        env="test",
        filesystem=FilesystemSettings(
            allowed_roots=[desktop, downloads, documents], workspace=workspace
        ),
        browser=BrowserSettings(allow_private_network=True, headless=True),
        limits=limits or LimitSettings(approval_timeout_s=5),
        terminal=terminal or TerminalSettings(),
    )
    paths = PathPolicy(
        allowed_roots=settings.resolved_allowed_roots(),
        workspace=workspace,
        blocked_paths=[],
        blocked_write_extensions=settings.filesystem.blocked_write_extensions,
    )
    repo = InMemoryRepository()
    metrics = Metrics()
    kill = KillSwitch()
    engine = PermissionEngine(policy or PermissionPolicy())
    approvals = ApprovalManager(timeout_s=settings.limits.approval_timeout_s)
    registry = build_registry(
        include_browser=include_browser,
        include_desktop=include_desktop,
        include_vision=include_vision,
    )
    router = ToolRouter(registry, engine, approvals, repo, block_on_injection=block_on_injection)
    services = ToolServices(settings=settings, path_policy=paths, metrics=metrics)
    return Env(
        root=root, desktop=desktop, downloads=downloads, documents=documents, workspace=workspace,
        settings=settings, paths=paths, repo=repo, bus=EventBus(repo), engine=engine, approvals=approvals,
        router=router, metrics=metrics, services=services, kill=kill,
    )  # fmt: skip


# ---------------------------------------------------------------------------- runtime harness
from app.llm.gateway import LLMGateway  # noqa: E402
from app.llm.scripted import ScriptedProvider  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.orchestration.runner import RunnerDeps  # noqa: E402
from app.orchestration.runtime import TaskRuntime  # noqa: E402
from app.planner.planner import Planner  # noqa: E402


@dataclass
class Harness:
    env: Env
    llm: ScriptedProvider
    runtime: TaskRuntime

    async def run(self, instruction: str, *, timeout: float = 60) -> Task:
        task = await self.runtime.submit(instruction)
        await self.runtime.wait(task.id, timeout)
        done = await self.env.repo.get_task(task.id)
        assert done is not None
        return done

    async def events(self, task_id: str) -> list[str]:
        return [e.type.value for e in await self.env.repo.list_events(task_id)]


def make_harness(env: Env, script: list[Any] | None = None, *, fallback: Any = None) -> Harness:
    llm = ScriptedProvider(script or [], fallback=fallback)
    gateway = LLMGateway(llm)
    planner = Planner(gateway, env.router.registry, env.services)
    deps = RunnerDeps(
        repo=env.repo, bus=env.bus, registry=env.router.registry, router=env.router, approvals=env.approvals,
        services=env.services, metrics=env.metrics, gateway=gateway, planner=planner,
    )  # fmt: skip
    return Harness(env=env, llm=llm, runtime=TaskRuntime(deps, env.kill))


# ---------------------------------------------------------------------------- approval responder
import asyncio  # noqa: E402
from collections.abc import Callable  # noqa: E402

from app.models.approval import ApprovalDecision, ApprovalRequest  # noqa: E402


class Responder:
    """Plays the human: decides every approval request as it appears and records what it saw."""

    def __init__(self, env: Env, decide: Callable[[ApprovalRequest], ApprovalDecision]) -> None:
        self.env = env
        self.decide = decide
        self.seen: list[ApprovalRequest] = []
        self._task: asyncio.Task[None] | None = None

    async def _loop(self) -> None:
        while True:
            for req in list(self.env.approvals.pending()):
                if req.id in {s.id for s in self.seen}:
                    continue
                self.seen.append(req.model_copy(deep=True))
                await self.env.approvals.resolve(req.id, self.decide(req))
            await asyncio.sleep(0.01)

    def __enter__(self) -> Responder:
        self._task = asyncio.get_event_loop().create_task(self._loop())
        return self

    def __exit__(self, *exc: object) -> None:
        if self._task:
            self._task.cancel()


def approve_all(_req: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(approved=True)


def deny_all(_req: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(approved=False, note="denied by test")
