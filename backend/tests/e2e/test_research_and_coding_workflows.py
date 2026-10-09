"""NEXT-A: bounded web research (Workflow B) and a controlled coding project (Workflow D).

The model is scripted; search results come from a fake provider (no network); the terminal runs real
processes inside a disposable workspace, behind the real command policy and approval boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.llm.scripted import structured, tool_use
from app.models.approval import ApprovalDecision, ApprovalRequest
from app.models.enums import AnswerStatus, ApprovalScope, TaskOutcome, TaskStatus
from app.models.goal import RequirementStatus
from app.search.base import SearchError, SearchResult
from app.search.router import SearchRouter

from tests.helpers import Env, Responder, make_harness

HITS = [
    SearchResult(title="Spring Boot Reference Documentation", url="https://docs.spring.io/spring-boot/reference/",
                 snippet="Official reference documentation covering auto-configuration, starters and actuator."),
    SearchResult(title="Getting Started with Spring Boot", url="https://spring.io/guides/gs/spring-boot",
                 snippet="A short guided tutorial that builds a first application with Spring Boot."),
    SearchResult(title="Spring Boot Tutorial for Beginners", url="https://www.baeldung.com/spring-boot-start",
                 snippet="An article walking through project setup, dependency injection and configuration properties."),
    SearchResult(title="Spring Boot on Wikipedia", url="https://en.wikipedia.org/wiki/Spring_Framework",
                 snippet="Encyclopedia entry describing the Spring Framework and its history."),
]  # fmt: skip
INSTR = (
    "Find three useful resources about Spring Boot, identify the title and URL of each, summarize their main "
    "differences, and save the comparison to a Markdown file."
)
GOOD = (
    "# Spring Boot resources\n\n"
    "1. Spring Boot Reference Documentation - https://docs.spring.io/spring-boot/reference/ - reference documentation "
    "covering auto-configuration, starters and actuator.\n"
    "2. Getting Started with Spring Boot - https://spring.io/guides/gs/spring-boot - a short guided tutorial that builds "
    "a first application.\n"
    "3. Spring Boot Tutorial for Beginners - https://www.baeldung.com/spring-boot-start - an article about project setup, "
    "dependency injection and configuration properties.\n\n"
    "Differences: the reference documentation is exhaustive, the guide is a short tutorial, and the article is a beginner walkthrough.\n"
)


class FakeProvider:
    name = "fake"

    def __init__(self, results: list[SearchResult] | Exception) -> None:
        self.results = results

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        if isinstance(self.results, Exception):
            raise self.results
        return self.results[:max_results]

    async def aclose(self) -> None:
        return None


def research_plan(out: Path) -> dict[str, Any]:
    return {
        "summary": "Search, read, compare, save",
        "steps": [
            {"id": "s1", "title": "Search", "tool": "web_search", "args": {"query": "Spring Boot", "max_results": 5}, "rationale": "find sources"},
            {"id": "s2", "title": "Write the comparison", "kind": "adaptive", "depends_on": ["s1"], "allowed_tools": ["fs_create"], "rationale": "compare",
             "goal": f"Pick three useful results, compare them using only the search output, and save the comparison with fs_create at {out}"},
        ],
        "success_criteria": [{"kind": "file_exists", "params": {"path": str(out), "min_bytes": 1}}],
        "answer_template": "{{steps.s2.data.answer}}",
    }  # fmt: skip


def adaptive_save(out: Path, text: str) -> list[Any]:
    return [
        tool_use("fs_create", path=str(out), content=text),
        tool_use(
            "finish_step", success=True, summary="saved", answer=f"Saved the comparison to {out}"
        ),
    ]


def with_search(env: Env, provider: FakeProvider) -> None:
    env.services.search = SearchRouter([provider])


async def test_workflow_b_three_sources_saved_and_grounded(env: Env) -> None:
    with_search(env, FakeProvider(HITS))
    out = env.workspace / "spring-boot-comparison.md"
    h = make_harness(env, [structured(research_plan(out)), *adaptive_save(out, GOOD)])
    task = await h.run(INSTR)
    r = task.result
    assert task.status == TaskStatus.COMPLETED and r and r.verified, r
    by = {x.kind: x.status for x in r.report.requirements}  # type: ignore[union-attr]
    assert by == {
        "artifact": RequirementStatus.MET,
        "min_urls": RequirementStatus.MET,
        "grounded_summary": RequirementStatus.MET,
    }
    assert h.llm.purposes == ["plan", "step", "step"]


async def test_an_invented_source_url_fails_the_task(env: Env) -> None:
    with_search(env, FakeProvider(HITS))
    out = env.workspace / "cmp.md"
    invented = GOOD.replace(
        "https://www.baeldung.com/spring-boot-start", "https://spring-boot-made-up.example/tutorial"
    )
    no_more = {"summary": "stop", "steps": [], "needs_clarification": "cannot find a third source"}
    h = make_harness(
        env, [structured(research_plan(out)), *adaptive_save(out, invented), structured(no_more)]
    )
    task = await h.run(INSTR)
    r = task.result
    assert task.status == TaskStatus.FAILED and r and not r.verified
    assert r.answer_status is AnswerStatus.WITHHELD
    unmet = {x.kind: x for x in r.report.unmet}  # type: ignore[union-attr]
    assert "never returned by any tool" in unmet["min_urls"].evidence
    assert (
        r.outcome is TaskOutcome.PARTIAL
    )  # the file exists and is grounded in the search output, the sources are not
    assert "made-up.example" not in (r.answer or "")


async def test_fewer_sources_than_requested_is_reported_not_padded(env: Env) -> None:
    with_search(env, FakeProvider(HITS[:2]))
    out = env.workspace / "cmp.md"
    two = "\n".join(line for line in GOOD.splitlines() if "baeldung" not in line)
    no_more = {
        "summary": "stop",
        "steps": [],
        "needs_clarification": "the provider returned only two sources",
    }
    h = make_harness(
        env, [structured(research_plan(out)), *adaptive_save(out, two), structured(no_more)]
    )
    task = await h.run(INSTR)
    r = task.result
    assert task.status == TaskStatus.FAILED and r and not r.verified
    assert "2 distinct source URL(s); 3 were requested" in " ".join(
        x.evidence for x in r.report.unmet
    )  # type: ignore[union-attr]
    assert "Done:" in (r.answer or "") and "not fulfilled" in (r.answer or "").lower()


async def test_search_provider_unavailable_is_a_truthful_failure(env: Env) -> None:
    with_search(env, FakeProvider(SearchError("blocked", "bot check")))
    out = env.workspace / "cmp.md"
    no_more = {"summary": "stop", "steps": [], "needs_clarification": "web search is unavailable"}
    h = make_harness(env, [structured(research_plan(out)), structured(no_more)])
    task = await h.run(INSTR)
    assert task.status == TaskStatus.FAILED and task.result
    assert not task.result.verified and not out.exists()
    assert (
        "unavailable" in (task.result.summary + (task.result.answer or "")).lower()
        or task.result.error
    )


# ----------------------------------------------------------------------------- Workflow D: coding project
BUGGY = '''"""A tiny expense tracker."""


class Tracker:
    def __init__(self) -> None:
        self.items: list[tuple[str, float]] = []

    def add(self, category: str, amount: float) -> None:
        self.items.append((category, amount))

    def total(self, category: str | None = None) -> float:
        return sum(a for c, a in self.items if category is None or c != category)  # BUG: != instead of ==
'''
FIXED = BUGGY.replace("c != category)  # BUG: != instead of ==", "c == category)")
TESTS = """import unittest

