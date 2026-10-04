"""Observation cache and duplicate-observation suppression.

Cheap perception beats expensive perception, and *no* perception beats both:
identical read-only calls reuse a cached result until a mutating tool of the same
capability runs (which bumps that capability's state version).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from typing import Any

from app.models.enums import Capability


def content_hash(data: Any) -> str:
    blob = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def call_key(tool: str, args: dict[str, Any]) -> str:
    return f"{tool}:{content_hash(args)}"


class ObservationCache:
    """Per-task cache. Entries are invalidated by capability state bumps."""

    def __init__(self, max_entries: int = 256) -> None:
        self._entries: dict[str, tuple[Any, int, float]] = {}
        self._versions: dict[Capability, int] = defaultdict(int)
        self._last_hash: dict[str, str] = {}
        self._max = max_entries
        self.hits = 0
        self.misses = 0

    def version(self, capability: Capability) -> int:
        return self._versions[capability]

    def bump(self, capability: Capability) -> None:
        """Environment of this capability changed: cached observations are stale."""
        self._versions[capability] += 1

    def bump_all(self) -> None:
        for cap in Capability:
            self._versions[cap] += 1

    def get(self, key: str, capability: Capability, ttl_s: float) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        value, version, ts = entry
        if version != self._versions[capability] or (time.monotonic() - ts) > ttl_s:
            self._entries.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, key: str, capability: Capability, value: Any) -> None:
        if len(self._entries) >= self._max:
            oldest = min(self._entries, key=lambda k: self._entries[k][2])
            self._entries.pop(oldest, None)
        self._entries[key] = (value, self._versions[capability], time.monotonic())

    def unchanged(self, subject: str, data: Any) -> bool:
        """True if ``data`` is identical to the previous observation of ``subject``."""
        h = content_hash(data)
        same = self._last_hash.get(subject) == h
        self._last_hash[subject] = h
        return same
