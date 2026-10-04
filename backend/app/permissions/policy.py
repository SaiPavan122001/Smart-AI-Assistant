from __future__ import annotations

import fnmatch
from enum import StrEnum

from pydantic import BaseModel, Field

from app.models.enums import Capability, RiskLevel


class Action(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


_ORDER = {Action.ALLOW: 0, Action.ASK: 1, Action.DENY: 2}


def stricter(a: Action, b: Action) -> Action:
    return a if _ORDER[a] >= _ORDER[b] else b


class TrustedOperation(BaseModel):
    """A user-configured operation that may skip approval (never CRITICAL, never when tainted)."""

    tool: str
    resource_glob: str = "*"
    max_risk: RiskLevel = RiskLevel.HIGH
    note: str = ""

    def matches(self, tool: str, resource: str, risk: RiskLevel) -> bool:
        return (
            self.tool == tool
            and risk <= self.max_risk
            and risk < RiskLevel.CRITICAL
            and fnmatch.fnmatch(resource.lower(), self.resource_glob.lower())
        )


class PermissionPolicy(BaseModel):
    """User-editable permission configuration (persisted)."""

    risk_actions: dict[RiskLevel, Action] = Field(
        default_factory=lambda: {
            RiskLevel.LOW: Action.ALLOW,
            RiskLevel.MEDIUM: Action.ALLOW,
            RiskLevel.HIGH: Action.ASK,
            RiskLevel.CRITICAL: Action.ASK,
        }
    )
    tool_overrides: dict[str, Action] = Field(default_factory=dict)
    category_overrides: dict[str, Action] = Field(default_factory=dict)
    trusted_operations: list[TrustedOperation] = Field(default_factory=list)
    disabled_capabilities: list[Capability] = Field(default_factory=list)
    version: int = 1

    def normalized(self) -> PermissionPolicy:
        """Guard rails: CRITICAL can never be auto-allowed, whatever the user config says."""
        actions = dict(self.risk_actions)
        for risk in RiskLevel:
            actions.setdefault(risk, Action.ASK)
        if actions[RiskLevel.CRITICAL] == Action.ALLOW:
            actions[RiskLevel.CRITICAL] = Action.ASK
        return self.model_copy(update={"risk_actions": actions})
