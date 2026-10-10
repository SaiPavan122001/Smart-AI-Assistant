"""NEXT-B: a task survives the death of the process, without repeating or guessing side effects.

Every test runs the real runtime (planner validation, router, permission engine, approvals, tools,
verification) with a scripted model. A restart is two independent processes over the same storage
(``tests/restart_helpers.py``); the first one dies at an exact, chosen point.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from app.llm.scripted import structured, tool_use
from app.models.approval import ApprovalDecision, ApprovalRequest
from app.models.enums import (
    AnswerStatus,
    ApprovalScope,
    ApprovalStatus,
    StepStatus,
    TaskOutcome,
    TaskStatus,
)
from app.models.recovery import ActionCertainty, RecoveryOption
from app.utils.errors import AgentError, ErrorCode, NotFoundError

from tests.helpers import Env, Responder, approve_all, deny_all
from tests.restart_helpers import (
    MOVE,
    Crash,
    boot,
    bumps,
    crash_run,
    crashable,
    die_in_tool,
    executions,
    finish,
    kill,
    move_plan,
    restart,
    storage,
    with_bump,
)


def bump_plan(counter: Path, *, steps: int = 1) -> dict[str, Any]:
    return {
        "summary": "bump the counter",
        "steps": [
            {
                "id": f"s{i}", "title": f"Bump {i}", "tool": "test_bump", "rationale": "requested",
                "args": {"path": str(counter)},
            }
            for i in range(1, steps + 1)
        ],
        "success_criteria": [{"kind": "file_exists", "params": {"path": str(counter)}}],
    }  # fmt: skip


def delete_plan(*paths: Path) -> dict[str, Any]:
    return {
        "summary": "delete",
        "steps": [
            {
                "id": f"s{i}", "title": f"Delete {p.name}", "tool": "fs_delete", "rationale": "requested",
                "args": {"path": str(p)},
            }
            for i, p in enumerate(paths, 1)
        ],
        "success_criteria": [{"kind": "file_absent", "params": {"path": str(p)}} for p in paths],
    }  # fmt: skip


async def interrupted_move(env: Env, *, after_effect: bool) -> tuple[Env, str]:
    """Process 1 dies inside the move; returns process 2's environment and the task id."""
    repo = crashable(env)
    die_in_tool(env, repo, "fs_move", after_effect=after_effect)
    h1 = await boot(env, [structured(move_plan())])
    task = await crash_run(h1, MOVE)
    assert not task.status.is_terminal, "a dead process cannot have finished the task"
    return restart(env), task.id


# ----------------------------------------------------------------------------- restart while running
async def test_a_task_running_at_a_restart_is_interrupted_not_failed(env: Env) -> None:
    env2, tid = await interrupted_move(env, after_effect=False)
    h2 = await boot(env2)
    task = await env2.repo.get_task(tid)
    assert task is not None and task.status is TaskStatus.INTERRUPTED and task.result is None
    found = task.interruption
    assert found is not None and found.resumable and found.interrupted_step == "s3"
    assert found.completed_steps == ["s1", "s2"] and found.remaining_steps == ["s3"]
    assert RecoveryOption.RESUME in found.options and RecoveryOption.CANCEL in found.options
    events = await h2.events(tid)
    assert "task_interrupted" in events and "task_failed" not in events
    assert h2.runtime.active_task_ids() == [], "nothing resumes by itself"
    assert [a.kind for a in await env2.repo.list_agent_actions(tid)].count("interruption") == 1


async def test_restart_just_before_a_side_effect_runs_it_exactly_once(env: Env) -> None:
    env2, tid = await interrupted_move(env, after_effect=False)
    h2 = await boot(env2)
    task = await env2.repo.get_task(tid)
    assert task and task.interruption
    [action] = task.interruption.in_flight
    assert action.tool == "fs_move" and action.certainty is ActionCertainty.CONFIRMED_NOT_DONE
    assert (env.downloads / "note.txt").exists()

    await h2.runtime.resume_interrupted(tid)
    done = await finish(h2, tid)
    assert done.status is TaskStatus.COMPLETED and done.result and done.result.verified
    assert (env.desktop / "Spring Docs" / "note.txt").read_text() == "hello"
    assert not (env.downloads / "note.txt").exists()
    assert h2.llm.requests == [], "deterministic steps need no model call to continue"
    # completed steps were not executed again; the move ran in process 1 (died) and once in process 2
    assert await executions(env2, tid, "fs_create") == 2
    assert await executions(env2, tid, "fs_move") == 2


