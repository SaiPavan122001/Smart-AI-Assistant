"""OpenAI-compatible Chat Completions provider (OpenAI, Azure gateways, Ollama, vLLM, ...)."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.llm.base import (
    ImageBlock,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.llm.http import post_json
from app.llm.structured import extract_json


def _messages_to_wire(request: LLMRequest) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
    for m in request.messages:
        if m.role == "assistant":
            text = "".join(b.text for b in m.content if isinstance(b, TextBlock))
            calls = [
                {
                    "id": b.id,
                    "type": "function",
                    "function": {"name": b.name, "arguments": json.dumps(b.input)},
                }
                for b in m.content
                if isinstance(b, ToolUseBlock)
            ]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        # user turn: tool results become role=tool messages; the rest is a user message
        parts: list[dict[str, Any]] = []
        for b in m.content:
            if isinstance(b, ToolResultBlock):
                out.append({"role": "tool", "tool_call_id": b.tool_use_id, "content": b.content})
            elif isinstance(b, TextBlock):
                parts.append({"type": "text", "text": b.text})
            elif isinstance(b, ImageBlock):
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{b.media_type};base64,{b.data_b64}"},
                    }
                )
        if parts:
            out.append({"role": "user", "content": parts})
    return out


class OpenAIProvider(LLMProvider):
    name = "openai"

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
        self._url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _messages_to_wire(request),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.tools and request.response_schema is None:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in request.tools
            ]
            if request.tool_choice == "any":
                payload["tool_choice"] = "required"
            elif request.tool_choice and request.tool_choice != "auto":
                payload["tool_choice"] = {
                    "type": "function",
                    "function": {"name": request.tool_choice},
                }
        if request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.response_schema_name,
                    "schema": request.response_schema,
                    "strict": False,
                },
            }

        started = time.perf_counter()
        data = await post_json(
            self._client,
            self._url,
            headers={
                "authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
            },
            payload=payload,
        )
        latency_ms = (time.perf_counter() - started) * 1000

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        text = message.get("content") or ""
        tool_uses: list[ToolUseBlock] = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments", "")}
            tool_uses.append(
                ToolUseBlock(id=call.get("id", ""), name=fn.get("name", ""), input=args)
            )
        usage = data.get("usage", {})
        return LLMResponse(
            text=text,
            tool_uses=tool_uses,
            structured=extract_json(text) if request.response_schema is not None else None,
            usage=LLMUsage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
            ),
            model=data.get("model", self.model),
            stop_reason=choice.get("finish_reason", ""),
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
