"""Provider registry: name -> adapter builder. New providers register here; the agent never branches on provider."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.llm.anthropic import AnthropicProvider
from app.llm.base import LLMProvider
from app.llm.capabilities import ModelCapabilities, resolve_capabilities
from app.llm.gemini import GeminiProvider
from app.llm.openai import OpenAIProvider
from app.llm.openrouter import OpenRouterProvider
from app.utils.errors import AgentError, ErrorCode


@dataclass
class ProviderConfig:
    model: str
    api_key: str
    base_url: str | None = None
    timeout_s: float = 90.0
    capability_overrides: dict[str, bool] = field(default_factory=dict)
    app_name: str = "Smart AI Assistant"
    app_url: str | None = None

    def capabilities(self) -> ModelCapabilities:
        return resolve_capabilities(self.model, self.capability_overrides or None)


Builder = Callable[[ProviderConfig], LLMProvider]


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    builder: Builder
    key_env: tuple[str, ...]  # looked up through the SecretProvider when no key is configured
    requires_key: bool = True
    requires_base_url: bool = False
    description: str = ""


_REGISTRY: dict[str, ProviderSpec] = {}


def register_provider(spec: ProviderSpec) -> None:
    _REGISTRY[spec.name] = spec


def get_provider_spec(name: str) -> ProviderSpec:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise AgentError(
            f"Unknown LLM provider '{name}'. Available: {', '.join(provider_names())}",
            code=ErrorCode.INVALID_INPUT,
        ) from None


def provider_names() -> list[str]:
    return sorted(_REGISTRY)


def _openai(c: ProviderConfig) -> LLMProvider:
    return OpenAIProvider(
        api_key=c.api_key,
        model=c.model,
        base_url=c.base_url,
        timeout_s=c.timeout_s,
        capabilities=c.capabilities(),
    )


def _anthropic(c: ProviderConfig) -> LLMProvider:
    return AnthropicProvider(
        api_key=c.api_key,
        model=c.model,
        base_url=c.base_url,
        timeout_s=c.timeout_s,
        capabilities=c.capabilities(),
    )


def _gemini(c: ProviderConfig) -> LLMProvider:
    return GeminiProvider(
        api_key=c.api_key,
        model=c.model,
        base_url=c.base_url,
        timeout_s=c.timeout_s,
        capabilities=c.capabilities(),
    )


def _openrouter(c: ProviderConfig) -> LLMProvider:
    return OpenRouterProvider(api_key=c.api_key, model=c.model, base_url=c.base_url, timeout_s=c.timeout_s, capabilities=c.capabilities(),
                              app_name=c.app_name, app_url=c.app_url)  # fmt: skip


def _compatible(c: ProviderConfig) -> LLMProvider:
    return OpenAIProvider(api_key=c.api_key or "none", model=c.model, base_url=c.base_url, timeout_s=c.timeout_s, capabilities=c.capabilities(),
                          name="openai_compatible")  # fmt: skip


register_provider(ProviderSpec("openai", _openai, ("OPENAI_API_KEY",), description="OpenAI API"))
register_provider(
    ProviderSpec(
        "anthropic", _anthropic, ("ANTHROPIC_API_KEY",), description="Anthropic (Claude) API"
    )
)
register_provider(
    ProviderSpec(
        "gemini", _gemini, ("GEMINI_API_KEY", "GOOGLE_API_KEY"), description="Google Gemini API"
    )
)
register_provider(
    ProviderSpec(
        "openrouter", _openrouter, ("OPENROUTER_API_KEY",), description="OpenRouter (many vendors)"
    )
)
register_provider(ProviderSpec("openai_compatible", _compatible, (), requires_key=False, requires_base_url=True,
                               description="Any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio, gateways)"))  # fmt: skip
