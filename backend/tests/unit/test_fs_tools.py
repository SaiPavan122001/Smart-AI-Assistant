from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from app.models.approval import ApprovalDecision
from app.models.enums import RiskLevel
from app.models.tool import ToolCall
from app.utils.errors import ErrorCode

from tests.helpers import Env


async def run(env: Env, tool: str, **args: Any):  # type: ignore[no-untyped-def]
    return await env.router.execute(ToolCall(tool=tool, args=args, rationale="test"), env.ctx())


async def test_create_list_read_search(env: Env) -> None:
    assert (await run(env, "fs_create", path="@Desktop/proj", kind="dir")).ok
    r = await run(env, "fs_create", path="@Desktop/proj/notes.txt", content="Spring Boot rocks")
    assert r.ok and r.artifacts["path"].endswith("notes.txt")
    again = await run(env, "fs_create", path="@Desktop/proj/notes.txt", content="x")
    assert again.error and again.error.code == ErrorCode.ALREADY_EXISTS
    listing = await run(env, "fs_list", path="@Desktop/proj")
    assert [e["name"] for e in listing.data["entries"]] == ["notes.txt"]
    read = await run(env, "fs_read", path="@Desktop/proj/notes.txt")
    assert read.data["text"] == "Spring Boot rocks" and read.untrusted
    found = await run(env, "fs_search", root="@Desktop", content_query="spring")
    assert [Path(m["path"]).name for m in found.data["matches"]] == ["notes.txt"]
    pdfs = await run(env, "fs_search", root="@Desktop", name_pattern="*.txt", extensions=[".txt"])
    assert len(pdfs.data["matches"]) == 1


async def test_overwrite_requires_flag_and_raises_risk(env: Env) -> None:
    (env.desktop / "a.txt").write_text("old")
    tool = env.router.registry.get("fs_create")
    ctx = env.ctx()
    args = tool.Input(path="@Desktop/a.txt", content="new", overwrite=True)  # type: ignore[union-attr]
    assert tool.assess(args, ctx).risk == RiskLevel.HIGH  # type: ignore[union-attr]
    t = asyncio.create_task(
        run(env, "fs_create", path="@Desktop/a.txt", content="new", overwrite=True)
    )
    for _ in range(100):
        if env.approvals.pending():
            break
        await asyncio.sleep(0.01)
    assert (env.desktop / "a.txt").read_text() == "old", "overwrite must wait for approval"
    await env.approvals.resolve(env.approvals.pending()[0].id, ApprovalDecision(approved=True))
    assert (await t).ok and (env.desktop / "a.txt").read_text() == "new"


async def test_move_and_copy_semantics(env: Env) -> None:
    (env.downloads / "doc.pdf").write_bytes(b"%PDF-1.4")
    (env.desktop / "Docs").mkdir()
    r = await run(env, "fs_move", src="@Downloads/doc.pdf", dst="@Desktop/Docs")
    assert (
        r.ok
        and (env.desktop / "Docs" / "doc.pdf").exists()
        and not (env.downloads / "doc.pdf").exists()
    )
    c = await run(env, "fs_copy", src="@Desktop/Docs/doc.pdf", dst="@Documents/copy.pdf")
    assert (
        c.ok
        and (env.documents / "copy.pdf").exists()
        and (env.desktop / "Docs" / "doc.pdf").exists()
    )
    exists = await run(env, "fs_copy", src="@Desktop/Docs/doc.pdf", dst="@Documents/copy.pdf")
    assert exists.error and exists.error.code == ErrorCode.ALREADY_EXISTS
    missing = await run(env, "fs_move", src="@Downloads/nope.pdf", dst="@Desktop")
    assert missing.error and missing.error.code == ErrorCode.NOT_FOUND
    (env.desktop / "Docs" / "inner").mkdir()
    into_self = await run(env, "fs_move", src="@Desktop/Docs", dst="@Desktop/Docs/inner")
    assert not into_self.ok