async def test_restart_just_after_a_side_effect_does_not_repeat_it(env: Env) -> None:
    env2, tid = await interrupted_move(env, after_effect=True)
    assert (env.desktop / "Spring Docs" / "note.txt").exists(), "the move happened before the crash"
    h2 = await boot(env2)
    task = await env2.repo.get_task(tid)
    assert task and task.interruption
    [action] = task.interruption.in_flight
    assert action.certainty is ActionCertainty.CONFIRMED_DONE

    await h2.runtime.resume_interrupted(tid)
    done = await finish(h2, tid)
    assert done.status is TaskStatus.COMPLETED and done.result and done.result.verified
    assert await executions(env2, tid, "fs_move") == 1, (
        "confirmed done: replayed, never re-executed"
    )
    calls = await env2.repo.list_tool_calls(tid)
    assert [c.decision for c in calls if c.call.tool == "fs_move"] == ["reconciled", "replayed"]
    assert (env.desktop / "Spring Docs" / "note.txt").read_text() == "hello"
    assert "task_resuming" in await h2.events(tid)


async def test_result_recorded_but_step_not_completed_is_replayed(env: Env) -> None:
    """The process dies between "tool result written" and "step marked done"."""
    repo = crashable(env)

    def after_move_result(name: str, args: tuple[Any, ...]) -> str | None:
        if name != "save_tool_call":
            return None
        record = args[1]
        return "after" if record.call.tool == "fs_move" and record.result is not None else None

    repo.trigger = after_move_result
    h1 = await boot(env, [structured(move_plan())])
    task = await crash_run(h1, MOVE)
    assert task.plan and task.plan.step("s3").status is StepStatus.RUNNING

    env2 = restart(env)
    h2 = await boot(env2)
    found = (await env2.repo.get_task(task.id)).interruption  # type: ignore[union-attr]
    assert found is not None and found.in_flight == [], "the result was recorded: nothing in flight"
    await h2.runtime.resume_interrupted(task.id)
    done = await finish(h2, task.id)
    assert done.status is TaskStatus.COMPLETED and done.result and done.result.verified
    assert await executions(env2, task.id, "fs_move") == 1
    assert done.plan and done.plan.step("s3").status is StepStatus.SUCCEEDED


async def test_completed_step_outputs_survive_and_feed_later_steps(env: Env) -> None:
    """s3 needs ``{{steps.s2.artifacts.path}}``: the output of a step that ran in the dead process."""
    env2, tid = await interrupted_move(env, after_effect=False)
    stored = await env2.repo.get_task(tid)
    assert stored and stored.durable and stored.durable.step_data["s2"]["artifacts"]["path"]
    assert stored.plan and stored.plan.goal is not None, (
        "the explicit goal is persisted with the plan"
    )
    h2 = await boot(env2)
    await h2.runtime.resume_interrupted(tid)
    assert (await finish(h2, tid)).status is TaskStatus.COMPLETED


async def test_graceful_shutdown_leaves_the_task_resumable(env: Env) -> None:
    with_bump(env)
    counter = env.documents / "n.txt"
    gate = asyncio.Event()
    tool = env.router.registry.get("test_bump")
    assert tool is not None
    original = tool.run

    async def slow(args: Any, ctx: Any) -> Any:
        out = await original(args, ctx)
        gate.set()
        await asyncio.sleep(30)  # the service stops while the action is still "running"
        return out

    tool.run = slow  # type: ignore[method-assign]
    h1 = await boot(env, [structured(bump_plan(counter))])
    task = await h1.runtime.submit("bump the counter")
    await asyncio.wait_for(gate.wait(), 10)
    await h1.runtime.shutdown()
    stored = await env.repo.get_task(task.id)
    assert stored and not stored.status.is_terminal, "a service stop is not a cancellation"
    assert "task_cancelled" not in await h1.events(task.id)

    env2 = with_bump(restart(env))
    await boot(env2)
    after = await env2.repo.get_task(task.id)
    assert after and after.status is TaskStatus.INTERRUPTED and after.interruption
    assert [a.certainty for a in after.interruption.in_flight] == [ActionCertainty.UNCERTAIN]
    assert after.interruption.needs_attention


# ----------------------------------------------------------------------------- uncertain side effects
async def uncertain_bump(env: Env) -> tuple[Env, str, Path]:
    with_bump(env)
    counter = env.documents / "n.txt"
    repo = crashable(env)
    die_in_tool(env, repo, "test_bump", after_effect=True)
    h1 = await boot(env, [structured(bump_plan(counter))])
    task = await crash_run(h1, "bump the counter")
    assert bumps(counter) == 1
    return with_bump(restart(env)), task.id, counter


async def test_an_uncertain_action_is_never_repeated_without_approval(env: Env) -> None:
    env2, tid, counter = await uncertain_bump(env)
    h2 = await boot(env2)
    with Responder(env2, deny_all) as human:
        await h2.runtime.resume_interrupted(tid)
        done = await finish(h2, tid)
    assert bumps(counter) == 1, "refused: the action did not run a second time"
    [asked] = human.seen
    assert asked.tool == "test_bump" and asked.allowed_scopes == [ApprovalScope.ONCE]
    assert "in progress when the service stopped" in asked.risk_explanation[0]
    assert done.status is TaskStatus.FAILED and done.result
    assert done.result.outcome is TaskOutcome.DENIED and not done.result.verified
    assert done.result.answer_status is AnswerStatus.WITHHELD


