"""NEXT-C: filesystem confinement against Windows path semantics.

Negative tests: every case here is an attempt to read, list or write something outside the allowed
folders (or to name something Windows treats specially) and must be refused or must not reveal it.
The traversal tests exist because ``os.path.islink`` is False for a junction: a junction placed inside
an allowed folder used to be walked by ``fs_search``, exposing the names of files outside it.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from app.models.tool import ToolCall
from app.security.paths import PathPolicy, is_redirect
from app.utils.errors import ErrorCode, PathViolation

from tests.helpers import Env
from tests.workflow_fixtures import make_pdf

windows = pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")


def junction(link: Path, target: Path) -> None:
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
    )
    if r.returncode != 0:
        pytest.skip("cannot create a junction on this machine")


@pytest.fixture
def outside(env: Env) -> Path:
    """A folder next to the allowed ones, with content that must never be seen."""
    out = env.root / "outside"
    out.mkdir()
    (out / "payroll.txt").write_text("salary figures: confidential-marker-7731")
    (out / "java-notes.pdf").write_bytes(
        make_pdf(["Java JVM class bytecode confidential-marker-7731"])
    )
    sub = out / "deep"
    sub.mkdir()
    (sub / "more.txt").write_text("confidential-marker-7731")
    return out


async def run(env: Env, tool: str, **args: Any) -> Any:
    return await env.router.execute(ToolCall(tool=tool, args=args), env.ctx())


# ----------------------------------------------------------------------------- names and syntax
@windows
@pytest.mark.parametrize(
    "name",
    [
        "CONIN$",
        "CONOUT$",
        "conin$.txt",
        "COM\u00b9",
        "LPT\u00b2.log",
        "com1",
        "LPT9.txt",
        "AUX",
        "prn.md",
        "NUL",
    ],
)
def test_reserved_device_names_are_refused_anywhere_in_a_path(env: Env, name: str) -> None:
    for raw in (str(env.documents / name), str(env.documents / "sub" / name / "x.txt")):
        with pytest.raises(PathViolation):
            env.paths.resolve(raw, write=True)


@windows
@pytest.mark.parametrize("raw", ["C:notes.txt", "c:folder\\notes.txt", "D:x", "C:..\\x.txt"])
def test_drive_relative_paths_are_refused(env: Env, raw: str) -> None:
    """``C:name`` means "relative to the current directory of drive C", which is not the workspace."""
    with pytest.raises(PathViolation):
        env.paths.resolve(raw)


@windows
@pytest.mark.parametrize(
    "raw",
    [
        "\\\\?\\C:\\Windows\\win.ini",
        "\\\\.\\C:\\Windows\\win.ini",
        "\\\\.\\GLOBALROOT\\Device\\HarddiskVolume1\\x",
        "\\??\\C:\\Windows\\win.ini",
        "//?/C:/Windows/win.ini",
        "\\\\localhost\\C$\\Windows\\win.ini",
        "\\\\127.0.0.1\\C$\\x",
        "C:\\Windows\\win.ini",
        "C:/Windows/System32/config/SAM",
    ],
)
def test_device_unc_and_system_paths_are_refused(env: Env, raw: str) -> None:
    with pytest.raises(PathViolation):
        env.paths.resolve(raw)


@windows
def test_case_separators_and_short_names_cannot_leave_or_disguise_a_path(
    env: Env, outside: Path
) -> None:
    inside = env.documents / "Long Folder Name"
    inside.mkdir()
    (inside / "a.txt").write_text("x")
    buf = ctypes.create_unicode_buffer(600)
    ctypes.windll.kernel32.GetShortPathNameW(str(inside), buf, 600)
    assert env.paths.resolve(buf.value + "\\a.txt").path == (inside / "a.txt").resolve()
    assert (
        env.paths.resolve(str(inside).upper().replace("\\", "/") + "/a.txt").path
        == (inside / "a.txt").resolve()
    )
    ctypes.windll.kernel32.GetShortPathNameW(str(outside), buf, 600)
    for raw in (
        buf.value + "\\payroll.txt",
        str(outside).upper() + "\\payroll.txt",
        str(env.documents) + "\\..\\outside\\payroll.txt",
        str(env.documents) + "/sub/../../outside/payroll.txt",
    ):
        with pytest.raises(PathViolation):
            env.paths.resolve(raw)


@windows
def test_a_trailing_dot_or_space_cannot_smuggle_a_blocked_name_or_extension(env: Env) -> None:
    """Windows drops trailing dots and spaces: ``run.exe.`` IS ``run.exe``."""
    for raw in ("tool.exe.", "tool.exe ", "script.ps1. .", ".env.", "id_rsa "):
        with pytest.raises(PathViolation):
            env.paths.resolve(str(env.documents / raw), write=True)


# ----------------------------------------------------------------------------- junctions
@windows
def test_a_junction_is_recognised_as_a_redirect(env: Env, outside: Path) -> None:
    link = env.documents / "shortcut"
    junction(link, outside)
    assert is_redirect(link) and not os.path.islink(link), "islink() alone misses a junction"
    assert not is_redirect(env.documents) and not is_redirect(outside / "payroll.txt")
    for raw in (str(link), str(link / "payroll.txt"), str(link / "new.txt")):
        with pytest.raises(PathViolation):
            env.paths.resolve(raw, write=True)


@windows
async def test_listing_and_searching_never_follow_a_junction_out_of_the_allowed_folder(
    env: Env, outside: Path
) -> None:
    (env.documents / "mine.txt").write_text("my own note")
    junction(env.documents / "shortcut", outside)
    nested = env.documents / "projects"
    nested.mkdir()
    junction(nested / "also", outside / "deep")
    marker = "confidential-marker-7731"
    calls: list[tuple[str, dict[str, Any]]] = [
        ("fs_search", {"root": str(env.documents)}),
        ("fs_search", {"root": str(env.documents), "name_pattern": "*.txt"}),
        ("fs_search", {"root": str(env.documents), "content_query": "confidential"}),
        ("fs_list", {"path": str(env.documents), "recursive": True}),
        ("fs_find", {"root": str(env.documents), "terms": ["java"], "context_terms": ["jvm", "class"]}),
        ("fs_find", {"root": str(env.documents), "terms": ["salary"], "context_terms": ["figures"]}),
    ]  # fmt: skip
    for tool, args in calls:
        result = await run(env, tool, **args)
        assert result.ok, (tool, result.error)
        text = repr(result.data) + result.summary
        for leaked in ("payroll", "java-notes", "more.txt", marker):
            assert leaked not in text, (
                f"{tool} {args} revealed '{leaked}' from outside the allowed folder"
            )
    found = await run(env, "fs_search", root=str(env.documents), name_pattern="*.txt")
    assert [m["name"] for m in found.data["matches"]] == ["mine.txt"]


@windows
async def test_reading_writing_and_copying_through_a_junction_are_refused(
    env: Env, outside: Path
) -> None:
    link = env.documents / "shortcut"
    junction(link, outside)
    before = sorted(p.name for p in outside.rglob("*"))
    for tool, args in (
        ("fs_read", {"path": str(link / "payroll.txt")}),
        ("fs_read_many", {"paths": [str(link / "payroll.txt")]}),
        ("fs_create", {"path": str(link / "planted.txt"), "content": "x"}),
        ("fs_move", {"src": str(link / "payroll.txt"), "dst": str(env.documents / "got.txt")}),
        ("fs_copy", {"src": str(link / "payroll.txt"), "dst": str(env.documents / "got.txt")}),
        ("fs_delete", {"path": str(link / "payroll.txt")}),
        ("fs_organize", {"sources": [str(link / "payroll.txt")], "dest_dir": str(env.documents / "x")}),
    ):  # fmt: skip
        result = await run(env, tool, **args)
        leaked = "confidential-marker-7731" in repr(result.data)
        assert not leaked and (not result.ok or tool == "fs_read_many"), (tool, result)
    assert sorted(p.name for p in outside.rglob("*")) == before, "nothing outside was touched"
    assert not (env.documents / "got.txt").exists()


@windows
async def test_copying_a_folder_does_not_copy_what_a_junction_inside_it_points_to(
    env: Env, outside: Path
) -> None:
    src = env.documents / "project"
    src.mkdir()
    (src / "readme.txt").write_text("mine")
    junction(src / "vendor", outside)
    result = await run(env, "fs_copy", src=str(src), dst=str(env.documents / "project-copy"))
    copied = env.documents / "project-copy"
    names = sorted(p.name for p in copied.rglob("*")) if copied.exists() else []
    assert "payroll.txt" not in names and "more.txt" not in names, names
    if result.ok:
        assert (copied / "readme.txt").read_text() == "mine"
    else:
        assert result.error and result.error.code in (
            ErrorCode.PATH_BLOCKED,
            ErrorCode.POLICY_DENIED,
        )


@windows
async def test_a_junction_planted_after_the_check_is_still_caught_at_execution(
    env: Env, outside: Path
) -> None:
    """The path is resolved again when the tool runs, not only when the risk is assessed."""
    target = env.documents / "inbox"
    target.mkdir()
    tool = env.router.registry.get("fs_create")
    assert tool is not None
    ctx = env.ctx()
    args = tool.Input.model_validate({"path": str(target / "note.txt"), "content": "x"})
    tool.assess(args, ctx)  # passes: an ordinary folder
    target.rmdir()
    junction(target, outside)  # swapped between the check and the use
    with pytest.raises(PathViolation):
        await tool.run(args, ctx)
    assert not (outside / "note.txt").exists()


def test_the_goal_check_cannot_probe_names_outside_the_allowed_folders(env: Env) -> None:
    policy: PathPolicy = env.paths
    with pytest.raises(PathViolation):
        policy.resolve(str(env.root.parent / "anything.txt"))


# ----------------------------------------------------------------------------- credential files
async def test_search_cannot_see_or_probe_credential_looking_files(env: Env) -> None:
    """A content search must not become an oracle for what a secrets file contains."""
    (env.documents / "notes.txt").write_text("the launch code word is tangerine")
    for name in ("secrets.json", "credentials.yaml", "server.pem", "id_rsa"):
        (env.documents / name).write_text("tangerine is also in here: hunter2")
    by_name = await run(env, "fs_search", root=str(env.documents))
    assert [m["name"] for m in by_name.data["matches"]] == ["notes.txt"]
    by_content = await run(env, "fs_search", root=str(env.documents), content_query="tangerine")
    assert [m["name"] for m in by_content.data["matches"]] == ["notes.txt"]
    probe = await run(env, "fs_search", root=str(env.documents), content_query="hunter2")
    assert probe.ok and probe.data["matches"] == [], "the content of a credential file was probed"
    found = await run(
        env, "fs_find", root=str(env.documents), terms=["tangerine"], context_terms=["launch"]
    )
    assert all("notes.txt" in c["path"] for c in found.data["candidates"])
    for name in ("secrets.json", "id_rsa"):
        read = await run(env, "fs_read", path=str(env.documents / name))
        assert not read.ok and "hunter2" not in repr(read)
