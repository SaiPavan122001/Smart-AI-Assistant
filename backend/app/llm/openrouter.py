"""OpenRouter adapter: an OpenAI-compatible gateway to many vendors' models.

It is just another adapter (no special casing in the agent). Model ids look like ``vendor/model``; capabilities are resolved
from the bare model name, and can be overridden by the user because OpenRouter models differ in tool/schema support.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.llm.capabilities import ModelCapabilities
from app.llm.openai import OpenAIProvider

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(OpenAIProvider):
    name = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout_s: float = 90.0,
        client: httpx.AsyncClient | None = None,
        capabilities: ModelCapabilities | None = None,
        app_name: str = "Smart AI Assistant",
        app_url: str | None = None,
        **_: Any,
    ) -> None:
        headers = {"X-Title": app_name}  # optional attribution headers recommended by OpenRouter
        if app_url:
            headers["HTTP-Referer"] = app_url
        super().__init__(
            api_key=api_key, model=model, base_url=base_url or DEFAULT_BASE_URL, timeout_s=timeout_s, client=client,
            extra_headers=headers, capabilities=capabilities, name="openrouter",
        )  # fmt: skip
