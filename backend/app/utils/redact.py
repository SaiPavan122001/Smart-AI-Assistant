"""Secret masking applied to logs, events, tool results, prompts and audit records.

The redactor is intentionally conservative: it is cheaper to mask a harmless
string than to leak a credential into task history.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping
from typing import Any

MASK = "***REDACTED***"

# Keys whose *values* are always masked, matched on whole word-tokens ("passed" is not "pass").
_KEY_TOKENS = frozenset({
    "password", "passwd", "pwd", "passphrase", "pass", "secret", "secrets", "token", "apikey",
    "authorization", "auth", "cookie", "cookies", "credential", "credentials", "bearer",
})  # fmt: skip
_KEY_PAIRS = frozenset({
    ("api", "key"), ("private", "key"), ("session", "id"), ("client", "secret"), ("access", "key"),
    ("secret", "key"), ("connection", "string"), ("database", "url"), ("db", "url"), ("auth", "token"),
})  # fmt: skip
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

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
    tokens = [t for t in re.split(r"[^a-z0-9]+", _CAMEL.sub("_", key).lower()) if t]
    if any(t in _KEY_TOKENS for t in tokens):
        return True
    return any(pair in _KEY_PAIRS for pair in itertools.pairwise(tokens))


def _is_maskable(v: Any) -> bool:
    """Credentials are strings or structures; counters/flags (e.g. token usage) are not secrets."""
    if isinstance(v, bool | int | float) or v is None:
        return False
    return not (isinstance(v, str) and not v)


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a JSON-like structure. Returns a new object."""
    if _depth > 12:
        return MASK
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and is_sensitive_key(k) and _is_maskable(v):
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