async def test_an_uncertain_action_runs_again_only_after_the_user_approves(env: Env) -> None:
    env2, tid, counter = await uncertain_bump(env)
    h2 = await boot(env2)
    decisions = await env2.repo.list_permission_decisions(tid)
    assert [d.action for d in decisions] == ["allow"], (
        "before the crash the policy allowed it silently"
    )
    with Responder(env2, approve_all) as human:
        await h2.runtime.resume_interrupted(tid)
        done = await finish(h2, tid)
    assert len(human.seen) == 1 and bumps(counter) == 2
    assert done.status is TaskStatus.COMPLETED
    after = await env2.repo.list_permission_decisions(tid)
    assert after[-1].action == "ask" and after[-1].via == "restart_uncertain"
    # the old in-flight record was closed: a further restart does not ask about it again
    assert all(c.result is not None for c in await env2.repo.list_tool_calls(tid))


async def test_uncertainty_survives_a_second_restart(env: Env) -> None:
    env2, tid, counter = await uncertain_bump(env)
    await boot(env2)  # process 2 starts and dies without the user deciding anything
    env3 = with_bump(restart(env2))
    h3 = await boot(env3)
    task = await env3.repo.get_task(tid)
    assert task and task.status is TaskStatus.INTERRUPTED and task.interruption
    assert [a.certainty for a in task.interruption.in_flight] == [ActionCertainty.UNCERTAIN]
    assert (await h3.events(tid)).count("task_interrupted") == 1, "not announced a second time"
    with Responder(env3, deny_all):
        await h3.runtime.resume_interrupted(tid)
        await finish(h3, tid)
    assert bumps(counter) == 1


async def test_a_trusted_operation_or_grant_cannot_wave_an_uncertain_action_through(
    env: Env,
) -> None:
    from app.models.enums import RiskLevel
    from app.models.tool import RiskAssessment
    from app.permissions.engine import TaskGrants
    from app.permissions.policy import Action

    tool = with_bump(env).router.registry.get("test_bump")
    assert tool is not None
    assessment = RiskAssessment(risk=RiskLevel.LOW, action="bump", resource="n.txt")
    grants = TaskGrants()
    grants.add("test_bump", assessment.category)
    normal = env.engine.decide(tool.spec(), assessment, grants=grants)
    held = env.engine.decide(tool.spec(), assessment, grants=grants, uncertain="outcome unknown")
    assert normal.action is Action.ALLOW
    assert held.action is Action.ASK and held.allowed_scopes == [ApprovalScope.ONCE]
    read = env.router.registry.get("fs_read")
    assert read is not None
    assert (
        env.engine.decide(read.spec(), assessment, grants=grants, uncertain="x").action
        is Action.ALLOW
    )


# ----------------------------------------------------------------------------- approvals
async def waiting_for_delete(env: Env, *paths: Path) -> tuple[Env, str, ApprovalRequest]:
    """Process 1 dies while its task waits for the user's decision on a delete."""
    for p in paths:
        p.write_text("keep me")
    repo = crashable(env)
    h1 = await boot(env, [structured(delete_plan(*paths))])
    task = await h1.runtime.submit("delete the old files")
    for _ in range(500):
        if env.approvals.pending():
            break
        await asyncio.sleep(0.01)
    [pending] = env.approvals.pending()
    await kill(h1, repo, task.id)
    return restart(env), task.id, pending


async def test_restart_while_awaiting_approval_asks_again_and_never_acts_by_itself(
    env: Env,
) -> None:
    victim = env.documents / "old.txt"
    env2, tid, old = await waiting_for_delete(env, victim)
    h2 = await boot(env2)
    task = await env2.repo.get_task(tid)
    assert task and task.status is TaskStatus.INTERRUPTED and task.interruption
    assert task.interruption.awaiting_approval and task.interruption.in_flight == []
    stale = await env2.repo.get_approval(old.id)
    assert stale is not None and stale.status is ApprovalStatus.EXPIRED
    with pytest.raises(NotFoundError):  # the old request cannot be answered in the new process
        await env2.approvals.resolve(old.id, ApprovalDecision(approved=True))
    assert victim.exists()

    with Responder(env2, approve_all) as human:
        await h2.runtime.resume_interrupted(tid)
        done = await finish(h2, tid)
    [asked] = human.seen
    assert asked.id != old.id and asked.arguments == old.arguments, "same exact action, new request"
    assert done.status is TaskStatus.COMPLETED and not victim.exists()


