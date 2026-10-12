"""Execution context handed to tools and the router for one task."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.config.settings import Settings
from app.models.enums import EventType, TaskStatus
from app.models.goal import GoalSpec
from app.observability.metrics import Metrics
from app.observability.usage import UsageMeter
from app.perception.cache import ObservationCache
from app.permissions.engine import TaskGrants
from app.security.injection import InjectionGuard
from app.security.paths import PathPolicy
from app.tools.loop_guard import LoopGuard
from app.tools.observed import Observed
from app.tools.resume import ResumeLedger
from app.utils.control import CancelToken, PauseGate

if TYPE_CHECKING:
    from app.search.router import SearchRouter
    from app.tools.browser.manager import BrowserManager
    from app.tools.desktop.service import DesktopService
    from app.tools.filesystem.journal import FileJournal
    from app.tools.vision.service import VisionService


@dataclass
class ToolServices:
    """Process-level collaborators shared by tools (explicit DI, no globals)."""

    settings: Settings
    path_policy: PathPolicy
    metrics: Metrics | None = None
    browser: BrowserManager | None = None
    desktop: DesktopService | None = None
    vision: VisionService | None = None
    search: SearchRouter | None = None
    # Every change the filesystem tools make is journalled here first (NEXT-D). Without it they change nothing.
    journal: FileJournal | None = None


Emit = Callable[..., Awaitable[None]]
SetStatus = Callable[[TaskStatus], Awaitable[None]]


@dataclass
class ExecutionContext:
    task_id: str
    services: ToolServices
    token: CancelToken
    pause: PauseGate
    meter: UsageMeter
    guard: InjectionGuard
    grants: TaskGrants
    cache: ObservationCache
    cwd: Path
    emit: Emit  # emit(type, message, data=None, *, step_id=None, tool_call_id=None)
    set_status: SetStatus
    session_id: str | None = None
    request_id: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    tool_call_id: str | None = None
    rationale: str = ""
    denied: set[str] = field(default_factory=set)  # call keys the user refused: never re-asked
    loop_guard: LoopGuard = field(
        default_factory=LoopGuard
    )  # per-task action ledger + loop detection
    extras: dict[str, Any] = field(default_factory=dict)
    # The user's explicit goal (restrictions are enforced by the router) and what the task has seen/written.
    goal: GoalSpec | None = None
    observed: Observed = field(default_factory=Observed)
    # Set only while a task continues after an interruption (see app/tools/resume.py).
    resume: ResumeLedger | None = None

    @property
    def settings(self) -> Settings:
        return self.services.settings

    @property
    def paths(self) -> PathPolicy:
        return self.services.path_policy

    async def event(
        self, type_: EventType, message: str = "", data: dict[str, Any] | None = None
    ) -> None:
        await self.emit(type_, message, data, step_id=self.step_id, tool_call_id=self.tool_call_id)
