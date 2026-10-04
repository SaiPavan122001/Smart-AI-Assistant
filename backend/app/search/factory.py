"""Build the search router from settings + the secret chain (keys never live in settings dumps)."""

from __future__ import annotations

from app.config.secrets import SecretProvider, build_secret_provider
from app.config.settings import Settings
from app.search.base import SearchProvider
from app.search.providers import BraveProvider, DuckDuckGoProvider, TavilyProvider
from app.search.router import SearchRouter

_KEYS = {"brave": ("BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY"), "tavily": ("TAVILY_API_KEY",)}


def build_search_router(settings: Settings, secrets: SecretProvider | None = None) -> SearchRouter:
    secrets = secrets or build_secret_provider(
        settings.secrets.provider, keyring_service=settings.secrets.keyring_service
    )
    t = settings.search.timeout_s
    providers: list[SearchProvider] = []
    for name in settings.search.providers:
        key = next((v for k in _KEYS.get(name, ()) if (v := secrets.get(k))), None)
        if name == "brave":
            providers.append(BraveProvider(key, timeout_s=t))
        elif name == "tavily":
            providers.append(TavilyProvider(key, timeout_s=t))
        elif name == "duckduckgo":
            providers.append(DuckDuckGoProvider(timeout_s=t))
    return SearchRouter(providers)
