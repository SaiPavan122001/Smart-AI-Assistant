"""F2 regression: the final answer is derived from authoritative FINAL task state.

A task that did not succeed can never carry a success answer, whatever a plan template or a
model said along the way. Also covers the `verified=true` integrity invariants (Phase 4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.agent.answer import (
    UNAVAILABLE_TEXT,
    candidate_answer,
    decide_answer,
    failure_statement,
    outcome_of,
)
from app.config.settings import LimitSettings
from app.llm.base import LLMRequest, LLMResponse
from app.llm.scripted import structured, tool_use
from app.models.enums import (
    AnswerStatus,
    StepStatus,
    TaskOutcome,
    TaskStatus,
    VerificationStatus,
)
from app.models.plan import Plan, PlanStep
from app.models.task import TaskResult
from app.utils.errors import ErrorCode, ErrorInfo
from pydantic import ValidationError

from tests.helpers import Responder, deny_all, make_env, make_harness

SUCCESS_WORDS = ("has been deleted", "was clicked", "was created", "items")


def step(sid: str, tool: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"id": sid, "title": sid, "tool": tool, "args": args, "rationale": sid, **kw}


def assert_no_success_claim(result: TaskResult) -> None:
    assert result.answer_status is AnswerStatus.WITHHELD
    assert result.verified is False
    assert result.answer and not any(w in result.answer for w in SUCCESS_WORDS), result.answer
    assert "Answer:" not in result.summary


# ============================================================================ 1. FAILED
async def test_failed_task_cannot_render_the_success_template(tmp_path: Path) -> None:
    env = make_env(tmp_path, limits=LimitSettings(max_replans=0, approval_timeout_s=5))
    plan = {
        "summary": "make a folder",
        "steps": [step("s1", "fs_create", {"path": "@Desktop/a", "kind": "dir"})],
        # the request is NOT satisfied: this folder is never made
        "success_criteria": [{"kind": "dir_exists", "params": {"path": "@Desktop/other"}}],
        "answer_template": "The folder was created at {{steps.s1.data.path}}.",
    }
    task = await make_harness(env, [structured(plan)]).run("make the other folder")
    assert task.status is TaskStatus.FAILED and task.result
    assert_no_success_claim(task.result)
    assert task.result.answer.startswith("The task could not be completed.")  # type: ignore[union-attr]
    assert "not verified" in task.result.answer  # type: ignore[operator]
    assert task.result.outcome is TaskOutcome.FAILED
    assert task.result.verification_status is VerificationStatus.FAILED


# ============================================================================ 3. DENIED (the live F2 case)
async def test_denied_delete_never_says_the_file_was_deleted(tmp_path: Path) -> None:
    """Live Step 7 / 7a: fs_delete was denied, the file stayed, the answer claimed it was deleted."""
    env = make_env(tmp_path)
    victim = env.desktop / "old-backup.zip"
    victim.write_bytes(b"zip")
    plan = {
        "summary": "delete the backup",
        "steps": [step("s1", "fs_delete", {"path": "@Desktop/old-backup.zip"})],
        "success_criteria": [
            {"kind": "file_absent", "params": {"path": "@Desktop/old-backup.zip"}}
        ],
        "answer_template": "The file old-backup.zip has been deleted.",
    }
    h = make_harness(env, [structured(plan)])
    with Responder(env, deny_all):
        task = await h.run("delete old-backup.zip from my desktop")
    assert victim.exists(), "the file was not deleted"
    assert task.status is TaskStatus.FAILED and task.result
    assert_no_success_claim(task.result)
    assert task.result.outcome is TaskOutcome.DENIED
    assert "not approved" in task.result.answer and "not performed" in task.result.answer  # type: ignore[operator]
    assert h.llm.purposes == ["plan"], "a user denial is final: no re-plan, no retry"


# ============================================================================ 2. CANCELLED
async def test_cancelled_task_cannot_render_the_success_template(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    plan = {
        "summary": "two folders",
        "steps": [
            step("s1", "fs_create", {"path": "@Desktop/a", "kind": "dir"}),
            step("s2", "fs_create", {"path": "@Desktop/b", "kind": "dir"}),
        ],
        "success_criteria": [{"kind": "dir_exists", "params": {"path": "@Desktop/b"}}],
        "answer_template": "Both folders: {{steps.s1.data.path}} was created.",
    }
    h = make_harness(env, [structured(plan)])
    orig = env.router.execute

    async def cancel_after_first(call: Any, ctx: Any) -> Any:
        result = await orig(call, ctx)
        await h.runtime.cancel(ctx.task_id)
        return result

    env.router.execute = cancel_after_first  # type: ignore[method-assign]
    task = await h.run("make two folders")
    assert task.status is TaskStatus.CANCELLED and task.result
    assert_no_success_claim(task.result)
    assert task.result.outcome is TaskOutcome.CANCELLED
    assert "stopped before it finished" in task.result.answer  # type: ignore[operator]
    assert not (env.desktop / "b").exists()


# ============================================================================ 4. LOOP_DETECTED
async def test_loop_detected_cannot_render_the_success_template(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    plan = {
        "summary": "folder",
        "steps": [
            step("s1", "fs_create", {"path": "@Desktop/a", "kind": "dir"}),
            step("s2", "fs_create", {"path": "@Desktop/b", "kind": "dir"}),
        ],
        "answer_template": "The folder was created at {{steps.s1.data.path}}.",
    }
    h = make_harness(env, [structured(plan)])
    orig = env.router.execute

    async def trip(call: Any, ctx: Any) -> Any:
        result = await orig(call, ctx)
        ctx.loop_guard.trip("Loop detected: test")
        return result

    env.router.execute = trip  # type: ignore[method-assign]
    task = await h.run("make folders")
    assert task.status is TaskStatus.FAILED and task.result
    assert task.result.error and task.result.error.code is ErrorCode.LOOP_DETECTED
    assert_no_success_claim(task.result)
    assert task.result.outcome is TaskOutcome.LOOP_DETECTED
    assert "repeating actions" in task.result.answer  # type: ignore[operator]


# ============================================================================ PLAN_INVALID / TIMED_OUT statements
@pytest.mark.parametrize(
    ("status", "code", "outcome", "needle"),
    [
        (TaskStatus.FAILED, ErrorCode.PLAN_INVALID, TaskOutcome.PLAN_INVALID, "no valid plan"),
        (TaskStatus.TIMED_OUT, ErrorCode.TIMEOUT, TaskOutcome.TIMED_OUT, "ran out of time"),
        (TaskStatus.FAILED, ErrorCode.APPROVAL_TIMEOUT, TaskOutcome.DENIED, "not approved"),
        (TaskStatus.FAILED, ErrorCode.PATH_BLOCKED, TaskOutcome.DENIED, "security policy"),
        (TaskStatus.CANCELLED, ErrorCode.CANCELLED, TaskOutcome.CANCELLED, "stopped"),
        (TaskStatus.FAILED, ErrorCode.LOOP_DETECTED, TaskOutcome.LOOP_DETECTED, "repeating"),
        (TaskStatus.FAILED, ErrorCode.IO_ERROR, TaskOutcome.FAILED, "could not be completed"),
    ],
)
def test_every_non_success_outcome_gets_a_truthful_statement_and_never_the_template(
    status: TaskStatus, code: ErrorCode, outcome: TaskOutcome, needle: str
) -> None:
    done = PlanStep(id="s1", title="t", tool="fs_delete", args={}, status=StepStatus.SUCCEEDED)
    plan = Plan(steps=[done], answer_template="The file has been deleted.")
    error = ErrorInfo(code=code, message="details")
    assert outcome_of(status, error) is outcome
    decision = decide_answer(
        status=status, error=error, verification_passed=False, plan=plan,
        step_data={"s1": {"data": {"path": "x"}, "artifacts": {}, "summary": "", "ok": True}},
        step_answers={"s1": "The file has been deleted."},
    )  # fmt: skip
    assert decision.status is AnswerStatus.WITHHELD
    assert needle in (decision.text or "") and "has been deleted" not in (decision.text or "")
    assert failure_statement(outcome, error) == decision.text


# ============================================================================ 5. missing answer field
def test_missing_answer_field_never_produces_an_invented_value() -> None:
    ok = PlanStep(id="s1", title="t", tool="browser_open", args={}, status=StepStatus.SUCCEEDED)
    failed = PlanStep(id="s2", title="t", tool="browser_open", args={}, status=StepStatus.FAILED)
    data = {
        "s1": {"data": {"title": "Example Domain", "none": None, "empty": "", "list": [1]},
               "artifacts": {}, "summary": "", "ok": True},
        "s2": {"data": {"title": "stale title"}, "artifacts": {}, "summary": "", "ok": False},
    }  # fmt: skip

    def render(template: str) -> Any:
        return candidate_answer(Plan(steps=[ok, failed], answer_template=template), data, {})

    good = render("The page title is {{steps.s1.data.title}}")
    assert good.status is AnswerStatus.ANSWERED and good.text == "The page title is Example Domain"
    for template in (
        "The title is {{steps.s1.data.missing}}",  # never produced
        "The value is {{steps.s1.data.none}}",  # produced as null
        "The value is {{steps.s1.data.empty}}",  # produced empty
        "The list is {{steps.s1.data.list}}",  # not a displayable value
        "The title is {{steps.s2.data.title}}",  # the step FAILED: its data is not evidence
        "The title is {{steps.s9.data.title}}",  # no such step
        "{{steps.s1.data.title}} and {{steps.s1.data.missing}}",  # one bad field poisons all
    ):
        bad = render(template)
        assert bad.status is AnswerStatus.UNAVAILABLE and bad.text == UNAVAILABLE_TEXT, template
        assert bad.missing, template


# ============================================================================ 6. stale answers and replanning
def _v1() -> dict[str, Any]:
    return {
        "summary": "count",
        "steps": [step("s1", "fs_list", {"path": "@Desktop"})],
        "success_criteria": [{"kind": "dir_exists", "params": {"path": "@Desktop/made"}}],
        "answer_template": "v1: the desktop has {{steps.s1.data.entries.length}} items",
    }


async def test_stale_answer_cannot_survive_replanning_and_final_state_wins(tmp_path: Path) -> None:
    """failure -> replan -> success: the answer comes from the FINAL plan and final outputs."""
    env = make_env(tmp_path)
    v2 = {
        "summary": "make then count",
        "steps": [
            step("s2", "fs_create", {"path": "@Desktop/made", "kind": "dir"}),
            step("s3", "fs_list", {"path": "@Desktop"}, depends_on=["s2"]),
        ],
        "success_criteria": [{"kind": "dir_exists", "params": {"path": "@Desktop/made"}}],
        "answer_template": "v2: the desktop has {{steps.s3.data.entries.length}} items",
    }

    def brain(r: LLMRequest) -> LLMResponse:
        return structured(_v1() if r.purpose == "plan" else v2)

    h = make_harness(env, fallback=brain)
    task = await h.run("make the folder and count the desktop")
    assert task.status is TaskStatus.COMPLETED and task.result
    assert task.result.answer == "v2: the desktop has 1 items", "not the stale v1 text (0 items)"
    assert task.result.answer_status is AnswerStatus.ANSWERED and task.result.verified is True
    assert h.llm.purposes == ["plan", "replan"]


async def test_success_looking_intermediate_result_then_final_failure_has_no_answer(
    tmp_path: Path,
) -> None:
    """success -> failure: s1 succeeded and its template WOULD render, but the task failed."""
    env = make_env(tmp_path, limits=LimitSettings(max_replans=1, approval_timeout_s=5))
    again = {**_v1(), "steps": [step("s2", "fs_list", {"path": "@Desktop"})]}
    again["answer_template"] = "v2: the desktop has {{steps.s2.data.entries.length}} items"

    def brain(r: LLMRequest) -> LLMResponse:
        return structured(_v1() if r.purpose == "plan" else again)

    h = make_harness(env, fallback=brain)
    task = await h.run("make the folder and count the desktop")
    assert task.status is TaskStatus.FAILED and task.result
    assert_no_success_claim(task.result)
    assert h.llm.purposes == ["plan", "replan"]


async def test_answer_of_a_failed_adaptive_step_is_dropped(tmp_path: Path) -> None:
    """Live Step 7 / S2: 'The Confirm button was clicked as requested.' on a FAILED task."""
    env = make_env(tmp_path, limits=LimitSettings(max_replans=0, approval_timeout_s=5))
    plan = {
        "summary": "click",
        "steps": [
            {"id": "s1", "title": "Click Confirm", "kind": "adaptive", "goal": "click Confirm",
             "allowed_tools": ["fs_list"]},
        ],
        "success_criteria": [{"kind": "dir_exists", "params": {"path": "@Desktop/never"}}],
    }  # fmt: skip
    h = make_harness(
        env,
        [
            structured(plan),
            tool_use("finish_step", success=True, summary="clicked",
                     answer="The 'Confirm' button was clicked as requested."),
        ],
    )  # fmt: skip
    task = await h.run("click confirm")
    assert task.status is TaskStatus.FAILED and task.result
    assert_no_success_claim(task.result)


# ============================================================================ 7 / 19. verified=true invariants
def result(**kw: Any) -> TaskResult:
    base: dict[str, Any] = {"status": TaskStatus.COMPLETED, "summary": "s"}
    return TaskResult(**{**base, **kw})


def test_verified_true_invariants() -> None:
    ok = result(
        verified=True, outcome=TaskOutcome.COMPLETED, verification_status=VerificationStatus.VERIFIED,
        answer_status=AnswerStatus.ANSWERED, answer="42",
    )  # fmt: skip
    assert ok.verified
    invalid: list[dict[str, Any]] = [
        # failed + verified true
        {"status": TaskStatus.FAILED, "verified": True, "outcome": TaskOutcome.FAILED,
         "verification_status": VerificationStatus.VERIFIED, "answer_status": AnswerStatus.WITHHELD},
        # cancelled / timed out + verified true
        {"status": TaskStatus.CANCELLED, "verified": True, "outcome": TaskOutcome.CANCELLED,
         "verification_status": VerificationStatus.VERIFIED, "answer_status": AnswerStatus.WITHHELD},
        # verified true while required answer data is missing
        {"verified": True, "outcome": TaskOutcome.COMPLETED,
         "verification_status": VerificationStatus.VERIFIED, "answer_status": AnswerStatus.UNAVAILABLE},
        # verified true without a VERIFIED contract (e.g. only a model judgement)
        {"verified": True, "outcome": TaskOutcome.COMPLETED,
         "verification_status": VerificationStatus.JUDGED, "answer_status": AnswerStatus.NOT_REQUESTED},
        # a success answer on a failed task
        {"status": TaskStatus.FAILED, "outcome": TaskOutcome.FAILED,
         "verification_status": VerificationStatus.FAILED, "answer_status": AnswerStatus.ANSWERED,
         "answer": "The file has been deleted."},
        # a success answer state with no answer text
        {"outcome": TaskOutcome.COMPLETED, "verification_status": VerificationStatus.JUDGED,
         "answer_status": AnswerStatus.ANSWERED, "answer": None},
        # outcome and status disagree
        {"outcome": TaskOutcome.FAILED, "verification_status": VerificationStatus.JUDGED,
         "answer_status": AnswerStatus.NOT_REQUESTED},
    ]  # fmt: skip
    for kw in invalid:
        with pytest.raises(ValidationError):
            result(**kw)


def test_results_stored_before_the_state_split_load_without_upgrading_any_claim() -> None:
    old_failed = TaskResult.model_validate(
        {"status": "FAILED", "summary": "x", "verified": True,
         "answer": "The file old-backup.zip has been deleted."}
    )  # fmt: skip
    assert old_failed.verified is False and old_failed.answer is None
    assert old_failed.answer_status is AnswerStatus.WITHHELD
    old_ok = TaskResult.model_validate(
        {"status": "COMPLETED", "summary": "x", "verified": True, "answer": "42"}
    )
    assert old_ok.verified and old_ok.answer_status is AnswerStatus.ANSWERED


async def test_missing_required_step_can_never_be_completed_or_verified(tmp_path: Path) -> None:
    """A required step that was skipped (its optional dependency failed) blocks success."""
    env = make_env(tmp_path, limits=LimitSettings(max_replans=0, approval_timeout_s=5))
    plan = {
        "summary": "read then make",
        "steps": [
            step("s1", "fs_read", {"path": "@Desktop/missing.txt"}, optional=True),
            step("s2", "fs_create", {"path": "@Desktop/out", "kind": "dir"}, depends_on=["s1"]),
        ],
        # a contract that passes although the required step never ran
        "success_criteria": [{"kind": "dir_exists", "params": {"path": "@Desktop"}}],
        "answer_template": "Done.",
    }
    h = make_harness(env, fallback=lambda r: structured(plan) if r.purpose == "plan" else tool_use(
        "finish_step", success=False, summary="cannot read"))  # fmt: skip
    task = await h.run("read and make")
    assert task.status is TaskStatus.FAILED and task.result
    assert task.result.verified is False and task.result.answer_status is AnswerStatus.WITHHELD
    s2 = next(s for s in task.plan.steps if s.id == "s2")  # type: ignore[union-attr]
    assert s2.status is StepStatus.SKIPPED and "did not run" in task.result.summary


async def test_completed_verified_task_reports_all_three_states(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    plan = {
        "summary": "folder",
        "steps": [step("s1", "fs_create", {"path": "@Desktop/a", "kind": "dir"})],
        "success_criteria": [{"kind": "dir_exists", "params": {"path": "@Desktop/a"}}],
        "answer_template": "Created {{steps.s1.data.kind}} at {{steps.s1.data.path}}",
    }
    task = await make_harness(env, [structured(plan)]).run("make a")
    r = task.result
    assert r and task.status is TaskStatus.COMPLETED
    assert (r.outcome, r.verification_status, r.answer_status, r.verified) == (
        TaskOutcome.COMPLETED, VerificationStatus.VERIFIED, AnswerStatus.ANSWERED, True,
    )  # fmt: skip
    assert r.answer == f"Created dir at {env.desktop / 'a'}"
    assert r.metrics and r.metrics.final_status == "COMPLETED" and r.metrics.verified is True
    assert r.metrics.answer_status == "ANSWERED" and r.metrics.answer_generation_calls == 0


# ============================================================================ live F2 re-validation findings
async def test_a_replan_cannot_relax_the_success_criteria_of_the_request(tmp_path: Path) -> None:
    """Live re-run: the rename was impossible, the re-plan swapped the criteria for 'some other
    file exists' and the task ended COMPLETED + verified without doing what was asked."""
    env = make_env(tmp_path, limits=LimitSettings(max_replans=1, approval_timeout_s=5))
    (env.desktop / "draft.txt").write_text("draft")
    (env.desktop / "final.txt").write_text("existing final")
    first = {
        "summary": "rename",
        "steps": [step("s1", "fs_move", {"src": "@Desktop/draft.txt", "dst": "@Desktop/final.txt"})],
        "success_criteria": [
            {"kind": "file_moved",
             "params": {"src": "@Desktop/draft.txt", "dst": "@Desktop/final.txt"}}
        ],
        "answer_template": "Renamed to {{steps.s1.data.dst}}",
    }  # fmt: skip
    easier = {
        "summary": "write a copy under another name instead",
        "steps": [step("s2", "fs_create", {"path": "@Desktop/draft_renamed.txt", "content": "draft"})],
        "success_criteria": [
            {"kind": "file_exists", "params": {"path": "@Desktop/draft_renamed.txt"}}
        ],
        "answer_template": "The new file is at {{steps.s2.data.path}}",
    }  # fmt: skip

    def brain(r: LLMRequest) -> LLMResponse:
        if r.purpose == "plan":
            return structured(first)
        if r.purpose == "replan":
            return structured(easier)
        return tool_use("finish_step", success=False, summary="final.txt already exists")

    h = make_harness(env, fallback=brain)
    task = await h.run("rename draft.txt to final.txt and tell me the new path")
    assert (env.desktop / "draft.txt").read_text() == "draft", "the rename did not happen"
    assert task.status is TaskStatus.FAILED and task.result
    assert_no_success_claim(task.result)
    assert "new file is at" not in (task.result.answer or "")
    kinds = [c.kind for c in task.plan.success_criteria]  # type: ignore[union-attr]
    assert kinds == ["file_moved", "file_exists"], "the original criterion stayed in force"
    assert any(v.kind == "file_moved" and not v.passed for v in task.result.verification)


