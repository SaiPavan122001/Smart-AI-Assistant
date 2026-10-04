"""web_search: structured search results through the SearchRouter (no browser, no CAPTCHA)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import Capability, RiskLevel
from app.models.tool import RiskAssessment
from app.tools.base import Tool, ToolOutput
from app.tools.context import ExecutionContext
from app.utils.errors import AgentError, ErrorCode


class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=300, description="What to search the web for")
    max_results: int = Field(default=5, ge=1, le=20)


class WebSearch(Tool[WebSearchInput]):
    name = "web_search"
    description = (
        "Search the web through a structured search provider (API) and get titles, URLs and snippets. "
        "Prefer this over typing into a search engine in the browser: it cannot be blocked by a CAPTCHA. "
        "Open a result afterwards with browser_open if you need the page itself."
    )
    returns = "provider, query, results[{title,url,snippet}] (use results[0].url), attempts[{provider,ok,reason}]"
    capability = Capability.API
    Input = WebSearchInput
    risk = RiskLevel.LOW
    read_only = True
    idempotent = True
    timeout_s = 45.0
    returns_external_content = True  # snippets are untrusted data: scanned, never instructions

    def assess(self, args: WebSearchInput, ctx: ExecutionContext) -> RiskAssessment:
        return RiskAssessment(
            risk=RiskLevel.LOW,
            action=f"Web search: {args.query[:80]}",
            resource="search provider",
            category="web_search",
            reasons=["Sends your search words to a search provider"],
        )

    async def run(self, args: WebSearchInput, ctx: ExecutionContext) -> ToolOutput:
        router = ctx.services.search
        if router is None:
            raise AgentError("Web search is not configured", code=ErrorCode.NOT_AVAILABLE)
        cap = ctx.settings.search.max_results
        resp = await router.search(args.query, min(args.max_results, cap))
        data: dict[str, Any] = resp.model_dump()
        lines = "; ".join(f"{i + 1}. {r.title[:70]} <{r.url}>" for i, r in enumerate(resp.results))
        return ToolOutput(
            data=data,
            summary=f"{len(resp.results)} result(s) via {resp.provider}: {lines}"[:1200],
            artifacts={"url": resp.results[0].url} if resp.results else {},
            perception_source="api",
        )


SEARCH_TOOLS: list[type[Tool[Any]]] = [WebSearch]