@pytest.mark.parametrize(
    "path",
    [
        "../../outside.txt",
        "C:/Windows/win.ini",
        "@Desktop/../../escape.txt",
        "@Nope/x",
        "\\\\server\\share\\x.txt",
        "@Desktop/a.exe",
        "@Desktop/run.ps1",
        "@Desktop/x.bat",
    ],
)
async def test_writes_outside_policy_are_refused(env: Env, path: str) -> None:
    r = await run(env, "fs_create", path=path, content="x")
    assert not r.ok and r.error and r.error.code == ErrorCode.POLICY_DENIED, path
    assert not env.approvals.pending()


async def test_reads_are_policy_controlled_and_binary_safe(env: Env) -> None:
    (env.desktop / ".env").write_text("SECRET=1")
    (env.desktop / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    for p in ("@Desktop/.env", "C:/Windows/System32/drivers/etc/hosts", "../../etc/passwd"):
        r = await run(env, "fs_read", path=p)
        assert r.error and r.error.code in (ErrorCode.POLICY_DENIED, ErrorCode.NOT_FOUND), p
    r = await run(env, "fs_read", path="@Desktop/blob.bin")
    assert r.error and "binary" in r.error.message


async def test_delete_always_needs_approval_uses_recycle_bin_and_flags_recursive(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    trashed: list[str] = []
    monkeypatch.setattr(
        "send2trash.send2trash",
        lambda p: (trashed.append(p), Path(p).unlink() if Path(p).is_file() else None)[0],
    )
    (env.desktop / "old.txt").write_text("x")
    t = asyncio.create_task(run(env, "fs_delete", path="@Desktop/old.txt"))
    for _ in range(100):
        if env.approvals.pending():
            break
        await asyncio.sleep(0.01)
    req = env.approvals.pending()[0]
    assert (
        req.risk == RiskLevel.HIGH
        and "old.txt" in req.action
        and (env.desktop / "old.txt").exists()
    )
    await env.approvals.resolve(req.id, ApprovalDecision(approved=True))
    assert (await t).ok and trashed == [str(env.desktop / "old.txt")]

    folder = env.desktop / "proj"
    folder.mkdir()
    (folder / "f.txt").write_text("x")
    blocked = await run(env, "fs_delete", path="@Desktop/proj")  # non-empty without recursive=true
    assert not blocked.ok and blocked.error.code == ErrorCode.POLICY_DENIED  # type: ignore[union-attr]
    t2 = asyncio.create_task(run(env, "fs_delete", path="@Desktop/proj", recursive=True))
    for _ in range(100):
        if env.approvals.pending():
            break
        await asyncio.sleep(0.01)
    req2 = env.approvals.pending()[0]
    assert (
        req2.risk == RiskLevel.CRITICAL
        and "1 file" in " ".join(req2.risk_explanation)
        and req2.allowed_scopes == [req2.allowed_scopes[0]]
    )
    await env.approvals.resolve(req2.id, ApprovalDecision(approved=False))
    assert not (await t2).ok and folder.exists()


async def test_root_folders_themselves_cannot_be_deleted_or_moved(env: Env) -> None:
    assert not (await run(env, "fs_delete", path="@Desktop")).ok
    assert not (await run(env, "fs_move", src="@Desktop", dst="@Documents/d")).ok
    assert env.desktop.exists()


async def test_pdf_text_extraction(env: Env) -> None:
    import io

    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    (env.downloads / "blank.pdf").write_bytes(buf.getvalue())
    r = await run(env, "fs_read", path="@Downloads/blank.pdf")
    assert r.ok and r.data["format"] == "pdf" and r.data["pages"] == 1
    (env.downloads / "broken.pdf").write_bytes(b"not a pdf")
    assert not (await run(env, "fs_read", path="@Downloads/broken.pdf")).ok


async def test_write_size_limit(env: Env) -> None:
    big = "x" * (env.settings.filesystem.max_write_bytes + 1)
    r = await run(env, "fs_create", path="@Desktop/big.txt", content=big)
    assert not r.ok and "maximum write size" in r.error.message  # type: ignore[union-attr]
