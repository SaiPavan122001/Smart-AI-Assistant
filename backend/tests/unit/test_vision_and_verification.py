from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from app.config.settings import LimitSettings
from app.llm.gateway import LLMGateway
from app.llm.scripted import ScriptedProvider, structured
from app.models.tool import ToolCall
from app.observability.usage import UsageMeter
from app.tools.desktop.memory import FakeDesktopBackend, FakeElement
from app.tools.desktop.service import DesktopService
from app.tools.vision.service import (
    LLMVisionProvider,
    ObservationRequest,
    VisionService,
)
from app.utils.control import CancelToken
from app.utils.errors import AgentError, ErrorCode
from app.verification.checks import run_checker
from PIL import Image

from tests.helpers import Env, make_env


def png(w: int = 400, h: int = 300) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


async def run(env: Env, ctx: Any, tool: str, **args: Any):  # type: ignore[no-untyped-def]
    return await env.router.execute(ToolCall(tool=tool, args=args, rationale="test"), ctx)


# ----------------------------------------------------------------------------- vision
def vision_env(tmp_path: Path, script: list[Any]) -> tuple[Env, VisionService, ScriptedProvider]:
    env = make_env(tmp_path, include_desktop=True, include_vision=True)
    llm = ScriptedProvider(script)
    svc = VisionService(LLMVisionProvider(LLMGateway(llm)), min_confidence=0.6)
    env.services.vision = svc
    be = FakeDesktopBackend()
    be.add_window("LegacyApp", "legacy.exe", elements=[FakeElement("pane", "canvas")], active=True)
    env.services.desktop = DesktopService(be, env.settings.desktop)
    return env, svc, llm


async def test_vision_is_never_used_unless_explicitly_requested(tmp_path: Path) -> None:
    env, svc, llm = vision_env(tmp_path, [])
    ctx = env.ctx()
    await run(env, ctx, "desktop_find_element", query="Save")  # UIA fails; still no vision call
    assert llm.requests == [] and ctx.meter.usage.vision_calls == 0


async def test_capture_analyze_and_confident_click_flow(tmp_path: Path) -> None:
    analysis = {
        "answer": "The Save button is at the top right",
        "confidence": 0.9,
        "elements": [
            {"label": "Save", "kind": "button", "bbox": [100, 50, 140, 70], "confidence": 0.92},
            {"label": "Maybe", "kind": "icon", "bbox": [10, 10, 20, 20], "confidence": 0.3},
        ],
    }
    env, svc, llm = vision_env(tmp_path, [structured(analysis)])
    clicked: list[tuple[int, int]] = []
    env.services.desktop.fallback = type(
        "F",
        (),
        {
            "click_point": lambda self, x, y: clicked.append((x, y)),
            "screenshot_region": lambda self, *a: png(),
            "hotkey": lambda self, k: None,
        },
    )()  # type: ignore[union-attr,assignment]
    ctx = env.ctx()
    cap = await run(
        env,
        ctx,
        "vision_capture",
        source="window",
        window_title="LegacyApp",
        reason="UI Automation exposes only an empty canvas",
    )
    assert cap.ok and cap.perception_source == "vision"
    ana = await run(
        env,
        ctx,
        "vision_analyze",
        capture_id=cap.data["capture_id"],
        goal="the Save button",
        mode="locate",
    )
    assert ana.ok and ana.data["confidence"] == 0.9 and ana.data["needs_confirmation"] is False
    assert ctx.meter.usage.vision_calls == 1 and ctx.meter.usage.llm_calls == 0
    ref = ana.data["elements"][0]["ref"]
    click = await run(env, ctx, "desktop_click", vision_ref=ref)
    assert click.ok and click.perception_source == "vision" and len(clicked) == 1
    assert (
        100 <= clicked[0][0] <= 140 and 50 <= clicked[0][1] <= 70
    )  # window origin is (0,0) in the fake
    low = await run(env, ctx, "desktop_click", vision_ref=ana.data["elements"][1]["ref"])
    assert not low.ok and low.error.code == ErrorCode.LOW_CONFIDENCE and len(clicked) == 1, (
        "low confidence must not click"
    )  # type: ignore[union-attr]
    bad = await run(env, ctx, "desktop_click", vision_ref="vis_deadbeef:e1")
    assert not bad.ok


