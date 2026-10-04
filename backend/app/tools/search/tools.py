"""web_search: structured search results through the SearchRouter (no browser, no CAPTCHA)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from app.models.enums import Capability, RiskLevel
from app.models.tool import RiskAssessment
from app.tools.base import Tool, ToolOutput
from app.tools.context import ExecutionContext
from app.tools.schema import OutputModel
from app.utils.errors import AgentError, ErrorCode

VERIFIED_NOTE = "Verified official source (domain {domain})."
UNVERIFIED_NOTE = "Found a relevant result, but I could not verify that it is the official source."

_DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
# Suffixes under which anyone can publish: owning the suffix says nothing about a page under it,
# so they can never be used as an "official" domain on their own.
_SHARED_SUFFIXES = frozenset(
    {
        "co.uk", "com.au", "co.in", "co.jp", "com.br", "github.io", "gitlab.io", "blogspot.com",
        "wordpress.com", "medium.com", "web.app", "pages.dev", "netlify.app", "vercel.app",
        "herokuapp.com", "readthedocs.io", "substack.com",
    }
)  # fmt: skip


def normalise_domain(raw: str) -> str | None:
    """'https://www.Spring.io/docs' -> 'spring.io'. None if it is not a usable domain."""
    text = raw.strip().lower()
    host = (urlsplit(text if "://" in text else f"//{text}").hostname or "").removeprefix("www.")
    if not _DOMAIN.match(host) or host in _SHARED_SUFFIXES:
        return None
    return host


def matching_domain(url: str, domains: list[str]) -> str | None:
    """The domain in ``domains`` that ``url`` belongs to (https only, exact host or subdomain)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return None
    for d in domains:
        if host == d or host.endswith("." + d):
            return d
    return None


class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=300, description="What to search the web for")
    max_results: int = Field(default=5, ge=1, le=20)
    official_domains: list[str] = Field(
        default_factory=list,
        max_length=5,
        description=(
            "ONLY when the user asks for the OFFICIAL source: the publisher's domain(s), e.g. "
            "['spring.io']. A result is marked official only if its https URL is on one of them."
        ),
    )


class SearchHit(OutputModel):
    title: str
    url: str
    snippet: str
    domain: str
    official: bool = Field(description="URL is on a requested/trusted official domain (checked)")


class SearchAttemptOut(OutputModel):
    provider: str
    ok: bool
    reason: str


class WebSearchOutput(OutputModel):
    query: str
    provider: str
    results: list[SearchHit]
    attempts: list[SearchAttemptOut]
    best: SearchHit | None = Field(
        default=None,
        description="the verified-official result if there is one, else the top result",
    )
    official_verified: bool = Field(description="true only if `best` passed the domain check")
    source_note: str = Field(description="sentence stating whether `best` was verified as official")


class WebSearchArtifacts(OutputModel):
    url: str | None = None


class WebSearch(Tool[WebSearchInput]):
    name = "web_search"
    Output = WebSearchOutput
    Artifacts = WebSearchArtifacts
    description = (
        "Search the web through a structured search provider (API) and get titles, URLs and snippets. "
        "Prefer this over typing into a search engine in the browser: it cannot be blocked by a CAPTCHA. "
        "Open a result afterwards with browser_open if you need the page itself. For an OFFICIAL source "
        "pass official_domains and report data.best with data.source_note."
    )
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
            raise AgentError("Web search is not configured", code=ErrorCode.SEARCH_UNAVAILABLE)
        known = ctx.extras.get("search_unavailable")
        if known:
            # Every provider already failed in this task: asking again cannot give another answer.
            raise AgentError(
                f"{known} (not retried in this task)",
                code=ErrorCode.SEARCH_UNAVAILABLE,
                details={"repeated": True},
            )
        cap = ctx.settings.search.max_results
        try:
            resp = await router.search(args.query, min(args.max_results, cap))
        except AgentError as exc:
            if exc.code is ErrorCode.SEARCH_UNAVAILABLE:
                ctx.extras["search_unavailable"] = exc.message
            raise
        domains = [
            d
            for d in (
                normalise_domain(x)
                for x in [*args.official_domains, *ctx.settings.search.trusted_domains]
            )
            if d
        ]
        hits = [
            SearchHit(
                title=r.title, url=r.url, snippet=r.snippet,
                domain=(urlsplit(r.url).hostname or "").lower(),
                official=matching_domain(r.url, domains) is not None,
            )
            for r in resp.results
        ]  # fmt: skip
        best = next((h for h in hits if h.official), hits[0] if hits else None)
        verified = bool(best and best.official)
        note = (
            VERIFIED_NOTE.format(domain=matching_domain(best.url, domains))
            if best and verified
            else UNVERIFIED_NOTE
        )
        data: dict[str, Any] = WebSearchOutput(
            query=resp.query, provider=resp.provider, results=hits,
            attempts=[SearchAttemptOut(**a.model_dump()) for a in resp.attempts],
            best=best, official_verified=verified, source_note=note,
        ).model_dump()  # fmt: skip
        lines = "; ".join(f"{i + 1}. {r.title[:70]} <{r.url}>" for i, r in enumerate(resp.results))
        return ToolOutput(
            data=data,
            summary=f"{len(resp.results)} result(s) via {resp.provider}: {lines}"[:1200],
            artifacts={"url": best.url} if best else {},
            perception_source="api",
        )


SEARCH_TOOLS: list[type[Tool[Any]]] = [WebSearch]
