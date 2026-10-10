"""NEXT-C: every tool is placed in exactly one permission, and the placement is true."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.models.enums import ApprovalScope, RiskLevel
from app.models.tool import ToolCall
from app.permissions.classes import (
    EXTERNAL_OR_IRREVERSIBLE,
    OBSERVING,
    TOOL_PERMISSIONS,
    Permission,
)
from app.permissions.engine import PermissionEngine, TaskGrants
from app.permissions.policy import Action, PermissionPolicy
from app.registry import build_registry
from app.utils.errors import ErrorCode

from tests.helpers import Env, Responder, deny_all

REGISTRY = build_registry(include_browser=True, include_desktop=True, include_vision=True)
DOC = Path(__file__).resolve().parents[3] / "docs" / "architecture" / "permissions.md"


def test_every_registered_tool_has_exactly_one_permission_and_nothing_is_stale() -> None:
    assert sorted(TOOL_PERMISSIONS) == REGISTRY.names(), (
        "a tool was added or removed without placing it in app/permissions/classes.py"
    )
    assert set(TOOL_PERMISSIONS.values()) == set(Permission), "an unused permission class"


@pytest.mark.parametrize("name", sorted(TOOL_PERMISSIONS))
def test_a_tool_in_an_observing_permission_really_is_read_only(name: str) -> None:
    tool = REGISTRY.get(name)
    assert tool is not None
    observing = TOOL_PERMISSIONS[name] in OBSERVING
    if observing:
        assert tool.read_only, f"{name} is classed as observing but may change something"
        assert tool.risk is RiskLevel.LOW
    elif TOOL_PERMISSIONS[name] is not Permission.SCREEN_CAPTURE:
        assert not tool.read_only, f"{name} acts but is declared read-only"


def test_the_documentation_lists_every_tool_under_its_permission() -> None:
    text = DOC.read_text(encoding="utf-8")
    for name, permission in TOOL_PERMISSIONS.items():
        row = next(
            (line for line in text.splitlines() if line.startswith(f"| `{permission.value}`")), ""
        )
        assert f"`{name}`" in row, (
            f"{name} is missing from the '{permission.value}' row of permissions.md"
        )


def test_high_and_critical_risk_is_never_silently_allowed_by_default() -> None:
    from app.models.tool import RiskAssessment

    engine = PermissionEngine(PermissionPolicy())
    for name, permission in TOOL_PERMISSIONS.items():
        tool = REGISTRY.get(name)
        assert tool is not None
        for risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            decision = engine.decide(
                tool.spec(),
                RiskAssessment(risk=risk, action="x", resource="x"),
                grants=TaskGrants(),
            )
            assert decision.action is Action.ASK, (name, permission, risk)
        critical = engine.decide(
            tool.spec(), RiskAssessment(risk=RiskLevel.CRITICAL, action="x", resource="x"),
            grants=TaskGrants(),
        )  # fmt: skip
        assert critical.allowed_scopes == [ApprovalScope.ONCE]


def test_no_policy_edit_can_auto_allow_critical_actions() -> None:
    wide_open = PermissionPolicy(
        risk_actions=dict.fromkeys(RiskLevel, Action.ALLOW),
        tool_overrides=dict.fromkeys(TOOL_PERMISSIONS, Action.ALLOW),
    )
    engine = PermissionEngine(wide_open)
    from app.models.tool import RiskAssessment

    for name in TOOL_PERMISSIONS:
        tool = REGISTRY.get(name)
        assert tool is not None
        d = engine.decide(
            tool.spec(), RiskAssessment(risk=RiskLevel.CRITICAL, action="x", resource="x"),
            grants=TaskGrants(),
        )  # fmt: skip
        assert d.action is Action.ASK, name


async def run(env: Env, tool: str, **args: Any) -> Any:
    return await env.router.execute(ToolCall(tool=tool, args=args, rationale="test"), env.ctx())


async def test_the_irreversible_permissions_ask_before_acting_on_real_tools(env: Env) -> None:
    """Delete and execute, the two available without a browser or desktop, with the default policy."""
    assert {Permission.DELETE, Permission.EXECUTE} <= EXTERNAL_OR_IRREVERSIBLE
    victim = env.documents / "keep.txt"
    victim.write_text("x")
    (env.workspace / "run.py").write_text("open('ran.txt', 'w').write('x')\n")
    with Responder(env, deny_all) as human:
        deleted = await run(env, "fs_delete", path=str(victim))
        executed = await run(env, "terminal_execute_safe", command="python run.py")
    assert [r.tool for r in human.seen] == ["fs_delete", "terminal_execute_safe"]
    assert deleted.error and deleted.error.code is ErrorCode.APPROVAL_DENIED and victim.exists()
    assert executed.error and executed.error.code is ErrorCode.APPROVAL_DENIED
    assert not (env.workspace / "ran.txt").exists()


async def test_a_disabled_capability_cannot_be_used_whatever_the_permission(env: Env) -> None:
    from app.models.enums import Capability

    env.engine.update_policy(PermissionPolicy(disabled_capabilities=[Capability.TERMINAL]))
    for tool, args in (
        ("terminal_query", {"command": "python --version"}),
        ("terminal_execute_safe", {"command": "python --version"}),
        ("terminal_environment", {}),
    ):
        result = await run(env, tool, **args)
        assert result.error and result.error.code is ErrorCode.POLICY_DENIED, tool
