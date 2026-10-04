"""Provider-neutral search contract."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""


class SearchAttempt(BaseModel):
    provider: str
    ok: bool
    reason: str = ""  # e.g. "not_configured: ...", "blocked: ...", "5 result(s)"


class SearchResponse(BaseModel):
    query: str
    provider: str
    results: list[SearchResult]
    attempts: list[SearchAttempt] = Field(default_factory=list)


class SearchError(Exception):
    """A provider could not answer. ``kind`` lets the router explain *why* and move on safely."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # not_configured | auth | rate_limited | blocked | unavailable | empty
        self.message = message


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, max_results: int) -> list[SearchResult]: ...

    async def aclose(self) -> None: ...
