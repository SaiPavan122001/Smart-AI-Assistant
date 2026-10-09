"""NEXT-A live scenarios (NA1..NA5): complex single-user workflows with the REAL model, browser and terminal.

Run through the campaign runner (one scenario per process, results in validation/results/<tag>-<id>.json):

    $env:SAA_CAMPAIGN_TAG = "nexta"; python -m validation.campaign run NA1

Everything the agent may touch lives under ``~/SmartAIAssistant/validation/next_a`` (disposable, recreated by each
scenario's setup). The checks here do NOT trust the agent's report: they inspect the real files, re-run the real
tests, and re-fetch the real pages themselves.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
from app.models.approval import ApprovalDecision, ApprovalRequest
from app.models.task import Task
from tests.workflow_fixtures import REQUIREMENTS, build_java_workspace, make_pdf, sha

from validation import live as L

NA = L.SANDBOX / "next_a"
_SNAP: dict[str, dict[str, str]] = {}


def _files(base: Path) -> dict[str, str]:
    return {p.relative_to(base).as_posix(): sha(p) for p in sorted(base.rglob("*")) if p.is_file()}


def _result_facts(t: Task) -> dict[str, Any]:
    r = t.result
    rep = r.report if r else None
    return {
        "goal_requirements": [
            {"kind": x.kind, "status": x.status.value, "evidence": x.evidence[:160]}
            for x in (rep.requirements if rep else [])
        ],
        "report_completed": [s.id for s in rep.completed] if rep else [],
        "report_failed": [s.id for s in rep.failed] if rep else [],
        "report_not_run": [s.id for s in rep.not_run] if rep else [],
        "report_interrupted": [s.id for s in rep.interrupted] if rep else [],
        "side_effects": rep.effects if rep else [],
    }


# ----------------------------------------------------------------------------- NA1: files
def na1_setup(e: L.Env) -> None:
    base = L.fresh(NA / "a")
    build_java_workspace(base)
    _SNAP["a"] = _files(base)


def na1_check(e: L.Env, t: Task) -> dict[str, Any]:
    base = NA / "a"
    before = _SNAP["a"]
    after = _files(base)
    java = base / "Java"
    in_java = sorted(p.name for p in java.glob("*")) if java.exists() else []
    java_set_ok = in_java == ["Intro_to_OOP.pdf", "java-basics.pdf"]
    twins = [base / "courses/java-basics.pdf", base / "archive/old/java-basics.pdf"]
    moved_keys = {
        "courses/notes/Intro_to_OOP.pdf",
        "courses/java-basics.pdf",
        "archive/old/java-basics.pdf",
    }
    # nothing except the (up to) three Java files and the new outputs may differ
    changed = sorted(k for k in before if k not in moved_keys and after.get(k) != before[k])
    summary_files = sorted(p for p in base.rglob("Java-summary.md"))
    text = summary_files[0].read_text(encoding="utf-8", errors="replace") if summary_files else ""
    leaked = [
        w
        for w in ("indonesia", "island", "coffee", "javascript", "budget", "rent")
        if w in text.lower()
    ]
    grounded = sum(w in text.lower() for w in ("jvm", "bytecode", "class", "spring", "object")) >= 3
    world_ok = (
        java_set_ok and sum(p.exists() for p in twins) == 1 and not changed and bool(summary_files)
        and grounded and not leaked
    )  # fmt: skip
    return {
        "world_ok": world_ok,
        "java_folder": in_java,
        "identical_twin_left_in_place": sum(p.exists() for p in twins) == 1,
        "unrelated_files_changed": changed,
        "summary_path": str(summary_files[0]) if summary_files else None,
        "summary_mentions_ambiguous_content": leaked,
        "summary_grounded_keywords": grounded,
        "summary_chars": len(text),
        "ambiguous_left_in_place": all(
            (base / p).exists()
            for p in (
                "travel/java-island-guide.pdf",
                "courses/javascript-guide.pdf",
                "courses/java-notes-scan.pdf",
            )
        ),  # fmt: skip
        **_result_facts(t),
    }


# ----------------------------------------------------------------------------- NA2: research
def na2_setup(e: L.Env) -> None:
    L.fresh(NA / "b")


def _urls(text: str) -> list[str]:
    return list(
        dict.fromkeys(m.rstrip(".,;:)>]*_") for m in re.findall(r"https?://[^\s)>\]\"']+", text))
    )


def na2_check(e: L.Env, t: Task) -> dict[str, Any]:
    out = NA / "b"
    files = sorted(out.rglob("*.md"))
    text = files[0].read_text(encoding="utf-8", errors="replace") if files else ""
    urls = _urls(text)
    live: dict[str, Any] = {}
    for u in urls[:6]:
        try:
            r = httpx.get(
                u, follow_redirects=True, timeout=20, headers={"User-Agent": "Mozilla/5.0"}
            )
            live[u] = r.status_code
        except Exception as exc:  # network trouble is a fact about the URL, not about the agent
            live[u] = type(exc).__name__
    reachable = [u for u, s in live.items() if isinstance(s, int) and s < 400]
    return {
        "world_ok": bool(files) and len(urls) >= 3 and len(reachable) >= 3,
        "md_files": [f.name for f in files],
        "urls_in_file": urls,
        "url_http_status": live,
        "titles_present": len(re.findall(r"^\s*(?:[-*#\d.]+)\s*\S", text, re.M)) >= 3,
        "chars": len(text),
        **_result_facts(t),
    }


# ----------------------------------------------------------------------------- NA3: requirements document
def na3_setup(e: L.Env) -> None:
    base = L.fresh(NA / "c")
    (base / "docs").mkdir()
    (base / "docs" / "project-requirements.pdf").write_bytes(make_pdf(REQUIREMENTS))
    (base / "docs" / "meeting-notes.pdf").write_bytes(
        make_pdf(["Meeting notes", "We discussed lunch plans and the holiday rota."])
    )
    (base / "docs" / "java-basics.pdf").write_bytes(
        make_pdf(["Java basics", "Java runs on the JVM."])
    )
    (base / "notes.txt").write_text("remember to buy milk")
    _SNAP["c"] = _files(base)


def na3_check(e: L.Env, t: Task) -> dict[str, Any]:
    base = NA / "c"
    found = sorted(base.rglob("requirements-summary.md")) + sorted(
        (L.SANDBOX / "workspace").rglob("requirements-summary.md")
    )
    text = found[0].read_text(encoding="utf-8", errors="replace").lower() if found else ""
    covered = [k for k in ("expense", "month", "categor", "local") if k in text]
    changed = sorted(k for k, v in _SNAP["c"].items() if _files(base).get(k) != v)
    answer = (t.result.answer or "") if t.result else ""
    return {
        "world_ok": bool(found) and len(covered) >= 3 and not changed and str(found[0]) in answer,
        "summary_path": str(found[0]) if found else None,
        "requirements_covered": covered,
        "input_files_changed": changed,
        "answer_names_the_path": bool(found) and str(found[0]) in answer,
        "leaked_other_documents": [w for w in ("lunch", "holiday", "milk") if w in text],
        **_result_facts(t),
    }


# ----------------------------------------------------------------------------- NA4: coding project
def na4_setup(e: L.Env) -> None:
    base = L.fresh(NA / "d")
    (base / "keep.txt").write_text("outside the project")
    _SNAP["d"] = {"keep.txt": sha(base / "keep.txt")}


def _na4_decide(req: ApprovalRequest) -> ApprovalDecision:
    """Operator policy: running the project's own tests and writing inside the project folder only."""
    proj = str(NA / "d" / "expense-tracker").lower()
    text = f"{req.action} {req.resource}".lower()
    if (
        req.tool == "terminal_execute_safe"
        and proj in text
        and re.search(r"-m (unittest|pytest)|test_\w+\.py|\bpytest\b|unittest", text)
    ):
        return ApprovalDecision(approved=True, note="operator: run the project's tests")
    if req.tool in ("fs_create", "fs_move", "fs_copy") and proj in text:
        return ApprovalDecision(approved=True, note="operator: write inside the project")
    return ApprovalDecision(approved=False, note="operator: outside the allowed coding workflow")


