"""Google Gemini adapter (generateContent REST API).

Translates the neutral request/response to Gemini's wire format and back. Gemini function calls carry no ids,
so ids are minted here and mapped back to function names when tool results are sent. Opaque ``thoughtSignature``
values (required by thinking models when function calls are replayed) travel in ``ToolUseBlock.provider_state``.
"""

from __future__ import annotations

import time
import uuid
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
from app.llm.schema import to_gemini_schema
from app.llm.structured import extract_json, schema_instruction
from app.utils.errors import ErrorCode, LLMError

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"


def _to_contents(request: LLMRequest) -> list[dict[str, Any]]:
    names: dict[str, str] = {}
    contents: list[dict[str, Any]] = []
    for m in request.messages:
        parts: list[dict[str, Any]] = []
        for b in m.content:
            if isinstance(b, TextBlock):
                parts.append({"text": b.text})
            elif isinstance(b, ImageBlock):
                parts.append({"inlineData": {"mimeType": b.media_type, "data": b.data_b64}})
            elif isinstance(b, ToolUseBlock):
                names[b.id] = b.name
                part: dict[str, Any] = {"functionCall": {"name": b.name, "args": b.input}}
                if sig := b.provider_state.get("thoughtSignature"):
                    part["thoughtSignature"] = sig
                parts.append(part)
            elif isinstance(b, ToolResultBlock):
                body = {"error": b.content} if b.is_error else {"result": b.content}
                parts.append(
                    {
                        "functionResponse": {
                            "name": names.get(b.tool_use_id, "unknown_tool"),
                            "response": body,
                        }
                    }
                )
        if parts:
            contents.append({"role": "model" if m.role == "assistant" else "user", "parts": parts})
    return contents


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout_s: float = 90.0,
        client: httpx.AsyncClient | None = None,
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        self.model = model.removeprefix("models/")
        self.capabilities = capabilities or resolve_capabilities(self.model)
        self._api_key = api_key
        self._url = f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}/v1beta/models/{self.model}:generateContent"
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        caps = self.capabilities
        system = request.system
        structured = request.response_schema is not None
        gen: dict[str, Any] = {"maxOutputTokens": request.max_tokens}
        if caps.supports_temperature:
            gen["temperature"] = request.temperature
        payload: dict[str, Any] = {"contents": _to_contents(request), "generationConfig": gen}
        if structured:
            assert request.response_schema is not None
            # Gemini's schema dialect cannot express free-form objects; use JSON mode + instruction, validate downstream.
            gen["responseMimeType"] = "application/json"
            system = system + "\n\n" + schema_instruction(request.response_schema)
        elif request.tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": to_gemini_schema(t.input_schema),
                        }
                        for t in request.tools
                    ]
                }
            ]
            if request.tool_choice == "any":
                payload["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
            elif request.tool_choice and request.tool_choice != "auto":
                payload["toolConfig"] = {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [request.tool_choice],
                    }
                }
        payload["systemInstruction"] = {"parts": [{"text": system}]}

        started = time.perf_counter()
        data = await post_json(
            self._client,
            self._url,
            headers={"x-goog-api-key": self._api_key, "content-type": "application/json"},
            payload=payload,
        )
        latency_ms = (time.perf_counter() - started) * 1000

        block = (data.get("promptFeedback") or {}).get("blockReason")
        cands = data.get("candidates") or []
        if block or not cands:
            raise LLMError(
                f"Gemini returned no candidate (blockReason={block or 'none'})", retryable=False
            )
        cand = cands[0]
        finish = cand.get("finishReason", "") or ""
        if finish == "MALFORMED_FUNCTION_CALL":
            raise LLMError("Gemini produced a malformed function call", retryable=True)
        text_parts: list[str] = []
        tool_uses: list[ToolUseBlock] = []
        for part in (cand.get("content") or {}).get("parts", []):
            if fc := part.get("functionCall"):
                args = fc.get("args")
                state = (
                    {"thoughtSignature": part["thoughtSignature"]}
                    if part.get("thoughtSignature")
                    else {}
                )
                tool_uses.append(
                    ToolUseBlock(
                        id=f"call_{uuid.uuid4().hex[:12]}",
                        name=fc.get("name", ""),
                        input=args if isinstance(args, dict) else {"_raw": args},
                        provider_state=state,
                    )
                )
            elif "text" in part and not part.get("thought"):
                text_parts.append(part["text"])
        text = "".join(text_parts)
        u = data.get("usageMetadata") or {}
        return LLMResponse(
            text=text,
            tool_uses=tool_uses,
            structured=extract_json(text) if structured else None,
            usage=LLMUsage(
                input_tokens=int(u.get("promptTokenCount", 0)),
                output_tokens=int(u.get("candidatesTokenCount", 0))
                + int(u.get("thoughtsTokenCount", 0)),
                cached_input_tokens=int(u.get("cachedContentTokenCount", 0) or 0),
            ),
            model=data.get("modelVersion", self.model),
            stop_reason=finish,
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


_ = ErrorCode
