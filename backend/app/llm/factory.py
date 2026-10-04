"""Build an LLM provider from configuration."""

from __future__ import annotations

from app.config.settings import Settings
from app.llm.anthropic import AnthropicProvider
from app.llm.base import LLMProvider
from app.llm.openai import OpenAIProvider
from app.utils.errors import AgentError, ErrorCode


def build_llm_provider(settings: Settings) -> LLMProvider | None:
    """Return the configured provider, or ``None`` when no provider/key is configured.

    The API key is read from settings (``SAA_LLM__API_KEY``) or the vendor's standard
    environment variable; it is never exposed to the frontend or logged.
    """
    import os

    cfg = settings.llm
    if cfg.provider == "none":
        return None
    key = cfg.api_key.get_secret_value() if cfg.api_key else None
    if cfg.provider == "anthropic":
        key = key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        return AnthropicProvider(
            api_key=key, model=cfg.model, base_url=cfg.base_url, timeout_s=cfg.request_timeout_s
        )
    if cfg.provider == "openai":
        key = key or os.environ.get("OPENAI_API_KEY") or ("local" if cfg.base_url else None)
        if not key:
            return None
        return OpenAIProvider(
            api_key=key, model=cfg.model, base_url=cfg.base_url, timeout_s=cfg.request_timeout_s
        )
    raise AgentError(f"Unknown LLM provider {cfg.provider!r}", code=ErrorCode.INVALID_INPUT)
