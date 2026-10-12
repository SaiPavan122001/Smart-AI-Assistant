"""Evaluation harness: the REAL composition root with a scripted model and an in-memory repo.

Everything the agent does (planner, router, permission engine, tools, verification, recovery)
is production code; only the model's decisions are scripted so runs are deterministic.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config.settings import (
    BrowserSettings,
    FilesystemSettings,
    LimitSettings,
    SecuritySettings,
    Settings,
)
from app.container import Container, build_container
from app.llm.scripted import ScriptedProvider
from app.models.approval import ApprovalDecision, ApprovalRequest
from app.models.enums import TaskStatus
from app.models.task import Task
from app.state.memory import InMemoryRepository
from app.tools.desktop.memory import FakeDesktopBackend

from evals.site import Site


@dataclass
class EvalEnv:
    root: Path
    desktop: Path
    downloads: Path
    documents: Path
    workspace: Path
    container: Container
    llm: ScriptedProvider
    site: Site | None = None
    fake_desktop: FakeDesktopBackend | None = None
    approvals_seen: list[ApprovalRequest] = field(default_factory=list)


Decide = Callable[[ApprovalRequest], ApprovalDecision]


def approve_all(_r: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(approved=True)


def deny_all(_r: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(approved=False, note="denied by evaluation policy")


def build_env(
    root: Path, *, limits: LimitSettings | None = None, browser: bool = False, desktop: bool = False,
    site: Site | None = None,
) -> EvalEnv:  # fmt: skip
    dirs = {n: root / n for n in ("Desktop", "Downloads", "Documents", "workspace")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        env="test", log_json=False, log_level="WARNING", data_dir=root / "data",
        filesystem=FilesystemSettings(allowed_roots=[dirs["Desktop"], dirs["Downloads"], dirs["Documents"]], workspace=dirs["workspace"]),
        browser=BrowserSettings(allow_private_network=True, headless=True),
        limits=limits or LimitSettings(approval_timeout_s=20, max_execution_seconds=120),
        security=SecuritySettings(api_token="eval-token"),
    )  # fmt: skip
    llm = ScriptedProvider()
    fake = FakeDesktopBackend() if desktop else None
    container = build_container(
        settings, repo=InMemoryRepository(), llm=llm, enable_browser=browser, enable_desktop=desktop, desktop_backend=fake,
    )  # fmt: skip
    return EvalEnv(
        root=root, desktop=dirs["Desktop"], downloads=dirs["Downloads"], documents=dirs["Documents"],
        workspace=dirs["workspace"], container=container, llm=llm, site=site, fake_desktop=fake,
    )  # fmt: skip


async def run_task(env: EvalEnv, instruction: str, decide: Decide, *, timeout_s: float = 90.0,
                   mid_run: Callable[[EvalEnv, Task], Any] | None = None) -> tuple[Task, float]:  # fmt: skip
    c = env.container
    await c.start()
    stop = asyncio.Event()

    async def responder() -> None:
        seen: set[str] = set()
        while not stop.is_set():
            for req in list(c.approvals.pending()):
                if req.id not in seen:
                    seen.add(req.id)
                    env.approvals_seen.append(req.model_copy(deep=True))
                    await c.approvals.resolve(req.id, decide(req))
            await asyncio.sleep(0.01)

    resp = asyncio.create_task(responder())
    started = time.monotonic()
    try:
        task = await c.runtime.submit(instruction)
        if mid_run is not None:
            await mid_run(env, task)
        await c.runtime.wait(task.id, timeout_s)
    finally:
        stop.set()
        resp.cancel()
    elapsed = time.monotonic() - started
    final = await c.repo.get_task(task.id)
    assert final is not None
    return final, elapsed


async def shutdown(env: EvalEnv) -> None:
    await env.container.stop()


_ = TaskStatus
