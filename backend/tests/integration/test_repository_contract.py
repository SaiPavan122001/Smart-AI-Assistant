"""Every Repository implementation must behave identically (memory, SQLite, PostgreSQL)."""

from __future__ import annotations

from typing import Any

from app.models.approval import ApprovalRequest
from app.models.enums import (
    ApprovalScope,
    ApprovalStatus,
    EventType,
    PerceptionSource,
    RiskLevel,
    StepStatus,
    TaskStatus,
)
from app.models.events import Correlation, ExecutionEvent
from app.models.journal import JournalEntry, JournalOp, JournalState
from app.models.observation import Observation
from app.models.plan import Plan, PlanStep
from app.models.task import Task, TaskResult, UsageStats
from app.models.tool import RiskAssessment, ToolCall, ToolResult
from app.models.verification import VerificationResult, VerificationSpec
from app.permissions.policy import Action, PermissionPolicy
from app.state.repository import (
    AgentActionRecord,
    LogEntry,
    PermissionDecisionRecord,
    ToolCallRecord,
)
from app.utils.errors import ErrorCode, ErrorInfo


def make_task() -> Task:
    t = Task(instruction="move the report", session_id="sess_1", request_id="req_1")
    t.plan = Plan(
        task_id=t.id, summary="plan",
        steps=[
            PlanStep(id="s1", title="Create", tool="fs_create", args={"path": "@Desktop/x"}, verify=[VerificationSpec(kind="dir_exists", params={"path": "@Desktop/x"})]),
            PlanStep(id="s2", title="Adaptive", kind="adaptive", goal="do it", depends_on=["s1"]),  # type: ignore[arg-type]
        ],
        success_criteria=[VerificationSpec(kind="file_exists", params={"path": "@Desktop/x/a.txt"})],
    )  # fmt: skip
    t.plan.steps[0].status = StepStatus.SUCCEEDED
    t.usage = UsageStats(llm_calls=2, estimated_cost_usd=0.01)
    return t


async def test_task_roundtrip_with_plan_and_steps(repo: Any) -> None:
    task = make_task()
    await repo.save_task(task)
    loaded = await repo.get_task(task.id)
    assert loaded is not None
    assert loaded.instruction == task.instruction and loaded.status == TaskStatus.CREATED
    assert loaded.plan is not None and [s.id for s in loaded.plan.steps] == ["s1", "s2"]
    assert loaded.plan.steps[0].status == StepStatus.SUCCEEDED
    assert loaded.plan.steps[0].verify[0].kind == "dir_exists"
    assert loaded.plan.success_criteria[0].kind == "file_exists"
    assert loaded.usage.llm_calls == 2
    assert loaded.created_at.tzinfo is not None

    task.status = TaskStatus.COMPLETED
    task.plan.steps[1].status = StepStatus.FAILED
    task.plan.steps[1].error = ErrorInfo(code=ErrorCode.TIMEOUT, message="slow")
    task.result = TaskResult(
        status=TaskStatus.COMPLETED, summary="done", verified=True, usage=task.usage
    )
    await repo.save_task(task)  # upsert
    loaded = await repo.get_task(task.id)
    assert loaded is not None and loaded.status == TaskStatus.COMPLETED
    assert loaded.result is not None and loaded.result.verified
    assert (
        loaded.plan.steps[1].error is not None
        and loaded.plan.steps[1].error.code == ErrorCode.TIMEOUT
    )

    listed = await repo.list_tasks(limit=10)
    assert [t.id for t in listed] == [task.id]
    assert await repo.list_tasks(status=TaskStatus.FAILED) == []
    assert await repo.get_task("nope") is None


async def test_events_are_ordered_and_replayable(repo: Any) -> None:
    task = make_task()
    await repo.save_task(task)
    for seq in range(1, 6):
        await repo.append_event(
            ExecutionEvent(
                task_id=task.id,
                seq=seq,
                type=EventType.STEP_STARTED,
                message=f"m{seq}",
                data={"n": seq},
                correlation=Correlation(task_id=task.id, step_id="s1"),
            )
        )
    after = await repo.list_events(task.id, after_seq=3)
    assert [e.seq for e in after] == [4, 5]
    assert after[0].correlation.step_id == "s1" and after[0].data == {"n": 4}