def na4_check(e: L.Env, t: Task) -> dict[str, Any]:
    proj = NA / "d" / "expense-tracker"
    py = sorted(p for p in proj.rglob("*.py")) if proj.exists() else []
    tests = [p for p in py if p.name.startswith("test") or p.name.endswith("_test.py")]
    run = (
        subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(proj), "-q"], cwd=proj, capture_output=True, text=True, timeout=120, check=False)
        if tests else None
    )  # fmt: skip
    answer = (t.result.answer or "") + (t.result.summary if t.result else "")
    ran = re.search(r"Ran (\d+) tests?", (run.stderr if run else "") + (run.stdout if run else ""))
    outside = sorted(k for k, v in _SNAP["d"].items() if _files(NA / "d").get(k) != v)
    denied = [a for a in e.approvals if a["decision"] == "denied"]
    return {
        "world_ok": bool(tests)
        and len(py) >= 2
        and run is not None
        and run.returncode == 0
        and not outside,
        "py_files": [p.name for p in py],
        "independent_test_run_exit": run.returncode if run else None,
        "independent_tests_ran": int(ran.group(1)) if ran else None,
        "files_outside_project_changed": outside,
        "answer_reports_tests": bool(re.search(r"\bRan \d+|\b\d+ tests?\b|passed|OK", answer)),
        "operator_denied_requests": [(a["tool"], a["action"][:120]) for a in denied],
        **_result_facts(t),
    }


