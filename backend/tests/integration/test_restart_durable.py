"""NEXT-B against real storage: SQLite and PostgreSQL through the real migrations, and a process
that is really killed.

``tests/e2e/test_restart_recovery.py`` covers the behaviour with a simulated death; these tests prove
the same recovery works when the state actually went through SQL and when the process actually died.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from app.config.settings import DatabaseSettings
from app.database.repository import SqlRepository
from app.database.session import create_engine, create_session_factory
from app.llm.scripted import structured, tool_use
from app.models.enums import EventType, StepStatus, TaskStatus
from app.models.events import ExecutionEvent
from app.models.recovery import ActionCertainty
from app.state.memory import InMemoryRepository

from tests.helpers import Responder, approve_all, make_env
from tests.integration.conftest import BACKEND, migrate
from tests.restart_helpers import (
    MOVE,
    boot,
    crash_run,
    crashable,
    die_in_tool,
    executions,
    finish,
    move_plan,
    restart,
    use_repo,
)
from tests.workflow_fixtures import build_java_workspace, sha


# ----------------------------------------------------------------------------- every repository
@pytest.mark.parametrize("after_effect", [False, True])
async def test_interrupted_file_task_resumes_on_every_repository(
    own_repo: Any, tmp_path: Path, after_effect: bool
) -> None:
    repo = own_repo
    env = use_repo(make_env(tmp_path / "fs"), repo)
    dying = crashable(env)
    die_in_tool(env, dying, "fs_move", after_effect=after_effect)
    h1 = await boot(env, [structured(move_plan())])
    task = await crash_run(h1, MOVE)

    env2 = restart(env)
    h2 = await boot(env2)
    stored = await repo.get_task(task.id)
    assert stored is not None and stored.status is TaskStatus.INTERRUPTED
    assert stored.interruption is not None and stored.durable is not None
    assert stored.plan is not None and stored.plan.goal is not None
    [action] = stored.interruption.in_flight
    assert action.certainty is (
        ActionCertainty.CONFIRMED_DONE if after_effect else ActionCertainty.CONFIRMED_NOT_DONE
    )
    assert action.fingerprint.startswith("fs_move:"), "the fingerprint survived the round trip"

    await h2.runtime.resume_interrupted(task.id)
    done = await finish(h2, task.id)
    assert done.status is TaskStatus.COMPLETED and done.result and done.result.verified
    assert (env.desktop / "Spring Docs" / "note.txt").read_text() == "hello"
    assert await executions(env2, task.id, "fs_move") == (1 if after_effect else 2)
    assert await executions(env2, task.id, "fs_create") == 2, "completed steps did not run again"
    seqs = [e.seq for e in await repo.list_events(task.id)]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_multi_file_organisation_is_not_repeated_after_a_restart(
    own_repo: Any, tmp_path: Path
) -> None:
    """Workflow A: the batch move completed, the process died before the documents were read."""
    repo = own_repo
    env = use_repo(make_env(tmp_path / "fs"), repo)
    base = env.documents / "ws"
    files = build_java_workspace(base)
    dest = str(base / "Java")
    plan = {
        "summary": "Find, organise, read",
        "steps": [
            {"id": "s1", "title": "Find", "tool": "fs_find", "rationale": "find",
             "args": {"root": str(base), "terms": ["java"], "extensions": [".pdf"],
                      "context_terms": ["jvm", "class", "compiler", "bytecode", "spring"]}},
            {"id": "s2", "title": "Move", "tool": "fs_organize", "rationale": "move", "depends_on": ["s1"],
             "args": {"sources": "{{steps.s1.data.strong_paths}}", "dest_dir": dest}},
            {"id": "s3", "title": "Read", "tool": "fs_read_many", "rationale": "read", "depends_on": ["s2"],
             "args": {"paths": "{{steps.s2.data.dest_paths}}"}},
        ],
        "success_criteria": [{"kind": "dir_exists", "params": {"path": dest}}],
    }  # fmt: skip
    dying = crashable(env)
    die_in_tool(env, dying, "fs_read_many", after_effect=False)
    h1 = await boot(env, [structured(plan)])
    task = await crash_run(
        h1, "Find the Java PDFs in the workspace and move them into a Java folder"
    )
    moved = sorted(p.name for p in (base / "Java").iterdir())
    assert moved == ["Intro_to_OOP.pdf", "java-basics.pdf"]
    before = {str(p): sha(p) for p in base.rglob("*") if p.is_file()}

    env2 = restart(env)
    h2 = await boot(env2)
    stored = await repo.get_task(task.id)
    assert (
        stored and stored.interruption and stored.interruption.in_flight == []
    )  # a read is never recorded as in flight
    await h2.runtime.resume_interrupted(task.id)
    done = await finish(h2, task.id)
    assert done.status is TaskStatus.COMPLETED, done.result
    assert await executions(env2, task.id, "fs_organize") == 1, (
        "the batch move ran once, in the dead process"
    )
    assert {str(p): sha(p) for p in base.rglob("*") if p.is_file()} == before, (
        "no file changed again"
    )
    assert files["travel/java-island-guide.pdf"].exists(), (
        "the ambiguous document stayed where it was"
    )


# ----------------------------------------------------------------------------- transactions and events
async def test_a_failed_task_save_changes_nothing(own_repo: Any) -> None:
    """Task row, plan, steps and durable state are written in one transaction."""
    repo = own_repo
    if isinstance(repo, InMemoryRepository):
        pytest.skip("transactions are a property of the SQL repositories")
    env_task = (await _seed(repo)).model_copy(deep=True)
    env_task.status = TaskStatus.COMPLETED
    assert env_task.plan is not None and env_task.durable is not None
    env_task.plan.steps[0].status = StepStatus.FAILED
    env_task.durable.replans_used = 9
    env_task.plan.steps[1].args = {"bad": {1, 2, 3}}  # cannot be stored: the write fails part-way
    with pytest.raises(Exception):  # noqa: B017 - any storage error
        await repo.save_task(env_task)
    stored = await repo.get_task(env_task.id)
    assert stored is not None and stored.status is TaskStatus.EXECUTING
    assert stored.plan is not None and stored.plan.steps[0].status is StepStatus.SUCCEEDED
    assert stored.durable is not None and stored.durable.replans_used == 0


async def test_a_duplicate_event_sequence_number_is_rejected(own_repo: Any) -> None:
    repo = own_repo
    if isinstance(repo, InMemoryRepository):
        pytest.skip("uniqueness is enforced by the database")
    task = await _seed(repo)
    event = ExecutionEvent(task_id=task.id, seq=1, type=EventType.STEP_STARTED, message="once")
    await repo.append_event(event)
    with pytest.raises(Exception):  # noqa: B017 - unique (task_id, seq)
        await repo.append_event(
            ExecutionEvent(task_id=task.id, seq=1, type=EventType.STEP_STARTED, message="twice")
        )
    assert [e.message for e in await repo.list_events(task.id)] == ["once"]


async def _seed(repo: Any) -> Any:
    from app.models.plan import Plan, PlanStep
    from app.models.recovery import DurableState
    from app.models.task import Task

    task = Task(instruction="seeded")
    task.status = TaskStatus.EXECUTING
    task.plan = Plan(
        task_id=task.id,
        steps=[
            PlanStep(id="s1", title="one", tool="fs_list", args={"path": "@Documents"}, status=StepStatus.SUCCEEDED),
            PlanStep(id="s2", title="two", tool="fs_list", args={"path": "@Documents"}),
        ],
    )  # fmt: skip
    task.durable = DurableState(
        step_data={"s1": {"data": {}, "artifacts": {}, "summary": "", "ok": True}}
    )
    await repo.save_task(task)
    return task


# ----------------------------------------------------------------------------- a really killed process
def _run_until_ready(url: str, root: Path, where: str) -> tuple[subprocess.Popen[str], str]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.crash_driver", url, str(root), where],
        cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )  # noqa: S603  # fmt: skip
    task_id = ""
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("TASK "):
            task_id = line.split()[1]
        if line.strip() == "READY":
            return proc, task_id
    proc.kill()
    raise AssertionError("the driver process ended before it reached the kill point")


@pytest.mark.parametrize("where", ["model", "tool"])
async def test_a_killed_process_leaves_a_task_that_resumes_correctly(
    tmp_path: Path, where: str
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'saa.db'}"
    await asyncio.to_thread(migrate, url)
    root = tmp_path / "fs"
    proc, task_id = await asyncio.to_thread(_run_until_ready, url, root, where)
    proc.kill()  # TerminateProcess: no handler, no cleanup, no flush
    await asyncio.to_thread(proc.wait, 20)

    env = make_env(root)
    engine = create_engine(DatabaseSettings(url=url))
    repo = SqlRepository(engine, create_session_factory(engine))
    try:
        use_repo(env, repo)
        dead = await repo.get_task(task_id)
        assert dead is not None and not dead.status.is_terminal and dead.result is None
        script = (
            [tool_use("finish_step", success=True, summary="the note is there")]
            if where == "model"
            else []
        )
        h = await boot(env, script)
        found = (await repo.get_task(task_id)).interruption  # type: ignore[union-attr]
        assert found is not None
        if where == "tool":
            assert [a.certainty for a in found.in_flight] == [ActionCertainty.CONFIRMED_DONE]
        else:
            assert found.in_flight == [] and found.completed_steps == ["s1", "s2", "s3"]
        with Responder(env, approve_all) as human:
            await h.runtime.resume_interrupted(task_id)
            done = await finish(h, task_id)
        assert human.seen == []
        assert done.status is TaskStatus.COMPLETED and done.result and done.result.verified
        assert (env.desktop / "Spring Docs" / "note.txt").read_text() == "hello"
        assert await executions(env, task_id, "fs_move") == 1, "the move was never executed twice"
    finally:
        await repo.close()