async def test_a_denial_after_a_restart_is_final(env: Env) -> None:
    victim = env.documents / "old.txt"
    env2, tid, _ = await waiting_for_delete(env, victim)
    h2 = await boot(env2)
    with Responder(env2, deny_all):
        await h2.runtime.resume_interrupted(tid)
        done = await finish(h2, tid)
    assert victim.exists() and done.status is TaskStatus.FAILED
    assert done.result and done.result.outcome is TaskOutcome.DENIED


async def test_an_approval_given_before_the_crash_is_not_reused(env: Env) -> None:
    """Approved once, then the process dies before the action starts: the approval died with it."""
    victim = env.documents / "old.txt"
    victim.write_text("keep me")
    repo = crashable(env)

    def at_intent(name: str, args: tuple[Any, ...]) -> str | None:
        return "before" if name == "save_tool_call" and args[1].decision == "executing" else None

    repo.trigger = at_intent
    h1 = await boot(env, [structured(delete_plan(victim))])
    with Responder(env, approve_all) as first:
        task = await crash_run(h1, "delete the old file")
    assert len(first.seen) == 1 and victim.exists(), "approved, but never executed"

    env2 = restart(env)
    h2 = await boot(env2)
    with Responder(env2, deny_all) as second:
        await h2.runtime.resume_interrupted(task.id)
        done = await finish(h2, task.id)
    assert len(second.seen) == 1, "the user is asked again"
    assert victim.exists() and done.status is TaskStatus.FAILED


async def test_a_task_scoped_grant_does_not_survive_a_restart(env: Env) -> None:
    a, b = env.documents / "a.txt", env.documents / "b.txt"
    for p in (a, b):
        p.write_text("x")
    repo = crashable(env)

    def second_delete(name: str, args: tuple[Any, ...]) -> str | None:
        if name == "save_tool_call" and args[1].decision == "executing":
            return "before" if args[1].call.step_id == "s2" else None
        return None

    repo.trigger = second_delete
    h1 = await boot(env, [structured(delete_plan(a, b))])
    with Responder(env, lambda _r: ApprovalDecision(approved=True, scope=ApprovalScope.TASK)) as p1:
        task = await crash_run(h1, "delete both files")
    assert len(p1.seen) == 1 and not a.exists() and b.exists(), (
        "the grant covered the second delete"
    )

    env2 = restart(env)
    h2 = await boot(env2)
    with Responder(env2, approve_all) as p2:
        await h2.runtime.resume_interrupted(task.id)
        done = await finish(h2, task.id)
    assert len(p2.seen) == 1, "after a restart the grant is gone: the delete is asked again"
    assert done.status is TaskStatus.COMPLETED and not b.exists()
    assert await executions(env2, task.id, "fs_delete") == 2, "the first delete was not repeated"


# ----------------------------------------------------------------------------- pause, cancel, STOP
async def test_restart_while_paused_resumes_only_when_the_user_asks(env: Env) -> None:
    repo = crashable(env)
    h1 = await boot(env, [structured(move_plan())])
    original = h1.runtime._runner.run

    async def pause_first(task: Any, handle: Any) -> Any:
        await h1.runtime.pause(task.id)
        return await original(task, handle)

    h1.runtime._runner.run = pause_first  # type: ignore[method-assign]
    task = await h1.runtime.submit(MOVE)
    for _ in range(300):
        stored = await env.repo.get_task(task.id)
        if stored and stored.status is TaskStatus.PAUSED:
            break
        await asyncio.sleep(0.01)
    assert stored and stored.status is TaskStatus.PAUSED
    await kill(h1, repo, task.id)

    env2 = restart(env)
    h2 = await boot(env2, [structured(move_plan())])
    after = await env2.repo.get_task(task.id)
    assert after and after.status is TaskStatus.INTERRUPTED and after.interruption
    assert after.interruption.was_paused and h2.llm.requests == []
    await h2.runtime.resume_interrupted(task.id)
    assert (await finish(h2, task.id)).status is TaskStatus.COMPLETED


async def test_a_cancel_recorded_before_the_crash_is_honoured(env: Env) -> None:
    with_bump(env)
    counter = env.documents / "n.txt"
    repo = crashable(env)
    entered = asyncio.Event()
    tool = env.router.registry.get("test_bump")
    assert tool is not None

    async def hang(_args: Any, _ctx: Any) -> Any:
        entered.set()
        await asyncio.sleep(30)

    tool.run = hang  # type: ignore[method-assign]
    h1 = await boot(env, [structured(bump_plan(counter))])
    task = await h1.runtime.submit("bump the counter")
    await asyncio.wait_for(entered.wait(), 10)

    def after_cancel_flag(name: str, args: tuple[Any, ...]) -> str | None:
        return "after" if name == "save_task" and args[0].cancel_requested else None

    repo.trigger = after_cancel_flag
    with pytest.raises(Crash):  # the process dies right after recording the user's decision
        await h1.runtime.cancel(task.id)
    await kill(h1, repo, task.id)

    env2 = with_bump(restart(env))
    h2 = await boot(env2)
    after = await env2.repo.get_task(task.id)
    assert after and after.status is TaskStatus.CANCELLED and after.result
    assert after.result.answer_status is AnswerStatus.WITHHELD
    assert "task_interrupted" not in await h2.events(task.id)
    with pytest.raises(AgentError):
        await h2.runtime.resume_interrupted(task.id)
    assert bumps(counter) == 0


