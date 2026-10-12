"""Simulating a process that dies, and the process that starts after it.

A "process" is one ``Env`` (router, approvals, event bus, runtime). Two processes share only what is
on disk: the repository and the folders. ``CrashingRepo`` makes the first process die at an exact
point: from then on none of its writes reach storage (as if it had been killed), while the storage
itself stays intact for the second process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.models.enums import Capability, RiskLevel
from app.models.task import Task
from app.state.events import EventBus
from app.tools.base import Tool, ToolOutput
from app.tools.context import ExecutionContext
from app.tools.schema import OutputModel
from pydantic import BaseModel

from tests.helpers import Env, Harness, make_env, make_harness

_WRITES = ("save_", "append_", "add_", "record_", "ensure_")


class Crash(BaseException):
    """The simulated death of the process. Not an ``Exception``: nothing in the app may handle it."""


class CrashingRepo:
    """Forwards to a real repository until it "dies"; then every write is silently lost."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.dead = False
        # (method name, positional args) -> "before" (die instead of writing), "after" (write, then die) or None
        self.trigger: Callable[[str, tuple[Any, ...]], str | None] | None = None
        # (method name, positional args) -> True: the write fails with an ordinary database error
        self.fail: Callable[[str, tuple[Any, ...]], bool] | None = None

    def die(self) -> None:
        self.dead = True

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.inner, name)
        if not name.startswith(_WRITES):
            return target

        async def write(*args: Any, **kwargs: Any) -> Any:
            if self.dead:
                return None
            if self.fail is not None and self.fail(name, args):
                raise RuntimeError("simulated database failure")
            when = self.trigger(name, args) if self.trigger is not None else None
            if when == "before":
                self.dead = True
                raise Crash(name)
            out = await target(*args, **kwargs)
            if when == "after":
                self.dead = True
                raise Crash(name)
            return out

        return write


def use_repo(env: Env, repo: Any) -> Env:
    env.repo = repo
    env.router.repo = repo
    env.bus = EventBus(repo)
    return env


def crashable(env: Env) -> CrashingRepo:
    """Put a crash switch between this process and its storage."""
    repo = CrashingRepo(env.repo)
    use_repo(env, repo)
    return repo


def storage(env: Env) -> Any:
    return env.repo.inner if isinstance(env.repo, CrashingRepo) else env.repo


def restart(env: Env, **make_env_kwargs: Any) -> Env:
    """A new process over the same storage and folders: nothing in memory survives."""
    new = make_env(env.root, **make_env_kwargs)
    return use_repo(new, storage(env))


async def boot(env: Env, script: list[Any] | None = None, **kw: Any) -> Harness:
    """Start the runtime of a (new) process the way the container does: recover first."""
    h = make_harness(env, script, **kw)
    await h.runtime.recover_interrupted()
    return h


async def crash_run(h: Harness, instruction: str, **submit_kwargs: Any) -> Task:
    """Submit a task in a process that is expected to die while running it."""
    task = await h.runtime.submit(instruction, **submit_kwargs)
    runner = h.runtime._tasks[task.id]
    try:
        await asyncio.wait_for(asyncio.shield(runner), 30)
    except Crash:
        pass
    else:
        raise AssertionError("the process was expected to die during this task")
    stored = await storage(h.env).get_task(task.id)
    assert stored is not None
    return stored


async def kill(h: Harness, repo: CrashingRepo, task_id: str) -> None:
    """Kill the process now, wherever the task is: storage stops receiving writes, the task dies."""
    repo.die()
    runner = h.runtime._tasks.get(task_id)
    if runner is not None:
        runner.cancel()
        try:
            await runner
        except (asyncio.CancelledError, Crash):
            pass


async def finish(h: Harness, task_id: str, timeout: float = 30) -> Task:
    await h.runtime.wait(task_id, timeout)
    done = await storage(h.env).get_task(task_id)
    assert done is not None
    return done


def die_in_tool(env: Env, repo: CrashingRepo, tool: str, *, after_effect: bool) -> None:
    """The process dies inside ``tool``: before it touched anything, or right after it finished and
    before its result could be recorded."""
    target = env.router.registry.get(tool)
    assert target is not None
    original = target.run

    async def run(args: Any, ctx: ExecutionContext) -> ToolOutput:
        if after_effect:
            await original(args, ctx)
        repo.die()
        raise Crash(tool)

    target.run = run  # type: ignore[method-assign]


async def executions(env: Env, task_id: str, tool: str) -> int:
    """How many times ``tool`` really started executing for this task (replays are not executions)."""
    events = await storage(env).list_events(task_id)
    return sum(1 for e in events if e.type.value == "action_started" and e.data.get("tool") == tool)


# ---------------------------------------------------------------------------- a non-idempotent tool
class BumpInput(BaseModel):
    path: str


class BumpOutput(OutputModel):
    path: str
    count: int


class Bump(Tool[BumpInput]):
    """Appends one line to a file: a visible, non-idempotent side effect whose outcome cannot be
    reconciled (no ``reconcile``), like a form submission or a click."""

    name = "test_bump"
    description = "Append one line to a counter file."
    capability = Capability.FILESYSTEM
    Input = BumpInput
    Output = BumpOutput
    risk = RiskLevel.LOW

    async def run(self, args: BumpInput, ctx: ExecutionContext) -> ToolOutput:
        p = Path(args.path)
        with p.open("a", encoding="utf-8") as fh:
            fh.write("x\n")
        count = len(p.read_text(encoding="utf-8").splitlines())
        return ToolOutput(data={"path": str(p), "count": count}, summary=f"bumped to {count}")


def with_bump(env: Env) -> Env:
    env.router.registry.register(Bump())
    return env


def bumps(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


# ---------------------------------------------------------------------------- a three-step file task
MOVE = "Create a folder Spring Docs on my desktop and move note.txt there"


def move_plan() -> dict[str, Any]:
    return {
        "summary": "Create a folder, write a note, move it into the folder",
        "steps": [
            {
                "id": "s1", "title": "Create folder", "tool": "fs_create", "rationale": "requested",
                "args": {"path": "@Desktop/Spring Docs", "kind": "dir"},
                "verify": [{"kind": "dir_exists", "params": {"path": "@Desktop/Spring Docs"}}],
            },
            {
                "id": "s2", "title": "Write note", "tool": "fs_create", "rationale": "the file to move",
                "args": {"path": "@Downloads/note.txt", "content": "hello"},
            },
            {
                "id": "s3", "title": "Move note into folder", "tool": "fs_move", "rationale": "requested",
                "depends_on": ["s1", "s2"],
                "args": {"src": "{{steps.s2.artifacts.path}}", "dst": "@Desktop/Spring Docs"},
            },
        ],
        "success_criteria": [
            {"kind": "file_moved", "params": {"src": "@Downloads/note.txt", "dst": "@Desktop/Spring Docs/note.txt"}}
        ],
    }  # fmt: skip