from expenses import Tracker


class TrackerTests(unittest.TestCase):
    def test_total_per_category(self) -> None:
        t = Tracker()
        t.add("food", 10.0)
        t.add("food", 5.0)
        t.add("rent", 100.0)
        self.assertEqual(t.total("food"), 15.0)

    def test_total_all(self) -> None:
        t = Tracker()
        t.add("food", 10.0)
        t.add("rent", 100.0)
        self.assertEqual(t.total(), 110.0)


if __name__ == "__main__":
    unittest.main()
"""


def coding_plan(proj: Path) -> dict[str, Any]:
    run = {"command": "python -m unittest -q", "cwd": str(proj)}
    return {
        "summary": "Create the project, run its tests, fix, re-run",
        "steps": [
            {"id": "s1", "title": "Create the source file", "tool": "fs_create", "args": {"path": str(proj / "expenses.py"), "content": BUGGY}, "rationale": "implement the feature"},
            {"id": "s2", "title": "Create the tests", "tool": "fs_create", "args": {"path": str(proj / "test_expenses.py"), "content": TESTS}, "rationale": "tests", "depends_on": ["s1"]},
            {"id": "s3", "title": "Run the tests", "tool": "terminal_execute_safe", "args": run, "rationale": "run the tests", "depends_on": ["s2"]},
            {"id": "s4", "title": "Final test run", "tool": "terminal_execute_safe", "args": run, "rationale": "confirm", "depends_on": ["s3"],
             "verify": [{"kind": "value_check", "params": {"actual": "{{steps.s4.data.exit_code}}", "op": "equals", "expected": 0}}]},
        ],
        "success_criteria": [{"kind": "file_exists", "params": {"path": str(proj / "expenses.py"), "min_bytes": 10}}],
        "answer_template": "Tests passed (exit code {{steps.s4.data.exit_code}}): {{steps.s4.data.stderr}}",
    }  # fmt: skip


def approve_run(req: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(approved=True, scope=ApprovalScope.ONCE)


async def test_workflow_d_failing_tests_are_diagnosed_from_real_output_and_fixed(env: Env) -> None:
    proj = env.workspace / "expense-tracker"
    seen: list[str] = []

    def diagnose(req: Any) -> Any:
        seen.append(str(req.messages[0].content))
        return tool_use("fs_read", path=str(proj / "expenses.py"))

    script = [
        structured(coding_plan(proj)),
        diagnose,  # the adaptive recovery of s3 starts: it is given the failed run's output
        tool_use("fs_create", path=str(proj / "expenses.py"), content=FIXED, overwrite=True),
        tool_use("terminal_execute_safe", command="python -m unittest -q", cwd=str(proj)),
        tool_use("finish_step", success=True, summary="fixed the category filter; tests pass"),
    ]
    h = make_harness(env, script)
    with Responder(env, approve_run) as resp:
        task = await h.run(
            "Create a small Python expense-tracker project, implement a basic feature, run its tests, fix failures and report the result."
        )
    r = task.result
    assert task.status == TaskStatus.COMPLETED and r, r
    assert (proj / "expenses.py").read_text() == FIXED
    # diagnosis used real evidence: the failing assertion text was in the recovery context, as fenced data
    ctx_text = seen[0]
    assert (
        "failed_action_output" in ctx_text
        and "FAIL: test_total_per_category" in ctx_text
        and "15.0" in ctx_text
    )
    # nothing outside the workspace changed; running project code and overwriting both asked first
    assert {q.category for q in resp.seen} >= {"code_execution", "file_overwrite"}
    assert sorted(p.name for p in env.workspace.iterdir() if p.is_file()) == []
    assert all(
        str(proj) in q.action or "expenses.py" in q.action or "unittest" in q.action
        for q in resp.seen
    )
    assert r.answer_status is AnswerStatus.ANSWERED and "exit code 0" in (r.answer or "")
    assert "Ran 2 tests" in (r.answer or "")  # the real test output, not the model's claim


async def test_workflow_d_unfixable_failure_is_reported_with_the_real_result(env: Env) -> None:
    proj = env.workspace / "expense-tracker"
    no_more = {"summary": "stop", "steps": [], "needs_clarification": "the tests still fail"}
    script = [
        structured(coding_plan(proj)),
        tool_use("finish_step", success=False, summary="could not find the defect"),
        structured(no_more),
    ]
    h = make_harness(
        env, script, fallback=lambda r: tool_use("finish_step", success=False, summary="giving up")
    )
    with Responder(env, approve_run):
        task = await h.run(
            "Create a small Python expense-tracker project, run its tests and report the result."
        )
    r = task.result
    assert task.status == TaskStatus.FAILED and r and not r.verified
    assert r.answer_status is AnswerStatus.WITHHELD
    assert "Tests passed" not in (r.answer or "") and "Tests passed" not in r.summary
    assert (proj / "expenses.py").read_text() == BUGGY, "no unreviewed change was made"


async def test_workflow_d_denied_execution_is_final(env: Env) -> None:
    proj = env.workspace / "expense-tracker"
    no_more = {
        "summary": "stop",
        "steps": [],
        "needs_clarification": "running the tests was not approved",
    }
    h = make_harness(env, [structured(coding_plan(proj)), structured(no_more)])
    with Responder(env, lambda q: ApprovalDecision(approved=False, note="no")):
        task = await h.run("Create a small Python expense-tracker project and run its tests.")
    r = task.result
    assert task.status == TaskStatus.FAILED and r and r.outcome is TaskOutcome.DENIED
    assert "not approved" in (r.answer or "")
    assert h.llm.purposes == ["plan"], "a denial is final: no recovery, no re-plan"


async def test_coding_task_cannot_leave_the_workspace_roots(env: Env, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    plan = {"summary": "x", "steps": [{"id": "s1", "title": "Run", "tool": "terminal_execute_safe",
                                       "args": {"command": "python --version", "cwd": str(outside)}, "rationale": "r"}],
            "success_criteria": [{"kind": "dir_exists", "params": {"path": str(env.workspace)}}]}  # fmt: skip
    no_more = {"summary": "stop", "steps": [], "needs_clarification": "that folder is not allowed"}
    h = make_harness(env, [structured(plan), structured(no_more)])
    task = await h.run("Run python in the folder " + str(outside))
    assert (
        task.status == TaskStatus.FAILED
        and task.result
        and task.result.outcome is TaskOutcome.DENIED
    )
