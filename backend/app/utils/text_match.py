"""Deterministic name matching for UI targets (browser elements, desktop controls).

Goal: never act on a weak guess. Matching is token based; very short or generic words
carry no weight, and partial matches must cover most of the query to be actionable.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset({
    "the", "and", "for", "with", "page", "from", "this", "that", "your", "into", "onto", "button", "link",
    "field", "box", "input", "click", "press", "tab", "icon", "text", "menu", "item", "control",
})  # fmt: skip

# Minimum score at which a match may be *acted on* (click/type) vs merely *suggested*.
ACT_THRESHOLD = 50.0
SUGGEST_THRESHOLD = 30.0


def tokens(s: str) -> list[str]:
    return _WORD.findall(s.lower())


def significant(s: str) -> list[str]:
    return [t for t in tokens(s) if len(t) >= 3 and t not in _STOP]


def _stem(t: str) -> str:
    for suffix in ("ing", "es", "s"):
        if t.endswith(suffix) and len(t) - len(suffix) >= 3:
            return t[: -len(suffix)]
    return t


def _token_matches(q: str, words: set[str]) -> bool:
    """Exact word, same stem, or the *haystack* word extends the query word (download -> downloads)."""
    if q in words:
        return True
    qs = _stem(q)
    return any(_stem(w) == qs or (len(qs) >= 4 and w.startswith(qs)) for w in words)


def partial_score(query: str, haystack: str) -> float:
    """0 when nothing meaningful matches; 20..70 by the share of significant query words covered."""
    qt = significant(query)
    if not qt:
        return 0.0
    words = set(tokens(haystack))
    hits = sum(1 for t in qt if _token_matches(t, words))
    if hits == 0:
        return 0.0
    return 20.0 + 50.0 * hits / len(qt)
