"""STEP 7 campaign runner (validation tooling only; no production code is touched).

    python -m validation.campaign run S1          # one scenario, one process, durable SQLite
    python -m validation.campaign list

Reuses validation.live scenarios/run_scenario, adds scenario-specific setups, a real-window watcher
(launch/close counts) and the extra metrics Step 7 asks for. Results: validation/results/step7-<id>.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from evals.site import Site

from validation import live as L
from validation import smoke as sm  # window watcher only

DB = L.HOME / "SmartAIAssistant" / "data" / "step7.db"
os.environ["SAA_DATABASE__URL"] = (
    f"sqlite+aiosqlite:///{DB.as_posix()}"  # set AFTER importing smoke (which sets its own)
)
os.environ["SAA_DATABASE__AUTO_MIGRATE"] = "true"

from app.config.settings import (  # noqa: E402
    BrowserSettings,
    FilesystemSettings,
    LimitSettings,
    Settings,
)
from app.container import build_container  # noqa: E402

S7 = L.SANDBOX / "step7"
TAG = os.environ.get("SAA_CAMPAIGN_TAG", "step7")  # result-file prefix (e.g. "p01" for a re-run)


def settings(extra: dict[str, Any] | None = None) -> Settings:
    return Settings(
        env="dev", log_json=False, log_level="WARNING",
        filesystem=FilesystemSettings(allowed_roots=[L.HOME / "Desktop", L.HOME / "Downloads", L.SANDBOX], workspace=L.SANDBOX / "workspace"),
        browser=BrowserSettings(headless=False, channel="chrome", allow_private_network=True),
        limits=LimitSettings(max_llm_calls=40, approval_timeout_s=60, max_execution_seconds=420),
        **(extra or {}),
    )  # type: ignore[arg-type]  # fmt: skip


# ----------------------------------------------------------------------------- extra scenarios
def scenarios() -> dict[str, L.Scenario]:
    base = L.build_scenarios()
    sc: dict[str, L.Scenario] = {}
    org = S7 / "org"

    def s1_setup(e: L.Env) -> None:
        d = L.fresh(org)
        for n in ("java-1.pdf", "java-2.pdf", "python-1.pdf"):
            (d / n).write_bytes(b"%PDF-1.4 " + n.encode())
        (d / "notes.txt").write_text("my notes")

    def s1_check(e: L.Env, t: Any) -> dict[str, Any]:
        ok = (
            (org / "Java" / "java-1.pdf").read_bytes() == b"%PDF-1.4 java-1.pdf"
            and (org / "Java" / "java-2.pdf").exists()
            and (org / "python-1.pdf").read_bytes() == b"%PDF-1.4 python-1.pdf"
            and (org / "notes.txt").read_text() == "my notes"
            and not (org / "java-1.pdf").exists() and not (org / "java-2.pdf").exists()
            and not (org / "Java" / "python-1.pdf").exists() and not (org / "Java" / "notes.txt").exists()
        )  # fmt: skip
        return {"world_ok": ok, "listing": sorted(str(p.relative_to(org)) for p in org.rglob("*"))}

    sc["S1"] = L.S("S1", "File organization", f"In the folder {org} find the Java PDFs and move them into a Java folder.",
                   setup=s1_setup, check=s1_check, max_llm_calls=8, decide=L.allow_once)  # fmt: skip
    # decide=allow_once: moving files may ask; the disposable folder is the only thing reachable by the task.

    sc["S2"] = base[
        "K1"
    ]  # safe recovery: SPA re-renders (stale element refs), local test site, non-destructive
    sc["S3"] = base["L"]  # pause / resume (browser, local site)
    sc["S4"] = L.S(
        "S4", "STOP AGENT during a running browser tool",
        lambda e: "Open Chrome and visit each of these slow pages in turn, reporting their titles: " + ", ".join(f"{e.site.base}/slow/{n}" for n in range(1, 8)),
        expect_status="CANCELLED", hook=lambda e, t: L.hook_stop_when_running(e, t, "browser_open"), max_llm_calls=8, timeout_s=150,
        check=lambda e, t: {"calls_before": e.__dict__.get("stop_calls_before"), "calls_after": e.__dict__.get("stop_calls_after")},
    )  # fmt: skip
    # (the first M2 attempt waited for a browser_navigate that never started: planning + verification polling used the whole timeout)
    sc["S7a"] = base["H1"]  # approval -> DENY
    sc["S7b"] = base["H2"]  # approval -> ALLOW once
    sc["S8a"] = base["I1"]
    sc["S8b"] = base["I2"]
    sc["S8c"] = base["J"]

    sc["S6"] = L.S("S6", "Vision fallback (canvas)", lambda e: f"Open {e.site.base}/canvas and tell me the visitor code shown in the badge picture.",
                   max_llm_calls=10, check=lambda e, t: {"answer_has_code": "4821" in ((t.result.answer or "") + (t.result.summary or "")) if t.result else False})  # fmt: skip
    sc["S9a"] = L.S(
        "S9a",
        "Search API success",
        "Search the web for Java Spring Boot official documentation and tell me the URL of the top result.",
        max_llm_calls=6,
    )
    sc["S9b"] = L.S(
        "S9b",
        "Search provider unavailable",
        "Search the web for Java Spring Boot official documentation and tell me the URL of the top result.",
        expect_status="FAILED",
        max_llm_calls=8,
    )  # run with SAA_SEARCH__PROVIDERS='["brave"]' (no key)
    # ---- P0/P1 hardening re-validation ------------------------------------------------------
    f2 = S7 / "f2"

    def f2_setup(e: L.Env) -> None:
        d = L.fresh(f2)
        (d / "draft.txt").write_text("draft v1")
        (d / "final.txt").write_text("the existing final")

    def f2_check(e: L.Env, t: Any) -> dict[str, Any]:
        answer = (t.result.answer or "") if t.result else ""
        return {
            "world_unchanged": (f2 / "draft.txt").read_text() == "draft v1"
            and (f2 / "final.txt").read_text() == "the existing final",
            "answer_describes_failure": bool(t.result)
            and t.result.answer_status.value == "WITHHELD"
            and not any(
                w in answer.lower() for w in ("has been renamed", "was renamed", "successfully")
            ),
        }

    sc["F2"] = L.S(
        "F2", "Failed filesystem operation (rename onto an existing file, overwrite denied)",
        f"Rename {f2 / 'draft.txt'} to final.txt in the same folder and tell me the new path.",
        setup=f2_setup, check=f2_check, decide=L.deny, expect_status="FAILED", max_llm_calls=8,
    )  # fmt: skip
    # Phase 15: harmless simple tasks, measured against the pre-hardening baseline
    sc["PA"] = L.S(
        "PA", "Calculator 25 x 16", "Open Calculator and calculate 25 × 16.", max_llm_calls=8
    )
    sc["PB"] = L.S("PB", "example.com page title",
                   "Open Chrome, go to example.com, and tell me the page title.", max_llm_calls=4)  # fmt: skip
    sc["PC"] = L.S("PC", "Official Spring Boot docs (title + URL)",
                   "Search the web for Spring Boot official documentation and tell me the title and URL.",
                   max_llm_calls=6)  # fmt: skip
    cnt = S7 / "count"

    def cnt_setup(e: L.Env) -> None:
        d = L.fresh(cnt)
        for n in ("a.pdf", "b.pdf", "c.pdf", "notes.txt", "todo.md"):
            (d / n).write_text(n)

    sc["CNT"] = L.S(
        "CNT",
        "User-intent count (how many PDFs)",
        f"How many PDF files are in the folder {cnt}? Tell me the number.",
        setup=cnt_setup,
        max_llm_calls=4,
        check=lambda e, t: {
            "answer_has_count": bool(t.result and "3" in (t.result.answer or "")),
            "world_unchanged": len(list(cnt.iterdir())) == 5,
        },
    )
    sc["S9c"] = L.S("S9c", "Browser search blocked", "Open https://www.google.com in Chrome, type Java Spring Boot official documentation into the Google search box and press Enter, then open the official Spring Boot documentation.",
                    max_llm_calls=10, timeout_s=300)  # fmt: skip
    from validation import next_a  # NEXT-A complex workflows (NA1..NA5)

    sc.update(next_a.scenarios())
    return sc


# ----------------------------------------------------------------------------- run
async def run(sid: str) -> None:
    sc = scenarios()[sid]
    with Site() as site:
        c = build_container(settings())
        if c.gateway is None:
            sys.exit("no LLM configured")
        await c.start()
        env = L.Env(c=c, site=site, sandbox=L.SANDBOX)
        stop = threading.Event()
        th = threading.Thread(target=sm.watch, args=(stop,), daemon=True)
        th.start()
        await asyncio.sleep(0.5)
        t0 = time.time()
        try:
            res = await L.run_scenario(env, sc)
            await asyncio.sleep(2.0)
            rep = await sm.report(c, (await c.repo.list_tasks(limit=1))[0].id)
        finally:
            stop.set()
            th.join(timeout=2)
        with sm.lock:
            win = [{**e, "t": round(e["t"] - t0, 1)} for e in sm.events if e["t"] >= t0 - 0.1]
        approvals_db = []
        for r in (
            await c.repo.list_approvals(task_id=rep["task_id"])
            if hasattr(c.repo, "list_approvals")
            else []
        ):
            approvals_db.append(
                {
                    "tool": r.tool,
                    "action": r.action[:80],
                    "status": r.status.value,
                    "scope": getattr(r.scope_granted, "value", None),
                }
            )
        perms = [
            {"tool": d.tool, "via": d.via, "action": d.action}
            for d in await c.repo.list_permission_decisions(rep["task_id"])
        ]
        out = {
            **res,
            "task_id": rep["task_id"],
            "model": rep.get("model") or f"{c.llm.name}/{c.llm.model}" if c.llm else "?",
            "replans": rep["replans"],
            "launch_tool_calls": rep["launch_tool_calls"],
            "replayed_calls": rep["replayed_calls"],
            "loop_guard_triggered": rep["loop_guard_triggered"],
            "identical_side_effect_repeats": rep["identical_side_effect_repeats"],
            "approvals_db": approvals_db,
            "permission_decisions": perms,
            "window_events": win,
            "real_opens": {
                p: sum(1 for e in win if e["ev"] == "OPEN" and e["proc"] == p) for p in sm.WATCH
            },
            "real_closes": {
                p: sum(1 for e in win if e["ev"] == "CLOSE" and e["proc"] == p) for p in sm.WATCH
            },
            "vision_calls_db": rep["vision_calls"],
            "wall_s": round(time.time() - t0, 1),
        }
        await c.stop()
    path = Path(__file__).parent / "results" / f"{TAG}-{sid}.json"
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    keep = (
        "id",
        "title",
        "status",
        "expected_status",
        "status_ok",
        "verified",
        "outcome",
        "verification_status",
        "answer_status",
        "calls_by_purpose",
        "verification",
        "pause_resume_events",
        "persisted_status_while_paused",
        "llm_calls_while_paused",
        "events_while_paused",
        "world_unchanged",
        "answer_describes_failure",
        "answer_has_count",
        "plan_invalid_details",
        "task_id",
        "model",
        "duration_s",
        "llm_calls",
        "llm_budget",
        "tool_calls",
        "replans",
        "recoveries",
        "recovery_events",
        "vision_calls",
        "launch_tool_calls",
        "replayed_calls",
        "identical_side_effect_repeats",
        "loop_guard_triggered",
        "approvals",
        "approvals_db",
        "injection_events",
        "real_opens",
        "real_closes",
        "world_ok",
        "listing",
        "answer",
        "error",
        "summary",
        "calls_before",
        "calls_after",
        "paused_flag",
        "calls_started_while_paused",
        "file_still_exists",
        "file_deleted",
        "victim_exists",
        "answer_has_code",
        "cost_usd",
    )
    for k in keep:
        if k in out:
            v = out[k]
            if k in {"real_opens", "real_closes"}:
                v = {p: n for p, n in v.items() if n}
            print(f"{k} = {str(v)[:420]}".encode("ascii", "replace").decode())
    print("tools =", [(a, b) for a, b in out["tools"]])
    print(
        "permission =",
        [(p["tool"], p["via"], p["action"]) for p in perms if p["action"] != "allow"],
    )
    print("saved", path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("id")
    a = ap.parse_args()
    if a.cmd == "list":
        for k, v in scenarios().items():
            print(k, v.title)
    else:
        asyncio.run(run(a.id))
