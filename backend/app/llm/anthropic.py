"""Anthropic Messages API provider (httpx, no SDK dependency)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.llm.base import (
    ImageBlock,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.llm.http import post_json
from app.llm.structured import extract_json

_API_VERSION = "2023-06-01"
_STRUCTURED_TOOL = "emit_structured_output"


def _block_to_wire(block: Any) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ImageBlock):
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": block.media_type, "data": block.data_b64},
        }
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    raise TypeError(f"unsupported block {type(block)}")


def _message_to_wire(m: Message) -> dict[str, Any]:
    return {"role": m.role, "content": [_block_to_wire(b) for b in m.content]}


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout_s: float = 90.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        tools: list[dict[str, Any]] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in request.tools
        ]
        tool_choice: dict[str, Any] | None = None
        if request.tool_choice == "any":
            tool_choice = {"type": "any"}
        elif request.tool_choice and request.tool_choice != "auto":
            tool_choice = {"type": "tool", "name": request.tool_choice}

        structured = request.response_schema is not None
        if structured:
            # Structured output is delivered through a forced tool call whose schema we control.
            tools = [
                {
                    "name": _STRUCTURED_TOOL,
                    "description": "Return the final answer as structured data.",
                    "input_schema": request.response_schema,
                }
            ]
            tool_choice = {"type": "tool", "name": _STRUCTURED_TOOL}

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            # Prompt caching: the (large, stable) system prompt is marked cacheable.
            "system": [
                {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [_message_to_wire(m) for m in request.messages],
        }
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

        started = time.perf_counter()
        data = await post_json(
            self._client,
            self._url,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": _API_VERSION,
                "content-type": "application/json",
            },
            payload=payload,
        )
        latency_ms = (time.perf_counter() - started) * 1000

        text_parts: list[str] = []
        tool_uses: list[ToolUseBlock] = []
        structured_out: dict[str, Any] | None = None
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                if structured and block.get("name") == _STRUCTURED_TOOL:
                    structured_out = block.get("input") or {}
                else:
                    tool_uses.append(
                        ToolUseBlock(
                            id=block["id"], name=block["name"], input=block.get("input") or {}
                        )
                    )
        text = "".join(text_parts)
        if structured and structured_out is None:
            structured_out = extract_json(text)
        usage = data.get("usage", {})
        return LLMResponse(
            text=text,
            tool_uses=tool_uses,
            structured=structured_out,
            usage=LLMUsage(
                input_tokens=int(usage.get("input_tokens", 0))
                + int(usage.get("cache_creation_input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0)),
                cached_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            ),
            model=data.get("model", self.model),
            stop_reason=data.get("stop_reason", ""),
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
