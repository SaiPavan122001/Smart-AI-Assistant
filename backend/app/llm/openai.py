"""OpenAI Chat Completions adapter. Also the base for OpenRouter and any OpenAI-compatible endpoint."""

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
from app.llm.capabilities import ModelCapabilities, resolve_capabilities
from app.llm.http import post_json
from app.llm.structured import extract_json, schema_instruction
from app.utils.errors import LLMError
from app.utils.redact import redact_text

DEFAULT_BASE_URL = "https://api.openai.com/v1"


def _messages_to_wire(request: LLMRequest, system: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
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
    """Chat Completions wire format. ``name`` identifies the concrete service (openai, openrouter, ...)."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout_s: float = 90.0,
        client: httpx.AsyncClient | None = None,
        extra_headers: dict[str, str] | None = None,
        capabilities: ModelCapabilities | None = None,
        name: str | None = None,
    ) -> None:
        self.model = model
        if name:
            self.name = name
        self.capabilities = capabilities or resolve_capabilities(model)
        self._api_key = api_key
        self._url = (base_url or DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"
        self._extra_headers = extra_headers or {}
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        caps = self.capabilities
        system = request.system
        payload: dict[str, Any] = {"model": self.model}
        if caps.supports_temperature:
            payload["temperature"] = request.temperature
        payload[caps.max_tokens_param] = request.max_tokens

        structured = request.response_schema is not None
        if request.tools and not structured:
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
            if caps.supports_parallel_tools:
                payload["parallel_tool_calls"] = True  # several predictable actions per round trip
            if request.tool_choice == "any":
                payload["tool_choice"] = "required"
            elif request.tool_choice and request.tool_choice != "auto":
                payload["tool_choice"] = {
                    "type": "function",
                    "function": {"name": request.tool_choice},
                }
        if structured:
            assert request.response_schema is not None
            if caps.supports_json_schema:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.response_schema_name,
                        "schema": request.response_schema,
                        "strict": False,
                    },
                }
            else:  # compatibility mode: instruct + validate/repair downstream (never trusted unvalidated)
                system = system + "\n\n" + schema_instruction(request.response_schema)
        payload["messages"] = _messages_to_wire(request, system)

        started = time.perf_counter()
        data = await post_json(
            self._client,
            self._url,
            headers={
                "authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
                **self._extra_headers,
            },
            payload=payload,
        )
        latency_ms = (time.perf_counter() - started) * 1000

        if data.get("error") and not data.get(
            "choices"
        ):  # some gateways return errors with HTTP 200
            err = (
                data["error"]
                if isinstance(data["error"], dict)
                else {"message": str(data["error"])}
            )
            raise LLMError(
                f"{self.name} error: {redact_text(str(err.get('message', err))[:300])}",
                retryable=False,
            )
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        text = message.get("content") or ""
        tool_uses: list[ToolUseBlock] = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {"_raw": fn.get("arguments", "")}
            except json.JSONDecodeError:
                args = {
                    "_raw": fn.get("arguments", "")
                }  # malformed JSON from the model: router validation rejects it
            tool_uses.append(
                ToolUseBlock(
                    id=call.get("id") or f"call_{len(tool_uses)}",
                    name=fn.get("name", ""),
                    input=args,
                )
            )
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            tool_uses=tool_uses,
            structured=extract_json(text) if structured else None,
            usage=LLMUsage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
            ),
            model=data.get("model", self.model),
            stop_reason=choice.get("finish_reason", "") or "",
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
