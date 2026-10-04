"""Deterministic LLM *test adapter*.

This is NOT a production provider and is never selected by configuration. It lets
tests and the evaluation suite drive the real agent runtime (planner, router,
permissions, tools, verification, recovery) with scripted model decisions, so the
whole pipeline is exercised without network access or API keys.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from typing import Any

from app.llm.base import LLMProvider, LLMRequest, LLMResponse, LLMUsage, ToolUseBlock

Handler = LLMResponse | Callable[[LLMRequest], LLMResponse]


class ScriptedProvider(LLMProvider):
    name = "scripted"
    model = "scripted-test-model"

    def __init__(self, script: Sequence[Handler] = (), *, fallback: Handler | None = None) -> None:
        self._script = list(script)
        self._fallback = fallback
        self.requests: list[LLMRequest] = []

    def push(self, handler: Handler) -> None:
        self._script.append(handler)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        handler = self._script.pop(0) if self._script else self._fallback
        if handler is None:
            raise AssertionError(
                f"ScriptedProvider exhausted (purpose={request.purpose!r}); "
                f"{len(self.requests)} calls made"
            )
        resp = handler(request) if callable(handler) else handler
        if not resp.usage.input_tokens:
            resp = resp.model_copy(update={"usage": LLMUsage(input_tokens=100, output_tokens=40)})
        return resp.model_copy(update={"model": self.model, "latency_ms": 1.0})

    @property
    def purposes(self) -> list[str]:
        return [r.purpose for r in self.requests]


_counter = itertools.count(1)


def structured(payload: dict[str, Any]) -> LLMResponse:
    return LLMResponse(structured=payload, stop_reason="end_turn")


def tool_use(name: str, **inputs: Any) -> LLMResponse:
    return LLMResponse(
        tool_uses=[ToolUseBlock(id=f"tu_{next(_counter)}", name=name, input=inputs)],
        stop_reason="tool_use",
    )


def text(msg: str) -> LLMResponse:
    return LLMResponse(text=msg, stop_reason="end_turn")
