"""NEXT-A: ranked discovery, batch organization and batch reading on a disposable workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models.tool import ToolCall
from app.utils.errors import ErrorCode

from tests.helpers import Env
from tests.workflow_fixtures import build_java_workspace, sha


async def run(env: Env, tool: str, **args: Any):  # type: ignore[no-untyped-def]
    return await env.router.execute(ToolCall(tool=tool, args=args, rationale="test"), env.ctx())


CTX = ["jvm", "class", "compiler", "bytecode", "spring"]


def rel(env_base: Path, paths: list[str]) -> set[str]:
    return {Path(p).relative_to(env_base).as_posix() for p in paths}


async def test_find_ranks_strong_and_ambiguous_conservatively(env: Env) -> None:
    base = env.documents / "ws"
    build_java_workspace(base)
    r = await run(
        env, "fs_find", root=str(base), terms=["java"], context_terms=CTX, extensions=[".pdf"]
    )
    assert r.ok, r.error
    assert rel(base, r.data["strong_paths"]) == {
        "courses/java-basics.pdf", "courses/notes/Intro_to_OOP.pdf", "archive/old/java-basics.pdf",
    }  # fmt: skip
    # the island, JavaScript and the unreadable scan are NOT confident matches
    assert rel(base, r.data["ambiguous_paths"]) == {
        "travel/java-island-guide.pdf", "courses/javascript-guide.pdf", "courses/java-notes-scan.pdf",
    }  # fmt: skip
    assert "finance/budget-2026.pdf" not in rel(base, [c["path"] for c in r.data["candidates"]])
    assert r.untrusted, "file names and excerpts are untrusted data"
    # strong candidates come first, best score first
    classes = [c["classification"] for c in r.data["candidates"]]
    assert classes == sorted(classes, key=lambda c: c != "strong")
    by_name = {Path(c["path"]).name: c for c in r.data["candidates"]}
    assert (
        "unreadable" in by_name["java-notes-scan.pdf"]["reason"]
        or "could not be read" in by_name["java-notes-scan.pdf"]["reason"]
    )
    assert (
        by_name["Intro_to_OOP.pdf"]["name_hit"] is False
        and by_name["Intro_to_OOP.pdf"]["hits"] >= 3
    )


async def test_find_reports_duplicates_and_identical_copies(env: Env) -> None:
    base = env.documents / "ws"
    build_java_workspace(base)
    r = await run(env, "fs_find", root=str(base), terms=["java"], context_terms=CTX)
    dup = {d["name"]: rel(base, d["paths"]) for d in r.data["duplicate_names"]}
    assert dup == {"java-basics.pdf": {"courses/java-basics.pdf", "archive/old/java-basics.pdf"}}
    groups = [rel(base, g["paths"]) for g in r.data["identical_groups"]]
    assert {"courses/java-basics.pdf", "archive/old/java-basics.pdf"} in groups


async def test_find_context_and_exclude_terms_demote_to_ambiguous(env: Env) -> None:
    base = env.documents / "ws"
    build_java_workspace(base)
    r = await run(
        env, "fs_find", root=str(base), terms=["java"], context_terms=["jvm", "compiler"],
        exclude_terms=["island", "indonesia"],
    )  # fmt: skip
    strong = rel(base, r.data["strong_paths"])
    assert "courses/notes/Intro_to_OOP.pdf" in strong  # mentions the JVM
    assert "travel/java-island-guide.pdf" not in strong


async def test_find_metadata_filters_and_text_files(env: Env) -> None:
    base = env.documents / "ws"
    files = build_java_workspace(base)
    r = await run(
        env, "fs_find", root=str(base), terms=["java"], context_terms=CTX, extensions=[".txt"]
    )
    assert rel(base, r.data["strong_paths"]) == set(), "no confirming word in the text file"
    big = await run(
        env, "fs_find", root=str(base), terms=["java"], context_terms=CTX, min_size=10_000_000
    )
    assert big.data["candidates"] == []
    future = await run(
        env,
        "fs_find",
        root=str(base),
        terms=["java"],
        context_terms=CTX,
        modified_after="2999-01-01",
    )
    assert future.data["candidates"] == []
    bad = await run(
        env,
        "fs_find",
        root=str(base),
        terms=["java"],
        context_terms=CTX,
        modified_after="yesterday-ish",
    )
    assert bad.error and bad.error.code == ErrorCode.INVALID_INPUT
    assert files["finance/budget-2026.pdf"].exists()


async def test_find_stays_inside_allowed_roots_and_hides_credentials(
    env: Env, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    r = await run(env, "fs_find", root=str(outside), terms=["java"], context_terms=CTX)
    assert r.error and r.error.code in (ErrorCode.PATH_BLOCKED, ErrorCode.POLICY_DENIED)
    d = env.documents / "ws"
    d.mkdir()
    (d / "credentials.txt").write_text("java java java jvm")
    (d / "ok.txt").write_text("java java java jvm")
    got = await run(
        env, "fs_find", root=str(d), terms=["java"], context_terms=CTX, extensions=[".txt"]
    )
    assert rel(d, got.data["strong_paths"]) == {"ok.txt"}


async def test_organize_moves_only_given_files_and_never_overwrites(env: Env) -> None:
    base = env.documents / "ws"
    files = build_java_workspace(base)
    found = await run(env, "fs_find", root=str(base), terms=["java"], context_terms=CTX)
    before = {k: sha(v) for k, v in files.items()}
    dest = base / "Java"
    r = await run(env, "fs_organize", sources=found.data["strong_paths"], dest_dir=str(dest))
    assert r.ok, r.error
    # one of the two identical java-basics.pdf could not be added again: it is reported, not overwritten
    assert r.data["moved_count"] == 2 and r.data["skipped_count"] == 1
    assert "identical" in r.data["skipped"][0]["reason"]
    assert sorted(p.name for p in dest.iterdir()) == ["Intro_to_OOP.pdf", "java-basics.pdf"]
    # nothing else moved or changed
    for key in ("travel/java-island-guide.pdf", "courses/javascript-guide.pdf",
                "courses/java-notes-scan.pdf", "finance/budget-2026.pdf", "misc/readme.txt"):  # fmt: skip
        assert files[key].exists() and sha(files[key]) == before[key]
    assert r.artifacts == {"dest_dir": str(dest), "moved_count": 2}


async def test_organize_is_idempotent_on_rerun(env: Env) -> None:
    base = env.documents / "ws"
    build_java_workspace(base)
    found = await run(env, "fs_find", root=str(base), terms=["java"], context_terms=CTX)
    args = {"sources": found.data["strong_paths"], "dest_dir": str(base / "Java")}
    first = await run(env, "fs_organize", **args)
    again = await run(env, "fs_organize", **args)
    assert first.ok and again.ok, again.error
    assert again.data["moved_count"] == 0
    assert len(again.data["already_in_place"]) >= 2
    assert sorted(p.name for p in (base / "Java").iterdir()) == [
        "Intro_to_OOP.pdf",
        "java-basics.pdf",
    ]


async def test_organize_validates_whole_batch_before_moving(env: Env, tmp_path: Path) -> None:
    base = env.documents / "ws"
    base.mkdir()
    (base / "a.pdf").write_bytes(b"a")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"x")
    r = await run(
        env, "fs_organize", sources=[str(base / "a.pdf"), str(outside)], dest_dir=str(base / "D")
    )
    assert r.error and r.error.code in (ErrorCode.PATH_BLOCKED, ErrorCode.POLICY_DENIED)
    assert (base / "a.pdf").exists() and not (base / "D").exists(), (
        "nothing may move on a bad batch"
    )
    missing = await run(
        env,
        "fs_organize",
        sources=[str(base / "a.pdf"), str(base / "nope.pdf")],
        dest_dir=str(base / "D"),
    )
    assert missing.error and missing.error.code == ErrorCode.NOT_FOUND
    assert (base / "a.pdf").exists() and not (base / "D").exists()


async def test_organize_rename_keeps_both_and_copy_keeps_source(env: Env) -> None:
    base = env.documents / "ws"
    (base / "x").mkdir(parents=True)
    (base / "y").mkdir()
    (base / "x" / "a.pdf").write_bytes(b"one")
    (base / "y" / "a.pdf").write_bytes(b"two")
    r = await run(env, "fs_organize", sources=[str(base / "x" / "a.pdf"), str(base / "y" / "a.pdf")],
                  dest_dir=str(base / "All"), action="copy", on_conflict="rename")  # fmt: skip
    assert r.ok, r.error
    assert sorted(p.name for p in (base / "All").iterdir()) == ["a (2).pdf", "a.pdf"]
    assert (base / "x" / "a.pdf").exists() and (base / "y" / "a.pdf").exists()
    skip = await run(
        env,
        "fs_organize",
        sources=[str(base / "y" / "a.pdf")],
        dest_dir=str(base / "All"),
        action="move",
    )
    assert skip.ok and skip.data["moved_count"] == 0 and (base / "y" / "a.pdf").exists()


async def test_read_many_bounded_and_reports_unreadable(env: Env) -> None:
    base = env.documents / "ws"
    build_java_workspace(base)
    paths = [
        str(base / "courses/java-basics.pdf"),
        str(base / "courses/java-notes-scan.pdf"),
        str(base / "misc/readme.txt"),
    ]
    r = await run(env, "fs_read_many", paths=paths, max_chars_each=300)
    assert r.ok and r.untrusted
    docs = {d["name"]: d for d in r.data["documents"]}
    assert "Java" in docs["java-basics.pdf"]["text"] and docs["java-basics.pdf"]["format"] == "pdf"
    assert docs["java-notes-scan.pdf"]["error"] and docs["java-notes-scan.pdf"]["text"] == ""
    assert r.data["read_count"] == 2 and r.data["failed_count"] == 1
    only_bad = await run(env, "fs_read_many", paths=[str(base / "courses/java-notes-scan.pdf")])
    assert only_bad.error and only_bad.error.code == ErrorCode.IO_ERROR


async def test_observed_ledger_records_reads_urls_and_writes(env: Env) -> None:
    base = env.documents / "ws"
    build_java_workspace(base)
    ctx = env.ctx()
    await env.router.execute(
        ToolCall(
            tool="fs_read_many",
            args={"paths": [str(base / "courses/java-basics.pdf")]},
            rationale="t",
        ),
        ctx,
    )
    assert any("bytecode" in t for t in ctx.observed.texts)
    w = await env.router.execute(
        ToolCall(
            tool="fs_create",
            args={
                "path": str(base / "out.md"),
                "content": "see https://spring.io/projects/spring-boot/ ok",
            },
            rationale="t",
        ),
        ctx,
    )
    assert w.ok and ctx.observed.written == [str(base / "out.md")]
