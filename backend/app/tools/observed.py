"""What a task has really seen and written, recorded by the router from tool results.

The goal evaluation (``app.verification.goal``) compares the final deliverables with this ledger instead
of with anything a model said: a URL is a *source* only if a tool returned it; a summary is *grounded*
only if its vocabulary comes from text a tool returned. Bounded, in memory, never persisted.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

_URL = re.compile(r"https?://[^\s\"'<>\]\)\}]+", re.I)
MAX_URLS = 2000
MAX_TEXT_CHARS = 400_000
MIN_TEXT_LEN = 20


def normalize_url(url: str) -> str:
    """Comparable form: lower-case scheme and host, no fragment, no trailing punctuation or slash."""
    url = url.strip().rstrip(".,;:!?*_`'\"")
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower()
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}{query}"


def urls_in(text: str) -> list[str]:
    return [normalize_url(m.group(0)) for m in _URL.finditer(text)]


def _strings(value: Any, depth: int = 0) -> Iterator[str]:
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v, depth + 1)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v, depth + 1)


@dataclass
class Observed:
    urls: set[str] = field(default_factory=set)
    written: list[str] = field(default_factory=list)  # files written by side-effecting tools
    texts: list[str] = field(default_factory=list)  # text returned by observation tools
    text_chars: int = 0
    successes: dict[str, int] = field(default_factory=dict)  # tool -> successful calls
    effects: list[str] = field(default_factory=list)  # successful side-effecting calls, in order

    def note(
        self, *, tool: str, read_only: bool, external: bool, filesystem: bool,
        data: dict[str, Any], artifacts: dict[str, Any],
    ) -> None:  # fmt: skip
        """Record one SUCCESSFUL tool call."""
        self.successes[tool] = self.successes.get(tool, 0) + 1
        if not read_only:
            self.effects.append(tool)
        for s in _strings(data):
            if len(self.urls) >= MAX_URLS:
                break
            if "http" in s.lower():
                self.urls.update(urls_in(s))
        if filesystem and not read_only:
            path = artifacts.get("path")
            if path and artifacts.get("kind") == "file":
                self.written.append(str(path))
        if read_only and external:
            for s in _strings(data):
                if len(s) >= MIN_TEXT_LEN and self.text_chars < MAX_TEXT_CHARS:
                    self.texts.append(s)
                    self.text_chars += len(s)
