"""Single-task real-machine smoke runner (validation tooling only; no application code is modified).

    python -m validation.smoke run "<instruction>" --name calc
    python -m validation.smoke inspect <task_id>

* durable SQLite repository (configuration only), so the task stays inspectable after the process exits;
* the real configured LLM, headful Chrome, real desktop backend, approvals denied by default (and recorded);
* a read-only window watcher counting REAL window appear/disappear events for Calculator / Notepad / Chrome.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

HOME = Path.home()
DB = HOME / "SmartAIAssistant" / "data" / "smoke.db"
os.environ["SAA_DATABASE__URL"] = f"sqlite+aiosqlite:///{DB.as_posix()}"
os.environ.setdefault("SAA_DATABASE__AUTO_MIGRATE", "true")

from app.config.settings import (  # noqa: E402
    BrowserSettings,
    FilesystemSettings,
    LimitSettings,
    Settings,
)
from app.container import build_container  # noqa: E402
from app.models.approval import ApprovalDecision  # noqa: E402

WATCH = {"notepad.exe", "calculatorapp.exe", "calc.exe", "chrome.exe"}
events: list[dict[str, Any]] = []
lock = threading.Lock()


def _proc(pid: int) -> str:
    k = ctypes.windll.kernel32  # type: ignore[attr-defined]
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return "?"
    try:
        buf = ctypes.create_unicode_buffer(520)
        n = ctypes.c_ulong(520)
        return (
            Path(buf.value).name.lower()
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
            else "?"
        )
    finally:
        k.CloseHandle(h)


def _snap() -> dict[int, tuple[str, str]]:
    import win32gui
    import win32process

    out: dict[int, tuple[str, str]] = {}

    def cb(h: int, _: Any) -> None:
        if not win32gui.IsWindowVisible(h) or not win32gui.GetWindowText(h):
            return
        proc = _proc(win32process.GetWindowThreadProcessId(h)[1])
        if proc in WATCH and (
            proc != "chrome.exe" or win32gui.GetClassName(h) == "Chrome_WidgetWin_1"
        ):
            out[h] = (proc, win32gui.GetWindowText(h)[:50])

    win32gui.EnumWindows(cb, None)
    return out


def watch(stop: threading.Event) -> None:
    prev = _snap()
    while not stop.is_set():
        try:
            cur = _snap()
        except Exception:
            time.sleep(0.2)
            continue
        now = time.time()
        with lock:
            for h, (p, t) in cur.items():
                if h not in prev:
                    events.append({"t": now, "ev": "OPEN", "hwnd": h, "proc": p, "title": t})
            for h, (p, t) in prev.items():
                if h not in cur:
                    events.append({"t": now, "ev": "CLOSE", "hwnd": h, "proc": p, "title": t})
        prev = cur
        time.sleep(0.1)


def settings() -> Settings:
    return Settings(
        env="dev",
        log_json=False,
        log_level="WARNING",
        filesystem=FilesystemSettings(
            allowed_roots=[HOME / "Desktop", HOME / "Downloads"],
            workspace=HOME / "SmartAIAssistant" / "workspace",
        ),
        browser=BrowserSettings(headless=False, channel="chrome"),
        limits=LimitSettings(max_llm_calls=40, approval_timeout_s=30, max_execution_seconds=300),
    )  # type: ignore[call-arg]


async def report(c: Any, task_id: str, t0: float | None = None) -> dict[str, Any]:
    repo = c.repo
    task = await repo.get_task(task_id)
    calls = await repo.list_tool_calls(task_id)
    evs = await repo.list_events(task_id)
    acts = await repo.list_agent_actions(task_id)
    u = task.usage
    side = [r for r in calls if r.result and r.decision != "replayed"]
    fp: dict[str, int] = {}
    for r in calls:
        if r.decision == "replayed" or not r.result:
            continue
        fp[f"{r.call.tool}|{json.dumps(r.call.args, sort_keys=True, default=str)[:90]}"] = (
            fp.get(f"{r.call.tool}|{json.dumps(r.call.args, sort_keys=True, default=str)[:90]}", 0)
            + 1
        )
    res = task.result
    return {
        "task_id": task_id,
        "status": task.status.value,
        "instruction": task.instruction,
        "summary": (res.summary if res else "")[:500],
        "answer": res.answer if res else None,
        "verified": bool(res and res.verified),
        "error": (res.error.code.value + ": " + res.error.message[:200])
        if res and res.error
        else None,
        "llm_calls": u.llm_calls,
        "vision_calls": u.vision_calls,
        "tool_calls": len(calls),
        "executed_tool_calls": len(side),
        "replayed_calls": sum(1 for r in calls if r.decision == "replayed"),
        "replans": u.replans,
        "recoveries": sum(1 for e in evs if e.type.value == "recovery_started"),
        "recovery_events": [
            e.data.get("strategy") for e in evs if e.type.value == "recovery_started"
        ],
        "launch_tool_calls": sum(
            1
            for r in calls
            if r.call.tool in ("desktop_launch", "browser_open") and r.decision != "replayed"
        ),
        "approvals": u.approvals,
        "loop_guard_triggered": any(a.kind == "loop_detected" for a in acts)
        or any(e.data.get("code") == "loop_detected" for e in evs),
        "identical_side_effect_repeats": {
            k: v
            for k, v in fp.items()
            if v > 1
            and not k.startswith(
                (
                    "browser_find",
                    "browser_read",
                    "desktop_find",
                    "desktop_read",
                    "desktop_windows",
                    "desktop_active",
                    "verify_",
                )
            )
        },
        "tools": [
            (
                r.call.tool,
                "ok"
                if r.result and r.result.ok
                else (r.result.error.code.value if r.result and r.result.error else "?"),
                r.decision,
            )
            for r in calls
        ],
        "task_executions_in_db": len(await repo.list_tasks(limit=50)),
        "duration_s": round((task.finished_at - task.started_at).total_seconds(), 1)
        if task.finished_at and task.started_at
        else None,
        "cost_usd": round(u.estimated_cost_usd, 4),
    }


async def run(instruction: str, name: str) -> None:
    c = build_container(settings())
    if c.gateway is None:
        sys.exit("no LLM configured")
    await c.start()
    stop = threading.Event()
    th = threading.Thread(target=watch, args=(stop,), daemon=True)
    th.start()
    await asyncio.sleep(0.5)
    approvals: list[dict[str, Any]] = []
    done = asyncio.Event()

    async def responder() -> None:
        seen: set[str] = set()
        while not done.is_set():
            for req in list(c.approvals.pending()):
                if req.id not in seen:
                    seen.add(req.id)
                    approvals.append(
                        {
                            "tool": req.tool,
                            "action": req.action,
                            "risk": req.risk.label,
                            "decision": "denied",
                        }
                    )
                    await c.approvals.resolve(
                        req.id,
                        ApprovalDecision(approved=False, note="smoke: operator denies by default"),
                    )
            await asyncio.sleep(0.05)

    r_task = asyncio.create_task(responder())
    t0 = time.time()
    task = await c.runtime.submit(instruction)
    try:
        await c.runtime.wait(task.id, 300)
    except TimeoutError:
        await c.runtime.cancel(task.id, "smoke timeout")
        await c.runtime.wait(task.id, 20)
    wall = time.time() - t0
    await asyncio.sleep(2.0)  # catch late window closes
    done.set()
    r_task.cancel()
    rep = await report(c, task.id)
    stop.set()
    th.join(timeout=2)
    with lock:
        win = [{**e, "t": round(e["t"] - t0, 1)} for e in events if e["t"] >= t0 - 0.1]
    rep.update(
        model=f"{c.llm.name}/{c.llm.model}" if c.llm else "?",
        wall_s=round(wall, 1),
        approvals_asked=approvals,
        window_events=win,
        real_opens={p: sum(1 for e in win if e["ev"] == "OPEN" and e["proc"] == p) for p in WATCH},
        real_closes={
            p: sum(1 for e in win if e["ev"] == "CLOSE" and e["proc"] == p) for p in WATCH
        },
    )
    await c.stop()
    out = Path(__file__).parent / "results" / f"smoke-{name}.json"
    out.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    print(json.dumps(rep, indent=2, default=str, ensure_ascii=True))


async def inspect(task_id: str) -> None:
    c = build_container(settings())
    await c.start()
    rep = await report(c, task_id)
    print(
        "RE-OPENED FROM DISK:",
        json.dumps(
            {
                k: rep[k]
                for k in ("task_id", "status", "llm_calls", "tool_calls", "verified", "answer")
            },
            default=str,
            ensure_ascii=True,
        ),
    )
    await c.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("instruction")
    r.add_argument("--name", required=True)
    i = sub.add_parser("inspect")
    i.add_argument("task_id")
    a = ap.parse_args()
    asyncio.run(run(a.instruction, a.name) if a.cmd == "run" else inspect(a.task_id))