async def test_stop_recorded_before_the_crash_is_honoured(env: Env) -> None:
    with_bump(env)
    counter = env.documents / "n.txt"
    repo = crashable(env)
    entered = asyncio.Event()
    tool = env.router.registry.get("test_bump")
    assert tool is not None

    async def hang(_args: Any, _ctx: Any) -> Any:
        entered.set()
        await asyncio.sleep(30)

    tool.run = hang  # type: ignore[method-assign]
    h1 = await boot(env, [structured(bump_plan(counter))])
    task = await h1.runtime.submit("bump the counter")
    await asyncio.wait_for(entered.wait(), 10)
    repo.trigger = lambda name, args: (
        "after" if name == "save_task" and args[0].cancel_requested else None
    )
    with pytest.raises(Crash):
        await h1.runtime.stop_all()
    assert env.kill.engaged
    await kill(h1, repo, task.id)

    env2 = with_bump(restart(env))
    await boot(env2)
    after = await env2.repo.get_task(task.id)
    assert after and after.status is TaskStatus.CANCELLED


async def test_resume_is_refused_while_the_emergency_stop_is_engaged(env: Env) -> None:
    env2, tid = await interrupted_move(env, after_effect=False)
    h2 = await boot(env2)
    env2.kill.engage("STOP")
    with pytest.raises(AgentError) as err:
        await h2.runtime.resume_interrupted(tid)
    assert err.value.code is ErrorCode.KILL_SWITCH
    assert (await env2.repo.get_task(tid)).status is TaskStatus.INTERRUPTED  # type: ignore[union-attr]
    assert (env.downloads / "note.txt").exists()


async def test_stop_during_a_resumed_task_cancels_it(env: Env) -> None:
    env2, tid, counter = await uncertain_bump(env)
    h2 = await boot(env2)
    await h2.runtime.resume_interrupted(tid)
    for _ in range(500):  # it waits for the approval of the uncertain action
        if env2.approvals.pending():
            break
        await asyncio.sleep(0.01)
    await h2.runtime.stop_all()
    done = await finish(h2, tid)
    assert done.status is TaskStatus.CANCELLED and bumps(counter) == 1


# ----------------------------------------------------------------------------- the user's options
async def test_cancelling_an_interrupted_task_reports_what_was_done(env: Env) -> None:
    env2, tid = await interrupted_move(env, after_effect=False)
    h2 = await boot(env2)
    await h2.runtime.cancel(tid)
    done = await env2.repo.get_task(tid)
    assert (
        done and done.status is TaskStatus.CANCELLED and done.result and done.interruption is None
    )
    r = done.result
    assert r.outcome is TaskOutcome.CANCELLED and not r.verified
    assert r.answer_status is AnswerStatus.WITHHELD and r.report
    assert [s.id for s in r.report.completed] == ["s1", "s2"]
    assert [s.id for s in r.report.interrupted] == ["s3"]
    assert "task_cancelled" in await h2.events(tid)
    with pytest.raises(AgentError):
        await h2.runtime.resume_interrupted(tid)


async def test_restart_is_an_explicit_new_task(env: Env) -> None:
    env2, tid = await interrupted_move(env, after_effect=False)
    (env.downloads / "note.txt").unlink()  # let the new run start from a clean note
    h2 = await boot(env2, [structured(move_plan())])
    new = await h2.runtime.restart_interrupted(tid)
    assert new.id != tid and new.restarted_from == tid
    done = await finish(h2, new.id)
    old = await env2.repo.get_task(tid)
    assert old and old.status is TaskStatus.CANCELLED
    assert done.status is TaskStatus.COMPLETED and h2.llm.purposes == ["plan"]


async def test_a_task_without_durable_state_can_only_be_restarted_or_cancelled(env: Env) -> None:
    """Rows written before migration 0002 have a plan but no step outputs."""
    env2, tid = await interrupted_move(env, after_effect=False)
    legacy = await env2.repo.get_task(tid)
    assert legacy is not None
    legacy.durable = None
    await env2.repo.save_task(legacy)
    h2 = await boot(env2)
    task = await env2.repo.get_task(tid)
    assert task and task.interruption and not task.interruption.resumable
    assert RecoveryOption.RESUME not in task.interruption.options
    with pytest.raises(AgentError):
        await h2.runtime.resume_interrupted(tid)