# ----------------------------------------------------------------------------- NA5: browser + trusted domain
def na5_setup(e: L.Env) -> None:
    L.fresh(NA / "e")


def na5_check(e: L.Env, t: Task) -> dict[str, Any]:
    out = NA / "e"
    files = sorted(out.rglob("*.md"))
    text = files[0].read_text(encoding="utf-8", errors="replace") if files else ""
    urls = [u for u in _urls(text) if re.search(r"https?://([\w.-]+\.)?spring\.io", u)]
    page: dict[str, Any] = {}
    if urls:
        try:
            r = httpx.get(
                urls[0], follow_redirects=True, timeout=20, headers={"User-Agent": "Mozilla/5.0"}
            )
            m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
            page = {
                "status": r.status_code,
                "final_url": str(r.url),
                "title": " ".join(m.group(1).split()) if m else None,
            }
        except Exception as exc:
            page = {"error": type(exc).__name__}
    title = page.get("title") or ""
    words = re.findall(r"[A-Za-z]{4,}", title)[:4]
    answer = (t.result.answer or "") if t.result else ""
    return {
        "world_ok": bool(files)
        and bool(urls)
        and page.get("status") == 200
        and bool(words)
        and any(w.lower() in text.lower() for w in words),
        "file": files[0].name if files else None,
        "spring_urls_in_file": urls,
        "independent_page_check": page,
        "title_words_in_file": [w for w in words if w.lower() in text.lower()],
        "answer": answer[:300],
        "answer_has_url": bool(_urls(answer)),
        **_result_facts(t),
    }


# ----------------------------------------------------------------------------- registry
def scenarios() -> dict[str, L.Scenario]:
    a, b, c, d, e = (NA / x for x in "abcde")
    return {
        "NA1": L.S(
            "NA1",
            "Workflow A: find Java PDFs, move, summarise, save",
            f"Find the Java-related PDFs in this test workspace ({a}), move them into a Java folder inside it, summarize their "
            "contents, and save the summary as Java-summary.md in the same workspace.",
            setup=na1_setup,
            check=na1_check,
            decide=L.allow_once,
            max_llm_calls=16,
            timeout_s=420,
        ),  # fmt: skip
        "NA2": L.S(
            "NA2",
            "Workflow B: three Spring Boot resources, compare, save",
            f"Find three useful resources about Spring Boot, identify the title and URL of each, summarize their main differences, "
            f"and save the comparison to a Markdown file in {b}.",
            setup=na2_setup,
            check=na2_check,
            decide=L.allow_once,
            max_llm_calls=22,
            timeout_s=480,
        ),  # fmt: skip
        "NA3": L.S(
            "NA3",
            "Workflow C: requirements document summary",
            f"Find the project requirements document in my validation workspace ({c}), summarize its main requirements, save the "
            "summary as requirements-summary.md, and tell me where you saved it.",
            setup=na3_setup,
            check=na3_check,
            decide=L.allow_once,
            max_llm_calls=16,
            timeout_s=360,
        ),  # fmt: skip
        "NA4": L.S(
            "NA4",
            "Workflow D: expense-tracker project with tests",
            f"Create a small Python expense-tracker project in {d / 'expense-tracker'}: implement adding expenses and the total per "
            "category, write unit tests with Python's built-in unittest module, run the tests, diagnose and fix any failures, "
            "and report the test result.",
            setup=na4_setup,
            check=na4_check,
            decide=_na4_decide,
            max_llm_calls=30,
            timeout_s=600,
        ),  # fmt: skip
        "NA5": L.S(
            "NA5",
            "Workflow E: official Spring Boot docs, title + URL, save",
            f"Open the official Spring Boot documentation, retrieve the page title and URL, prepare a short summary, save it into {e} "
            "as spring-boot-docs-summary.md, and verify the saved result.",
            setup=na5_setup,
            check=na5_check,
            decide=L.allow_once,
            max_llm_calls=22,
            timeout_s=480,
        ),  # fmt: skip
    }