async def test_low_overall_confidence_is_flagged_and_budget_is_enforced(tmp_path: Path) -> None:
    env, svc, llm = vision_env(
        tmp_path,
        [
            structured({"answer": "not sure", "confidence": 0.2, "elements": []}),
            structured({"answer": "x", "confidence": 0.9}),
        ],
    )
    env.settings.limits.max_vision_calls = 1
    ctx = env.ctx()
    ctx.meter.limits = LimitSettings(max_vision_calls=1)
    cap = await run(
        env, ctx, "vision_capture", source="window", reason="no accessibility info at all"
    )
    a = await run(env, ctx, "vision_analyze", capture_id=cap.data["capture_id"], goal="find x")
    assert a.data["needs_confirmation"] is True and "LOW CONFIDENCE" in a.summary
    again = await run(env, ctx, "vision_analyze", capture_id=cap.data["capture_id"], goal="find x")
    assert not again.ok and again.error.code == ErrorCode.LIMIT_EXCEEDED  # type: ignore[union-attr]


async def test_capture_refuses_protected_windows_and_requires_a_reason(tmp_path: Path) -> None:
    env, svc, llm = vision_env(tmp_path, [])
    env.services.desktop.backend.add_window("KeePassXC", "KeePassXC.exe", active=True)  # type: ignore[union-attr]
    ctx = env.ctx()
    r = await run(
        env,
        ctx,
        "vision_capture",
        source="window",
        window_title="KeePassXC",
        reason="please look at it",
    )
    assert not r.ok and r.error.code == ErrorCode.POLICY_DENIED  # type: ignore[union-attr]
    r2 = await run(env, ctx, "vision_capture", source="window", reason="x")  # too short to justify
    assert not r2.ok and r2.error.code == ErrorCode.INVALID_INPUT  # type: ignore[union-attr]


def test_capture_store_downscales_and_maps_coordinates() -> None:
    svc = VisionService(None, min_confidence=0.5, max_captures_per_task=2)
    meta = svc.store_capture("t", png(3200, 1600), source="screen", origin=(100, 200))
    assert max(meta.image_width, meta.image_height) <= 1568 and meta.width == 3200
    for _ in range(3):
        svc.store_capture("t", png(), source="screen")
    with pytest.raises(AgentError):
        svc.get_capture("t", meta.id)  # evicted: captures are bounded per task


async def test_analyze_without_provider_fails_clearly() -> None:
    svc = VisionService(None)
    meta = svc.store_capture("t", png(), source="screen")
    with pytest.raises(AgentError) as ei:
        await svc.analyze(
            "t",
            ObservationRequest(capture_id=meta.id, goal="x"),
            meter=UsageMeter(LimitSettings()),
            token=CancelToken(),
        )
    assert ei.value.code == ErrorCode.NOT_AVAILABLE


# ----------------------------------------------------------------------------- verification contracts
async def check(env: Env, name: str, **params: Any) -> bool:
    return (await run_checker(name, params, env.ctx())).passed


async def test_file_verifications(env: Env) -> None:
    (env.desktop / "a.txt").write_text("hello world")
    (env.desktop / "dir").mkdir()
    (env.downloads / "doc.pdf").write_bytes(b"x" * 2000)
    assert await check(env, "file_exists", path="@Desktop/a.txt")
    assert await check(env, "file_exists", path="@Desktop/a.txt", kind="file", min_bytes=5)
    assert not await check(env, "file_exists", path="@Desktop/a.txt", min_bytes=500)
    assert not await check(env, "file_exists", path="@Desktop/a.txt", kind="dir")
    assert await check(env, "dir_exists", path="@Desktop/dir") and not await check(
        env, "file_exists", path="@Desktop/missing"
    )
    assert await check(env, "file_absent", path="@Desktop/missing") and not await check(
        env, "file_absent", path="@Desktop/a.txt"
    )
    assert await check(
        env, "file_contains", path="@Desktop/a.txt", text="HELLO"
    ) and not await check(env, "file_contains", path="@Desktop/a.txt", text="bye")
    assert await check(
        env, "files_exist", base="@Desktop", files=["a.txt", "dir"]
    ) and not await check(env, "files_exist", base="@Desktop", files=["a.txt", "nope.txt"])
    assert await check(env, "download_complete", path="@Downloads/doc.pdf", min_bytes=1000)
    assert not await check(env, "download_complete", path="@Downloads/doc.pdf", min_bytes=5000)
    (env.downloads / "x.crdownload").write_bytes(b"partial")
    assert not await check(env, "download_complete", path="@Downloads/x.crdownload")


