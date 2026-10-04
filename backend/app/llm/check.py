"""Live provider conformance probes: ``python -m app llm-check``.

Verifies, against the *real* configured model, the behaviours the agent depends on — in provider-neutral terms:
plain completion, forced tool call (valid normalized ToolUse), tool-result round trip, structured output, and
(optionally) image input. Nothing here touches the computer.
"""

from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.llm.base import (
    ImageBlock,
    LLMProvider,
    LLMRequest,
    LLMToolDef,
    Message,
    TextBlock,
    ToolResultBlock,
)
from app.llm.capabilities import require_for_agent
from app.utils.errors import AgentError


class Answer(BaseModel):
    answer: int
    unit: str = ""


@dataclass
class Probe:
    name: str
    ok: bool
    detail: str
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class CheckReport:
    provider: str
    model: str
    capabilities: dict[str, Any]
    warnings: list[str]
    probes: list[Probe] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(p.ok for p in self.probes)


ECHO = LLMToolDef(
    name="echo",
    description="Echo a value back. Always call this tool.",
    input_schema={
        "type": "object",
        "properties": {"value": {"type": "string"}, "times": {"type": "integer"}},
        "required": ["value"],
    },
)


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (220, 20, 20)).save(buf, format="PNG")
    return buf.getvalue()


async def _probe(name: str, fn: Any) -> Probe:
    t0 = time.perf_counter()
    try:
        detail, usage = await fn()
        return Probe(name, True, detail, (time.perf_counter() - t0) * 1000, usage[0], usage[1])
    except Exception as exc:  # report every failure mode, never crash the check
        msg = exc.message if isinstance(exc, AgentError) else f"{type(exc).__name__}: {exc}"
        return Probe(name, False, msg[:300], (time.perf_counter() - t0) * 1000)


async def run_check(provider: LLMProvider, *, vision: bool = False) -> CheckReport:
    caps = provider.capabilities
    try:
        warnings = require_for_agent(caps, model=provider.model)
    except AgentError as exc:
        return CheckReport(
            provider.name,
            provider.model,
            caps.model_dump(),
            [exc.message],
            [Probe("capability preflight", False, exc.message)],
        )
    rep = CheckReport(provider.name, provider.model, caps.model_dump(), warnings)
    system = "You are a connectivity test. Follow instructions exactly."

    async def plain() -> tuple[str, tuple[int, int]]:
        r = await provider.complete(
            LLMRequest(
                system=system,
                messages=[Message.user("Reply with exactly the word: OK")],
                max_tokens=64,
            )
        )
        assert r.text.strip(), "empty response"
        return f"reply={r.text.strip()[:40]!r} model={r.model}", (
            r.usage.input_tokens,
            r.usage.output_tokens,
        )

    async def forced_tool() -> tuple[str, tuple[int, int]]:
        r = await provider.complete(
            LLMRequest(
                system=system,
                messages=[Message.user("Call echo with value 'ping'.")],
                tools=[ECHO],
                tool_choice="any",
                max_tokens=200,
            )
        )
        assert r.tool_uses, "model did not return a tool call"
        tu = r.tool_uses[0]
        assert (
            tu.name == "echo"
            and isinstance(tu.input, dict)
            and "ping" in str(tu.input.get("value", "")).lower()
        ), f"unexpected tool call {tu.name} {tu.input}"
        assert tu.id, "tool call has no id"
        forced_tool.state = (r, tu)  # type: ignore[attr-defined]
        return f"{tu.name}({tu.input}) id={tu.id}", (r.usage.input_tokens, r.usage.output_tokens)

    async def round_trip() -> tuple[str, tuple[int, int]]:
        first, tu = forced_tool.state  # type: ignore[attr-defined]
        msgs = [
            Message.user("Call echo with value 'ping', then tell me what it returned."),
            Message(
                role="assistant",
                content=[*([TextBlock(text=first.text)] if first.text else []), tu],
            ),
            Message(role="user", content=[ToolResultBlock(tool_use_id=tu.id, content="PONG-42")]),
        ]
        r = await provider.complete(
            LLMRequest(system=system, messages=msgs, tools=[ECHO], max_tokens=200)
        )
        assert "pong" in r.text.lower() or r.tool_uses, f"no sensible continuation: {r.text[:80]!r}"
        return f"continued: {r.text.strip()[:60]!r}", (r.usage.input_tokens, r.usage.output_tokens)

    async def structured() -> tuple[str, tuple[int, int]]:
        r = await provider.complete(
            LLMRequest(system=system, messages=[Message.user("What is 6 times 7? Give the number and unit 'none'.")], response_schema=Answer.model_json_schema(),
                       response_schema_name="answer", max_tokens=200)
        )  # fmt: skip
        out = Answer.model_validate(r.structured)
        assert out.answer == 42, f"wrong answer {out.answer}"
        mode = "native schema" if caps.supports_json_schema else "compat mode"
        return f"{out.model_dump()} ({mode})", (r.usage.input_tokens, r.usage.output_tokens)

    async def image() -> tuple[str, tuple[int, int]]:
        r = await provider.complete(
            LLMRequest(system=system, messages=[Message(role="user", content=[ImageBlock(data_b64=base64.b64encode(_png()).decode()),
                       TextBlock(text="What is the dominant colour of this image? One word.")])], max_tokens=60)
        )  # fmt: skip
        assert "red" in r.text.lower(), f"image not understood: {r.text[:60]!r}"
        return f"reply={r.text.strip()[:30]!r}", (r.usage.input_tokens, r.usage.output_tokens)

    rep.probes.append(await _probe("plain completion", plain))
    ft = await _probe("forced tool call -> normalized ToolUse", forced_tool)
    rep.probes.append(ft)
    if ft.ok:
        rep.probes.append(await _probe("tool-result round trip", round_trip))
    rep.probes.append(await _probe("structured output (validated)", structured))
    if vision:
        rep.probes.append(await _probe("image input", image))
    return rep