# ----------------------------------------------------------------------------- duplicates
async def test_the_same_request_is_not_started_next_to_an_interrupted_task(env: Env) -> None:
    env2, tid = await interrupted_move(env, after_effect=False)
    h2 = await boot(env2)
    again = await h2.runtime.submit(MOVE)
    assert again.id == tid and again.status is TaskStatus.INTERRUPTED
    assert h2.runtime.active_task_ids() == [] and h2.llm.requests == []


async def test_an_idempotency_key_is_honoured_across_a_restart(env: Env) -> None:
    repo = crashable(env)
    die_in_tool(env, repo, "fs_move", after_effect=False)
    h1 = await boot(env, [structured(move_plan())])
    task = await crash_run(h1, MOVE, idempotency_key="client-key-1")
    h2 = await boot(restart(env))
    again = await h2.runtime.submit(
        "a re-sent request with other words", idempotency_key="client-key-1"
    )
    assert again.id == task.id and h2.llm.requests == []


async def test_resuming_twice_runs_the_task_once(env: Env) -> None:
    env2, tid = await interrupted_move(env, after_effect=False)
    h2 = await boot(env2)
    first, second = await asyncio.gather(
        h2.runtime.resume_interrupted(tid), h2.runtime.resume_interrupted(tid),
        return_exceptions=True,
    )  # fmt: skip
    assert any(not isinstance(r, Exception) for r in (first, second))
    done = await finish(h2, tid)
    assert done.status is TaskStatus.COMPLETED
    assert await executions(env2, tid, "fs_move") == 2, "once in the dead process, once now"
    with pytest.raises(AgentError):  # finished: there is nothing to resume any more
        await h2.runtime.resume_interrupted(tid)


async def test_events_stay_ordered_and_unique_across_a_restart(env: Env) -> None:
    env2, tid = await interrupted_move(env, after_effect=True)
    h2 = await boot(env2)
    await h2.runtime.resume_interrupted(tid)
    await finish(h2, tid)
    events = await env2.repo.list_events(tid)
    seqs = [e.seq for e in events]
    assert seqs == sorted(set(seqs)) == list(range(1, len(seqs) + 1)), "no gap, no duplicate"
    types = [e.type.value for e in events]
    assert (
        types.index("task_interrupted")
        < types.index("task_resuming")
        < types.index("task_completed")
    )
    assert types.count("task_completed") == 1 and types.count("task_created") == 1
    statuses = [e.data["status"] for e in events if e.type.value == "status_changed"]
    assert statuses[statuses.index("INTERRUPTED") if "INTERRUPTED" in statuses else 0 :]
    assert statuses[statuses.index("RECOVERING") + 1] == "EXECUTING"


# ----------------------------------------------------------------------------- limits and policy
async def test_a_resumed_task_keeps_its_usage_and_its_explicit_goal(env: Env) -> None:
    """The prohibition in the request still binds after the restart, and budgets continue."""
    note = env.documents / "note.txt"
    victim = env.documents / "victim.txt"
    victim.write_text("important")
    plan = {
        "summary": "write, then finish",
        "steps": [
            {"id": "s1", "title": "Write", "tool": "fs_create", "rationale": "requested",
             "args": {"path": str(note), "content": "hi"}},
            {"id": "s2", "title": "Finish up", "kind": "adaptive", "depends_on": ["s1"],
             "rationale": "wrap up", "goal": "Confirm the note exists"},
        ],
        "success_criteria": [{"kind": "file_exists", "params": {"path": str(note)}}],
    }  # fmt: skip
    repo = crashable(env)

    def die_in_model(_request: Any) -> Any:
        repo.die()
        raise Crash("model call")

    h1 = await boot(env, [structured(plan), die_in_model])
    task = await crash_run(h1, f"Write {note.name} in my documents. Do not delete anything.")
    assert task.usage.llm_calls >= 1

    env2 = restart(env)
    h2 = await boot(
        env2,
        [
            tool_use("fs_delete", path=str(victim)),
            tool_use("finish_step", success=True, summary="the note exists"),
        ],
    )
    await h2.runtime.resume_interrupted(task.id)
    done = await finish(h2, task.id)
    assert victim.read_text() == "important"
    blocked = [c for c in await env2.repo.list_tool_calls(task.id) if c.call.tool == "fs_delete"]
    assert blocked and blocked[0].result and blocked[0].result.error
    assert blocked[0].result.error.code is ErrorCode.GOAL_VIOLATION
    assert done.status is TaskStatus.COMPLETED
    assert done.usage.llm_calls > task.usage.llm_calls, (
        "usage continues, it does not restart at zero"
    )
    assert await executions(env2, task.id, "fs_create") == 1


