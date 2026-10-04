"""Structured-output helpers: lenient JSON extraction and schema-validated calls."""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def extract_json(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from model text (handles code fences and prose)."""
    if not text:
        return None
    candidates = [text.strip()]
    candidates += [m.group(1).strip() for m in _FENCE.finditer(text)]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_structured(payload: dict[str, Any] | None, model: type[T]) -> T:
    if payload is None:
        raise ValueError("model returned no structured output")
    return model.model_validate(payload)


def validation_feedback(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8]
        )
        return f"Your previous output failed schema validation: {problems}. Return corrected JSON only."
    return f"Your previous output was unusable: {exc}. Return valid JSON matching the schema only."
