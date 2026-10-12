"""NEXT-D: transactional file changes. Journal written before every change, staged and verified
writes, nothing destroyed by an overwrite, undo only from the state the agent left, rollback that is
idempotent and continues after it stopped half-way."""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.models.enums import TaskStatus
from app.models.journal import JournalEntry, JournalOp, JournalState
from app.models.tool import ToolCall, ToolResult
from app.orchestration.rollback import RollbackResult, run_rollback
from app.security.paths import PathPolicy
from app.tools.filesystem import journal as jmod
from app.tools.filesystem.journal import FileJournal
from app.utils.errors import ErrorCode

from tests.helpers import Env, Responder, approve_all

TASK = "task_test"
Op, St = JournalOp, JournalState


@pytest.fixture
def recycle(env: Env, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for the Recycle Bin: what is sent there can be inspected, nothing is destroyed."""
    bin_ = env.root / "RecycleBin"
    bin_.mkdir()

    def send(p: str) -> None:
        src = Path(p)
        os.rename(src, bin_ / f"{len(os.listdir(bin_))}-{src.name}")

    monkeypatch.setattr("send2trash.send2trash", send)
    return bin_


async def run(env: Env, tool: str, **args: Any) -> ToolResult:
    return await env.router.execute(ToolCall(tool=tool, args=args, rationale="test"), env.ctx())


async def rollback(env: Env) -> RollbackResult:
    deps = SimpleNamespace(
        services=env.services, bus=env.bus, router=env.router, metrics=env.metrics
    )
    return await run_rollback(deps, env.kill, TASK, TaskStatus.COMPLETED)  # type: ignore[arg-type]


async def journal(env: Env) -> list[JournalEntry]:
    return await env.repo.list_journal(TASK)


def snapshot(env: Env) -> dict[str, Any]:
    """Everything in the user's folders: path -> bytes (files) or '<dir>'."""
    out: dict[str, Any] = {}
    for top in (env.desktop, env.downloads, env.documents):
        for p in sorted(top.rglob("*")):
            out[str(p.relative_to(env.root))] = "<dir>" if p.is_dir() else p.read_bytes()
    return out


def in_bin(recycle: Path) -> list[str]:
    return sorted(n.split("-", 1)[1] for n in os.listdir(recycle))


# ============================================================================ journal: before and after
async def test_every_change_is_journalled_before_it_happens_and_settled_from_the_files(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[JournalOp, JournalState, bool]] = []
    save = env.repo.save_journal_entry

    async def spy(e: JournalEntry) -> None:
        seen.append((e.op, e.state, os.path.exists(e.path)))
        await save(e)

    monkeypatch.setattr(env.repo, "save_journal_entry", spy)
    r = await run(env, "fs_create", path="@Desktop/a/b/notes.txt", content="hello")
    assert r.ok
    entries = await journal(env)
    assert [(e.op, Path(e.path).name, e.state) for e in entries] == [
        (Op.CREATE_DIR, "a", St.APPLIED), (Op.CREATE_DIR, "b", St.APPLIED),
        (Op.CREATE_FILE, "notes.txt", St.APPLIED),
    ]  # fmt: skip
    assert [e.seq for e in entries] == [1, 2, 3]
    assert {e.tool_call_id for e in entries} == {r.tool_call_id}
    assert entries[-1].fingerprint == "sha256:" + hashlib.sha256(b"hello").hexdigest()
    # Write-ahead: every entry was stored while its item did not exist yet, then settled after.
    assert seen == [
        (Op.CREATE_DIR, St.PENDING, False), (Op.CREATE_DIR, St.APPLIED, True),
        (Op.CREATE_DIR, St.PENDING, False), (Op.CREATE_DIR, St.APPLIED, True),
        (Op.CREATE_FILE, St.PENDING, False), (Op.CREATE_FILE, St.APPLIED, True),
    ]  # fmt: skip


async def test_a_change_that_cannot_be_journalled_is_not_made(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    (env.downloads / "r.pdf").write_bytes(b"%PDF")

    async def broken(_e: JournalEntry) -> None:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(env.repo, "save_journal_entry", broken)
    created = await run(env, "fs_create", path="@Desktop/new.txt", content="x")
    assert created.error is not None and "NOT made" in created.error.message
    assert not (env.desktop / "new.txt").exists()
    moved = await run(env, "fs_move", src="@Downloads/r.pdf", dst="@Desktop/r.pdf")
    assert moved.error is not None
    assert (env.downloads / "r.pdf").exists() and not (env.desktop / "r.pdf").exists()


async def test_a_new_journal_after_a_restart_continues_the_numbering(env: Env) -> None:
    assert (await run(env, "fs_create", path="@Desktop/one.txt", content="1")).ok
    env.services.journal = FileJournal(env.repo, env.settings.data_dir / "journal")  # new process
    assert (await run(env, "fs_create", path="@Desktop/two.txt", content="2")).ok
    assert [e.seq for e in await journal(env)] == [1, 2]


# ============================================================================ staging and verification
async def test_an_overwrite_destroys_nothing_and_a_rollback_restores_it(
    env: Env, recycle: Path
) -> None:
    for name in ("a", "b", "c"):
        (env.desktop / f"{name}.txt").write_text(f"old {name}")
    (env.downloads / "new_b.txt").write_text("new b")
    (env.downloads / "new_c.txt").write_text("new c")
    before = snapshot(env)
    with Responder(env, approve_all):
        assert (
            await run(env, "fs_create", path="@Desktop/a.txt", content="new a", overwrite=True)
        ).ok
        assert (
            await run(
                env, "fs_move", src="@Downloads/new_b.txt", dst="@Desktop/b.txt", overwrite=True
            )
        ).ok
        assert (
            await run(
                env, "fs_copy", src="@Downloads/new_c.txt", dst="@Desktop/c.txt", overwrite=True
            )
        ).ok
    assert [(env.desktop / f"{n}.txt").read_text() for n in "abc"] == ["new a", "new b", "new c"]
    kept = {e.op: Path(e.backup).read_text() for e in await journal(env) if e.backup}
    assert kept == {Op.REPLACE_FILE: "old a", Op.MOVE: "old b", Op.COPY: "old c"}

    result = await rollback(env)
    assert snapshot(env) == before
    assert {e.state for e in await journal(env)} == {St.UNDONE} and result.view.open == 0
    assert in_bin(recycle) == ["a.txt", "c.txt"], "the agent's own versions go to the Recycle Bin"
    assert not any((env.settings.data_dir / "journal").rglob("*.txt")), "backups were put back"


async def test_a_copy_that_does_not_verify_is_not_put_in_place(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    (env.downloads / "a.txt").write_text("data")
    monkeypatch.setattr(jmod, "_same", lambda _a, _b: False)
    r = await run(env, "fs_copy", src="@Downloads/a.txt", dst="@Desktop/a.txt")
    assert r.error is not None and r.error.code is ErrorCode.VERIFICATION_FAILED
    assert os.listdir(env.desktop) == [], "neither the copy nor a staging file is left behind"
    copies = [e for e in await journal(env) if e.op is Op.COPY]
    assert copies and {e.state for e in copies} == {St.FAILED}


async def test_a_write_that_fails_half_way_leaves_the_previous_file_intact(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = env.desktop / "a.txt"
    target.write_text("old")
    real = os.replace

    def failing(a: Any, b: Any) -> None:
        if Path(b) == target:
            raise OSError(errno.EIO, "I/O error")
        real(a, b)

    monkeypatch.setattr(jmod.os, "replace", failing)
    with Responder(env, approve_all):
        r = await run(env, "fs_create", path="@Desktop/a.txt", content="new", overwrite=True)
    assert not r.ok and target.read_text() == "old"
    assert os.listdir(env.desktop) == ["a.txt"], "no staging file is left behind"
    entries = [e for e in await journal(env) if e.op is Op.REPLACE_FILE]
    assert entries and {e.state for e in entries} == {St.FAILED}
    assert not any(p.is_file() for p in (env.settings.data_dir / "journal").rglob("*")), (
        "an unused backup copy is removed"
    )


async def test_moving_a_file_onto_itself_is_refused_and_destroys_nothing(env: Env) -> None:
    """Regression: overwrite=true cleared the destination first, which was the source itself."""
    victim = env.desktop / "a.txt"
    victim.write_text("precious")
    with Responder(env, approve_all):
        for tool in ("fs_move", "fs_copy"):
            r = await run(env, tool, src="@Desktop/a.txt", dst="@Desktop/a.txt", overwrite=True)
            assert r.error is not None and r.error.code is ErrorCode.INVALID_INPUT
    assert victim.read_text() == "precious"


def test_a_move_across_volumes_is_a_verified_copy_then_a_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.rename
    sources: set[Path] = set()

    def rename(a: Any, b: Any) -> None:
        if Path(a) in sources:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real(a, b)

    monkeypatch.setattr(jmod.os, "rename", rename)
    f = tmp_path / "f.txt"
    f.write_text("content")
    d = tmp_path / "folder"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "x.txt").write_text("x")
    sources.update({f, d})
    (tmp_path / "other").mkdir()
    jmod.move_verified(f, tmp_path / "other" / "f.txt")
    jmod.move_verified(d, tmp_path / "other" / "folder")
    assert (tmp_path / "other" / "f.txt").read_text() == "content" and not f.exists()
    assert (tmp_path / "other" / "folder" / "sub" / "x.txt").read_text() == "x" and not d.exists()


def test_fingerprints_follow_content_not_names(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "x.txt").write_text("same")
    assert jmod.fingerprint(a) == jmod.fingerprint(b)
    assert (jmod.fingerprint(a) or "").startswith("tree:")
    (b / "sub" / "x.txt").write_text("different")
    assert jmod.fingerprint(a) != jmod.fingerprint(b)
    assert jmod.fingerprint(tmp_path / "missing") is None


# ============================================================================ rollback
async def test_a_rollback_returns_every_item_to_where_it_was(env: Env, recycle: Path) -> None:
    (env.downloads / "r1.pdf").write_bytes(b"%PDF one")
    (env.downloads / "r2.pdf").write_bytes(b"%PDF two")
    (env.documents / "proj" / "sub").mkdir(parents=True)
    (env.documents / "proj" / "a.py").write_text("print(1)")
    (env.documents / "proj" / "sub" / "b.py").write_text("print(2)")
    before = snapshot(env)
    assert (await run(env, "fs_create", path="@Desktop/Reports", kind="dir")).ok
    assert (await run(env, "fs_organize", sources=["@Downloads/r1.pdf", "@Downloads/r2.pdf"],
                      dest_dir="@Desktop/Reports/2026")).ok  # fmt: skip
    assert (await run(env, "fs_copy", src="@Documents/proj", dst="@Desktop/proj-copy")).ok
    assert (await run(env, "fs_create", path="@Desktop/Reports/index.txt", content="r1, r2")).ok
    assert (await run(env, "fs_move", src="@Documents/proj", dst="@Desktop/proj")).ok
    assert snapshot(env) != before

    result = await rollback(env)
    assert snapshot(env) == before
    assert result.view.counts == {"undone": len(result.view.entries)} and result.view.open == 0
    assert result.errors == [] and not result.view.can_roll_back
    assert in_bin(recycle) == ["index.txt", "proj-copy"]
    types = [e.type.value for e in await env.repo.list_events(TASK)]
    assert types.index("rollback_started") < types.index("rollback_completed")
    undo_calls = [c for c in await env.repo.list_tool_calls(TASK) if c.call.tool == "fs_undo"]
    assert len(undo_calls) == len(result.view.entries), "every undo went through the router"


async def test_a_rollback_leaves_alone_what_changed_after_the_agent_left_it(
    env: Env, recycle: Path
) -> None:
    assert (await run(env, "fs_create", path="@Desktop/notes.txt", content="agent")).ok
    assert (await run(env, "fs_create", path="@Desktop/keep/x.txt", content="agent")).ok
    (env.desktop / "notes.txt").write_text("the user's edit")
    (env.desktop / "keep" / "mine.txt").write_text("the user's file")

    result = await rollback(env)
    by_name = {Path(e.path).name: e for e in result.view.entries}
    assert (
        by_name["notes.txt"].state is St.SKIPPED and "changed since" in by_name["notes.txt"].detail
    )
    assert (env.desktop / "notes.txt").read_text() == "the user's edit"
    assert by_name["x.txt"].state is St.UNDONE and not (env.desktop / "keep" / "x.txt").exists()
    assert by_name["keep"].state is St.SKIPPED and "not empty" in by_name["keep"].detail
    assert (env.desktop / "keep" / "mine.txt").read_text() == "the user's file"


async def test_a_rollback_run_twice_does_nothing_the_second_time(env: Env, recycle: Path) -> None:
    (env.downloads / "r.pdf").write_bytes(b"%PDF")
    assert (await run(env, "fs_move", src="@Downloads/r.pdf", dst="@Desktop/r.pdf")).ok
    first = await rollback(env)
    after_first = snapshot(env)
    second = await rollback(env)
    assert first.attempted == 1 and second.attempted == 0
    assert snapshot(env) == after_first and second.view.entries == first.view.entries


async def test_a_rollback_that_stopped_half_way_continues_where_it_was(
    env: Env, recycle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (env.desktop / "b.txt").write_text("old b")
    (env.downloads / "new_b.txt").write_text("new b")
    before = snapshot(env)
    with Responder(env, approve_all):
        assert (
            await run(
                env, "fs_move", src="@Downloads/new_b.txt", dst="@Desktop/b.txt", overwrite=True
            )
        ).ok
    real = jmod._restore_backup

    def device_gone(_e: JournalEntry) -> None:
        raise OSError(errno.EIO, "the device went away")

    monkeypatch.setattr(jmod, "_restore_backup", device_gone)
    first = await rollback(env)
    [entry] = first.view.entries
    assert entry.state is St.UNDOING and "did not complete" in entry.detail and first.errors
    assert (env.downloads / "new_b.txt").read_text() == "new b", "the first half was done"
    assert not (env.desktop / "b.txt").exists() and Path(entry.backup or "").exists()

    monkeypatch.setattr(jmod, "_restore_backup", real)
    second = await rollback(env)
    assert second.view.entries[0].state is St.UNDONE and second.errors == []
    assert snapshot(env) == before, "continued from the half-way state without repeating the move"


async def test_a_deletion_is_left_for_the_user_to_restore_from_the_recycle_bin(
    env: Env, recycle: Path
) -> None:
    (env.documents / "old.zip").write_bytes(b"zip")
    with Responder(env, approve_all):
        assert (await run(env, "fs_delete", path="@Documents/old.zip")).ok
    result = await rollback(env)
    [entry] = result.view.entries
    assert entry.op is Op.RECYCLE and entry.state is St.MANUAL and "Recycle Bin" in entry.detail
    assert in_bin(recycle) == ["old.zip"] and result.view.open == 0


async def test_a_change_left_pending_by_a_crash_is_judged_from_the_files(
    env: Env, recycle: Path
) -> None:
    happened = env.desktop / "done.txt"
    happened.write_bytes(b"x")
    await env.repo.save_journal_entry(JournalEntry(
        task_id=TASK, seq=1, op=Op.CREATE_FILE, path=str(happened), fingerprint=jmod.content_fingerprint(b"x"),
    ))  # fmt: skip
    await env.repo.save_journal_entry(JournalEntry(
        task_id=TASK, seq=2, op=Op.CREATE_FILE, path=str(env.desktop / "never.txt"),
        fingerprint=jmod.content_fingerprint(b"y"),
    ))  # fmt: skip
    result = await rollback(env)
    done, never = result.view.entries
    assert done.state is St.UNDONE and not happened.exists()
    assert never.state is St.SKIPPED and "never took effect" in never.detail


async def test_a_batch_that_failed_half_way_can_be_rolled_back_exactly(
    env: Env, recycle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a", "b", "c"):
        (env.downloads / f"{name}.txt").write_text(name)
    before = snapshot(env)
    real = jmod.move_verified

    def locked(src: Path, dst: Path) -> None:
        if src.name == "b.txt":
            raise OSError(errno.EACCES, "The file is in use")
        real(src, dst)

    monkeypatch.setattr("app.tools.filesystem.workflow.move_verified", locked)
    r = await run(env, "fs_organize", sources=[f"@Downloads/{n}.txt" for n in "abc"],
                  dest_dir="@Desktop/Sorted")  # fmt: skip
    assert r.error is not None and r.error.code is ErrorCode.IO_ERROR
    states = {(Path(e.path).name, e.state) for e in await journal(env)}
    assert {("a.txt", St.APPLIED), ("c.txt", St.APPLIED), ("b.txt", St.FAILED)} <= states
    assert ("b.txt", St.APPLIED) not in states

    await rollback(env)
    assert snapshot(env) == before


# ============================================================================ who may undo what
async def test_undo_runs_only_inside_a_rollback_and_only_on_this_tasks_changes(env: Env) -> None:
    assert (await run(env, "fs_create", path="@Desktop/a.txt", content="x")).ok
    [entry] = await journal(env)
    outside = await run(env, "fs_undo", entry_id=entry.id)
    assert outside.error is not None and outside.error.code is ErrorCode.POLICY_DENIED
    other_task = env.ctx("task_other", extras={"rollback": True})
    foreign = await env.router.execute(
        ToolCall(tool="fs_undo", args={"entry_id": entry.id}), other_task
    )
    assert foreign.error is not None and foreign.error.code is ErrorCode.NOT_FOUND
    assert (env.desktop / "a.txt").exists() and (await journal(env))[0].state is St.APPLIED
    assert "fs_undo" not in {t.name for t in env.router.registry.llm_tools()}


async def test_undo_is_refused_when_the_path_is_no_longer_allowed(env: Env, recycle: Path) -> None:
    assert (await run(env, "fs_create", path="@Downloads/x.txt", content="x")).ok
    env.services.path_policy = PathPolicy(
        allowed_roots=[env.desktop, env.documents], workspace=env.workspace, blocked_paths=[],
    )  # fmt: skip
    result = await rollback(env)
    assert result.errors and result.errors[0].code is ErrorCode.POLICY_DENIED
    assert (env.downloads / "x.txt").exists() and result.view.entries[0].state is St.APPLIED


async def test_an_undo_is_recorded_before_it_touches_anything(
    env: Env, recycle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process can die in the middle of an undo without running any error handler: the entry must
    already say UNDOING, so the next rollback continues it from the half-way state."""
    (env.downloads / "r.pdf").write_bytes(b"%PDF")
    assert (await run(env, "fs_move", src="@Downloads/r.pdf", dst="@Desktop/r.pdf")).ok
    seen: list[tuple[JournalState, bool]] = []
    save = env.repo.save_journal_entry

    async def spy(e: JournalEntry) -> None:
        seen.append((e.state, (env.desktop / "r.pdf").exists()))
        await save(e)

    monkeypatch.setattr(env.repo, "save_journal_entry", spy)
    await rollback(env)
    assert seen == [(St.UNDOING, True), (St.UNDONE, False)]

    # If that record cannot be written, nothing is undone.
    assert (await run(env, "fs_move", src="@Downloads/r.pdf", dst="@Desktop/r.pdf")).ok

    async def broken(_e: JournalEntry) -> None:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(env.repo, "save_journal_entry", broken)
    result = await rollback(env)
    assert (
        result.errors
        and (env.desktop / "r.pdf").exists()
        and not (env.downloads / "r.pdf").exists()
    )
