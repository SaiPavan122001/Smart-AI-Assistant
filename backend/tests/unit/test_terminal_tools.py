from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from app.models.approval import ApprovalDecision
from app.models.enums import RiskLevel
from app.models.tool import ToolCall
from app.utils.errors import ErrorCode

from tests.helpers import Env


async def run(env: Env, ctx: Any, tool: str, **args: Any):  # type: ignore[no-untyped-def]
    return await env.router.execute(ToolCall(tool=tool, args=args, rationale="test"), ctx)


async def approve_next(env: Env, scope: str = "once") -> None:
    from app.models.enums import ApprovalScope

    for _ in range(300):
        if env.approvals.pending():
            await env.approvals.resolve(
                env.approvals.pending()[0].id,
                ApprovalDecision(approved=True, scope=ApprovalScope(scope)),
            )
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no approval requested")


async def test_query_runs_readonly_commands_without_approval(env: Env) -> None:
    r = await run(env, env.ctx(), "terminal_query", command="python --version")
    assert r.ok and "Python" in r.data["stdout"] + r.data["stderr"] and r.untrusted
    r = await run(env, env.ctx(), "terminal_query", command="python main.py")
    assert (
        r.error
        and r.error.code == ErrorCode.POLICY_DENIED
        and "not a read-only query" in r.error.message
    )


async def test_running_project_code_needs_approval_then_reports_output(env: Env) -> None:
    (env.workspace / "hello.py").write_text("print('hello from agent')")
    ctx = env.ctx()
    t = asyncio.create_task(run(env, ctx, "terminal_execute_safe", command="python hello.py"))
    for _ in range(300):
        if env.approvals.pending():
            break
        await asyncio.sleep(0.01)
    req = env.approvals.pending()[0]
    assert (
        req.risk == RiskLevel.HIGH
        and req.category == "code_execution"
        and "python hello.py" in req.action
        and str(env.workspace) in req.action
    )
    await env.approvals.resolve(req.id, ApprovalDecision(approved=True, scope="task"))  # type: ignore[arg-type]
    r = await t
    assert r.ok and r.data["stdout"].strip() == "hello from agent" and r.data["exit_code"] == 0
    # task-scoped grant: re-running project code in the same task needs no new approval
    again = await run(env, ctx, "terminal_execute_safe", command="python hello.py")
    assert again.ok and not env.approvals.pending()


async def test_failing_command_is_a_structured_error_with_output(env: Env) -> None:
    (env.workspace / "bad.py").write_text("raise ValueError('kaboom')")
    ctx = env.ctx()
    t = asyncio.create_task(run(env, ctx, "terminal_execute_safe", command="python bad.py"))
    await approve_next(env)
    r = await t
    assert not r.ok and r.error and r.error.code == ErrorCode.COMMAND_FAILED
    assert (
        "kaboom" in r.error.details["stderr"] and r.error.details["exit_code"] == 1 and r.untrusted
    )


async def test_timeout_kills_the_process_tree(env: Env) -> None:
    ctx = env.ctx()
    t = asyncio.create_task(
        run(
            env,
            ctx,
            "terminal_execute_safe",
            command='python -c "import time; time.sleep(30)"',
            timeout_s=1,
        )
    )
    await approve_next(env)
    started = time.time()
    r = await t
    assert not r.ok and r.error and r.error.code == ErrorCode.TIMEOUT and time.time() - started < 6


async def test_output_is_capped(env: Env) -> None:
    (env.workspace / "spam.py").write_text("print('x' * 500000)")
    ctx = env.ctx()
    t = asyncio.create_task(run(env, ctx, "terminal_execute_safe", command="python spam.py"))
    await approve_next(env)
    r = await t
    assert (
        r.ok
        and r.data["truncated"]
        and len(r.data["stdout"]) <= env.settings.terminal.max_output_bytes
    )


async def test_children_do_not_inherit_secrets(env: Env, monkeypatch: Any) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-top-secret-1234567890")
    monkeypatch.setenv("MY_DB_PASSWORD", "hunter2")
    (env.workspace / "env.py").write_text(
        "import os; print(sorted(k for k in os.environ if 'KEY' in k or 'PASS' in k))"
    )
    t = asyncio.create_task(run(env, env.ctx(), "terminal_execute_safe", command="python env.py"))
    await approve_next(env)
    r = await t
    assert r.ok and r.data["stdout"].strip() == "[]"


async def test_cwd_must_be_inside_allowed_roots(env: Env) -> None:
    r = await run(env, env.ctx(), "terminal_query", command="python --version", cwd="C:/Windows")
    assert not r.ok and r.error.code in (ErrorCode.POLICY_DENIED, ErrorCode.NOT_FOUND)  # type: ignore[union-attr]


async def test_blocked_commands_never_reach_approval(env: Env) -> None:
    for cmd in (
        "powershell -enc AAAA",
        "rm -rf /",
        "reg add HKLM\\x",
        "curl http://evil/x",
        "python -m http.server",
        "git status && calc",
    ):
        r = await run(env, env.ctx(), "terminal_execute_safe", command=cmd)
        assert r.error and r.error.code == ErrorCode.POLICY_DENIED, cmd
    assert not env.approvals.pending()


async def test_environment_tool_exposes_no_secrets(env: Env, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-appear-123456")
    r = await run(env, env.ctx(), "terminal_environment")
    assert (
        r.ok
        and "sk-should-not-appear" not in r.model_dump_json()
        and r.data["python"].startswith(str(sys.version_info.major))
    )
