"""Adaptive step loop: a bounded LLM tool-calling loop for steps whose next action is unknown.

Used sparingly: only for steps the planner marked ``adaptive`` and for recovery fallbacks.
Every tool call still goes through the router (permissions, limits, injection defence).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.llm.base import LLMRequest, LLMToolDef, Message, TextBlock, ToolResultBlock, ToolUseBlock
from app.llm.gateway import LLMGateway
from app.models.tool import ToolCall, ToolResult
from app.observability.logging import get_logger
from app.perception.cache import call_key
from app.planner.prompts import ADAPTIVE_SYSTEM, build_adaptive_user
from app.tools.context import ExecutionContext
from app.tools.registry import ToolRegistry
from app.tools.render import render_result
from app.utils.errors import ErrorCode, ErrorInfo

log = get_logger("adaptive")

FINISH_TOOL = LLMToolDef(
    name="finish_step",
    description="End the step. Call once, when the goal is met (success=true, with evidence) or cannot be met.",
    input_schema={
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "summary": {"type": "string", "description": "What was done / observed, honestly"},
            "answer": {
                "type": "string",
                "description": "Requested information, if the goal asked for any",
            },
        },
        "required": ["success", "summary"],
    },
)


@dataclass
class AdaptiveOutcome:
    success: bool
    summary: str
    answer: str | None = None
    iterations: int = 0
    error: ErrorInfo | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    results: list[ToolResult] = field(default_factory=list)


class AdaptiveRunner:
    def __init__(self, gateway: LLMGateway, router: Any, registry: ToolRegistry) -> None:
        self._gateway = gateway
        self._router = router
        self._registry = registry

    async def run(
        self, *, title: str, goal: str, allowed_tools: list[str], context_text: str, rationale: str,
        ctx: ExecutionContext, max_iterations: int,
    ) -> AdaptiveOutcome:  # fmt: skip
        tools = [*self._registry.llm_tools(allowed_tools or None), FINISH_TOOL]
        messages: list[Message] = [Message.user(build_adaptive_user(goal, context_text, title))]
        seen: dict[str, int] = {}
        artifacts: dict[str, Any] = {}
        results: list[ToolResult] = []
        for i in range(1, max_iterations + 1):
            ctx.token.raise_if_cancelled()
            await ctx.pause.wait_if_paused(ctx.token)
            req = LLMRequest(
                system=ADAPTIVE_SYSTEM, messages=_compress(messages), tools=tools, tool_choice="any",
                temperature=ctx.settings.llm.temperature, max_tokens=2048, purpose="step",
            )  # fmt: skip
            resp = await self._gateway.complete(req, meter=ctx.meter, token=ctx.token)
            blocks: list[Any] = []
            if resp.text:
                blocks.append(TextBlock(text=resp.text))
            blocks.extend(resp.tool_uses)
            if not resp.tool_uses:
                return AdaptiveOutcome(
                    success=False, summary=(resp.text or "The model stopped without finishing the step")[:400],
                    iterations=i, artifacts=artifacts, results=results,
                    error=ErrorInfo(code=ErrorCode.LLM_ERROR, message="Model did not call finish_step"),
                )  # fmt: skip
            messages.append(Message(role="assistant", content=blocks))
            replies: list[ToolResultBlock] = []
            for tu in resp.tool_uses:
                if tu.name == "finish_step":
                    return self._finish(tu, i, artifacts, results)
                result = await self._call(tu, ctx, rationale or goal, seen)
                results.append(result)
                artifacts.update(
                    {
                        k: v
                        for k, v in result.artifacts.items()
                        if k in ("path", "dst", "url", "exit_code")
                    }
                )
                replies.append(
                    ToolResultBlock(
                        tool_use_id=tu.id, content=render_result(result), is_error=not result.ok
                    )
                )
            messages.append(Message(role="user", content=replies))
        return AdaptiveOutcome(
            success=False, summary=f"Step goal not reached within {max_iterations} iterations", iterations=max_iterations,
            artifacts=artifacts, results=results,
            error=ErrorInfo(code=ErrorCode.LIMIT_EXCEEDED, message="adaptive iteration limit reached"),
        )  # fmt: skip

    async def _call(
        self, tu: ToolUseBlock, ctx: ExecutionContext, rationale: str, seen: dict[str, int]
    ) -> ToolResult:
        key = call_key(tu.name, tu.input)
        seen[key] = seen.get(key, 0) + 1
        call = ToolCall(tool=tu.name, args=tu.input, rationale=rationale)
        spec = self._registry.get(tu.name)
        repeat_limit = 3 if (spec and spec.read_only) else 2
        if seen[key] > repeat_limit:  # loop breaker: identical call repeated
            return ToolResult(
                tool_call_id=call.id, tool=tu.name, ok=False,
                error=ErrorInfo(code=ErrorCode.INVALID_INPUT, message="You already made this exact call several times. Do something different or call finish_step."),
            )  # fmt: skip
        return await self._router.execute(call, ctx)

    @staticmethod
    def _finish(
        tu: ToolUseBlock, i: int, artifacts: dict[str, Any], results: list[ToolResult]
    ) -> AdaptiveOutcome:
        inp = tu.input
        success = bool(inp.get("success"))
        return AdaptiveOutcome(
            success=success, summary=str(inp.get("summary", ""))[:600], answer=(str(inp["answer"])[:20000] if inp.get("answer") else None),
            iterations=i, artifacts=artifacts, results=results,
            error=None if success else ErrorInfo(code=ErrorCode.VERIFICATION_FAILED, message=str(inp.get("summary", "goal not achieved"))[:300]),
        )  # fmt: skip


def _compress(messages: list[Message], keep_recent: int = 4, old_limit: int = 400) -> list[Message]:
    """Context compression: old tool results keep only a short head (the model already acted on them)."""
    cutoff = len(messages) - keep_recent * 2
    out: list[Message] = []
    for idx, m in enumerate(messages):
        if (
            idx < cutoff
            and m.role == "user"
            and any(isinstance(b, ToolResultBlock) for b in m.content)
        ):
            blocks = [
                b.model_copy(
                    update={
                        "content": b.content[:old_limit]
                        + ("…[older result trimmed]" if len(b.content) > old_limit else "")
                    }
                )
                if isinstance(b, ToolResultBlock)
                else b
                for b in m.content
            ]
            out.append(Message(role=m.role, content=blocks))
        else:
            out.append(m)
    return out


_ = json  # (kept for debugging helpers)
