"""Controllable tools for exercising the router/permission pipeline."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from app.models.enums import Capability, RiskLevel
from app.models.tool import RetryPolicy, RiskAssessment
from app.tools.base import Tool, ToolOutput
from app.tools.context import ExecutionContext
from app.utils.errors import AgentError, ErrorCode
from pydantic import BaseModel


class TextArgs(BaseModel):
    text: str = ""


class PageReader(Tool[TextArgs]):
    """Read-only tool that returns attacker-controllable content (like a web page)."""

    name = "test_read_page"
    description = "returns the given text as if it were page content"
    capability = Capability.BROWSER
    Input = TextArgs
    read_only = True
    returns_external_content = True
    runs: ClassVar[int] = 0

    async def run(self, args: TextArgs, ctx: ExecutionContext) -> ToolOutput:
        PageReader.runs += 1
        return ToolOutput(data={"text": args.text}, summary=f"page: {args.text[:40]}")


class Mutator(Tool[TextArgs]):
    """MEDIUM-risk mutating tool (allowed by default policy)."""

    name = "test_mutate"
    description = "mutates something"
    capability = Capability.FILESYSTEM
    Input = TextArgs
    risk = RiskLevel.MEDIUM
    runs: ClassVar[list[str]] = []

    async def run(self, args: TextArgs, ctx: ExecutionContext) -> ToolOutput:
        Mutator.runs.append(args.text)
        return ToolOutput(data={"done": args.text}, summary="mutated")


class Danger(Tool[TextArgs]):
    """HIGH-risk tool: must never run without approval."""

    name = "test_danger"
    description = "dangerous"
    capability = Capability.FILESYSTEM
    Input = TextArgs
    risk = RiskLevel.HIGH
    runs: ClassVar[list[str]] = []

    def assess(self, args: TextArgs, ctx: ExecutionContext) -> RiskAssessment:
        return RiskAssessment(
            risk=RiskLevel.HIGH, action=f"Do the dangerous thing to {args.text}", resource=args.text,
            reasons=["irreversible"], category="danger",
        )  # fmt: skip

    async def run(self, args: TextArgs, ctx: ExecutionContext) -> ToolOutput:
        Danger.runs.append(args.text)
        return ToolOutput(data={"done": args.text}, summary="dangerous thing done")


class Critical(Tool[TextArgs]):
    name = "test_critical"
    description = "critical"
    capability = Capability.FILESYSTEM
    Input = TextArgs
    runs: ClassVar[list[str]] = []

    def assess(self, args: TextArgs, ctx: ExecutionContext) -> RiskAssessment:
        return RiskAssessment(
            risk=RiskLevel.CRITICAL, action="critical", resource=args.text, category="crit"
        )

    async def run(self, args: TextArgs, ctx: ExecutionContext) -> ToolOutput:
        Critical.runs.append(args.text)
        return ToolOutput(summary="critical done")


class SecretLeaker(Tool[TextArgs]):
    name = "test_leak"
    description = "returns secrets"
    capability = Capability.FILESYSTEM
    Input = TextArgs
    read_only = True

    async def run(self, args: TextArgs, ctx: ExecutionContext) -> ToolOutput:
        return ToolOutput(
            data={"password": "hunter2", "note": "key is sk-ant-abcdefghijklmnopqrstu", "env": "API_TOKEN=abcd1234efgh"},
            summary="Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        )  # fmt: skip


class Sleeper(Tool[TextArgs]):
    name = "test_sleep"
    description = "sleeps"
    capability = Capability.FILESYSTEM
    Input = TextArgs
    timeout_s = 0.3
    idempotent = True
    retry = RetryPolicy(max_attempts=2, backoff_s=0.01)

    async def run(self, args: TextArgs, ctx: ExecutionContext) -> ToolOutput:
        await asyncio.sleep(5)
        return ToolOutput()


class Flaky(Tool[TextArgs]):
    name = "test_flaky"
    description = "fails then works"
    capability = Capability.FILESYSTEM
    Input = TextArgs
    idempotent = True
    retry = RetryPolicy(max_attempts=3, backoff_s=0.01)
    calls: ClassVar[int] = 0

    async def run(self, args: TextArgs, ctx: ExecutionContext) -> ToolOutput:
        Flaky.calls += 1
        if Flaky.calls < 3:
            raise AgentError("transient", code=ErrorCode.NAVIGATION_FAILED, retryable=True)
        return ToolOutput(summary="finally")


class Masked(Tool[TextArgs]):
    name = "test_masked"
    description = "has a masked argument"
    capability = Capability.FILESYSTEM
    Input = TextArgs
    seen: ClassVar[list[str]] = []

    def assess(self, args: TextArgs, ctx: ExecutionContext) -> RiskAssessment:
        return RiskAssessment(
            risk=RiskLevel.HIGH,
            action="enter a credential",
            resource="login",
            category="credential_entry",
            mask_args=["text"],
        )

    async def run(self, args: TextArgs, ctx: ExecutionContext) -> ToolOutput:
        Masked.seen.append(args.text)  # the tool still receives the real value
        return ToolOutput(summary="typed")


class CachedRead(Tool[TextArgs]):
    name = "test_cached"
    description = "cacheable read"
    capability = Capability.FILESYSTEM
    Input = TextArgs
    read_only = True
    cacheable = True
    cache_ttl_s = 60
    runs: ClassVar[int] = 0

    async def run(self, args: TextArgs, ctx: ExecutionContext) -> ToolOutput:
        CachedRead.runs += 1
        return ToolOutput(data={"n": CachedRead.runs}, summary="read")


TEST_TOOLS: list[type[Tool[Any]]] = [
    PageReader,
    Mutator,
    Danger,
    Critical,
    SecretLeaker,
    Sleeper,
    Flaky,
    Masked,
    CachedRead,
]


def reset_counters() -> None:
    PageReader.runs = 0
    Mutator.runs.clear()
    Danger.runs.clear()
    Critical.runs.clear()
    Flaky.calls = 0
    Masked.seen.clear()
    CachedRead.runs = 0


def register_test_tools(env: Any) -> None:
    reset_counters()
    for cls in TEST_TOOLS:
        env.router.registry.register(cls())