async def test_a_refusal_from_before_the_restart_is_still_final(env: Env) -> None:
    """Denied, then the process dies before the step is closed: the user is NOT asked again."""
    victim = env.documents / "old.txt"
    victim.write_text("keep me")
    repo = crashable(env)

    def once_refused(name: str, args: tuple[Any, ...]) -> str | None:
        if name == "save_task" and args[0].durable is not None and args[0].durable.denied:
            return "after"
        return None

    repo.trigger = once_refused
    h1 = await boot(env, [structured(delete_plan(victim))])
    with Responder(env, deny_all) as first:
        task = await crash_run(h1, "delete the old file")
    assert len(first.seen) == 1
    assert task.plan and task.plan.step("s1").status is not StepStatus.FAILED, "died mid-step"

    env2 = restart(env)
    h2 = await boot(env2)
    with Responder(env2, approve_all) as second:
        await h2.runtime.resume_interrupted(task.id)
        done = await finish(h2, task.id)
    assert second.seen == [], "a refusal is final: the same action is not requested again"
    assert victim.exists() and done.status is TaskStatus.FAILED
    assert done.result and done.result.outcome is TaskOutcome.DENIED
    assert await executions(env2, task.id, "fs_delete") == 0


# ----------------------------------------------------------------------------- storage failures
async def test_a_side_effect_does_not_start_when_its_start_cannot_be_recorded(env: Env) -> None:
    """Database failure at the write-ahead record: without it the action could not be recovered."""
    with_bump(env)
    counter = env.documents / "n.txt"
    repo = crashable(env)
    repo.fail = lambda name, args: name == "save_tool_call" and args[1].decision == "executing"
    h1 = await boot(
        env, [structured(bump_plan(counter))], fallback=structured({"clarification": "stop"})
    )
    task = await h1.runtime.submit("bump the counter")
    done = await finish(h1, task.id)
    assert bumps(counter) == 0, "not recorded, so not performed"
    assert done.status is TaskStatus.FAILED and done.result and not done.result.verified
    failed = [c for c in await storage(env).list_tool_calls(task.id) if c.call.tool == "test_bump"]
    assert failed and all(c.result and not c.result.ok for c in failed)
    assert any(
        "could not be recorded" in (c.result.error.message if c.result and c.result.error else "")
        for c in failed
    )


async def test_a_failing_task_save_ends_the_task_without_a_success_claim(env: Env) -> None:
    repo = crashable(env)
    seen = {"n": 0}

    def third_save(name: str, _args: tuple[Any, ...]) -> bool:
        if name != "save_task":
            return False
        seen["n"] += 1
        return seen["n"] == 6

    repo.fail = third_save
    h1 = await boot(env, [structured(move_plan())])
    task = await h1.runtime.submit(MOVE)
    done = await finish(h1, task.id)
    assert done.status is TaskStatus.FAILED and done.result and not done.result.verified
    assert done.result.answer_status is AnswerStatus.WITHHELD


# ----------------------------------------------------------------------------- classification
def _in_flight(tool: str, args: dict[str, Any]) -> Any:
    from app.models.tool import ToolCall
    from app.perception.cache import call_key
    from app.state.repository import ToolCallRecord

    call = ToolCall(
        tool=tool, args=args, task_id="t", step_id="s1", fingerprint=call_key(tool, args)
    )
    return ToolCallRecord(call=call, decision="executing")


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("browser_click", {"query": "Submit order"}, ActionCertainty.UNCERTAIN),
        ("browser_type", {"query": "Email", "text": "a@b.c"}, ActionCertainty.UNCERTAIN),
        ("browser_download", {"url": "https://example.com/f.zip"}, ActionCertainty.UNCERTAIN),
        ("desktop_click", {"query": "Send"}, ActionCertainty.UNCERTAIN),
        ("desktop_type", {"text": "hello"}, ActionCertainty.UNCERTAIN),
        ("desktop_launch", {"app": "notepad"}, ActionCertainty.UNCERTAIN),
        ("terminal_execute_safe", {"command": "python setup.py"}, ActionCertainty.UNCERTAIN),
        ("browser_open", {"url": "https://example.com"}, ActionCertainty.CONFIRMED_NOT_DONE),
        ("browser_navigate", {"url": "https://example.com"}, ActionCertainty.CONFIRMED_NOT_DONE),
        ("fs_create", {"path": "@Documents/x.txt", "content": "x"}, ActionCertainty.SAFE_TO_RETRY),
        ("fs_organize", {"sources": [], "dest_dir": "@Documents/J"}, ActionCertainty.SAFE_TO_RETRY),
        ("no_such_tool", {}, ActionCertainty.UNCERTAIN),
    ],
)
def test_classification_of_an_action_that_was_in_flight(
    tmp_path: Path, tool: str, args: dict[str, Any], expected: ActionCertainty
) -> None:
    from app.orchestration.recovery import classify

    from tests.helpers import make_env

    e = make_env(tmp_path, include_browser=True, include_desktop=True)
    found = classify(_in_flight(tool, args), e.router.registry, e.services)
    assert found.certainty is expected, found.detail


