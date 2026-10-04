"""Regression (live Test 3): 'tell me the page title' completed and verified but returned no answer at all."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.llm.scripted import structured
from app.models.enums import AnswerStatus, TaskStatus
from app.planner.planner import PlanDraft, draft_to_plan, validate_plan

from tests.helpers import make_env, make_harness

TRIVIAL = [{"kind": "dir_exists", "params": {"path": "@Desktop"}}]


def plan(template: str | None) -> dict[str, Any]:
    return {
        "summary": "count",
        "steps": [
            {
                "id": "s1",
                "title": "list",
                "tool": "fs_list",
                "args": {"path": "@Desktop"},
                "rationale": "r",
            }
        ],
        "success_criteria": TRIVIAL,
        "answer_template": template,
    }


async def test_answer_is_filled_from_recorded_tool_output_without_an_llm_call(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    (env.desktop / "a.txt").write_text("x")
    (env.desktop / "b.txt").write_text("x")
    h = make_harness(
        env, [structured(plan("The desktop has {{steps.s1.data.entries.length}} items."))]
    )
    task = await h.run("how many items are on my desktop")
    assert task.status is TaskStatus.COMPLETED
    assert task.result and task.result.answer == "The desktop has 2 items."
    assert "Answer: The desktop has 2 items." in task.result.summary
    assert h.llm.purposes == ["plan"], "no extra model call to phrase the answer"


async def test_a_field_that_was_never_produced_gives_no_answer_instead_of_an_invented_one(
    tmp_path: Path,
) -> None:
    """`size` is a real fs_list entry field but is null for folders: nothing may be invented."""
    env = make_env(tmp_path)
    (env.desktop / "sub").mkdir()
    h = make_harness(env, [structured(plan("The size is {{steps.s1.data.entries[0].size}}"))])
    task = await h.run("what is the size")
    assert task.status is TaskStatus.COMPLETED and task.result
    assert task.result.answer_status is AnswerStatus.UNAVAILABLE
    assert (
        task.result.answer == "The task completed, but the requested response data was unavailable."
    )
    assert task.result.verified is False, "incomplete answer data can never be 'verified'"
    assert "None" not in (task.result.answer or "")


async def test_template_may_only_reference_real_steps(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    p = draft_to_plan(PlanDraft.model_validate(plan("x {{steps.s9.data.y}}")), "t")
    assert any(
        "answer_template references unknown step 's9'" in problem
        for problem in validate_plan(p, env.router.registry)
    )
    ok = draft_to_plan(PlanDraft.model_validate(plan("n={{steps.s1.data.entries.length}}")), "t")
    assert validate_plan(ok, env.router.registry) == []
