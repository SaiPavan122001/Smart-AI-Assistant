"""Secret masking applied to logs, events, tool results, prompts and audit records.

The redactor is intentionally conservative: it is cheaper to mask a harmless
string than to leak a credential into task history.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

MASK = "***REDACTED***"

# Keys whose *values* are always masked, regardless of content.
_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|auth|cookie|session[_-]?id|"
    r"private[_-]?key|credential|bearer|client[_-]?secret|connection[_-]?string|database[_-]?url)",
    re.IGNORECASE,
)

# Value patterns that look like credentials, wherever they appear in free text.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=\-]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)"),
    # scheme://user:password@host
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
    # KEY=value / key: value assignments with sensitive-looking names
    re.compile(
        r"(?i)\b[A-Z0-9_]*(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)[A-Z0-9_]*"
        r"\s*[=:]\s*['\"]?[^\s'\"]{4,}"
    ),
)


def redact_text(text: str) -> str:
    """Mask credential-looking substrings in free text."""
    for pattern in _VALUE_PATTERNS:
        text = pattern.sub(MASK, text)
    return text


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key))


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a JSON-like structure. Returns a new object."""
    if _depth > 12:
        return MASK
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and is_sensitive_key(k) and v not in (None, "", False, 0):
                out[k] = MASK
            else:
                out[k] = redact(v, _depth=_depth + 1)
        return out
    if isinstance(value, list | tuple | set):
        return [redact(v, _depth=_depth + 1) for v in value]
    return value


def sanitize_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``env`` with any secret-looking variable removed."""
    return {k: v for k, v in env.items() if not is_sensitive_key(k)}