def test_file_actions_are_reconciled_from_the_files_only(env: Env) -> None:
    from app.orchestration.recovery import classify

    def verdict(tool: str, **args: Any) -> ActionCertainty:
        return classify(_in_flight(tool, args), env.router.registry, env.services).certainty

    src, dst = env.documents / "a.txt", env.documents / "b.txt"
    src.write_text("data")
    assert verdict("fs_copy", src=str(src), dst=str(dst)) is ActionCertainty.CONFIRMED_NOT_DONE
    dst.write_text("data")
    assert verdict("fs_copy", src=str(src), dst=str(dst)) is ActionCertainty.CONFIRMED_DONE
    dst.write_text("half")  # a partial or different file is NOT taken for a finished copy
    assert verdict("fs_copy", src=str(src), dst=str(dst)) is ActionCertainty.UNCERTAIN
    assert verdict("fs_move", src=str(src), dst=str(dst)) is ActionCertainty.UNCERTAIN, "both exist"
    assert (
        verdict("fs_move", src=str(src), dst=str(dst), overwrite=True) is ActionCertainty.UNCERTAIN
    )
    gone = env.documents / "gone.txt"
    assert (
        verdict("fs_move", src=str(gone), dst=str(env.documents / "nowhere.txt"))
        is ActionCertainty.UNCERTAIN
    )
    assert verdict("fs_delete", path=str(src)) is ActionCertainty.CONFIRMED_NOT_DONE
    assert verdict("fs_delete", path=str(gone)) is ActionCertainty.CONFIRMED_DONE
    # a path outside the allowed roots is never probed
    assert (
        verdict("fs_delete", path=str(env.root.parent / "elsewhere.txt"))
        is ActionCertainty.UNCERTAIN
    )
    # arguments that were masked when stored cannot be checked against the state
    assert verdict("fs_move", src="***REDACTED***", dst=str(dst)) is ActionCertainty.UNCERTAIN


# ----------------------------------------------------------------------------- browser state is lost
async def test_a_lost_browser_session_leads_to_a_revised_plan_not_to_blind_clicks(
    tmp_path: Path,
) -> None:
    """Persisted state of a task that had a page open; the new process has no such page."""
    from app.models.plan import Plan, PlanStep
    from app.models.recovery import DurableState
    from app.models.task import Task
    from app.models.tool import ToolCall, ToolResult
    from app.state.repository import ToolCallRecord

    from tests.helpers import make_env

    e = make_env(tmp_path, include_browser=True)
    note = e.documents / "title.txt"
    task = Task(instruction="Open example.com and save its title to title.txt")
    task.status = TaskStatus.EXECUTING
    task.plan = Plan(
        task_id=task.id,
        steps=[
            PlanStep(id="s1", title="Open", tool="browser_open", args={"url": "https://example.com"}, status=StepStatus.SUCCEEDED),
            PlanStep(id="s2", title="Read", tool="browser_read", args={"mode": "text"}),
        ],
    )  # fmt: skip
    state = {
        "url": "https://example.com",
        "title": "Example Domain",
        "tabs": 1,
        "navigation": "loaded",
    }
    task.durable = DurableState(
        step_data={"s1": {"data": state, "artifacts": {}, "summary": "open", "ok": True}}
    )
    await e.repo.save_task(task)
    call = ToolCall(
        tool="browser_open",
        args={"url": "https://example.com"},
        task_id=task.id,
        step_id="s1",
        fingerprint="browser_open:x",
    )
    await e.repo.save_tool_call(
        task.id,
        ToolCallRecord(call=call, decision="allow", result=ToolResult(tool_call_id=call.id, tool="browser_open", ok=True, data=state, summary="open")),
    )  # fmt: skip
    revised = {
        "summary": "write the title that was already read",
        "steps": [
            {"id": "r1", "title": "Save", "tool": "fs_create", "rationale": "requested",
             "args": {"path": str(note), "content": "Example Domain"}},
        ],
        "success_criteria": [{"kind": "file_exists", "params": {"path": str(note)}}],
    }  # fmt: skip
    h = await boot(e, [structured(revised)])
    stored = await e.repo.get_task(task.id)
    assert stored and stored.interruption
    assert any("browser session did not survive" in n for n in stored.interruption.notes)
    await h.runtime.resume_interrupted(task.id)
    done = await finish(h, task.id)
    assert h.llm.purposes == ["replan"], "the remaining step needs a page that no longer exists"
    assert "browser session did not survive" in str(h.llm.requests[0].messages[-1].content)
    assert done.status is TaskStatus.COMPLETED and note.read_text() == "Example Domain"
    assert await executions(e, task.id, "browser_read") == 0
