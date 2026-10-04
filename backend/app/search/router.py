"""SearchRouter: try configured providers in order, record every attempt, never bypass a challenge."""

from __future__ import annotations

from app.observability.logging import get_logger
from app.search.base import SearchAttempt, SearchError, SearchProvider, SearchResponse
from app.utils.errors import AgentError, ErrorCode
from app.utils.redact import redact_text

log = get_logger("search")


class SearchRouter:
    def __init__(self, providers: list[SearchProvider]) -> None:
        self._providers = providers

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self._providers]

    async def search(self, query: str, max_results: int = 5) -> SearchResponse:
        query = " ".join(query.split())
        if not query:
            raise AgentError("Search query must not be empty", code=ErrorCode.INVALID_INPUT)
        attempts: list[SearchAttempt] = []
        for p in self._providers:
            try:
                results = await p.search(query, max_results)
            except SearchError as exc:
                reason = f"{exc.kind}: {redact_text(exc.message)[:160]}"
                attempts.append(SearchAttempt(provider=p.name, ok=False, reason=reason))
                continue
            except Exception as exc:  # a broken adapter must not take the task down
                log.warning(
                    "search provider crashed",
                    extra={"provider": p.name, "error": type(exc).__name__},
                )
                attempts.append(
                    SearchAttempt(provider=p.name, ok=False, reason=f"error: {type(exc).__name__}")
                )
                continue
            attempts.append(
                SearchAttempt(provider=p.name, ok=True, reason=f"{len(results)} result(s)")
            )
            return SearchResponse(
                query=query, provider=p.name, results=results[:max_results], attempts=attempts
            )
        why = (
            "; ".join(f"{a.provider}: {a.reason}" for a in attempts)
            or "no search provider is configured"
        )
        raise AgentError(
            f"Web search is unavailable ({why}). Configure a search API key (BRAVE_API_KEY or TAVILY_API_KEY) "
            "or try again later. I will not try to solve a CAPTCHA or bypass a bot check.",
            code=ErrorCode.SEARCH_UNAVAILABLE,
            # provider-specific reasons stay in metadata; behaviour keys off the code only
            details={"attempts": [a.model_dump() for a in attempts]},
        )  # fmt: skip

    async def aclose(self) -> None:
        for p in self._providers:
            await p.aclose()
