"""NEXT-A: multi-step workflows through the real runtime with a scripted model.

Everything except the model's decisions is production code: planner validation, router, permission engine,
tools, goal evaluation, verification, recovery, answers and checkpoints.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.llm.scripted import structured, tool_use
from app.models.enums import AnswerStatus, TaskOutcome, TaskStatus, VerificationStatus
from app.models.goal import RequirementStatus

from tests.helpers import Env, make_harness
from tests.workflow_fixtures import REQUIREMENTS, build_java_workspace, make_pdf, sha

CTX = ["jvm", "class", "compiler", "bytecode", "spring"]
INSTR = (
    "Find the Java-related PDFs in this test workspace, move them into a Java folder, summarize their "
    "contents, and save the summary as Java-summary.md."
)
GOOD_SUMMARY = (
    "# Java summary\n\n## java-basics.pdf\nJava is a statically typed language running on the JVM; classes define "
    "fields and methods, and the compiler produces bytecode.\n\n## Intro_to_OOP.pdf\nEvery Java class extends "
    "Object; inheritance, polymorphism and encapsulation are shown with Java examples and Spring Boot programs.\n"
)
BAD_SUMMARY = "# Java summary\n\nPineapples mature slowly while avocados prefer shade, whereas cherries blossom early.\n"


def step(sid: str, title: str, tool: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"id": sid, "title": title, "tool": tool, "args": args, "rationale": title, **kw}


def java_plan(base: Path, *, with_save_criterion: bool = True) -> dict[str, Any]:
    dest = str(base / "Java")
    return {
        "summary": "Find, organise, read, summarise, save",
        "steps": [
            step(
                "s1",
                "Find the Java PDFs",
                "fs_find",
                {
                    "root": str(base),
                    "terms": ["java"],
                    "context_terms": CTX,
                    "extensions": [".pdf"],
                },
            ),
            step(
                "s2",
                "Move them into Java",
                "fs_organize",
                {"sources": "{{steps.s1.data.strong_paths}}", "dest_dir": dest},
                depends_on=["s1"],
                verify=[{"kind": "dir_exists", "params": {"path": dest}}],
            ),
            step(
                "s3",
                "Read the moved documents",
                "fs_read_many",
                {"paths": "{{steps.s2.data.dest_paths}}"},
                depends_on=["s2"],
            ),
            {
                "id": "s4",
                "title": "Write the summary",
                "kind": "adaptive",
                "depends_on": ["s3"],
                "allowed_tools": ["fs_create"],
                "rationale": "summarise",
                "goal": f"Summarise the documents from the previous step and save the summary with fs_create at {base / 'Java-summary.md'}",
            },
        ],  # fmt: skip
        "success_criteria": (
            [
                {
                    "kind": "file_exists",
                    "params": {"path": str(base / "Java-summary.md"), "min_bytes": 1},
                }
            ]
            if with_save_criterion
            else [{"kind": "dir_exists", "params": {"path": dest}}]
        ),
        "answer_template": "{{steps.s4.data.answer}}",
    }


def adaptive_write(base: Path, text: str) -> list[Any]:
    return [
        tool_use("fs_create", path=str(base / "Java-summary.md"), content=text),
        tool_use(
            "finish_step",
            success=True,
            summary="summary saved",
            answer=f"Summary saved to {base / 'Java-summary.md'}",
        ),
    ]


def snapshot(files: dict[str, Path], skip: set[str]) -> dict[str, str]:
    return {k: sha(v) for k, v in files.items() if k not in skip}


# ----------------------------------------------------------------------------- Workflow A
async def test_workflow_a_find_move_summarise_save_and_verify(env: Env) -> None:
    base = env.documents / "ws"
    files = build_java_workspace(base)
    untouched = snapshot(
        files,
        {
            "courses/java-basics.pdf",
            "courses/notes/Intro_to_OOP.pdf",
            "archive/old/java-basics.pdf",
        },
    )
    h = make_harness(env, [structured(java_plan(base)), *adaptive_write(base, GOOD_SUMMARY)])
    task = await h.run(INSTR)
    r = task.result
    assert task.status == TaskStatus.COMPLETED and r, r
    assert (
        r.verified
        and r.verification_status is VerificationStatus.VERIFIED
        and r.outcome is TaskOutcome.COMPLETED
    )
    # the right files moved, and only those
    assert sorted(p.name for p in (base / "Java").iterdir()) == [
        "Intro_to_OOP.pdf",
        "java-basics.pdf",
    ]
    for key, digest in untouched.items():
        assert sha(files[key]) == digest, f"{key} must not change"
    twins = [base / "courses/java-basics.pdf", base / "archive/old/java-basics.pdf"]
    assert sum(p.exists() for p in twins) == 1, (
        "one identical copy moves; the twin is left where it was"
    )
    assert (base / "Java-summary.md").read_text() == GOOD_SUMMARY
    # the explicit goal was evaluated against the real state, and passed
    assert r.report and {x.kind: x.status for x in r.report.requirements} == {
        "artifact": RequirementStatus.MET, "grounded_summary": RequirementStatus.MET,
    }  # fmt: skip
    assert any(v.kind == "goal:artifact" and v.passed for v in r.verification)
    assert r.answer_status is AnswerStatus.ANSWERED and "Java-summary.md" in (r.answer or "")
    # no model call for the predictable steps: plan + two adaptive iterations
    assert h.llm.purposes == ["plan", "step", "step"]
    assert [s.status.value for s in task.plan.steps] == ["SUCCEEDED"] * 4  # type: ignore[union-attr]
    assert task.plan.goal and task.plan.goal.requirements  # type: ignore[union-attr]


async def test_the_summary_step_sees_the_documents_it_depends_on(env: Env) -> None:
    base = env.documents / "ws"
    build_java_workspace(base)
    seen: list[str] = []

    def spy(req: Any) -> Any:
        seen.append(
            req.messages[0].content
            if isinstance(req.messages[0].content, str)
            else str(req.messages[0].content)
        )
        return tool_use("fs_create", path=str(base / "Java-summary.md"), content=GOOD_SUMMARY)

    h = make_harness(
        env,
        [
            structured(java_plan(base)),
            spy,
            tool_use("finish_step", success=True, summary="ok", answer="saved"),
        ],
    )
    task = await h.run(INSTR)
    assert task.status == TaskStatus.COMPLETED, task.result
    assert "dependency_outputs" in seen[0] and "bytecode" in seen[0]


async def test_a_plan_that_forgets_to_save_is_caught_and_the_replan_keeps_finished_work(
    env: Env,
) -> None:
    base = env.documents / "ws"
    files = build_java_workspace(base)
    forgot = [
        tool_use(
            "finish_step",
            success=True,
            summary="summarised in chat only",
            answer="Here is the summary: Java is great.",
        )
    ]
    replan = {
        "summary": "Save the summary",
        "steps": [
            step(
                "s5",
                "Write the summary file",
                "fs_create",
                {"path": str(base / "Java-summary.md"), "content": GOOD_SUMMARY},
            )
        ],
        "answer_template": "Saved to {{steps.s5.artifacts.path}}",
    }
    h = make_harness(
        env, [structured(java_plan(base, with_save_criterion=False)), *forgot, structured(replan)]
    )
    task = await h.run(INSTR)
    r = task.result
    assert task.status == TaskStatus.COMPLETED and r and r.verified, r
    assert h.llm.purposes == ["plan", "step", "replan"]
    assert (base / "Java-summary.md").read_text() == GOOD_SUMMARY
    # the move was NOT repeated: exactly one fs_organize ran and its result still stands
    done = [
        e
        for e in await env.repo.list_events(task.id)
        if e.type.value == "action_completed" and e.data.get("tool") == "fs_organize"
    ]
    assert len(done) == 1
    assert task.plan and task.plan.version == 2
    assert task.plan.goal and any(q.name == "Java-summary.md" for q in task.plan.goal.requirements)
    assert (base / "Java" / "java-basics.pdf").exists() and files[
        "finance/budget-2026.pdf"
    ].exists()


async def test_chat_only_summary_is_never_a_success(env: Env) -> None:
    """The model claims success, the plan has no save criterion, and no replan is possible."""
    base = env.documents / "ws"
    build_java_workspace(base)
    no_more = {"summary": "nothing", "steps": [], "needs_clarification": "I cannot save the file"}
    forgot = [tool_use("finish_step", success=True, summary="done", answer="Java summary in chat")]
    h = make_harness(
        env, [structured(java_plan(base, with_save_criterion=False)), *forgot, structured(no_more)]
    )
    task = await h.run(INSTR)
    r = task.result
    assert task.status == TaskStatus.FAILED and r and not r.verified
    assert r.answer_status is AnswerStatus.WITHHELD and "Java summary in chat" not in (
        r.answer or ""
    )
    assert r.report and r.report.unmet and r.report.unmet[0].kind == "artifact"
    assert "Java-summary.md" in (r.answer or "")  # says exactly what is missing
    assert (
        r.outcome is TaskOutcome.FAILED
    )  # nothing requested is met yet (the files moved, but that is not a stated requirement)


async def test_partial_result_is_reported_truthfully(env: Env) -> None:
    """The file exists but is not based on the documents: one requirement met, one not, work kept."""
    base = env.documents / "ws"
    build_java_workspace(base)
    no_more = {"summary": "stop", "steps": [], "needs_clarification": "cannot do better"}
    h = make_harness(
        env, [structured(java_plan(base)), *adaptive_write(base, BAD_SUMMARY), structured(no_more)]
    )
    task = await h.run(INSTR)
    r = task.result
    assert task.status == TaskStatus.FAILED and r and not r.verified
    assert r.outcome is TaskOutcome.PARTIAL
    assert r.answer_status is AnswerStatus.WITHHELD
    by = {x.kind: x for x in r.report.requirements}  # type: ignore[union-attr]
    assert by["artifact"].status is RequirementStatus.MET
    assert by["grounded_summary"].status is RequirementStatus.UNMET
    text = r.answer or ""
    assert "only partly completed" in text and "NOT fulfilled" in text
    assert "Completed actions were not undone" in text
    assert "Done:" in text and "Java-summary.md" in text
    assert (base / "Java" / "java-basics.pdf").exists(), "completed work stays in place"


async def test_workflow_survives_a_failed_summary_with_one_bounded_retry(env: Env) -> None:
    base = env.documents / "ws"
    build_java_workspace(base)
    replan = {
        "summary": "Write a grounded summary",
        "steps": [
            step(
                "s5",
                "Rewrite the summary",
                "fs_create",
                {"path": str(base / "Java-summary.md"), "content": GOOD_SUMMARY, "overwrite": True},
            )
        ],
        "answer_template": "Saved to {{steps.s5.artifacts.path}}",
    }
    h = make_harness(
        env, [structured(java_plan(base)), *adaptive_write(base, BAD_SUMMARY), structured(replan)]
    )
    from tests.helpers import Responder, approve_all

    with Responder(env, approve_all):  # overwriting an existing file asks first
        task = await h.run(INSTR)
    assert task.status == TaskStatus.COMPLETED, task.result
    assert task.result and task.result.verified
    assert (base / "Java-summary.md").read_text() == GOOD_SUMMARY
    assert task.usage.replans == 1


# ----------------------------------------------------------------------------- restrictions
async def test_a_plan_using_a_forbidden_tool_is_rejected_before_it_runs(env: Env) -> None:
    base = env.documents / "ws"
    build_java_workspace(base)
    bad = {
        "summary": "tidy",
        "steps": [
            step("s1", "Delete the junk", "fs_delete", {"path": str(base / "misc" / "readme.txt")})
        ],
    }
    good = {
        "summary": "list",
        "steps": [step("s1", "List", "fs_list", {"path": str(base)})],
        "answer_template": "Listed {{steps.s1.data.entries.length}} entries",
        "success_criteria": [
            {
                "kind": "value_check",
                "params": {
                    "actual": "{{steps.s1.data.entries.length}}",
                    "op": "gte",
                    "expected": 1,
                },
            }
        ],
    }
    h = make_harness(env, [structured(bad), structured(good)])
    task = await h.run("Tidy up the workspace listing. Do not delete anything.")
    assert h.llm.purposes == ["plan", "repair"]
    assert task.status == TaskStatus.COMPLETED, task.result
    assert (base / "misc" / "readme.txt").exists()


async def test_an_adaptive_step_cannot_delete_when_the_request_forbids_it(env: Env) -> None:
    base = env.documents / "ws"
    build_java_workspace(base)
    plan = {
        "summary": "tidy",
        "steps": [
            {
                "id": "s1",
                "title": "Tidy",
                "kind": "adaptive",
                "goal": "Tidy up",
                "allowed_tools": [],  # every tool is exposed: only the router stands in the way
                "rationale": "tidy",
            }
        ],
    }
    script = [
        structured(plan),
        tool_use("fs_delete", path=str(base / "misc" / "readme.txt")),
        tool_use("finish_step", success=False, summary="the deletion was refused"),
    ]
    h = make_harness(
        env, script, fallback=lambda r: tool_use("finish_step", success=False, summary="stop")
    )
    task = await h.run("Tidy the workspace. Do not delete anything.")
    assert task.status == TaskStatus.FAILED
    assert (base / "misc" / "readme.txt").exists()
    failed = [e for e in await env.repo.list_events(task.id) if e.type.value == "action_failed"]
    assert any(e.data.get("code") == "goal_violation" for e in failed), (
        "the router, not the model, refused"
    )
    assert not env.approvals.pending(), "a forbidden action is not even put to the user"


# ----------------------------------------------------------------------------- checkpoints
async def test_checkpoints_track_progress_without_leaking_secrets(env: Env) -> None:
    base = env.documents / "ws"
    build_java_workspace(base)
    secret = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"
    plan = java_plan(base)
    plan["steps"][0]["title"] = f"Find the Java PDFs ({secret})"
    h = make_harness(env, [structured(plan), *adaptive_write(base, GOOD_SUMMARY)])
    task = await h.run(INSTR)
    assert task.status == TaskStatus.COMPLETED, task.result
    cps = [a for a in await env.repo.list_agent_actions(task.id) if a.kind == "checkpoint"]
    assert len(cps) >= 5  # after each of the four steps, and at the end
    first, last = cps[0].payload, cps[-1].payload
    assert first["completed"] == ["s1"] and first["remaining"] == ["s2", "s3", "s4"]
    assert len(first["outstanding_requirements"]) == 2, (
        "nothing is saved yet: both requirements are outstanding"
    )
    mid = cps[2].payload  # after the documents were read, before the summary exists
    assert mid["completed"] == ["s1", "s2", "s3"] and mid["outstanding_requirements"]
    assert (
        last["remaining"] == [] and last["task_status"] and last["outstanding_requirements"] == []
    )
    assert set(last["completed"]) == {"s1", "s2", "s3", "s4"}
    assert "fs_organize" in last["state"]["side_effects"] and last["state"]["files_written"]
    blob = str(cps)
    assert secret not in blob and "ABCDEFGHIJKLMNOP" not in blob
    assert "documents" not in blob.lower() or "text" not in {
        k for c in cps for k in c.payload
    }  # no document text stored
    assert "checkpoint_saved" in await h.events(task.id)


async def test_requirements_document_workflow_c(env: Env) -> None:
    """Workflow C: find the one requirements document, summarise it, save, and tell where."""
    base = env.documents / "ws"
    (base / "docs").mkdir(parents=True)
    (base / "docs" / "requirements.pdf").write_bytes(make_pdf(REQUIREMENTS))
    (base / "docs" / "meeting-notes.pdf").write_bytes(
        make_pdf(["Meeting notes", "Discussed lunch plans and holidays."])
    )
    out = base / "requirements-summary.md"
    summary = "# Requirements\nThe system records expenses with an amount and a category, reports monthly totals per category, and stores all data locally in a single file."
    plan = {
        "summary": "Find, read, summarise, save",
        "steps": [
            step("s1", "Find the requirements document", "fs_find", {"root": str(base), "terms": ["requirements"], "context_terms": ["shall", "system"], "extensions": [".pdf"]}),
            step("s2", "Read it", "fs_read_many", {"paths": "{{steps.s1.data.strong_paths}}"}, depends_on=["s1"]),
            {"id": "s3", "title": "Summarise and save", "kind": "adaptive", "depends_on": ["s2"], "allowed_tools": ["fs_create"], "rationale": "summarise",
             "goal": f"Summarise the main requirements and save them with fs_create at {out}"},
        ],
        "success_criteria": [{"kind": "file_exists", "params": {"path": str(out), "min_bytes": 1}}],
        "answer_template": "{{steps.s3.data.answer}}",
    }  # fmt: skip
    h = make_harness(env, [structured(plan), tool_use("fs_create", path=str(out), content=summary),
                           tool_use("finish_step", success=True, summary="saved", answer=f"Saved the summary to {out}")])  # fmt: skip
    task = await h.run(
        "Find the project requirements document, summarize its main requirements, save the summary as requirements-summary.md, and tell me where you saved it."
    )
    r = task.result
    assert task.status == TaskStatus.COMPLETED and r and r.verified, r
    assert str(out) in (r.answer or "")
    assert (base / "docs" / "meeting-notes.pdf").exists()
    assert {x.status for x in r.report.requirements} == {RequirementStatus.MET}  # type: ignore[union-attr]


async def test_two_matching_documents_are_not_guessed(env: Env) -> None:
    """Ambiguity: two real requirements documents. The plan surfaces it instead of picking silently."""
    base = env.documents / "ws"
    (base / "a").mkdir(parents=True)
    (base / "b").mkdir()
    (base / "a" / "requirements-v1.pdf").write_bytes(make_pdf(REQUIREMENTS))
    (base / "b" / "requirements-v2.pdf").write_bytes(
        make_pdf([*REQUIREMENTS, "R4. The system shall export CSV."])
    )
    plan = {
        "summary": "Find",
        "steps": [step("s1", "Find", "fs_find", {"root": str(base), "terms": ["requirements"], "context_terms": ["shall"], "extensions": [".pdf"]})],
        "success_criteria": [{"kind": "value_check", "params": {"actual": "{{steps.s1.data.strong_paths.length}}", "op": "equals", "expected": 1}}],
        "answer_template": "Found {{steps.s1.data.strong_paths.length}}",
    }  # fmt: skip
    h = make_harness(
        env,
        [structured(plan)],
        fallback=lambda r: structured(
            {
                "summary": "x",
                "steps": [],
                "needs_clarification": "Two documents match: requirements-v1.pdf and requirements-v2.pdf. Which one?",
            }
        ),
    )
    task = await h.run("Find the project requirements document and summarize it")
    assert task.status == TaskStatus.FAILED and task.result
    assert not task.result.verified and task.result.answer_status is AnswerStatus.WITHHELD


# ----------------------------------------------------------------------------- limits, STOP, pause, bad references
async def test_stop_mid_workflow_keeps_finished_work_and_never_recovers(tmp_path: Path) -> None:
    from tests.helpers import make_env

    env = make_env(tmp_path)
    base = env.documents / "ws"
    build_java_workspace(base)
    h = make_harness(env, [structured(java_plan(base)), *adaptive_write(base, GOOD_SUMMARY)])
    orig = env.router.execute

    async def stop_after_organize(call: Any, ctx: Any) -> Any:
        result = await orig(call, ctx)
        if call.tool == "fs_organize":
            await h.runtime.cancel(ctx.task_id)
        return result

    env.router.execute = stop_after_organize  # type: ignore[method-assign]
    task = await h.run(INSTR)
    r = task.result
    assert task.status == TaskStatus.CANCELLED and r and not r.verified
    assert r.outcome is TaskOutcome.CANCELLED and r.answer_status is AnswerStatus.WITHHELD
    assert h.llm.purposes == ["plan"], "no model call, recovery or re-plan after STOP"
    assert (base / "Java" / "java-basics.pdf").exists() or (
        base / "Java" / "Intro_to_OOP.pdf"
    ).exists()
    assert not (base / "Java-summary.md").exists()
    assert r.report and [s.id for s in r.report.completed] == ["s1"]
    assert [s.id for s in r.report.interrupted] == ["s2"], (
        "the move ran but the step was cut off: say so"
    )
    assert [s.id for s in r.report.not_run] == ["s3", "s4"]
    assert "stopped before it finished" in (r.answer or "") and "not undone" in (r.answer or "")
    assert any(q.status is RequirementStatus.UNMET for q in r.report.requirements)


async def test_action_budget_ends_the_workflow_with_a_truthful_account(tmp_path: Path) -> None:
    from app.config.settings import LimitSettings

    from tests.helpers import make_env

    env = make_env(tmp_path, limits=LimitSettings(max_actions=1, approval_timeout_s=5))
    base = env.documents / "ws"
    build_java_workspace(base)
    h = make_harness(
        env,
        [structured(java_plan(base)), *adaptive_write(base, GOOD_SUMMARY)],
        fallback=lambda r: tool_use("finish_step", success=False, summary="limit"),
    )
    task = await h.run(INSTR)
    r = task.result
    assert task.status == TaskStatus.FAILED and r and not r.verified
    assert r.error and r.error.code.value == "limit_exceeded"
    assert h.llm.purposes == ["plan", "step"], (
        "the model is not asked for another way once a budget is spent"
    )
    assert not (env.documents / "ws" / "Java-summary.md").exists()
    assert r.report and r.report.effects == ["fs_organize"]
    assert [s.id for s in r.report.completed] == ["s1", "s2", "s3"] and [
        s.id for s in r.report.failed
    ] == ["s4"]
    assert r.answer_status is AnswerStatus.WITHHELD and "Done:" in (r.answer or "")


async def test_wrong_output_reference_is_repaired_before_anything_runs(env: Env) -> None:
    base = env.documents / "ws"
    build_java_workspace(base)
    bad = java_plan(base)
    bad["steps"][1]["args"]["sources"] = "{{steps.s1.data.matches}}"  # fs_find has no such field
    good = java_plan(base)
    h = make_harness(env, [structured(bad), structured(good), *adaptive_write(base, GOOD_SUMMARY)])
    task = await h.run(INSTR)
    assert h.llm.purposes[:2] == ["plan", "repair"]
    assert task.status == TaskStatus.COMPLETED, task.result
    repair = h.llm.requests[1].messages[-1].content
    assert "strong_paths" in str(repair), "the repair message lists the fields that really exist"


async def test_pause_between_workflow_steps_parks_without_acting(env: Env) -> None:
    import asyncio

    base = env.documents / "ws"
    build_java_workspace(base)
    h = make_harness(env, [structured(java_plan(base)), *adaptive_write(base, GOOD_SUMMARY)])
    orig = env.router.execute
    paused = asyncio.Event()

    async def pause_after_find(call: Any, ctx: Any) -> Any:
        result = await orig(call, ctx)
        if call.tool == "fs_find" and not paused.is_set():
            paused.set()
            await h.runtime.pause(ctx.task_id)
        return result

    env.router.execute = pause_after_find  # type: ignore[method-assign]
    submitted = await h.runtime.submit(INSTR)
    await asyncio.wait_for(paused.wait(), 20)
    await asyncio.sleep(0.5)
    assert not (base / "Java").exists(), "nothing moves while paused"
    mid = await env.repo.get_task(submitted.id)
    assert mid is not None and mid.status == TaskStatus.PAUSED
    await h.runtime.resume(submitted.id)
    await h.runtime.wait(submitted.id, 30)
    done = await env.repo.get_task(submitted.id)
    assert (
        done is not None
        and done.status == TaskStatus.COMPLETED
        and done.result
        and done.result.verified
    )
