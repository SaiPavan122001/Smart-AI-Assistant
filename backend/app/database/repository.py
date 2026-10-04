"""SQLAlchemy implementation of the Repository port."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.database import models as m
from app.models.approval import ApprovalRequest
from app.models.enums import ApprovalScope, ApprovalStatus, EventType, RiskLevel, TaskStatus
from app.models.events import Correlation, ExecutionEvent
from app.models.observation import Observation
from app.models.plan import Plan, PlanStep
from app.models.task import Task, TaskResult, UsageStats
from app.models.tool import RiskAssessment, ToolCall, ToolResult
from app.models.verification import VerificationResult
from app.state.repository import (
    AgentActionRecord,
    LogEntry,
    PermissionDecisionRecord,
    ToolCallRecord,
)
from app.utils.errors import ErrorInfo
from app.utils.ids import utcnow

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _j(model: Any) -> Any:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else model


class SqlRepository:
    def __init__(self, engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._engine = engine
        self._sessions = sessions

    # ------------------------------------------------------------------ users / sessions
    async def _ensure_user(self, s: AsyncSession, user_id: str) -> None:
        if await s.get(m.UserRow, user_id) is None:
            s.add(m.UserRow(id=user_id, name=user_id, created_at=utcnow()))
            await s.flush()

    async def ensure_session(self, session_id: str, user_id: str, meta: dict[str, Any]) -> None:
        async with self._sessions() as s, s.begin():
            await self._ensure_user(s, user_id)
            row = await s.get(m.SessionRow, session_id)
            if row is None:
                s.add(
                    m.SessionRow(
                        id=session_id,
                        user_id=user_id,
                        client_meta=meta,
                        created_at=utcnow(),
                        last_seen_at=utcnow(),
                    )
                )
            else:
                row.last_seen_at = utcnow()

    # ------------------------------------------------------------------ tasks
    async def save_task(self, task: Task) -> None:
        async with self._sessions() as s, s.begin():
            await self._ensure_user(s, task.user_id)
            if task.session_id and await s.get(m.SessionRow, task.session_id) is None:
                s.add(
                    m.SessionRow(
                        id=task.session_id,
                        user_id=task.user_id,
                        client_meta={},
                        created_at=utcnow(),
                        last_seen_at=utcnow(),
                    )
                )
                await s.flush()
            row = await s.get(m.TaskRow, task.id)
            if row is None:
                row = m.TaskRow(
                    id=task.id,
                    user_id=task.user_id,
                    created_at=task.created_at,
                    instruction=task.instruction,
                    status=task.status.value,
                    updated_at=task.updated_at,
                )
                s.add(row)
            row.session_id = task.session_id
            row.request_id = task.request_id
            row.status = task.status.value
            row.current_step_id = task.current_step_id
            row.current_tool = task.current_tool
            row.current_application = task.current_application
            row.paused = task.paused
            row.usage = _j(task.usage)
            row.result = _j(task.result) if task.result else None
            row.error = _j(task.result.error) if task.result and task.result.error else None
            row.updated_at = task.updated_at
            row.started_at = task.started_at
            row.finished_at = task.finished_at
            await s.flush()
            if task.plan is not None:
                await self._save_plan(s, task, task.plan)

    async def _save_plan(self, s: AsyncSession, task: Task, plan: Plan) -> None:
        row = await s.get(m.PlanRow, plan.id)
        if row is None:
            row = m.PlanRow(id=plan.id, task_id=task.id, created_at=plan.created_at)
            s.add(row)
        row.version = plan.version
        row.summary = plan.summary
        row.assumptions = list(plan.assumptions)
        row.success_criteria = [_j(c) for c in plan.success_criteria]
        await s.flush()
        for seq, step in enumerate(plan.steps):
            sid = f"{plan.id}:{step.id}"
            srow = await s.get(m.TaskStepRow, sid)
            if srow is None:
                srow = m.TaskStepRow(
                    id=sid,
                    plan_id=plan.id,
                    task_id=task.id,
                    step_key=step.id,
                    seq=seq,
                    title=step.title,
                    kind=step.kind.value,
                    status=step.status.value,
                )
                s.add(srow)
            srow.seq = seq
            srow.title = step.title
            srow.kind = step.kind.value
            srow.tool = step.tool
            srow.args = step.args
            srow.goal = step.goal
            srow.allowed_tools = step.allowed_tools
            srow.depends_on = step.depends_on
            srow.rationale = step.rationale
            srow.verify = [_j(v) for v in step.verify]
            srow.optional = step.optional
            srow.status = step.status.value
            srow.attempts = step.attempts
            srow.summary = step.summary
            srow.error = _j(step.error) if step.error else None
            srow.output = step.output
            srow.started_at = step.started_at
            srow.finished_at = step.finished_at

    async def get_task(self, task_id: str) -> Task | None:
        async with self._sessions() as s:
            row = await s.get(m.TaskRow, task_id)
            if row is None:
                return None
            task = self._task_from_row(row)
            plan_row = (
                (
                    await s.execute(
                        select(m.PlanRow)
                        .where(m.PlanRow.task_id == task_id)
                        .order_by(m.PlanRow.created_at.desc())
                    )
                )
                .scalars()
                .first()
            )
            if plan_row is not None:
                step_rows = (
                    (
                        await s.execute(
                            select(m.TaskStepRow)
                            .where(m.TaskStepRow.plan_id == plan_row.id)
                            .order_by(m.TaskStepRow.seq)
                        )
                    )
                    .scalars()
                    .all()
                )
                task.plan = Plan(
                    id=plan_row.id, task_id=task_id, version=plan_row.version, summary=plan_row.summary,
                    assumptions=list(plan_row.assumptions or []),
                    success_criteria=plan_row.success_criteria or [],
                    steps=[self._step_from_row(r) for r in step_rows], created_at=_aware(plan_row.created_at) or utcnow(),
                )  # fmt: skip
            return task

    async def list_tasks(
        self, *, limit: int = 50, offset: int = 0, status: TaskStatus | None = None
    ) -> list[Task]:
        async with self._sessions() as s:
            q = select(m.TaskRow).order_by(m.TaskRow.created_at.desc()).limit(limit).offset(offset)
            if status:
                q = q.where(m.TaskRow.status == status.value)
            rows = (await s.execute(q)).scalars().all()
            return [self._task_from_row(r) for r in rows]

    @staticmethod
    def _task_from_row(r: m.TaskRow) -> Task:
        return Task(
            id=r.id, user_id=r.user_id, session_id=r.session_id, request_id=r.request_id, instruction=r.instruction,
            status=TaskStatus(r.status), paused=r.paused, current_step_id=r.current_step_id, current_tool=r.current_tool,
            current_application=r.current_application, usage=UsageStats.model_validate(r.usage or {}),
            result=TaskResult.model_validate(r.result) if r.result else None,
            created_at=_aware(r.created_at) or utcnow(), updated_at=_aware(r.updated_at) or utcnow(),
            started_at=_aware(r.started_at), finished_at=_aware(r.finished_at),
        )  # fmt: skip

    @staticmethod
    def _step_from_row(r: m.TaskStepRow) -> PlanStep:
        return PlanStep.model_validate(
            {
                "id": r.step_key, "title": r.title, "kind": r.kind, "tool": r.tool, "args": r.args or {}, "goal": r.goal,
                "allowed_tools": r.allowed_tools or [], "depends_on": r.depends_on or [], "rationale": r.rationale,
                "verify": r.verify or [], "optional": r.optional, "status": r.status, "attempts": r.attempts,
                "summary": r.summary, "error": r.error, "output": r.output or {},
                "started_at": _aware(r.started_at), "finished_at": _aware(r.finished_at),
            }
        )  # fmt: skip

    # ------------------------------------------------------------------ events
    async def append_event(self, event: ExecutionEvent) -> None:
        async with self._sessions() as s, s.begin():
            s.add(
                m.TaskEventRow(
                    id=event.id,
                    task_id=event.task_id,
                    seq=event.seq,
                    type=event.type.value,
                    message=event.message,
                    data=event.data,
                    correlation=_j(event.correlation),
                    timestamp=event.timestamp,
                )
            )

    async def list_events(
        self, task_id: str, *, after_seq: int = 0, limit: int = 2000
    ) -> list[ExecutionEvent]:
        async with self._sessions() as s:
            q = (
                select(m.TaskEventRow)
                .where(m.TaskEventRow.task_id == task_id, m.TaskEventRow.seq > after_seq)
                .order_by(m.TaskEventRow.seq)
                .limit(limit)
            )
            rows = (await s.execute(q)).scalars().all()
            return [
                ExecutionEvent(id=r.id, task_id=r.task_id, seq=r.seq, type=EventType(r.type), message=r.message, data=r.data or {}, correlation=Correlation.model_validate(r.correlation or {}), timestamp=_aware(r.timestamp) or utcnow())
                for r in rows
            ]  # fmt: skip

    # ------------------------------------------------------------------ tool calls / observations
    async def save_tool_call(self, task_id: str, record: ToolCallRecord) -> None:
        async with self._sessions() as s, s.begin():
            row = await s.get(m.ToolCallRow, record.call.id)
            if row is None:
                row = m.ToolCallRow(
                    id=record.call.id,
                    task_id=task_id,
                    tool=record.call.tool,
                    created_at=record.created_at,
                )
                s.add(row)
            row.step_key = record.call.step_id
            row.args = record.call.args
            row.rationale = record.call.rationale
            row.assessment = _j(record.assessment) if record.assessment else None
            row.decision = record.decision
            row.decision_reason = record.decision_reason
            if record.result is not None:
                row.ok = record.result.ok
                row.error_code = record.result.error.code.value if record.result.error else None
                row.duration_ms = record.result.duration_ms
                row.result = _j(record.result)

    async def list_tool_calls(self, task_id: str) -> list[ToolCallRecord]:
        async with self._sessions() as s:
            rows = (
                (
                    await s.execute(
                        select(m.ToolCallRow)
                        .where(m.ToolCallRow.task_id == task_id)
                        .order_by(m.ToolCallRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [
                ToolCallRecord(
                    call=ToolCall(id=r.id, tool=r.tool, args=r.args or {}, task_id=task_id, step_id=r.step_key, rationale=r.rationale),
                    result=ToolResult.model_validate(r.result) if r.result else None,
                    assessment=RiskAssessment.model_validate(r.assessment) if r.assessment else None,
                    decision=r.decision, decision_reason=r.decision_reason, created_at=_aware(r.created_at) or utcnow(),
                )
                for r in rows
            ]  # fmt: skip

    async def save_observation(self, obs: Observation) -> None:
        if not obs.task_id:
            return
        async with self._sessions() as s, s.begin():
            s.add(
                m.ObservationRow(
                    id=obs.id,
                    task_id=obs.task_id,
                    step_key=obs.step_id,
                    source=obs.source.value,
                    kind=obs.kind,
                    subject=obs.subject,
                    data=obs.data,
                    content_hash=obs.content_hash,
                    untrusted=obs.untrusted,
                    approx_tokens=obs.approx_tokens,
                    created_at=obs.created_at,
                )
            )

    async def list_observations(self, task_id: str) -> list[Observation]:
        async with self._sessions() as s:
            rows = (
                (
                    await s.execute(
                        select(m.ObservationRow)
                        .where(m.ObservationRow.task_id == task_id)
                        .order_by(m.ObservationRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [
                Observation(id=r.id, task_id=task_id, step_id=r.step_key, source=r.source, kind=r.kind, subject=r.subject, data=r.data or {}, content_hash=r.content_hash, untrusted=r.untrusted, approx_tokens=r.approx_tokens, created_at=_aware(r.created_at) or utcnow())  # type: ignore[arg-type]
                for r in rows
            ]  # fmt: skip

    # ------------------------------------------------------------------ approvals
    async def save_approval(self, req: ApprovalRequest) -> None:
        async with self._sessions() as s, s.begin():
            row = await s.get(m.ApprovalRow, req.id)
            if row is None:
                row = m.ApprovalRow(
                    id=req.id,
                    task_id=req.task_id,
                    tool_call_id=req.tool_call_id,
                    tool=req.tool,
                    created_at=req.created_at,
                    action=req.action,
                    reason=req.reason,
                    resource=req.resource,
                    risk=int(req.risk),
                    status=req.status.value,
                )
                s.add(row)
            row.step_key = req.step_id
            row.action, row.reason, row.resource = req.action, req.reason, req.resource
            row.risk = int(req.risk)
            row.risk_explanation = req.risk_explanation
            row.arguments = req.arguments
            row.allowed_scopes = [x.value for x in req.allowed_scopes]
            row.tainted = req.tainted
            row.category = req.category
            row.status = req.status.value
            row.scope_granted = req.scope_granted.value if req.scope_granted else None
            row.decided_by = req.decided_by
            row.decision_note = req.decision_note
            row.expires_at = req.expires_at
            row.decided_at = req.decided_at

    @staticmethod
    def _approval(r: m.ApprovalRow) -> ApprovalRequest:
        return ApprovalRequest(
            id=r.id, task_id=r.task_id, step_id=r.step_key, tool_call_id=r.tool_call_id, tool=r.tool, action=r.action,
            reason=r.reason, resource=r.resource, risk=RiskLevel(r.risk), risk_explanation=list(r.risk_explanation or []),
            arguments=r.arguments or {}, allowed_scopes=[ApprovalScope(x) for x in (r.allowed_scopes or [])],
            tainted=r.tainted, category=r.category, status=ApprovalStatus(r.status),
            scope_granted=ApprovalScope(r.scope_granted) if r.scope_granted else None, decided_by=r.decided_by,
            decision_note=r.decision_note, created_at=_aware(r.created_at) or utcnow(),
            expires_at=_aware(r.expires_at), decided_at=_aware(r.decided_at),
        )  # fmt: skip

    async def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        async with self._sessions() as s:
            row = await s.get(m.ApprovalRow, approval_id)
            return self._approval(row) if row else None

    async def list_approvals(
        self, *, task_id: str | None = None, pending_only: bool = False
    ) -> list[ApprovalRequest]:
        async with self._sessions() as s:
            q = select(m.ApprovalRow).order_by(m.ApprovalRow.created_at)
            if task_id:
                q = q.where(m.ApprovalRow.task_id == task_id)
            if pending_only:
                q = q.where(m.ApprovalRow.status == ApprovalStatus.PENDING.value)
            return [self._approval(r) for r in (await s.execute(q)).scalars().all()]

    # ------------------------------------------------------------------ verification / actions / logs
    async def save_verification(self, result: VerificationResult) -> None:
        if not result.task_id:
            return
        async with self._sessions() as s, s.begin():
            s.add(
                m.VerificationResultRow(
                    id=result.id,
                    task_id=result.task_id,
                    step_key=result.step_id,
                    kind=result.kind,
                    passed=result.passed,
                    description=result.description,
                    evidence=result.evidence,
                    message=result.message,
                    method=result.method,
                    created_at=result.created_at,
                )
            )

    async def list_verifications(self, task_id: str) -> list[VerificationResult]:
        async with self._sessions() as s:
            rows = (
                (
                    await s.execute(
                        select(m.VerificationResultRow)
                        .where(m.VerificationResultRow.task_id == task_id)
                        .order_by(m.VerificationResultRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [
                VerificationResult(id=r.id, task_id=task_id, step_id=r.step_key, kind=r.kind, passed=r.passed, description=r.description, evidence=r.evidence or {}, message=r.message, method=r.method, created_at=_aware(r.created_at) or utcnow())
                for r in rows
            ]  # fmt: skip

    async def save_agent_action(self, record: AgentActionRecord) -> None:
        async with self._sessions() as s, s.begin():
            s.add(
                m.AgentActionRow(
                    id=record.id,
                    task_id=record.task_id,
                    step_key=record.step_id,
                    kind=record.kind,
                    payload=record.payload,
                    created_at=record.created_at,
                )
            )

    async def list_agent_actions(self, task_id: str) -> list[AgentActionRecord]:
        async with self._sessions() as s:
            rows = (
                (
                    await s.execute(
                        select(m.AgentActionRow)
                        .where(m.AgentActionRow.task_id == task_id)
                        .order_by(m.AgentActionRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [
                AgentActionRecord(
                    id=r.id,
                    task_id=task_id,
                    step_id=r.step_key,
                    kind=r.kind,
                    payload=r.payload or {},
                    created_at=_aware(r.created_at) or utcnow(),
                )
                for r in rows
            ]

    async def add_log(self, entry: LogEntry) -> None:
        async with self._sessions() as s, s.begin():
            if entry.task_id and await s.get(m.TaskRow, entry.task_id) is None:
                return  # task rows are written first; ignore logs for unknown tasks
            s.add(
                m.ExecutionLogRow(
                    id=entry.id,
                    task_id=entry.task_id,
                    level=entry.level,
                    component=entry.component,
                    message=entry.message,
                    data=entry.data,
                    correlation=entry.correlation,
                    created_at=entry.created_at,
                )
            )

    async def list_logs(
        self, task_id: str, *, limit: int = 1000, min_level: str | None = None
    ) -> list[LogEntry]:
        floor = _LEVELS.get((min_level or "DEBUG").upper(), 10)
        async with self._sessions() as s:
            rows = (
                (
                    await s.execute(
                        select(m.ExecutionLogRow)
                        .where(m.ExecutionLogRow.task_id == task_id)
                        .order_by(m.ExecutionLogRow.created_at)
                        .limit(limit * 3)
                    )
                )
                .scalars()
                .all()
            )
            out = [
                LogEntry(id=r.id, task_id=task_id, level=r.level, component=r.component, message=r.message, data=r.data or {}, correlation=r.correlation or {}, created_at=_aware(r.created_at) or utcnow())
                for r in rows if _LEVELS.get(r.level, 20) >= floor
            ]  # fmt: skip
            return out[:limit]

    # ------------------------------------------------------------------ permissions
    async def record_permission_decision(self, rec: PermissionDecisionRecord) -> None:
        if not rec.task_id:
            return
        async with self._sessions() as s, s.begin():
            s.add(
                m.PermissionRow(
                    id=rec.id,
                    kind="decision",
                    task_id=rec.task_id,
                    tool_call_id=rec.tool_call_id,
                    tool=rec.tool,
                    risk=rec.risk,
                    action=rec.action,
                    reason=rec.reason,
                    via=rec.via,
                    data={},
                    created_at=rec.created_at,
                )
            )

    async def list_permission_decisions(self, task_id: str) -> list[PermissionDecisionRecord]:
        async with self._sessions() as s:
            rows = (
                (
                    await s.execute(
                        select(m.PermissionRow)
                        .where(
                            m.PermissionRow.task_id == task_id, m.PermissionRow.kind == "decision"
                        )
                        .order_by(m.PermissionRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [
                PermissionDecisionRecord(
                    id=r.id,
                    task_id=task_id,
                    tool_call_id=r.tool_call_id,
                    tool=r.tool or "",
                    risk=r.risk or 0,
                    action=r.action or "",
                    reason=r.reason,
                    via=r.via,
                    created_at=_aware(r.created_at) or utcnow(),
                )
                for r in rows
            ]

    async def load_policy(self) -> dict[str, Any] | None:
        async with self._sessions() as s:
            row = await s.get(m.PermissionRow, "policy")
            return dict(row.data) if row else None

    async def save_policy(self, policy: dict[str, Any]) -> None:
        async with self._sessions() as s, s.begin():
            row = await s.get(m.PermissionRow, "policy")
            if row is None:
                s.add(m.PermissionRow(id="policy", kind="policy", data=policy, created_at=utcnow()))
            else:
                row.data = policy

    # ------------------------------------------------------------------ health
    async def ping(self) -> bool:
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self._engine.dispose()


_ = (delete, ErrorInfo)
