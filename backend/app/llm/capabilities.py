"""Model capability registry.

Nothing here assumes a model can do everything. Capabilities come from (in priority order):
1. explicit user overrides (``SAA_LLM__CAPABILITIES``),
2. a conservative table of well-known model families,
3. otherwise ``source="assumed"``: tools/structured output are presumed (a computer-use agent is
   useless without them) but the result is flagged so ``llm-check`` can verify it live.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from app.utils.errors import ErrorCode, LLMError


class ModelCapabilities(BaseModel):
    supports_tools: bool = True
    supports_parallel_tools: bool = True
    supports_structured_output: bool = (
        True  # can return JSON we validate (natively or via compat mode)
    )
    supports_json_schema: bool = (
        True  # NATIVE schema-constrained output (else prompt-based compat mode)
    )
    supports_vision: bool = False
    supports_streaming: bool = True
    supports_reasoning: bool = False
    supports_system_prompt: bool = True
    supports_long_context: bool = False
    supports_native_computer_use: bool = (
        False  # we never use vendor computer-use tools; informational
    )
    supports_temperature: bool = True
    max_tokens_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    source: Literal["known", "assumed", "override"] = "assumed"


_FIELDS = set(ModelCapabilities.model_fields)

# (provider-neutral regex on the bare model id, capabilities)
_FAMILIES: list[tuple[re.Pattern[str], dict[str, object]]] = [
    (
        re.compile(r"^(o1-mini|o1-preview)"),
        dict(
            supports_tools=False,
            supports_system_prompt=False,
            supports_json_schema=False,
            supports_reasoning=True,
            supports_temperature=False,
            max_tokens_param="max_completion_tokens",
        ),
    ),
    (
        re.compile(r"^(o1|o3|o4)"),
        dict(
            supports_reasoning=True,
            supports_vision=True,
            supports_temperature=False,
            max_tokens_param="max_completion_tokens",
            supports_long_context=True,
        ),
    ),
    (
        re.compile(r"^gpt-5"),
        dict(
            supports_reasoning=True,
            supports_vision=True,
            supports_temperature=False,
            max_tokens_param="max_completion_tokens",
            supports_long_context=True,
        ),
    ),
    (re.compile(r"^gpt-4\.1"), dict(supports_vision=True, supports_long_context=True)),
    (re.compile(r"^gpt-4o"), dict(supports_vision=True)),
    (re.compile(r"^gpt-4(-turbo)?"), dict(supports_vision=False)),
    (re.compile(r"^gpt-3\.5"), dict(supports_json_schema=False)),
    (
        re.compile(r"^(claude|fable|opus|sonnet|haiku)"),
        dict(supports_vision=True, supports_json_schema=False, supports_long_context=True),
    ),
    (
        re.compile(r"^gemini-(1\.5|2|3)"),
        dict(supports_vision=True, supports_long_context=True, supports_reasoning=True),
    ),
    (re.compile(r"^gemini"), dict(supports_vision=True)),
    (
        re.compile(r"^(llama|mistral|mixtral|qwen|deepseek|glm|kimi|gemma|phi)"),
        dict(supports_json_schema=False, supports_parallel_tools=False),
    ),
]


def _bare(model: str) -> str:
    """'anthropic/claude-x', 'models/gemini-y', 'openai/gpt-z:free' -> 'claude-x' ..."""
    m = model.lower().split("/")[-1]
    return m.split(":")[0]


def resolve_capabilities(model: str, overrides: dict[str, bool] | None = None) -> ModelCapabilities:
    bare = _bare(model)
    caps = ModelCapabilities()
    for pattern, values in _FAMILIES:
        if pattern.match(bare):
            caps = ModelCapabilities(**{**caps.model_dump(), **values, "source": "known"})  # type: ignore[arg-type]
            break
    if overrides:
        unknown = set(overrides) - _FIELDS
        if unknown:
            raise LLMError(
                f"Unknown capability override(s): {', '.join(sorted(unknown))}",
                code=ErrorCode.INVALID_INPUT,
            )
        caps = ModelCapabilities(**{**caps.model_dump(), **overrides, "source": "override"})  # type: ignore[arg-type]
    return caps


def require_for_agent(caps: ModelCapabilities, *, model: str) -> list[str]:
    """Check a model can power the agent. Raises ``LLMError`` if it cannot; returns warnings otherwise."""
    warnings: list[str] = []
    if not caps.supports_tools:
        raise LLMError(
            f"Model '{model}' does not support tool/function calling, which the agent needs to select computer tools. "
            "Choose a tool-capable model (or, if you know it does support tools, set SAA_LLM__CAPABILITIES={\"supports_tools\": true}).",
            code=ErrorCode.NOT_AVAILABLE,
        )  # fmt: skip
    if not caps.supports_system_prompt:
        raise LLMError(
            f"Model '{model}' does not accept a system prompt; the trust-model/policy instructions cannot be delivered safely.",
            code=ErrorCode.NOT_AVAILABLE,
        )  # fmt: skip
    if not caps.supports_structured_output:
        raise LLMError(
            f"Model '{model}' cannot produce structured (JSON) output required for planning.",
            code=ErrorCode.NOT_AVAILABLE,
        )
    if not caps.supports_json_schema:
        warnings.append(
            "native JSON-schema output is unavailable: using validated prompt-based compatibility mode (plans are still schema-checked and repaired)"
        )
    if not caps.supports_parallel_tools:
        warnings.append(
            "model does not support parallel tool calls; steps run one tool at a time (no correctness impact)"
        )
    if caps.source == "assumed":
        warnings.append(
            f"capabilities of '{model}' are assumed, not known: run `python -m app llm-check` to verify"
        )
    return warnings
