"""Controlled terminal tools. The LLM never gets a shell, only classified commands."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import Capability, RiskLevel
from app.models.tool import RiskAssessment
from app.security.commands import CommandAnalysis, analyze_command, build_environment
from app.tools.base import Tool, ToolOutput
from app.tools.context import ExecutionContext
from app.tools.schema import OutputModel
from app.tools.terminal.runner import run_process
from app.utils.errors import AgentError, CommandViolation, ErrorCode

_BATCH_UNSAFE = set('&|<>^%"!()')


def _cwd(ctx: ExecutionContext, raw: str | None) -> Path:
    if raw:
        return ctx.paths.resolve(raw, cwd=ctx.cwd, must_exist=True, write=False).path
    return ctx.paths.resolve(str(ctx.cwd), cwd=ctx.cwd).path


def _analyze(ctx: ExecutionContext, command: str, cwd: Path) -> CommandAnalysis:
    cfg = ctx.settings.terminal
    analysis = analyze_command(
        command,
        path_policy=ctx.paths,
        cwd=cwd,
        allow_shell_interpreters=cfg.allow_shell_interpreters,
        extra_allowed_programs=cfg.extra_allowed_programs,
    )
    exe = analysis.resolved_executable or ""
    if not analysis.hard_deny and exe.lower().endswith((".cmd", ".bat")):
        for arg in analysis.argv[1:]:
            if any(c in _BATCH_UNSAFE for c in arg):
                analysis.hard_deny = "Arguments containing shell-special characters cannot be passed to batch-file launchers"
                break
    return analysis


class TerminalOutput(OutputModel):
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool
    cwd: str


class TerminalArtifacts(OutputModel):
    command: str
    exit_code: int
    cwd: str
    stdout: str


class TerminalEnvOutput(OutputModel):
    os: str
    os_version: str
    python: str
    workspace: str
    cwd: str
    allowed_roots: list[str]
    path_aliases: dict[str, str]
    tools_installed: dict[str, bool]
    pid: int


class TerminalExecInput(BaseModel):
    command: str = Field(
        description="A single program invocation, e.g. 'python main.py' or 'git status'. "
        "No pipes, redirects, chaining (&&, ;) or variable expansion."
    )
    cwd: str | None = Field(
        default=None, description="Working directory inside an allowed root (default: workspace)"
    )
    timeout_s: float | None = Field(default=None, gt=0, le=600)


def _assessment(analysis: CommandAnalysis, cwd: Path) -> RiskAssessment:
    if analysis.hard_deny:
        raise CommandViolation(analysis.hard_deny)
    return RiskAssessment(
        risk=analysis.risk,
        action=f"Run command: {analysis.command}  (in {cwd})",
        resource=str(cwd),
        reasons=analysis.reasons,
        category=analysis.category,
        scoped_grant_allowed=analysis.scoped_grant_allowed,
    )


async def _execute(
    analysis: CommandAnalysis, cwd: Path, ctx: ExecutionContext, timeout_s: float | None
) -> ToolOutput:
    cfg = ctx.settings.terminal
    timeout = min(timeout_s or cfg.default_timeout_s, cfg.max_timeout_s)
    argv = [analysis.resolved_executable or analysis.argv[0], *analysis.argv[1:]]
    result = await run_process(
        argv,
        cwd=cwd,
        env=build_environment(),
        timeout_s=timeout,
        max_output_bytes=cfg.max_output_bytes,
        max_memory_mb=cfg.max_memory_mb,
        max_processes=cfg.max_processes,
    )
    data: dict[str, Any] = {
        "command": analysis.command,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": round(result.duration_ms),
        "timed_out": result.timed_out,
        "truncated": result.truncated,
        "cwd": str(cwd),
    }
    if result.timed_out:
        raise AgentError(
            f"Command timed out after {timeout:g}s and was terminated: {analysis.command}",
            code=ErrorCode.TIMEOUT, details={"stdout": result.stdout[-1500:], "stderr": result.stderr[-1500:]},
        )  # fmt: skip
    if result.exit_code != 0:
        raise AgentError(
            f"Command exited with code {result.exit_code}: {analysis.command}",
            code=ErrorCode.COMMAND_FAILED,
            details={
                "exit_code": result.exit_code,
                "stdout": result.stdout[-2500:],
                "stderr": result.stderr[-2500:],
                "cwd": str(cwd),
            },
        )
    return ToolOutput(
        data=data,
        summary=f"`{analysis.command}` exited 0 in {round(result.duration_ms)} ms",
        artifacts={
            "command": analysis.command,
            "exit_code": 0,
            "cwd": str(cwd),
            "stdout": result.stdout[-4000:],
        },
    )


class TerminalExecuteSafe(Tool[TerminalExecInput]):
    name = "terminal_execute_safe"
    Output = TerminalOutput
    Artifacts = TerminalArtifacts
    description = (
        "Run ONE allowlisted program (python, pip, node, npm, git, code, pytest, ...) with arguments in a "
        "folder inside an allowed root. No shell: pipes, redirects, &&, ; and %VAR% are rejected. "
        "Risky commands (running code, installing packages) require user approval. Non-zero exit is reported as an error."
    )
    capability = Capability.TERMINAL
    Input = TerminalExecInput
    risk = RiskLevel.MEDIUM
    timeout_s = 660.0
    returns_external_content = True

    def assess(self, args: TerminalExecInput, ctx: ExecutionContext) -> RiskAssessment:
        cwd = _cwd(ctx, args.cwd)
        return _assessment(_analyze(ctx, args.command, cwd), cwd)

    async def run(self, args: TerminalExecInput, ctx: ExecutionContext) -> ToolOutput:
        cwd = _cwd(ctx, args.cwd)
        analysis = _analyze(ctx, args.command, cwd)  # re-validate at execution time
        if analysis.hard_deny:
            raise CommandViolation(analysis.hard_deny)
        return await _execute(analysis, cwd, ctx, args.timeout_s)


class TerminalQueryInput(BaseModel):
    command: str = Field(
        description="A read-only query, e.g. 'git status', 'python --version', 'pip list'"
    )
    cwd: str | None = None


class TerminalQuery(Tool[TerminalQueryInput]):
    name = "terminal_query"
    Output = TerminalOutput
    Artifacts = TerminalArtifacts
    description = "Run a strictly read-only query command (versions, git status/log/diff, pip list, where). Never modifies anything."
    capability = Capability.TERMINAL
    Input = TerminalQueryInput
    risk = RiskLevel.LOW
    read_only = True
    cacheable = True
    cache_ttl_s = 3.0
    timeout_s = 60.0
    returns_external_content = True

    def _analysis(
        self, args: TerminalQueryInput, ctx: ExecutionContext
    ) -> tuple[CommandAnalysis, Path]:
        cwd = _cwd(ctx, args.cwd)
        analysis = _analyze(ctx, args.command, cwd)
        if analysis.hard_deny:
            raise CommandViolation(analysis.hard_deny)
        if not analysis.read_only:
            raise CommandViolation(
                f"'{args.command}' is not a read-only query; use terminal_execute_safe for commands that act"
            )
        return analysis, cwd

    def assess(self, args: TerminalQueryInput, ctx: ExecutionContext) -> RiskAssessment:
        analysis, cwd = self._analysis(args, ctx)
        return _assessment(analysis, cwd)

    async def run(self, args: TerminalQueryInput, ctx: ExecutionContext) -> ToolOutput:
        analysis, cwd = self._analysis(args, ctx)
        return await _execute(analysis, cwd, ctx, 30)


class TerminalEnvInput(BaseModel):
    pass


class TerminalEnvironment(Tool[TerminalEnvInput]):
    name = "terminal_environment"
    Output = TerminalEnvOutput
    description = "Describe the execution environment: OS, workspace folder, allowed folders, and which developer tools are installed. Contains no secrets."
    capability = Capability.TERMINAL
    Input = TerminalEnvInput
    read_only = True
    cacheable = True
    cache_ttl_s = 60.0
    idempotent = True

    async def run(self, args: TerminalEnvInput, ctx: ExecutionContext) -> ToolOutput:
        tools = {
            name: bool(shutil.which(name))
            for name in ("python", "pip", "node", "npm", "git", "code", "pytest")
        }
        data = {
            "os": platform.system(),
            "os_version": platform.version(),
            "python": sys.version.split()[0],
            "workspace": str(ctx.paths.workspace),
            "cwd": str(ctx.cwd),
            "allowed_roots": [str(r) for r in ctx.paths.writable_roots],
            "path_aliases": {
                k: str(v)
                for k, v in ctx.paths.aliases.items()
                if k in ("desktop", "downloads", "documents", "workspace")
            },
            "tools_installed": tools,
            "pid": os.getpid(),
        }
        return ToolOutput(
            data=data, summary=f"{data['os']} environment, workspace {data['workspace']}"
        )


TERMINAL_TOOLS: list[type[Tool[Any]]] = [TerminalExecuteSafe, TerminalQuery, TerminalEnvironment]
