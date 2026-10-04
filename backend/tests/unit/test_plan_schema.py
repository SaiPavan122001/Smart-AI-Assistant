"""F1 regression: a plan may only reference fields its tools really produce.

The tool output models are the single source of truth: the planner catalogue, plan validation
and the router's contract check are all derived from them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.llm.scripted import structured
from app.models.enums import StepStatus, TaskOutcome, TaskStatus
from app.models.plan import AdaptiveOutput, PlanStep
from app.models.tool import ToolCall
from app.planner.planner import (
    PlanDraft,
    draft_to_plan,
    validate_plan,
    validate_plan_detailed,
)
from app.planner.prompts import build_planner_system, build_planner_user, tool_catalog
from app.registry import build_registry
from app.tools.schema import PathError, describe, resolve_path
from app.utils.errors import ErrorCode

from tests.helpers import Env, make_env, make_harness

REG = build_registry()
TRIVIAL = [{"kind": "dir_exists", "params": {"path": "@Desktop"}}]


def step(sid: str, tool: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"id": sid, "title": sid, "tool": tool, "args": args, "rationale": sid, **kw}


def problems(payload: dict[str, Any], **kw: Any) -> list[str]:
    return validate_plan(draft_to_plan(PlanDraft.model_validate(payload), "t"), REG, **kw)


def plan(*steps: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"summary": "p", "steps": list(steps), "success_criteria": TRIVIAL, **kw}


OPEN = step("s1", "browser_open", {"url": "https://example.com"})
SEARCH = step("s1", "web_search", {"query": "spring boot docs"})
LIST = step("s1", "fs_list", {"path": "@Desktop"})


# ============================================================================ 8. nonexistent field
@pytest.mark.parametrize(
    ("first", "ref"),
    [
        (OPEN, "steps.s1.data.summary"),  # the live failure: browser tools have no 'summary'
        (OPEN, "steps.s1.data.entries"),
        (OPEN, "steps.s1.data.result"),
        (OPEN, "steps.s1.data.status"),  # browser_open has no status (browser_navigate does)
        (SEARCH, "steps.s1.data.candidates.length"),
        (LIST, "steps.s1.data.title"),
        (LIST, "steps.s1.artifacts.path"),  # fs_list declares no artifacts
        (LIST, "steps.s1.output.path"),  # not an output section
    ],
)
def test_nonexistent_output_field_is_rejected_before_execution(
    first: dict[str, Any], ref: str
) -> None:
    for payload in (
        plan(first, answer_template="It is {{" + ref + "}}"),
        plan(first, success_criteria=[
            {"kind": "value_check", "params": {"actual": "{{" + ref + "}}", "op": "equals", "expected": "x"}}
        ]),
        plan(first, step("s2", "fs_read", {"path": "{{" + ref + "}}"}, depends_on=["s1"])),
        plan({**first, "verify": [
            {"kind": "value_check", "params": {"actual": "{{" + ref + "}}", "op": "contains", "expected": "x"}}
        ]}),
    ):  # fmt: skip
        found = problems(payload)
        assert found and any("{{" + ref + "}}" in p for p in found), (payload, found)


def test_rejection_names_the_fields_that_really_exist() -> None:
    [p] = validate_plan_detailed(
        draft_to_plan(
            PlanDraft.model_validate(plan(OPEN, answer_template="{{steps.s1.data.summary}}")), "t"
        ),
        REG,
    )
    assert p.code == "unknown_field" and p.where == "answer_template"
    assert p.available == ["navigation", "tab", "tabs", "title", "url"]
    assert "Available fields: navigation, tab, tabs, title, url" in str(p)
    assert p.as_dict()["reference"] == "{{steps.s1.data.summary}}"


# ============================================================================ 9. nested paths
def test_nested_nonexistent_field_is_rejected_and_valid_nested_paths_are_accepted() -> None:
    for bad in (
        "steps.s1.data.results[0].link",  # no such field inside a result
        "steps.s1.data.results.url",  # a list needs an index
        "steps.s1.data.best.href",
        "steps.s1.data.query.text",  # cannot descend into a string
        "steps.s1.data.attempts[0].provider.name",
    ):
        found = problems(plan(SEARCH, answer_template="{{" + bad + "}}"))
        assert found and "{{" + bad + "}}" in found[0], (bad, found)
    for good in (
        "steps.s1.data.results[0].url",
        "steps.s1.data.results[2].title",
        "steps.s1.data.results.length",
        "steps.s1.data.best.url",
        "steps.s1.data.source_note",
        "steps.s1.artifacts.url",
        "steps.s1.summary",
    ):
        assert problems(plan(SEARCH, answer_template="{{" + good + "}}")) == [], good


# ============================================================================ 10. type compatibility
def test_type_mismatch_is_rejected() -> None:
    # string expected (fs_read.path), actual = array
    found = problems(plan(LIST, step("s2", "fs_read", {"path": "{{steps.s1.data.entries}}"})))
    assert found == [
        "s2.args.path: {{steps.s1.data.entries}} is a array but a string is expected here"
    ]
    # a list / object cannot be put into a sentence
    assert (
        "cannot be shown as text"
        in problems(plan(SEARCH, answer_template="Results: {{steps.s1.data.results}}"))[0]
    )
    assert (
        "cannot be placed inside text"
        in problems(
            plan(LIST, step("s2", "fs_read", {"path": "@Desktop/{{steps.s1.data.entries}}"}))
        )[0]
    )
    # numeric comparison of a list (its .length would be fine; numeric text is coerced)
    assert "is a array but a number is expected" in problems(
        plan(LIST, success_criteria=[
            {"kind": "value_check", "params": {"actual": "{{steps.s1.data.entries}}", "op": "gt", "expected": 1}}
        ])
    )[0]  # fmt: skip
    # integer where text is required
    assert (
        "is a integer but a string is expected"
        in problems(
            plan(LIST, step("s2", "fs_read", {"path": "{{steps.s1.data.entries.length}}"}))
        )[0]
    )
    # compatible: integer -> number comparison, string -> string argument, scalar in a sentence
    assert problems(
        plan(
            LIST,
            step("s2", "fs_read", {"path": "{{steps.s1.data.entries[0].name}}"}),
            success_criteria=[
                {"kind": "value_check",
                 "params": {"actual": "{{steps.s1.data.entries.length}}", "op": "gte", "expected": 1}}
            ],
            answer_template="{{steps.s1.data.entries.length}} items, truncated={{steps.s1.data.truncated}}",
        )
    ) == []  # fmt: skip


# ============================================================================ 11. valid fields
def test_valid_output_fields_are_accepted_for_every_reference_position() -> None:
    payload = plan(
        step("s1", "browser_open", {"url": "https://example.com"},
             verify=[{"kind": "value_check",
                      "params": {"actual": "{{steps.s1.data.title}}", "op": "contains", "expected": "Example"}}]),
        step("s2", "browser_read", {"mode": "text"}, depends_on=["s1"]),
        {"id": "s3", "title": "summarise", "kind": "adaptive", "goal": "summarise the page",
         "allowed_tools": ["browser_read"], "depends_on": ["s2"]},
        success_criteria=[{"kind": "browser_url", "params": {"contains": "example.com"}}],
        answer_template="{{steps.s1.data.title}} ({{steps.s2.data.url}}): {{steps.s3.data.answer}}",
    )  # fmt: skip
    assert problems(payload) == []


def test_adaptive_steps_expose_only_their_declared_fields() -> None:
    adaptive = {"id": "s1", "title": "a", "kind": "adaptive", "goal": "g", "allowed_tools": ["fs_list"]}  # fmt: skip
    for good in ("answer", "summary", "iterations", "tool_calls.length", "tool_calls[0]"):
        assert problems(plan(adaptive, answer_template="{{steps.s1.data." + good + "}}")) == []
    found = problems(plan(adaptive, answer_template="{{steps.s1.data.entries}}"))
    assert "an adaptive step" in found[0] and "answer, iterations, summary, tool_calls" in found[0]
    assert set(AdaptiveOutput.model_fields) == {"answer", "iterations", "summary", "tool_calls"}


def test_replans_may_reference_only_prior_steps_that_succeeded() -> None:
    done = PlanStep(id="s1", title="t", tool="fs_list", args={}, status=StepStatus.SUCCEEDED)
    failed = PlanStep(id="s2", title="t", tool="fs_list", args={}, status=StepStatus.FAILED)
    prior = {"s1": done, "s2": failed}
    ok = plan(step("s3", "fs_read", {"path": "{{steps.s1.data.entries[0].name}}"}))
    assert problems(ok, known_prior=prior) == []
    bad_field = plan(step("s3", "fs_read", {"path": "{{steps.s1.data.files[0]}}"}))
    assert "has no field 'files'" in problems(bad_field, known_prior=prior)[0]
    from_failed = plan(step("s3", "fs_read", {"path": "{{steps.s2.data.path}}"}))
    assert "did not succeed" in problems(from_failed, known_prior=prior)[0]
    assert "duplicate step id 's1'" in problems(plan(LIST), known_prior=prior)[0]


# ============================================================================ F1.6 verification contracts
@pytest.mark.parametrize(
    ("spec", "needle"),
    [
        ({"kind": "browser_text", "params": {}}, "contains: Field required"),
        ({"kind": "browser_text", "params": {"text": "x"}}, "Extra inputs are not permitted"),
        ({"kind": "browser_url", "params": {}}, "provide one of: contains, equals, regex"),
        ({"kind": "file_exists", "params": {"path": "@Desktop", "kind": "folder"}}, "kind"),
        ({"kind": "value_check", "params": {"actual": 1, "op": "is", "expected": 1}}, "op"),
        ({"kind": "value_check", "params": {"actual": "a", "op": "gt", "expected": 1}}, "needs a number"),
        ({"kind": "value_check", "params": {"actual": 1, "op": "equals"}}, "expected: Field required"),
        ({"kind": "value_check", "params": {"actual": "a", "op": "regex", "expected": "("}}, "invalid regular"),
        ({"kind": "file_exists", "params": {"path": "@Desktop", "wait_s": 120}}, "wait_s"),
        ({"kind": "element_state", "params": {"window_title": "X", "exists": True}}, "exists"),
        ({"kind": "page_contains", "params": {"text": "x"}}, "unknown verification kind"),
    ],
)  # fmt: skip
def test_invalid_verification_contracts_are_rejected_before_execution(
    spec: dict[str, Any], needle: str
) -> None:
    for payload in (plan(LIST, success_criteria=[spec]), plan({**LIST, "verify": [spec]})):
        found = problems(payload)
        assert found and any(needle in p for p in found), found


def test_every_verification_kind_documents_its_parameters_from_the_contract() -> None:
    system = build_planner_system()
    assert "- browser_text {contains*: string, selector: string}" in system
    assert "op: equals|contains|not_contains|regex|gt|gte|lt" in system
    assert "{verify_catalog}" not in system and "{adaptive_fields}" not in system
    assert describe(AdaptiveOutput.model_json_schema()) in system


# ============================================================================ 12. planner context
def test_planner_receives_the_actual_output_schema_of_every_tool(env: Env) -> None:
    cat = tool_catalog(env.router.registry)
    assert cat in build_planner_user("anything", env.router.registry, env.services)
    lines = {ln.split("(")[0][2:]: ln for ln in cat.splitlines()}
    for spec in env.router.registry.specs():
        if not spec.exposed_to_llm:
            continue
        tool = env.router.registry.get(spec.name)
        assert tool is not None and tool.Output is not None, f"{spec.name} declares no Output"
        assert spec.output_schema == tool.Output.model_json_schema(), "one source of truth"
        assert spec.returns == describe(spec.output_schema)
        assert f"-> data: {{{spec.returns}}}" in lines[spec.name]
        for name in tool.Output.model_fields:  # every real field is shown to the planner ...
            assert f"{name}:" in lines[spec.name] or f"{name}?:" in lines[spec.name]
            resolve_path(spec.output_schema, [name])  # ... and is accepted by validation
        with pytest.raises(PathError):
            resolve_path(spec.output_schema, ["definitely_not_a_field"])
        assert "{{steps.sN.data." in lines[spec.name], "a valid example reference is given"


def test_every_registered_tool_declares_an_output_contract() -> None:
    reg = build_registry(include_browser=True, include_desktop=True, include_vision=True)
    missing = [n for n in reg.names() if reg.get(n).Output is None]  # type: ignore[union-attr]
    assert missing == []


async def test_tool_output_must_match_its_declared_schema(env: Env) -> None:
    """The contract is enforced in both directions: real outputs validate against the model."""
    (env.desktop / "a.txt").write_text("hello")
    ctx = env.ctx()
    for tool, args in (
        ("fs_list", {"path": "@Desktop"}),
        ("fs_search", {"root": "@Desktop", "name_pattern": "*.txt"}),
        ("fs_read", {"path": "@Desktop/a.txt"}),
        ("fs_create", {"path": "@Desktop/d", "kind": "dir"}),
        ("fs_create", {"path": "@Desktop/f.txt", "content": "x"}),
        ("fs_copy", {"src": "@Desktop/a.txt", "dst": "@Desktop/b.txt"}),
        ("fs_move", {"src": "@Desktop/b.txt", "dst": "@Desktop/c.txt"}),
        ("terminal_environment", {}),
        ("verify_file_state", {"path": "@Desktop/a.txt", "expect": "contains", "text": "hello"}),
        ("verify_task_result", {"criteria": [{"kind": "file_exists", "params": {"path": "@Desktop/a.txt"}}]}),
    ):  # fmt: skip
        r = await env.router.execute(ToolCall(tool=tool, args=args, rationale="t"), ctx)
        assert r.ok, (tool, r.error)
        model = env.router.registry.get(tool).Output  # type: ignore[union-attr]
        model.model_validate(r.data)  # type: ignore[union-attr]

    class Drifting(type(env.router.registry.get("fs_list"))):  # type: ignore[misc]
        name = "drifting"

        async def run(self, args: Any, ctx: Any) -> Any:
            out = await super().run(args, ctx)
            out.data["summary"] = "an undeclared field"
            return out

    env.router.registry.register(Drifting())
    r = await env.router.execute(
        ToolCall(tool="drifting", args={"path": "@Desktop"}, rationale="t"), ctx
    )
    assert not r.ok and r.error and "does not match its declared schema" in r.error.message


# ============================================================================ 13 / 14. bounded repair
BAD = plan(
    step("s1", "fs_list", {"path": "@Desktop"}),
    answer_template="Summary: {{steps.s1.data.summary}}",
)
GOOD = plan(
    step("s1", "fs_list", {"path": "@Desktop"}),
    answer_template="{{steps.s1.data.entries.length}} entries",
)


async def test_one_repair_attempt_fixes_an_invalid_reference(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    h = make_harness(env, [structured(BAD), structured(GOOD)])
    task = await h.run("how many entries")
    assert task.status is TaskStatus.COMPLETED and task.result
    assert task.result.answer == "0 entries"
    assert h.llm.purposes == ["plan", "repair"]
    assert task.usage.planner_calls == 1 and task.usage.repair_calls == 1
    feedback = h.llm.requests[1].messages[-1].content[0].text  # type: ignore[union-attr]
    assert '"code": "unknown_field"' in feedback and '"where": "answer_template"' in feedback
    assert '"reference": "{{steps.s1.data.summary}}"' in feedback
    assert '"entries"' in feedback and '"truncated"' in feedback, "the real fields are listed"
    assert await env.repo.list_tool_calls(task.id), "the repaired plan executed"


async def test_second_invalid_plan_is_plan_invalid_and_nothing_executes(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    still_bad = {**BAD, "answer_template": "Summary: {{steps.s1.data.result}}"}
    h = make_harness(env, [structured(BAD), structured(still_bad)])
    task = await h.run("summarise the desktop")
    assert task.status is TaskStatus.FAILED and task.result and task.result.error
    assert task.result.error.code is ErrorCode.PLAN_INVALID
    assert task.result.outcome is TaskOutcome.PLAN_INVALID
    assert h.llm.purposes == ["plan", "repair"], "exactly one bounded repair, then stop"
    assert await env.repo.list_tool_calls(task.id) == [], "an invalid plan never executes"
    assert task.result.error.details["problems"][0]["code"] == "unknown_field"
    assert "no valid plan" in (task.result.answer or "")


async def test_an_invalid_replan_also_gets_one_repair_then_plan_invalid(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = plan(
        step("s1", "fs_list", {"path": "@Desktop"}),
        success_criteria=[{"kind": "dir_exists", "params": {"path": "@Desktop/never"}}],
    )
    bad_replan = plan(
        step("s2", "fs_read", {"path": "{{steps.s1.data.files[0]}}"}),
        success_criteria=[{"kind": "dir_exists", "params": {"path": "@Desktop/never"}}],
    )
    h = make_harness(env, [structured(first), structured(bad_replan), structured(bad_replan)])
    task = await h.run("x")
    assert task.status is TaskStatus.FAILED and task.result and task.result.error
    assert task.result.error.code is ErrorCode.PLAN_INVALID
    assert h.llm.purposes == ["plan", "replan", "repair"], "no uncontrolled planner loop"
    assert (task.usage.planner_calls, task.usage.replanner_calls, task.usage.repair_calls) == (
        1,
        1,
        1,
    )


# ============================================================================ live re-validation: bad criteria
def test_success_criteria_that_can_never_hold_together_are_rejected() -> None:
    """Live pause/resume re-run: the plan required the final URL to equal several pages."""
    pages = [
        {"kind": "browser_url", "params": {"equals": f"https://example.com/doc/{n}"}}
        for n in ("alpha", "beta")
    ]
    found = problems(plan(OPEN, success_criteria=pages))
    assert len(found) == 1 and "can never hold together" in found[0] and "FINAL state" in found[0]
    assert problems(plan(OPEN, success_criteria=pages[:1])) == []
    # the same check as a step's own `verify` is fine: it runs right after that step
    assert problems(plan({**OPEN, "verify": pages[:1]}, success_criteria=pages[1:])) == []
    # a re-plan is validated against the criteria already in force, which it cannot replace
    from app.models.verification import VerificationSpec

    in_force = [VerificationSpec.model_validate(pages[0])]
    replan = plan(step("s2", "browser_navigate", {"url": "https://example.com/doc/beta"}),
                  success_criteria=pages[1:])  # fmt: skip
    assert "can never hold together" in problems(replan, prior_criteria=in_force)[0]


def test_placeholder_expected_text_is_rejected() -> None:
    """Live: `browser_title contains ' '` was a guess for 'some title' and failed the task."""
    for spec in (
        {"kind": "browser_title", "params": {"contains": " "}},
        {"kind": "browser_text", "params": {"contains": "   "}},
        {"kind": "file_contains", "params": {"path": "@Desktop/a.txt", "text": " "}},
    ):
        found = problems(plan({**OPEN, "verify": [spec]}))
        assert found and "must contain visible characters" in found[0], spec


def test_a_replan_cannot_reference_a_prior_step_that_it_supersedes() -> None:
    """Live Calculator re-run: the re-plan's criteria kept `{{steps.s2.data.answer}}` although the
    pending s2 was replaced, so final verification could never resolve it."""
    done = PlanStep(id="s1", title="t", tool="fs_list", args={}, status=StepStatus.SUCCEEDED)
    pending = PlanStep(id="s2", title="t", kind="adaptive", goal="g")  # type: ignore[arg-type]
    prior = {"s1": done, "s2": pending}
    stale = plan(
        {"id": "s3", "title": "a", "kind": "adaptive", "goal": "g"},
        success_criteria=[
            {"kind": "value_check",
             "params": {"actual": "{{steps.s2.data.answer}}", "op": "equals", "expected": "400"}}
        ],
        answer_template="The result is {{steps.s2.data.answer}}",
    )  # fmt: skip
    found = problems(stale, known_prior=prior)
    assert len(found) == 2 and all("a revised plan replaces it" in p for p in found)
    fresh = {**stale, "answer_template": "The result is {{steps.s3.data.answer}}"}
    fresh["success_criteria"] = [
        {"kind": "value_check",
         "params": {"actual": "{{steps.s3.data.answer}}", "op": "contains", "expected": "400"}}
    ]  # fmt: skip
    assert problems(fresh, known_prior=prior) == []
    assert problems(fresh) == [], "state from a re-plan does not leak into later validations"
