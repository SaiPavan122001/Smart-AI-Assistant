"""Extraction of resource names (paths, files, URLs) from untrusted text.

Used by the injection guard: resources named next to injected instructions must not become
tool-call targets unless the *user's own request* named them too.
"""

from __future__ import annotations

import re

_ENTITY = re.compile(
    r"""[a-zA-Z]:[\\/][^\s"'<>|?*]+          # windows drive path
      | https?://[^\s"'<>)\]]+                # url
      | (?:[\w\-.~]+[\\/])+[\w\-. ]*\w        # relative/absolute path with separators
      | [\w\-]+\.[A-Za-z0-9]{2,5}\b           # file name with extension
    """,
    re.VERBOSE,
)
_GENERIC = frozenset({"e.g", "i.e", "etc.", "vs."})


def normalize_path_text(text: str) -> str:
    return text.replace("\\", "/").lower()


def extract_entities(text: str) -> set[str]:
    """Paths, files and URLs mentioned in ``text`` (lower-cased, '/'-normalised)."""
    out: set[str] = set()
    for m in _ENTITY.finditer(text):
        raw = normalize_path_text(m.group(0).strip(" .,;:)'\""))
        if len(raw) < 4 or raw in _GENERIC:
            continue
        out.add(raw)
        base = raw.rstrip("/").rsplit("/", 1)[-1]
        if "." in base and len(base) >= 4:
            out.add(base)  # the bare file name is what relative arguments usually contain
    return out
