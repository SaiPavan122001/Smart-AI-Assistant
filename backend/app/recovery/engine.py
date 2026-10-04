"""Recovery engine: deterministic, bounded, reasoned.

A failure never leads to blind repetition. The engine picks the next strategy from an
explicit table keyed by the structured error code, tracks every attempt with its reason,
and ends in ``FAIL`` when budgets are exhausted. Recovery actions are *proposals*: they
still run through the tool router and permission engine like any other action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.config.settings import LimitSettings
from app.models.plan import PlanStep
from app.utils.errors import ErrorCode
from app.utils.ids import utcnow


class Strategy(StrEnum):
    RETRY = "retry"  # same action again after a short backoff
    REFRESH_RETRY = "refresh_state_retry"  # drop caches / re-observe, then retry
    ALTERNATIVE = "alternative_target"  # same intent, different deterministic target
    ADAPTIVE = "adaptive_fallback"  # bounded LLM loop with the error in context
    REPLAN = "replan"  # new plan for the remaining work
    FAIL = "fail"  # stop safely


@dataclass
class RecoveryAttempt:
    step_id: str
    strategy: Strategy
    reason: str
    error_code: str
    phase: str
    at: Any = field(default_factory=utcnow)


@dataclass
class RecoveryDecision:
    strategy: Strategy
    reason: str
    delay_s: float = 0.0
    modified_args: dict[str, Any] | None = None
    adaptive_goal: str | None = None


# Errors where retrying or re-planning can never help (a human decision or a hard limit ended it).
TERMINAL_CODES = {
    ErrorCode.APPROVAL_DENIED, ErrorCode.APPROVAL_TIMEOUT, ErrorCode.INJECTION_BLOCKED, ErrorCode.CANCELLED,
    ErrorCode.KILL_SWITCH, ErrorCode.LIMIT_EXCEEDED, ErrorCode.LOOP_DETECTED, ErrorCode.NOT_AVAILABLE, ErrorCode.UNKNOWN_TOOL,
}  # fmt: skip
# Policy blocks: a compliant alternative may exist, but only one re-plan is worth trying.
POLICY_CODES = {
    ErrorCode.POLICY_DENIED,
    ErrorCode.PATH_BLOCKED,
    ErrorCode.COMMAND_BLOCKED,
    ErrorCode.URL_BLOCKED,
}
# Failures that happen *before* any side effect, so repeating the same call is safe.
PRE_EFFECT_CODES = {
    ErrorCode.STALE_REFERENCE, ErrorCode.ELEMENT_NOT_FOUND, ErrorCode.NAVIGATION_FAILED, ErrorCode.LLM_ERROR,
}  # fmt: skip

_SEQUENCES: dict[ErrorCode, list[Strategy]] = {
    ErrorCode.STALE_REFERENCE: [Strategy.REFRESH_RETRY, Strategy.REFRESH_RETRY, Strategy.ADAPTIVE],
    ErrorCode.ELEMENT_NOT_FOUND: [
        Strategy.REFRESH_RETRY,
        Strategy.ALTERNATIVE,
        Strategy.ADAPTIVE,
        Strategy.REPLAN,
    ],
    ErrorCode.AMBIGUOUS_TARGET: [Strategy.ADAPTIVE, Strategy.REPLAN],
    ErrorCode.TIMEOUT: [Strategy.RETRY, Strategy.RETRY, Strategy.ADAPTIVE],
    ErrorCode.NAVIGATION_FAILED: [Strategy.RETRY, Strategy.RETRY, Strategy.ADAPTIVE],
    ErrorCode.IO_ERROR: [Strategy.RETRY, Strategy.ADAPTIVE],
    ErrorCode.LLM_ERROR: [Strategy.RETRY, Strategy.RETRY],
    ErrorCode.COMMAND_FAILED: [Strategy.ADAPTIVE, Strategy.REPLAN],
    ErrorCode.INVALID_INPUT: [Strategy.ADAPTIVE, Strategy.REPLAN],
    ErrorCode.NOT_FOUND: [Strategy.ADAPTIVE, Strategy.REPLAN],
    ErrorCode.ALREADY_EXISTS: [Strategy.ADAPTIVE, Strategy.REPLAN],
    ErrorCode.LOW_CONFIDENCE: [Strategy.ADAPTIVE],
    ErrorCode.VERIFICATION_FAILED: [Strategy.REFRESH_RETRY, Strategy.ADAPTIVE, Strategy.REPLAN],
    ErrorCode.PLAN_INVALID: [Strategy.REPLAN],
    ErrorCode.BLOCKED_BY_SITE: [
        Strategy.REPLAN
    ],  # retrying the same page cannot help; use another source
}
_DEFAULT = [Strategy.ADAPTIVE, Strategy.REPLAN]


def alternative_args(tool: str, args: dict[str, Any], n: int) -> dict[str, Any] | None:
    """Same intent, different deterministic target. Returns None when nothing sensible remains."""
    if not (tool.startswith(("browser_", "desktop_")) and ("query" in args or "ref" in args)):
        return None
    new = dict(args)
    if new.get("ref") and new.get("query"):
        new.pop("ref")  # a stale ref is worse than the semantic query
        return new
    if new.get("role"):
        new.pop("role")  # relax the role constraint
        return new
    q = str(new.get("query", "")).strip()
    words = re.findall(r"[A-Za-z0-9]+", q)
    if len(words) > 1:
        new["query"] = words[0] if n % 2 == 0 else words[-1]  # most distinctive single word
        return new
    return None


class RecoveryEngine:
    def __init__(self, limits: LimitSettings) -> None:
        self._limits = limits

    def decide(
        self,
        step: PlanStep,
        code: ErrorCode,
        message: str,
        history: list[RecoveryAttempt],
        *,
        replans_used: int,
        tool_idempotent: bool,
        phase: str = "action",  # action | verify | adaptive
        cancelled: bool = False,
    ) -> RecoveryDecision:
        mine = [h for h in history if h.step_id == step.id]
        if cancelled or code in TERMINAL_CODES:
            return RecoveryDecision(
                Strategy.FAIL, f"{code.value} is final; no recovery is attempted"
            )
        if len(mine) >= self._limits.max_retries_per_step:
            return RecoveryDecision(
                Strategy.FAIL,
                f"recovery budget for step {step.id} exhausted ({len(mine)} attempts)",
            )

        seq = list(_SEQUENCES.get(code, _DEFAULT))
        if code in POLICY_CODES:
            seq = [Strategy.REPLAN]
        used = [h.strategy for h in mine]
        idx = len(mine)

        # choose the first strategy in the table that is still allowed
        for strategy in [*seq[idx:], Strategy.FAIL]:
            decision = self._materialise(
                strategy, step, code, message, mine, replans_used=replans_used,
                tool_idempotent=tool_idempotent, phase=phase, used=used,
            )  # fmt: skip
            if decision is not None:
                return decision
        return RecoveryDecision(Strategy.FAIL, "no applicable recovery strategy")

    def _materialise(
        self, strategy: Strategy, step: PlanStep, code: ErrorCode, message: str, mine: list[RecoveryAttempt],
        *, replans_used: int, tool_idempotent: bool, phase: str, used: list[Strategy],
    ) -> RecoveryDecision | None:  # fmt: skip
        if strategy is Strategy.FAIL:
            return RecoveryDecision(
                Strategy.FAIL, f"unrecoverable after {len(mine)} attempt(s): {message[:160]}"
            )
        if strategy is Strategy.RETRY:
            if phase == "verify":
                return RecoveryDecision(
                    Strategy.REFRESH_RETRY,
                    "re-check the condition after a short wait (eventual consistency)",
                    delay_s=1.0,
                )
            if not (tool_idempotent or code in PRE_EFFECT_CODES):
                return None  # repeating a possibly half-applied action is unsafe; inspect first
            return RecoveryDecision(
                Strategy.RETRY,
                f"transient {code.value}; retrying with backoff",
                delay_s=min(1.0 * (len(mine) + 1), 4.0),
            )
        if strategy is Strategy.REFRESH_RETRY:
            return RecoveryDecision(
                Strategy.REFRESH_RETRY,
                f"{code.value}: refreshing structured state before retrying",
                delay_s=0.5,
            )
        if strategy is Strategy.ALTERNATIVE:
            if phase != "action" or step.tool is None:
                return None
            alt = alternative_args(
                step.tool, step.args, len([u for u in used if u is Strategy.ALTERNATIVE])
            )
            if alt is None:
                return None
            return RecoveryDecision(
                Strategy.ALTERNATIVE,
                "trying an alternative deterministic selector for the same target",
                modified_args=alt,
            )
        if strategy is Strategy.ADAPTIVE:
            if step.kind.value == "adaptive" or Strategy.ADAPTIVE in used:
                return None  # never nest adaptive loops
            goal = (
                f"{step.title}. The planned action ({step.tool}) failed with {code.value}: {message[:300]}. "
                "Inspect the current state, choose a different way to achieve the same outcome, and perform it."
                if phase == "action"
                else f"{step.title}. The action ALREADY RAN (its side effect happened) but its verification failed: {message[:300]}. "
                "Do NOT repeat the action (do not launch/open it again). Inspect the current state and only "
                "complete or correct what is missing so the condition holds."
            )
            return RecoveryDecision(
                Strategy.ADAPTIVE,
                f"{code.value}: delegating to a bounded adaptive loop",
                adaptive_goal=goal,
            )
        if strategy is Strategy.REPLAN:
            if replans_used >= self._limits.max_replans:
                return None
            return RecoveryDecision(
                Strategy.REPLAN, f"{code.value}: asking the planner for a revised plan"
            )
        return None
