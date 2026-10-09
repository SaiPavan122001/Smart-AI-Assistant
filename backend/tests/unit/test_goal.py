"""NEXT-A: the explicit goal is extracted by code, enforced by the router, checked against real state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.models.goal import RequirementKind, RequirementStatus
from app.models.tool import ToolCall
from app.planner.goal import extract_goal, goal_prompt_block
from app.tools.observed import normalize_url
from app.utils.errors import ErrorCode
from app.verification.goal import evaluate_goal

from tests.helpers import Env


def kinds(goal: Any) -> list[str]:
    return [r.kind.value for r in goal.requirements]


# ----------------------------------------------------------------------------- extraction
def test_named_output_file_and_grounded_summary_are_required() -> None:
    g = extract_goal(
        "Find the Java-related PDFs in this test workspace, move them into a Java folder, summarize "
        "their contents, and save the summary as Java-summary.md."
    )
    assert kinds(g) == ["artifact", "grounded_summary"]
    assert g.requirements[0].name == "Java-summary.md" and g.requirements[0].path is None
    assert g.prohibitions == []


def test_input_files_are_not_outputs() -> None:
    g = extract_goal("Read notes.txt and tell me what it says")
    assert g.is_empty()
    g = extract_goal("Read notes.txt, then save the summary as out.md")
    assert [r.name for r in g.requirements if r.kind is RequirementKind.ARTIFACT] == ["out.md"]


def test_exact_destination_path_is_kept() -> None:
    g = extract_goal(r"Write the report to C:\Users\me\Documents\report.md please")
    assert g.requirements[0].path == r"C:\Users\me\Documents\report.md"
    g = extract_goal('Save it as "@Desktop/My Notes/today.txt"')
    assert g.requirements[0].path == "@Desktop/My Notes/today.txt"


def test_requested_source_count_with_urls() -> None:
    g = extract_goal(
        "Find three useful resources about Spring Boot, identify the title and URL of each, summarize "
        "their main differences, and save the comparison to a Markdown file."
    )
    urls = [r for r in g.requirements if r.kind is RequirementKind.MIN_URLS]
    assert len(urls) == 1 and urls[0].count == 3 and urls[0].where == "artifact"
    assert "artifact" in kinds(g) and "grounded_summary" in kinds(g)
    answer_only = extract_goal("Give me 5 links about rust async")
    assert [(r.count, r.where) for r in answer_only.requirements] == [(5, "answer")]
    assert extract_goal("Find five results for rust").requirements == [], (
        "no URL asked: not checkable"
    )


@pytest.mark.parametrize(
    ("text", "tools", "overwrite"),
    [
        ("Organize my files. Do not delete anything.", ["fs_delete"], False),
        ("Sort the folder, but never remove a file", ["fs_delete"], False),
        ("Copy them, don't move them", ["fs_move"], False),
        ("Rename it. Do not overwrite existing files", [], True),
        ("Tidy up without deleting anything", ["fs_delete"], False),
    ],
)
def test_prohibitions(text: str, tools: list[str], overwrite: bool) -> None:
    g = extract_goal(text)
    assert sorted(g.forbidden_tools) == sorted(tools) and g.forbids_overwrite is overwrite


def test_a_restriction_does_not_leak_across_a_conjunction() -> None:
    g = extract_goal("Do not delete anything, and move the PDFs into Docs")
    assert g.forbidden_tools == {"fs_delete"}, "the move was asked for"


def test_prompt_block_lists_requirements_and_prohibitions() -> None:
    block = goal_prompt_block(extract_goal("Save the summary as a.md. Do not delete anything."))
    assert "requirement r" in block and "FORBIDDEN" in block and "fs_delete" in block
    assert goal_prompt_block(extract_goal("open example.com")) == ""


def test_normalize_url() -> None:
    assert (
        normalize_url("HTTPS://Spring.IO/projects/spring-boot/#overview")
        == "https://spring.io/projects/spring-boot"
    )
    assert normalize_url("https://a.b/c/.") == "https://a.b/c"


# ----------------------------------------------------------------------------- enforcement
async def test_router_blocks_forbidden_tool_and_overwrite(env: Env) -> None:
    (env.documents / "keep.txt").write_text("precious")
    ctx = env.ctx(goal=extract_goal("Tidy up. Do not delete anything and do not overwrite files."))
    rm = await env.router.execute(
        ToolCall(tool="fs_delete", args={"path": str(env.documents / "keep.txt")}, rationale="t"),
        ctx,
    )
    assert rm.error and rm.error.code == ErrorCode.GOAL_VIOLATION
    ow = await env.router.execute(
        ToolCall(tool="fs_create", args={"path": str(env.documents / "keep.txt"), "content": "x", "overwrite": True}, rationale="t"), ctx
    )  # fmt: skip
    assert ow.error and ow.error.code == ErrorCode.GOAL_VIOLATION
    assert (env.documents / "keep.txt").read_text() == "precious"
    fine = await env.router.execute(
        ToolCall(tool="fs_read", args={"path": str(env.documents / "keep.txt")}, rationale="t"), ctx
    )
    assert fine.ok


# ----------------------------------------------------------------------------- evaluation
def _goal_ctx(env: Env, text: str):  # type: ignore[no-untyped-def]
    goal = extract_goal(text)
    return goal, env.ctx(goal=goal)


def test_artifact_requirement_needs_a_real_nonempty_file(env: Env) -> None:
    goal, ctx = _goal_ctx(env, "Summarize it and save the summary as sum.md")
    art = [r for r in goal.requirements if r.kind is RequirementKind.ARTIFACT]
    res = evaluate_goal(goal, ctx, answer=None)
    assert res[0].status is RequirementStatus.UNMET and "sum.md" in res[0].evidence
    f = env.documents / "sum.md"
    f.write_text("")
    ctx.observed.written.append(str(f))
    assert evaluate_goal(goal, ctx, answer=None)[0].status is RequirementStatus.UNMET
    f.write_text("content")
    assert evaluate_goal(goal, ctx, answer=None)[0].status is RequirementStatus.MET
    assert art


def test_a_file_saved_outside_the_tools_is_found_by_name(env: Env) -> None:
    goal, ctx = _goal_ctx(env, "Type a note in Notepad and save it as note.txt")
    (env.desktop / "note.txt").write_text("hi")
    assert evaluate_goal(goal, ctx, answer=None)[0].status is RequirementStatus.MET


def test_exact_path_must_have_been_written_by_this_task(env: Env) -> None:
    f = env.documents / "old.md"
    f.write_text("from yesterday")
    import os
    import time

    os.utime(f, (time.time() - 86400, time.time() - 86400))
    goal, ctx = _goal_ctx(env, f"Save the report as {f}")
    res = evaluate_goal(goal, ctx, answer=None, since=time.time())
    assert (
        res[0].status is RequirementStatus.UNMET and "not written by this task" in res[0].evidence
    )


def test_urls_must_be_enough_and_must_have_been_seen(env: Env) -> None:
    goal, ctx = _goal_ctx(
        env, "Find 3 resources about X with their URL and save the comparison as cmp.md"
    )
    f = env.documents / "cmp.md"
    ctx.observed.written.append(str(f))
    ctx.observed.urls.update({"https://a.dev/x", "https://b.dev/y", "https://c.dev/z"})
    f.write_text("A https://a.dev/x\nB https://b.dev/y\n")
    r = {x.kind: x for x in evaluate_goal(goal, ctx, answer=None)}
    assert (
        r["min_urls"].status is RequirementStatus.UNMET and "2 distinct" in r["min_urls"].evidence
    )
    f.write_text("A https://a.dev/x\nB https://b.dev/y\nC https://c.dev/z/\n")
    assert {x.kind: x for x in evaluate_goal(goal, ctx, answer=None)}[
        "min_urls"
    ].status is RequirementStatus.MET
    f.write_text("A https://a.dev/x\nB https://b.dev/y\nC https://made-up.example/z\n")
    bad = {x.kind: x for x in evaluate_goal(goal, ctx, answer=None)}["min_urls"]
    assert bad.status is RequirementStatus.UNMET and "never returned by any tool" in bad.evidence


def test_answer_urls_are_checked_when_nothing_is_saved(env: Env) -> None:
    goal, ctx = _goal_ctx(env, "Give me 2 links about rust")
    ctx.observed.urls.update({"https://rust-lang.org", "https://doc.rust-lang.org/book"})
    assert evaluate_goal(goal, ctx, answer=None)[0].status is RequirementStatus.UNVERIFIED
    ok = evaluate_goal(
        goal, ctx, answer="https://rust-lang.org and https://doc.rust-lang.org/book/"
    )
    assert ok[0].status is RequirementStatus.MET
    short = evaluate_goal(goal, ctx, answer="only https://rust-lang.org")
    assert short[0].status is RequirementStatus.UNMET


def test_summary_must_come_from_what_was_read(env: Env) -> None:
    goal, ctx = _goal_ctx(env, "Summarize the document and save the summary as sum.md")
    f = env.documents / "sum.md"
    ctx.observed.written.append(str(f))
    f.write_text("Mangoes ripen quickly during summer and bananas ripen slower under cloudy skies.")
    by = {x.kind: x for x in evaluate_goal(goal, ctx, answer=None)}
    assert (
        by["grounded_summary"].status is RequirementStatus.UNMET
        and "nothing was read" in by["grounded_summary"].evidence
    )
    ctx.observed.texts.append(
        "The expense tracker records spending per category. Monthly totals are reported for every category, "
        "and all data is stored locally in a single file."
    )
    assert {x.kind: x for x in evaluate_goal(goal, ctx, answer=None)}[
        "grounded_summary"
    ].status is RequirementStatus.UNMET
    f.write_text(
        "The tracker records spending per category, reports monthly totals, and stores all data locally in one file."
    )
    assert {x.kind: x for x in evaluate_goal(goal, ctx, answer=None)}[
        "grounded_summary"
    ].status is RequirementStatus.MET
    f.write_text("ok")
    assert {x.kind: x for x in evaluate_goal(goal, ctx, answer=None)}[
        "grounded_summary"
    ].status is RequirementStatus.UNMET


def test_path_outside_roots_cannot_be_probed(env: Env, tmp_path: Path) -> None:
    goal, ctx = _goal_ctx(env, f"Save the report as {tmp_path / 'outside' / 'r.md'}")
    res = evaluate_goal(goal, ctx, answer=None)
    assert res[0].status is RequirementStatus.UNMET and "cannot check" in res[0].evidence
