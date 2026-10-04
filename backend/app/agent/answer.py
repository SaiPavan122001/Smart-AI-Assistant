"""Final-answer authority: what the user is told is derived from the FINAL task state.

Nothing in here asks a model anything. The answer of a task is decided once, at the very end,
from authoritative state (final status, final verification, final step states and the recorded
outputs of steps that succeeded). A success answer exists only for a COMPLETED task; every other
outcome gets a truthful statement of what did not happen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.models.enums import AnswerStatus, StepStatus, TaskOutcome, TaskStatus
from app.models.plan import Plan, PlanStep
from app.utils.errors import ErrorCode, ErrorInfo
from app.utils.templating import REF_PATTERN, TemplateError, lookup, template_refs

ANSWER_LIMIT = 2000
UNAVAILABLE_TEXT = "The task completed, but the requested response data was unavailable."
UNVERIFIED_OFFICIAL_NOTE = (
    "Found a relevant result, but I could not verify that it is the official source."
)
_OFFICIAL = re.compile(r"\bofficial(ly)?\b", re.IGNORECASE)

_DENIAL_CODES = {ErrorCode.APPROVAL_DENIED, ErrorCode.APPROVAL_TIMEOUT}
_POLICY_CODES = {
    ErrorCode.POLICY_DENIED,
    ErrorCode.PATH_BLOCKED,
    ErrorCode.COMMAND_BLOCKED,
    ErrorCode.URL_BLOCKED,
    ErrorCode.INJECTION_BLOCKED,
}


@dataclass
class AnswerDecision:
    status: AnswerStatus
    text: str | None
    missing: list[str] = field(default_factory=list)  # template references that had no value


def outcome_of(status: TaskStatus, error: ErrorInfo | None) -> TaskOutcome:
    if status is TaskStatus.COMPLETED:
        return TaskOutcome.COMPLETED
    if status is TaskStatus.CANCELLED:
        return TaskOutcome.CANCELLED
    if status is TaskStatus.TIMED_OUT:
        return TaskOutcome.TIMED_OUT
    code = error.code if error else None
    if code in _DENIAL_CODES or code in _POLICY_CODES:
        return TaskOutcome.DENIED
    if code is ErrorCode.LOOP_DETECTED:
        return TaskOutcome.LOOP_DETECTED
    if code is ErrorCode.PLAN_INVALID:
        return TaskOutcome.PLAN_INVALID
    return TaskOutcome.FAILED


def is_superseded(step: PlanStep) -> bool:
    """Replaced by a revised plan: no longer part of what the task has to accomplish."""
    return bool((step.output or {}).get("superseded"))


def incomplete_required_steps(plan: Plan | None) -> list[PlanStep]:
    """Required (non-optional, not superseded) steps that did not succeed."""
    if plan is None:
        return []
    return [
        s
        for s in plan.steps
        if not s.optional and not is_superseded(s) and s.status is not StepStatus.SUCCEEDED
    ]


def failure_statement(outcome: TaskOutcome, error: ErrorInfo | None) -> str:
    """Truthful, status-specific text for a task that did not succeed. Never a success claim."""
    detail = (error.message if error else "").strip()[:400]
    code = error.code if error else None
    if outcome is TaskOutcome.CANCELLED:
        return (
            "The task was stopped before it finished, so the request was not completed. "
            "Actions already performed were not undone."
        )
    if outcome is TaskOutcome.TIMED_OUT:
        return (
            "The task ran out of time and was not completed. "
            "Actions already performed were not undone."
        )
    if outcome is TaskOutcome.DENIED:
        if code in _DENIAL_CODES:
            return (
                "The task was not completed: a required action was not approved, "
                "so it was not performed."
            )
        return (
            "The task was not completed: a required action is blocked by the security policy, "
            "so it was not performed." + (f" ({detail})" if detail else "")
        )
    if outcome is TaskOutcome.LOOP_DETECTED:
        return (
            "The task was stopped because the agent was repeating actions without making progress. "
            "The request was not completed."
        )
    if outcome is TaskOutcome.PLAN_INVALID:
        return "The task could not be completed: no valid plan could be produced for it." + (
            f" ({detail})" if detail else ""
        )
    if code is ErrorCode.VERIFICATION_FAILED:
        return "The task could not be completed. The requested result was not verified." + (
            f" ({detail})" if detail else ""
        )
    return "The task could not be completed." + (f" Reason: {detail}" if detail else "")


def render_template(
    plan: Plan, step_data: dict[str, dict[str, Any]]
) -> tuple[str | None, list[str]]:
    """Fill ``plan.answer_template`` from recorded outputs of steps that SUCCEEDED.

    Returns ``(text, missing)``. Any reference to a step that did not succeed, to a field that was
    never produced, or to an empty value makes the whole answer unavailable: nothing is invented.
    """
    template = plan.answer_template or ""
    succeeded = {s.id for s in plan.steps if s.status is StepStatus.SUCCEEDED}
    missing: list[str] = []
    values: dict[str, str] = {}
    for ref in template_refs(template):
        parts = ref.split(".")
        sid = parts[1] if len(parts) > 1 and parts[0] == "steps" else None
        recorded = step_data.get(sid or "")
        if sid is None or sid not in succeeded or not recorded or not recorded.get("ok"):
            missing.append(ref)
            continue
        try:
            value = lookup(ref, {"steps": step_data})
        except TemplateError:
            missing.append(ref)
            continue
        if value is None or isinstance(value, dict | list) or str(value).strip() == "":
            missing.append(ref)
            continue
        values[ref] = str(value)
    if missing:
        return None, missing

    def sub(match: re.Match[str]) -> str:
        return values[match.group(1)]

    text = REF_PATTERN.sub(sub, template).strip()
    return (text[:ANSWER_LIMIT] or None), []


def _official_verified(step_data: dict[str, dict[str, Any]]) -> bool:
    return any(
        d.get("ok") and isinstance(d.get("data"), dict) and d["data"].get("official_verified")
        for d in step_data.values()
    )


def guard_officialness(text: str, step_data: dict[str, dict[str, Any]]) -> str:
    """An answer may not assert that a source is 'official' unless a deterministic domain check
    in this task established it (``official_verified`` in a succeeded step's output)."""
    if not _OFFICIAL.search(text) or _official_verified(step_data):
        return text
    if UNVERIFIED_OFFICIAL_NOTE in text:
        return text
    cleaned = _OFFICIAL.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return f"{UNVERIFIED_OFFICIAL_NOTE} {cleaned}"[:ANSWER_LIMIT]


def candidate_answer(
    plan: Plan | None, step_data: dict[str, dict[str, Any]], step_answers: dict[str, str]
) -> AnswerDecision:
    """What the answer WOULD be if the task ends successfully now (no state is committed)."""
    if plan is None:
        return AnswerDecision(AnswerStatus.NOT_REQUESTED, None)
    if plan.answer_template:
        text, missing = render_template(plan, step_data)
        if text is None:
            return AnswerDecision(AnswerStatus.UNAVAILABLE, UNAVAILABLE_TEXT, missing)
        return AnswerDecision(AnswerStatus.ANSWERED, guard_officialness(text, step_data))
    # No template: the answer of the last adaptive step that SUCCEEDED in the final plan.
    for step in reversed(plan.steps):
        if step.status is StepStatus.SUCCEEDED and not is_superseded(step):
            text = (step_answers.get(step.id) or "").strip()
            if text:
                return AnswerDecision(
                    AnswerStatus.ANSWERED, guard_officialness(text, step_data)[:20000]
                )
    return AnswerDecision(AnswerStatus.NOT_REQUESTED, None)


def decide_answer(
    *,
    status: TaskStatus,
    error: ErrorInfo | None,
    verification_passed: bool,
    plan: Plan | None,
    step_data: dict[str, dict[str, Any]],
    step_answers: dict[str, str],
) -> AnswerDecision:
    """The one place a final answer is produced. Eligibility for a success answer:

    task COMPLETED  AND  final verification passed  AND  every required step succeeded
    AND  every template field exists in the recorded output of a succeeded step.
    """
    outcome = outcome_of(status, error)
    if outcome is not TaskOutcome.COMPLETED:
        return AnswerDecision(AnswerStatus.WITHHELD, failure_statement(outcome, error))
    if not verification_passed or incomplete_required_steps(plan):
        # A COMPLETED task always has both; if a caller ever gets here the answer is not rendered.
        return AnswerDecision(AnswerStatus.UNAVAILABLE, UNAVAILABLE_TEXT)
    return candidate_answer(plan, step_data, step_answers)
