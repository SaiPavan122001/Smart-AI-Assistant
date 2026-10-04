"""LLM gateway: the single choke point for every model call.

Responsibilities: budget enforcement, cancellation, outbound secret redaction,
accounting, and structured-output validation with one repair retry.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from app.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
)
from app.llm.structured import parse_structured, validation_feedback
from app.observability.logging import get_logger
from app.observability.usage import UsageMeter
from app.utils.control import CancelToken
from app.utils.errors import ErrorCode, LLMError
from app.utils.redact import redact_text

T = TypeVar("T", bound=BaseModel)
log = get_logger("llm")


def _redact_messages(messages: list[Message]) -> list[Message]:
    out: list[Message] = []
    for m in messages:
        blocks = []
        for b in m.content:
            if isinstance(b, TextBlock):
                blocks.append(TextBlock(text=redact_text(b.text)))
            elif isinstance(b, ToolResultBlock):
                blocks.append(b.model_copy(update={"content": redact_text(b.content)}))
            else:
                blocks.append(b)
        out.append(Message(role=m.role, content=blocks))
    return out


class LLMGateway:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    async def complete(
        self, request: LLMRequest, *, meter: UsageMeter, token: CancelToken
    ) -> LLMResponse:
        token.raise_if_cancelled()
        if request.purpose == "vision":
            meter.guard_vision()
        else:
            meter.guard_llm()
        safe = request.model_copy(
            update={
                "messages": _redact_messages(request.messages),
                "system": redact_text(request.system),
            }
        )
        try:
            response = await token.run(self.provider.complete(safe))
        except LLMError:
            # failed calls still consume budget: prevents unbounded retry loops against a dead provider
            self._charge(meter, request.purpose, 0, 0, 0)
            raise
        self._charge(
            meter,
            request.purpose,
            response.usage.input_tokens,
            response.usage.output_tokens,
            response.latency_ms,
        )
        log.debug(
            "llm_call",
            extra={
                "purpose": request.purpose,
                "model": response.model,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "latency_ms": round(response.latency_ms, 1),
                "tool_uses": [t.name for t in response.tool_uses],
            },
        )
        return response

    @staticmethod
    def _charge(meter: UsageMeter, purpose: str, tin: int, tout: int, latency_ms: float) -> None:
        if purpose == "vision":
            meter.charge_vision(input_tokens=tin, output_tokens=tout, latency_ms=latency_ms)
        else:
            meter.charge_llm(
                input_tokens=tin, output_tokens=tout, latency_ms=latency_ms, purpose=purpose
            )

    async def complete_structured(
        self,
        request: LLMRequest,
        model: type[T],
        *,
        meter: UsageMeter,
        token: CancelToken,
        max_repairs: int = 1,
    ) -> T:
        schema = model.model_json_schema()
        req = request.model_copy(
            update={"response_schema": schema, "response_schema_name": model.__name__.lower()}
        )
        last_exc: Exception | None = None
        for attempt in range(max_repairs + 1):
            response = await self.complete(req, meter=meter, token=token)
            try:
                return parse_structured(response.structured, model)
            except (ValueError, TypeError) as exc:
                last_exc = exc
                if attempt >= max_repairs:
                    break
                req = req.model_copy(
                    update={
                        "messages": [
                            *req.messages,
                            Message.assistant(response.text or str(response.structured)),
                            Message.user(validation_feedback(exc)),
                        ]
                    }
                )
        raise LLMError(
            f"Model failed to produce valid {model.__name__}: {last_exc}",
            code=ErrorCode.LLM_ERROR,
            retryable=False,
        )