async def test_a_user_denial_ends_adaptive_recovery_instead_of_asking_for_more(
    tmp_path: Path,
) -> None:
    """Live re-run: after the overwrite was denied, the recovery loop asked for two more
    destructive approvals and then improvised a different outcome."""
    env = make_env(tmp_path)
    (env.desktop / "draft.txt").write_text("draft")
    (env.desktop / "final.txt").write_text("existing final")
    plan = {
        "summary": "rename",
        "steps": [step("s1", "fs_move", {"src": "@Desktop/draft.txt", "dst": "@Desktop/final.txt"})],
        "success_criteria": [
            {"kind": "file_moved",
             "params": {"src": "@Desktop/draft.txt", "dst": "@Desktop/final.txt"}}
        ],
    }  # fmt: skip
    script = [
        structured(plan),
        tool_use("fs_delete", path="@Desktop/final.txt"),  # adaptive recovery: asks, is denied
        tool_use("fs_create", path="@Desktop/final.txt", content="draft", overwrite=True),
        tool_use("fs_delete", path="@Desktop/draft.txt"),
    ]
    h = make_harness(env, script)
    with Responder(env, deny_all) as human:
        task = await h.run("rename draft.txt to final.txt")
    assert len(human.seen) == 1, "exactly one approval request: the denial was final"
    assert h.llm.purposes == ["plan", "step"], "no further model call, no re-plan"
    assert task.status is TaskStatus.FAILED and task.result
    assert task.result.outcome is TaskOutcome.DENIED
    assert_no_success_claim(task.result)
    assert (env.desktop / "final.txt").read_text() == "existing final"
    assert (env.desktop / "draft.txt").exists()
