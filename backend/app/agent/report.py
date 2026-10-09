"""Build the completion report from the FINAL state (steps, recorded effects, evaluated requirements)."""

from __future__ import annotations

from app.agent.answer import is_superseded
from app.models.enums import StepStatus
from app.models.goal import RequirementResult
from app.models.plan import Plan, PlanStep
from app.models.report import CompletionReport, StepReport
from app.utils.redact import redact_text


def _row(s: PlanStep) -> StepReport:
    text = s.error.message if s.error else s.summary
    return StepReport(
        id=s.id, title=redact_text(s.title)[:120], status=s.status.value,
        summary=redact_text(text or "")[:200],
    )  # fmt: skip


def build_report(
    plan: Plan | None, requirements: list[RequirementResult], *, effects: list[str], replans: int,
    recoveries: int,
) -> CompletionReport:  # fmt: skip
    steps = [s for s in (plan.steps if plan else []) if not is_superseded(s)]
    return CompletionReport(
        completed=[_row(s) for s in steps if s.status is StepStatus.SUCCEEDED],
        failed=[_row(s) for s in steps if s.status is StepStatus.FAILED],
        interrupted=[
            _row(s) for s in steps if s.status in (StepStatus.RUNNING, StepStatus.WAITING_APPROVAL)
        ],
        not_run=[
            _row(s) for s in steps if s.status in (StepStatus.PENDING, StepStatus.SKIPPED)
        ],  # fmt: skip
        requirements=requirements,
        effects=effects[:40],
        replans=replans,
        recoveries=recoveries,
    )
