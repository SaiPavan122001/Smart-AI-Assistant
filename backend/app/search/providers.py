"""Search provider adapters. Vendor wire formats live ONLY here."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from app.search.base import SearchError, SearchResult

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SmartAIAssistant/1.0"


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()
    return "".join(ch for ch in text if ch.isprintable())[:limit]


def _safe_url(url: str) -> str | None:
    """Result URLs are untrusted data: only plain http(s) without credentials are passed on."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    ok = parts.scheme in ("http", "https") and parts.hostname and not parts.username
    return url.strip() if ok else None


def _raise_for_status(name: str, r: httpx.Response) -> None:
    if r.status_code in (401, 403):
        raise SearchError("auth", f"{name} rejected the request (HTTP {r.status_code})")
    if r.status_code == 429:
        raise SearchError("rate_limited", f"{name} rate limit / quota reached")
    if r.status_code >= 500:
        raise SearchError("unavailable", f"{name} is unavailable (HTTP {r.status_code})")
    if r.status_code >= 400:
        raise SearchError("unavailable", f"{name} returned HTTP {r.status_code}")


class _Base:
    name = ""

    def __init__(self, *, client: httpx.AsyncClient | None = None, timeout_s: float = 15.0) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout_s, follow_redirects=False)
        self._owns = client is None

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    async def _send(self, method: str, url: str, **kw: Any) -> httpx.Response:
        try:
            r = await self._client.request(method, url, **kw)
        except httpx.TimeoutException as exc:
            raise SearchError("unavailable", f"{self.name} timed out") from exc
        except httpx.HTTPError as exc:
            raise SearchError(
                "unavailable", f"{self.name} unreachable: {type(exc).__name__}"
            ) from exc
        _raise_for_status(self.name, r)
        return r


def _collect(items: list[dict[str, Any]], t: str, u: str, s: str) -> list[SearchResult]:
    out: list[SearchResult] = []
    seen: set[str] = set()
    for it in items:
        url = _safe_url(str(it.get(u) or ""))
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(
            SearchResult(
                title=_clean(str(it.get(t) or url), 200),
                url=url,
                snippet=_clean(str(it.get(s) or ""), 400),
            )
        )
    return out


class BraveProvider(_Base):
    name = "brave"

    def __init__(self, api_key: str | None, **kw: Any) -> None:
        super().__init__(**kw)
        self._key = api_key

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        if not self._key:
            raise SearchError("not_configured", "no BRAVE_API_KEY configured")
        r = await self._send(
            "GET", "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(max_results, 20)},
            headers={"X-Subscription-Token": self._key, "Accept": "application/json"},
        )  # fmt: skip
        out = _collect(
            (r.json().get("web") or {}).get("results") or [], "title", "url", "description"
        )
        if not out:
            raise SearchError("empty", "brave returned no results")
        return out[:max_results]


class TavilyProvider(_Base):
    name = "tavily"

    def __init__(self, api_key: str | None, **kw: Any) -> None:
        super().__init__(**kw)
        self._key = api_key

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        if not self._key:
            raise SearchError("not_configured", "no TAVILY_API_KEY configured")
        r = await self._send(
            "POST", "https://api.tavily.com/search",
            json={"query": query, "max_results": min(max_results, 20), "search_depth": "basic"},
            headers={"Authorization": f"Bearer {self._key}"},
        )  # fmt: skip
        out = _collect(r.json().get("results") or [], "title", "url", "content")
        if not out:
            raise SearchError("empty", "tavily returned no results")
        return out[:max_results]


class _DdgParser(HTMLParser):
    """Extracts result links/snippets from DuckDuckGo's static HTML endpoint."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._mode: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        cls = a.get("class", "")
        if tag == "a" and "result__a" in cls:
            self.results.append({"href": a.get("href", ""), "title": "", "snippet": ""})
            self._mode, self._buf = "title", []
        elif tag in ("a", "div") and "result__snippet" in cls and self.results:
            self._mode, self._buf = "snippet", []

    def handle_data(self, data: str) -> None:
        if self._mode:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._mode and tag in ("a", "div") and self.results:
            self.results[-1][self._mode] = " ".join(self._buf)
            self._mode = None


def _ddg_target(href: str) -> str:
    """DuckDuckGo wraps results as //duckduckgo.com/l/?uddg=<encoded real url>."""
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if (
        parts.hostname
        and parts.hostname.endswith("duckduckgo.com")
        and parts.path.startswith("/l/")
    ):
        return unquote(parse_qs(parts.query).get("uddg", [""])[0])
    return href


class DuckDuckGoProvider(_Base):
    """Keyless best effort. It may itself be rate-limited or challenged; that is reported, never bypassed."""

    name = "duckduckgo"

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        r = await self._send(
            "GET", "https://html.duckduckgo.com/html/", params={"q": query},
            headers={"User-Agent": UA, "Accept-Language": "en"},
        )  # fmt: skip
        low = r.text.lower()
        if (
            r.status_code == 202
            or "anomaly" in low
            or "bots use duckduckgo" in low
            or "captcha" in low
        ):
            raise SearchError("blocked", "duckduckgo served a bot challenge (not bypassed)")
        p = _DdgParser()
        p.feed(r.text)
        items = [
            {"title": x["title"], "url": _ddg_target(x["href"]), "snippet": x["snippet"]}
            for x in p.results
        ]
        out = _collect(items, "title", "url", "snippet")
        if not out:
            raise SearchError("empty", "duckduckgo returned no parsable results")
        return out[:max_results]
