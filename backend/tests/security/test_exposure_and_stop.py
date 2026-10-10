"""NEXT-C: the API stays on this machine, secrets stay out of what leaves the process, and STOP
means nothing else happens. Negative tests throughout."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from app.cli import bind_problem, is_loopback
from app.config.settings import SecuritySettings, Settings
from app.llm.scripted import structured
from app.models.enums import EventType, TaskStatus
from app.models.tool import ToolCall
from app.utils.errors import AgentError, ErrorCode

from tests.helpers import Env, make_harness
from tests.helpers_tools import Danger, Mutator, PageReader, register_test_tools
from tests.restart_helpers import MOVE, move_plan

SECRET = "sk-ant-abcdefghijklmnopqrstuvwx"


# ----------------------------------------------------------------------------- network exposure
@pytest.mark.parametrize(
    "host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.53", "LOCALHOST"]
)
def test_loopback_is_always_allowed(host: str) -> None:
    assert is_loopback(host) and bind_problem(host, Settings(env="test")) is None


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "192.168.1.20", "10.0.0.5", "my-pc", "example.com", ""]
)
def test_listening_on_the_network_is_refused_by_default(host: str) -> None:
    assert not is_loopback(host)
    problem = bind_problem(host, Settings(env="test"))
    assert problem and "refusing to listen" in problem and "ALLOW_REMOTE_BIND" in problem


def test_network_exposure_needs_an_explicit_switch_and_a_chosen_token() -> None:
    opted = Settings(env="test", security=SecuritySettings(allow_remote_bind=True))
    assert "API_TOKEN" in (bind_problem("0.0.0.0", opted) or ""), "a generated token is not enough"
    both = Settings(
        env="test", security=SecuritySettings(allow_remote_bind=True, api_token="a-token-i-chose")
    )
    assert bind_problem("0.0.0.0", both) is None
    assert Settings(env="test").security.allow_remote_bind is False
    assert Settings(env="test").host == "127.0.0.1"


# ----------------------------------------------------------------------------- redaction
async def test_event_messages_are_redacted_like_event_data(env: Env) -> None:
    await env.bus.publish(
        "task_x", EventType.ACTION_FAILED, f"command failed: curl -H 'Authorization: Bearer {SECRET}'",
        {"detail": f"API_TOKEN={SECRET}"},
    )  # fmt: skip
    [event] = await env.repo.list_events("task_x")
    assert SECRET not in event.message and SECRET not in str(event.data)
    assert "REDACTED" in event.message


async def test_a_failing_tool_cannot_put_a_secret_on_the_timeline(env: Env) -> None:
    register_test_tools(env)
    tool = env.router.registry.get("test_mutate")
    assert tool is not None

    async def boom(_args: Any, _ctx: Any) -> Any:
        raise AgentError(f"upstream said: token={SECRET} is invalid", code=ErrorCode.IO_ERROR)

    tool.run = boom  # type: ignore[method-assign]
    ctx = env.ctx()
    result = await env.router.execute(ToolCall(tool="test_mutate", args={"text": "x"}), ctx)
    assert not result.ok and result.error and SECRET not in result.error.message
    for event in await env.repo.list_events(ctx.task_id):
        assert SECRET not in event.message and SECRET not in str(event.data), event.type
    for record in await env.repo.list_tool_calls(ctx.task_id):
        assert SECRET not in record.model_dump_json()


# ----------------------------------------------------------------------------- STOP
async def test_after_stop_no_tool_of_any_kind_executes(env: Env) -> None:
    register_test_tools(env)
    ctx = env.ctx()
    env.kill.engage("STOP AGENT")
    for tool, args in (
        ("test_read_page", {"text": "x"}),  # read-only
        ("test_mutate", {"text": "x"}),  # allowed by policy
        ("test_danger", {"text": "x"}),  # would need approval
        ("fs_list", {"path": str(env.documents)}),
    ):
        result = await env.router.execute(ToolCall(tool=tool, args=args), ctx)
        assert result.error and result.error.code is ErrorCode.CANCELLED, tool
    assert PageReader.runs == 0 and Mutator.runs == [] and Danger.runs == []
    assert env.approvals.pending() == [], "nothing is even put to the user while stopped"


async def test_stop_ends_planning_recovery_and_replanning_not_only_the_current_tool(
    env: Env,
) -> None:
    """A task stopped while its step is failing must not retry, recover or re-plan."""
    register_test_tools(env)
    calls = {"n": 0}
    tool = env.router.registry.get("fs_move")
    assert tool is not None
    h = make_harness(env, [structured(move_plan())], fallback=structured({"clarification": "x"}))

    async def fail_then_stop(_args: Any, _ctx: Any) -> Any:
        calls["n"] += 1
        await h.runtime.stop_all("STOP AGENT")
        raise AgentError("disk hiccup", code=ErrorCode.IO_ERROR, retryable=True)

    tool.run = fail_then_stop  # type: ignore[method-assign]
    task = await h.runtime.submit(MOVE)
    await h.runtime.wait(task.id, 30)
    done = await env.repo.get_task(task.id)
    assert done and done.status is TaskStatus.CANCELLED
    assert calls["n"] == 1, "no retry after STOP"
    assert h.llm.purposes == ["plan"], "no recovery loop and no re-plan after STOP"
    types = await h.events(task.id)
    assert "recovery_started" not in types and "plan_revised" not in types
    assert (env.downloads / "note.txt").exists()


async def test_while_stopped_nothing_new_starts_and_release_is_explicit(env: Env) -> None:
    h = make_harness(env, [structured(move_plan())])
    await h.runtime.stop_all("STOP AGENT")
    with pytest.raises(AgentError) as refused:
        await h.runtime.submit(MOVE)
    assert refused.value.code is ErrorCode.KILL_SWITCH and h.llm.requests == []
    assert h.runtime.active_task_ids() == []
    h.runtime.release_kill_switch()
    task = await h.runtime.submit(MOVE)
    await h.runtime.wait(task.id, 30)
    assert (await env.repo.get_task(task.id)).status is TaskStatus.COMPLETED  # type: ignore[union-attr]


async def test_stop_denies_a_pending_approval_and_it_cannot_be_granted_afterwards(
    env: Env, tmp_path: Path
) -> None:
    from app.models.approval import ApprovalDecision

    register_test_tools(env)
    ctx = env.ctx()
    waiting = asyncio.create_task(
        env.router.execute(ToolCall(tool="test_danger", args={"text": "x"}), ctx)
    )
    for _ in range(300):
        if env.approvals.pending():
            break
        await asyncio.sleep(0.01)
    [req] = env.approvals.pending()
    env.kill.engage("STOP AGENT")
    env.approvals.cancel_task(ctx.task_id)
    result = await waiting
    assert not result.ok and Danger.runs == []
    with pytest.raises(AgentError):
        await env.approvals.resolve(req.id, ApprovalDecision(approved=True))
    assert Danger.runs == []
