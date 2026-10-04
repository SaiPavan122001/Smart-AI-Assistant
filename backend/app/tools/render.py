"""Render tool results into compact, safe text for the LLM context."""

from __future__ import annotations

import json
from typing import Any

from app.models.tool import ToolResult
from app.security.injection import wrap_untrusted


def compact_json(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 20] + '…"[truncated]"'


def render_result(result: ToolResult, *, max_chars: int = 6000) -> str:
    """Summary-first rendering. Untrusted content is fenced and labelled as data."""
    if not result.ok:
        err = result.error
        hint = ""
        if err is not None:
            hint = f" [{err.code.value}]"
        body = f"ERROR{hint}: {err.message if err else 'failed'}"
        if err and err.details:
            body += f" details={compact_json(err.details, max_chars)}"
        if result.untrusted and err and err.details:
            return wrap_untrusted(result.tool, body, findings=result.injection_findings)
        return body
    parts: list[str] = []
    if result.summary:
        parts.append(result.summary)
    if result.data:
        parts.append(compact_json(result.data, max_chars))
    if result.cached:
        parts.append("(cached result; environment unchanged since last identical call)")
    text = "\n".join(parts) or "ok"
    if result.untrusted:
        return wrap_untrusted(result.tool, text, findings=result.injection_findings)
    return text