async def test_move_verification_requires_both_halves(env: Env) -> None:
    (env.desktop / "d").mkdir()
    (env.desktop / "d" / "f.txt").write_text("x")
    assert await check(env, "file_moved", src="@Downloads/f.txt", dst="@Desktop/d/f.txt")
    (env.downloads / "f.txt").write_text("x")  # copy rather than move: source still present
    assert not await check(env, "file_moved", src="@Downloads/f.txt", dst="@Desktop/d/f.txt")
    assert not await check(
        env, "file_moved", src="@Downloads/gone.txt", dst="@Desktop/d/missing.txt"
    )
    assert not await check(
        env, "file_moved", src="@Downloads/g.txt", dst="@Desktop/d"
    )  # folder form needs <dst>/g.txt to exist


async def test_value_checks_and_unknown_kinds(env: Env) -> None:
    assert await check(env, "value_check", actual=0, op="equals", expected=0)
    assert await check(
        env, "value_check", actual="BUILD SUCCESS 5", op="contains", expected="success"
    )
    assert await check(env, "value_check", actual="abc123", op="regex", expected=r"\d{3}")
    assert await check(env, "value_check", actual=7, op="gt", expected=3) and not await check(
        env, "value_check", actual=1, op="gt", expected=3
    )
    assert not await check(env, "value_check", actual="x", op="gt", expected=1) and not await check(
        env, "value_check", actual=1, op="weird", expected=1
    )
    res = await run_checker("made_up_kind", {}, env.ctx())
    assert not res.passed and "unknown verification kind" in res.message


async def test_verification_cannot_probe_outside_allowed_roots(env: Env) -> None:
    res = await run_checker("file_exists", {"path": "C:/Windows/win.ini"}, env.ctx())
    assert not res.passed and "outside the allowed" in res.message
    res = await run_checker(
        "file_contains", {"path": "@Desktop/../../secrets.txt", "text": "x"}, env.ctx()
    )
    assert not res.passed


async def test_polling_waits_for_eventual_conditions(env: Env) -> None:
    import asyncio

    async def create_later() -> None:
        await asyncio.sleep(0.6)
        (env.desktop / "late.txt").write_text("x")

    t = asyncio.create_task(create_later())
    res = await run_checker("file_exists", {"path": "@Desktop/late.txt"}, env.ctx(), wait_s=3)
    await t
    assert res.passed
    res2 = await run_checker("file_exists", {"path": "@Desktop/never.txt"}, env.ctx(), wait_s=0.5)
    assert not res2.passed


async def test_browser_and_window_checks_without_a_session_fail_cleanly(env: Env) -> None:
    assert not await check(env, "browser_url", contains="x")
    assert not await check(env, "window_exists", title_contains="Notepad")


async def test_verify_tools_are_read_only_and_expose_results(env: Env) -> None:
    (env.desktop / "ok.txt").write_text("fine")
    ctx = env.ctx()
    r = await run(
        env,
        ctx,
        "verify_task_result",
        criteria=[
            {"kind": "file_exists", "params": {"path": "@Desktop/ok.txt"}},
            {"kind": "file_absent", "params": {"path": "@Desktop/zzz"}},
        ],
    )
    assert r.ok and r.data["passed"] and "ALL PASS" in r.summary and r.artifacts["verified"] is True
    r = await run(
        env,
        ctx,
        "verify_task_result",
        criteria=[{"kind": "file_exists", "params": {"path": "@Desktop/missing"}}],
    )
    assert r.data["passed"] is False and "FAILED" in r.summary
    fs = await run(
        env, ctx, "verify_file_state", path="@Desktop/ok.txt", expect="contains", text="fine"
    )
    assert fs.data["passed"]
