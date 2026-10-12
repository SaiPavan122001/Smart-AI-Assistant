"""NEXT-D end to end: a finished task's file changes are rolled back on the user's request, through
the real runtime, router, permission engine and tools. Only the model is scripted."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from app.llm.scripted import structured
from app.models.enums import TaskStatus
from app.models.journal import JournalState
from app.tools.filesystem import journal as jmod
from app.tools.filesystem.journal import FileJournal
from app.utils.errors import AgentError, ErrorCode

from tests.e2e.test_scenarios import step
from tests.helpers import Env, Responder, approve_all, make_harness


@pytest.fixture(autouse=True)
def recycle(env: Env, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_ = env.root / "RecycleBin"
    bin_.mkdir()
    monkeypatch.setattr(
        "send2trash.send2trash",
        lambda p: Path(p).rename(bin_ / f"{len(list(bin_.iterdir()))}-{Path(p).name}"),
    )
    return bin_


def tidy_plan(extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"summary": "tidy the reports", "steps": [
        step("s1", "Create Reports", "fs_create", {"path": "@Desktop/Reports", "kind": "dir"}),
        step("s2", "Move the reports", "fs_organize",
             {"sources": ["@Downloads/q1.pdf", "@Downloads/q2.pdf"], "dest_dir": "@Desktop/Reports"},
             depends_on=["s1"]),
        step("s3", "Write an index", "fs_create",
             {"path": "@Desktop/Reports/index.txt", "content": "q1, q2"}, depends_on=["s2"]),
        *(extra or []),
    ], "success_criteria": [{"kind": "file_exists", "params": {"path": "@Desktop/Reports/index.txt"}}]}  # fmt: skip


def seed(env: Env) -> dict[str, bytes | str]:
    (env.downloads / "q1.pdf").write_bytes(b"%PDF q1")
    (env.downloads / "q2.pdf").write_bytes(b"%PDF q2")
    return tree(env)


def tree(env: Env) -> dict[str, bytes | str]:
    return {
        str(p.relative_to(env.root)): "<dir>" if p.is_dir() else p.read_bytes()
        for top in (env.desktop, env.downloads, env.documents) for p in sorted(top.rglob("*"))
    }  # fmt: skip


async def test_a_completed_task_is_rolled_back_and_keeps_its_result(env: Env) -> None:
    before = seed(env)
    h = make_harness(env, [structured(tidy_plan())])
    task = await h.run(
        "Put my quarterly reports into a Reports folder on the desktop with an index"
    )
    assert task.status is TaskStatus.COMPLETED and tree(env) != before

    view = await h.runtime.journal(task.id)
    assert view.can_roll_back and view.open == 4  # folder, two moves, the index
    result = await h.runtime.rollback(task.id)
    assert tree(env) == before and result.errors == [] and result.view.open == 0
    assert {e.state for e in result.view.entries} == {JournalState.UNDONE}

    after = await env.repo.get_task(task.id)
    assert after is not None and after.status is TaskStatus.COMPLETED, "the record of the run stays"
    assert after.result == task.result
    assert not (await h.runtime.journal(task.id)).can_roll_back


async def test_a_failed_task_with_partial_changes_can_be_rolled_back(env: Env) -> None:
    before = seed(env)
    broken = step("s4", "Read a file that is not there", "fs_read", {"path": "@Desktop/missing.txt"},
                  depends_on=["s3"])  # fmt: skip
    plan = tidy_plan([broken])
    h = make_harness(env, [structured(plan), structured({"summary": "give up", "steps": [],
                                                         "needs_clarification": "missing.txt?"})])  # fmt: skip
    task = await h.run("Tidy my reports, then read missing.txt")
    assert task.status is TaskStatus.FAILED and (env.desktop / "Reports" / "index.txt").exists()
    await h.runtime.rollback(task.id)
    assert tree(env) == before


async def test_a_task_that_is_not_finished_cannot_be_rolled_back(env: Env) -> None:
    (env.desktop / "a.txt").write_text("old")
    plan = {"summary": "overwrite", "steps": [
        step("s1", "Replace a.txt", "fs_create", {"path": "@Desktop/a.txt", "content": "new", "overwrite": True}),
    ]}  # fmt: skip
    h = make_harness(env, [structured(plan)])
    task = await h.runtime.submit("replace a.txt on the desktop")
    for _ in range(300):
        if env.approvals.pending():
            break
        await asyncio.sleep(0.01)
    assert env.approvals.pending(), "the task is waiting for the approval"
    with pytest.raises(AgentError, match="only a finished task"):
        await h.runtime.rollback(task.id)
    assert not (await h.runtime.journal(task.id)).can_roll_back
    with Responder(env, approve_all):
        await h.runtime.wait(task.id, 30)
    assert (env.desktop / "a.txt").read_text() == "new"
    await h.runtime.rollback(task.id)
    assert (env.desktop / "a.txt").read_text() == "old"


async def test_a_plan_cannot_use_the_undo_tool(env: Env) -> None:
    (env.desktop / "a.txt").write_text("keep me")
    bad = {"summary": "undo", "steps": [step("s1", "Undo", "fs_undo", {"entry_id": "fj_x"})]}
    h = make_harness(env, [structured(bad), structured(bad)])
    task = await h.run("undo whatever you did")
    assert task.status is TaskStatus.FAILED and task.result is not None
    assert task.result.error is not None and task.result.error.code is ErrorCode.PLAN_INVALID
    assert (env.desktop / "a.txt").read_text() == "keep me"


async def test_stop_during_a_rollback_stops_it_and_a_second_rollback_finishes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = seed(env)
    h = make_harness(env, [structured(tidy_plan())])
    task = await h.run("Put my quarterly reports into a Reports folder with an index")
    real = jmod.undo_entry
    calls = 0

    def stop_after_first(e: Any, previous: Any) -> Any:
        nonlocal calls
        calls += 1
        out = real(e, previous)
        if calls == 1:
            env.kill.engage("STOP AGENT pressed")
        return out

    monkeypatch.setattr(jmod, "undo_entry", stop_after_first)
    first = await h.runtime.rollback(task.id)
    assert first.stopped and first.view.open == 3 and calls == 1
    with pytest.raises(AgentError, match="emergency stop"):
        await h.runtime.rollback(task.id)
    env.kill.release()
    second = await h.runtime.rollback(task.id)
    assert second.view.open == 0 and second.attempted == 3 and tree(env) == before


async def test_a_rollback_works_after_a_restart(env: Env) -> None:
    before = seed(env)
    task = await make_harness(env, [structured(tidy_plan())]).run("Tidy the quarterly reports")
    # A new process: new runtime and journal service over the same storage.
    env.services.journal = FileJournal(env.repo, env.settings.data_dir / "journal")
    fresh = make_harness(env, [])
    result = await fresh.runtime.rollback(task.id)
    assert result.view.open == 0 and tree(env) == before
