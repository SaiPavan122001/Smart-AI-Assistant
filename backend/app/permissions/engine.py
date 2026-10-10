"""Permission engine: the mandatory security boundary between the router and tools.

``decide`` is a pure function of (policy, tool spec, risk assessment, task grants,
taint state). It never executes anything; the router enforces the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.enums import ApprovalRequirement, ApprovalScope, RiskLevel
from app.models.tool import RiskAssessment, ToolSpec
from app.permissions.policy import Action, PermissionPolicy, stricter


@dataclass
class Grant:
    tool: str
    category: str
    note: str = ""


@dataclass
class TaskGrants:
    """Task-scoped approvals (cleared when the task ends)."""

    grants: list[Grant] = field(default_factory=list)

    def add(self, tool: str, category: str, note: str = "") -> None:
        if not self.has(tool, category):
            self.grants.append(Grant(tool, category, note))

    def has(self, tool: str, category: str) -> bool:
        return any(g.tool == tool and g.category == category for g in self.grants)


@dataclass
class Decision:
    action: Action
    reason: str
    tainted_escalation: bool = False
    allowed_scopes: list[ApprovalScope] = field(
        default_factory=lambda: [ApprovalScope.ONCE, ApprovalScope.TASK]
    )
    via: str = "policy"  # policy | trusted_operation | task_grant | hard_deny | override


class PermissionEngine:
    def __init__(self, policy: PermissionPolicy | None = None) -> None:
        self._policy = (policy or PermissionPolicy()).normalized()

    @property
    def policy(self) -> PermissionPolicy:
        return self._policy

    def update_policy(self, policy: PermissionPolicy) -> PermissionPolicy:
        self._policy = policy.model_copy(update={"version": self._policy.version + 1}).normalized()
        return self._policy

    def decide(
        self,
        spec: ToolSpec,
        assessment: RiskAssessment,
        *,
        grants: TaskGrants,
        tainted: bool = False,
        uncertain: str | None = None,
    ) -> Decision:
        """``uncertain``: this exact action was in flight when the service stopped and its outcome
        could not be determined. It is never allowed silently: not by policy, not by a trusted
        operation, not by a task grant. The user decides, for this one call only."""
        decision = self._decide(spec, assessment, grants=grants, tainted=tainted)
        if uncertain and not spec.read_only and decision.action != Action.DENY:
            return Decision(
                Action.ASK,
                f"Approval required: this action may already have happened ({uncertain})",
                tainted_escalation=decision.tainted_escalation,
                allowed_scopes=[ApprovalScope.ONCE],
                via="restart_uncertain",
            )
        return decision

    def _decide(
        self, spec: ToolSpec, assessment: RiskAssessment, *, grants: TaskGrants, tainted: bool
    ) -> Decision:
        pol = self._policy
        if assessment.hard_deny:
            return Decision(Action.DENY, assessment.hard_deny, via="hard_deny")
        if spec.capability in pol.disabled_capabilities:
            return Decision(
                Action.DENY, f"The '{spec.capability.value}' capability is disabled in permissions",
                via="override",
            )  # fmt: skip
        override = pol.tool_overrides.get(spec.name)
        if override == Action.DENY:
            return Decision(Action.DENY, f"Tool '{spec.name}' is denied by policy", via="override")
        cat_override = pol.category_overrides.get(assessment.category)
        if cat_override == Action.DENY:
            return Decision(
                Action.DENY, f"Category '{assessment.category}' is denied by policy", via="override"
            )  # fmt: skip

        risk = assessment.risk
        scopes = (
            [ApprovalScope.ONCE, ApprovalScope.TASK]
            if assessment.scoped_grant_allowed and risk < RiskLevel.CRITICAL and not tainted
            else [ApprovalScope.ONCE]
        )

        if spec.approval == ApprovalRequirement.ALWAYS:
            return Decision(Action.ASK, "This tool always requires approval", allowed_scopes=scopes)

        base = pol.risk_actions.get(risk, Action.ASK)
        if override is not None:
            base = stricter(base, override) if override == Action.ASK else base
            if override == Action.ALLOW and risk < RiskLevel.CRITICAL:
                base = Action.ALLOW
        if cat_override is not None:
            base = stricter(base, cat_override) if cat_override == Action.ASK else base
        if risk == RiskLevel.CRITICAL:
            base = Action.ASK

        escalated = False
        if tainted and risk >= RiskLevel.MEDIUM and base == Action.ALLOW:
            base = Action.ASK
            escalated = True

        if base == Action.ALLOW:
            return Decision(Action.ALLOW, f"{risk.label} risk allowed by policy", via="policy")

        # base == ASK: honour trusted operations and task-scoped grants (never when tainted)
        if not tainted:
            for op in pol.trusted_operations:
                if op.matches(spec.name, assessment.resource, risk):
                    return Decision(
                        Action.ALLOW, f"Trusted operation: {op.note or op.tool}", via="trusted_operation"
                    )  # fmt: skip
            if (
                assessment.scoped_grant_allowed
                and risk < RiskLevel.CRITICAL
                and grants.has(spec.name, assessment.category)
            ):
                return Decision(
                    Action.ALLOW, "Covered by an approval granted for this task", via="task_grant"
                )
        return Decision(
            Action.ASK,
            "Approval required"
            + (" (escalated: this task read content containing prompt-injection patterns)" if escalated or tainted else ""),
            tainted_escalation=escalated or tainted,
            allowed_scopes=scopes,
        )  # fmt: skip