async def test_tool_calls_observations_approvals_verifications(repo: Any) -> None:
    task = make_task()
    await repo.save_task(task)
    call = ToolCall(
        tool="fs_delete",
        args={"path": "@Desktop/x"},
        task_id=task.id,
        step_id="s1",
        rationale="cleanup",
    )
    assessment = RiskAssessment(
        risk=RiskLevel.HIGH, action="Delete x", resource="x", category="file_delete"
    )
    await repo.save_tool_call(
        task.id, ToolCallRecord(call=call, assessment=assessment, decision="ask")
    )
    await repo.save_tool_call(
        task.id,
        ToolCallRecord(
            call=call,
            result=ToolResult(
                tool_call_id=call.id,
                tool="fs_delete",
                ok=False,
                error=ErrorInfo(code=ErrorCode.APPROVAL_DENIED, message="no"),
            ),
            assessment=assessment,
            decision="deny",
        ),
    )  # update
    calls = await repo.list_tool_calls(task.id)
    assert (
        len(calls) == 1
        and calls[0].result is not None
        and calls[0].result.error.code == ErrorCode.APPROVAL_DENIED
    )  # type: ignore[union-attr]
    assert calls[0].assessment is not None and calls[0].assessment.risk == RiskLevel.HIGH

    await repo.save_observation(
        Observation(
            task_id=task.id,
            source=PerceptionSource.DOM,
            kind="browser_read",
            data={"summary": "x"},
            untrusted=True,
        )
    )
    obs = await repo.list_observations(task.id)
    assert len(obs) == 1 and obs[0].untrusted and obs[0].source == PerceptionSource.DOM

    req = ApprovalRequest(
        task_id=task.id,
        tool_call_id=call.id,
        tool="fs_delete",
        action="Delete x",
        reason="cleanup",
        resource="x",
        risk=RiskLevel.HIGH,
        risk_explanation=["irreversible"],
        arguments={"path": "x"},
    )
    await repo.save_approval(req)
    assert [a.id for a in await repo.list_approvals(pending_only=True)] == [req.id]
    req.status = ApprovalStatus.APPROVED
    req.scope_granted = ApprovalScope.TASK
    await repo.save_approval(req)
    got = await repo.get_approval(req.id)
    assert (
        got is not None
        and got.status == ApprovalStatus.APPROVED
        and got.scope_granted == ApprovalScope.TASK
    )
    assert got.risk == RiskLevel.HIGH and got.risk_explanation == ["irreversible"]
    assert await repo.list_approvals(pending_only=True) == []

    await repo.save_verification(
        VerificationResult(
            task_id=task.id,
            step_id="s1",
            kind="file_exists",
            passed=False,
            message="missing",
            evidence={"path": "x"},
        )
    )
    vr = await repo.list_verifications(task.id)
    assert len(vr) == 1 and not vr[0].passed and vr[0].evidence == {"path": "x"}


async def test_actions_logs_decisions_and_policy(repo: Any) -> None:
    task = make_task()
    await repo.save_task(task)
    await repo.save_agent_action(
        AgentActionRecord(task_id=task.id, kind="plan", payload={"steps": ["s1"]})
    )
    assert (await repo.list_agent_actions(task.id))[0].payload == {"steps": ["s1"]}

    await repo.add_log(LogEntry(task_id=task.id, level="INFO", component="router", message="hello"))
    await repo.add_log(LogEntry(task_id=task.id, level="ERROR", component="router", message="boom"))
    assert len(await repo.list_logs(task.id)) == 2
    assert [e.message for e in await repo.list_logs(task.id, min_level="ERROR")] == ["boom"]

    await repo.record_permission_decision(
        PermissionDecisionRecord(
            task_id=task.id,
            tool_call_id="tc1",
            tool="fs_delete",
            risk=3,
            action="ask",
            reason="high",
        )
    )
    d = await repo.list_permission_decisions(task.id)
    assert len(d) == 1 and d[0].action == "ask" and d[0].risk == 3

    assert await repo.load_policy() is None
    policy = PermissionPolicy(tool_overrides={"fs_delete": Action.DENY})
    await repo.save_policy(policy.model_dump(mode="json"))
    restored = PermissionPolicy.model_validate(await repo.load_policy())
    assert restored.tool_overrides == {"fs_delete": Action.DENY}
    assert restored.risk_actions[RiskLevel.HIGH] == Action.ASK
    assert await repo.ping()


async def test_file_journal_is_upserted_and_listed_in_order(repo: Any) -> None:
    task, other = make_task(), make_task()
    await repo.save_task(task)
    await repo.save_task(other)
    later = JournalEntry(task_id=task.id, seq=2, op=JournalOp.MOVE, path="C:/d/b.txt", src="C:/s/b.txt",
                         fingerprint="sha256:ab", backup="C:/data/journal/b.txt", backup_fingerprint="sha256:cd")  # fmt: skip
    first = JournalEntry(task_id=task.id, seq=1, op=JournalOp.CREATE_DIR, kind="dir", path="C:/d")
    await repo.save_journal_entry(later)
    await repo.save_journal_entry(first)
    await repo.save_journal_entry(
        JournalEntry(task_id=other.id, seq=1, op=JournalOp.RECYCLE, path="C:/x")
    )
    later.state, later.detail = JournalState.APPLIED, "done"
    await repo.save_journal_entry(later)  # the same entry again: updated, not duplicated

    listed = await repo.list_journal(task.id)
    assert [e.id for e in listed] == [first.id, later.id]
    got = listed[1]
    assert (got.op, got.state, got.kind, got.src, got.fingerprint, got.backup, got.backup_fingerprint, got.detail) == (
        JournalOp.MOVE, JournalState.APPLIED, "file", "C:/s/b.txt", "sha256:ab", "C:/data/journal/b.txt", "sha256:cd", "done",
    )  # fmt: skip
    assert listed[0].kind == "dir" and listed[0].state is JournalState.PENDING
    assert [e.op for e in await repo.list_journal(other.id)] == [JournalOp.RECYCLE]
    assert await repo.list_journal("task_unknown") == []
