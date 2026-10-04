"""Reference resolution for plan arguments: ``{{steps.s1.data.path}}``.

Plans are generated before execution, so later steps may need values produced by earlier
ones (a downloaded file's real path, a command's stdout). References are resolved by
deterministic code over *recorded step outputs*; no LLM call is involved.
"""

from __future__ import annotations

import re
from typing import Any

_REF = re.compile(r"\{\{\s*([A-Za-z0-9_.\[\]\-]+)\s*\}\}")
_FULL = re.compile(r"^\s*\{\{\s*([A-Za-z0-9_.\[\]\-]+)\s*\}\}\s*$")


class TemplateError(ValueError):
    pass


def _lookup(path: str, scope: dict[str, Any]) -> Any:
    cur: Any = scope
    for part in re.findall(r"[^.\[\]]+", path):
        if (part == "length" and isinstance(cur, list | str)) or (
            part == "length" and isinstance(cur, dict) and "length" not in cur
        ):
            cur = len(cur)
        elif isinstance(cur, dict):
            if part not in cur:
                raise TemplateError(f"reference '{{{{{path}}}}}' not found (missing '{part}')")
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError) as exc:
                raise TemplateError(f"reference '{{{{{path}}}}}' has a bad index '{part}'") from exc
        else:
            raise TemplateError(f"reference '{{{{{path}}}}}' cannot descend into '{part}'")
    return cur


REF_PATTERN = _REF
lookup = _lookup


def template_refs(value: Any) -> list[str]:
    """Every reference path (``steps.s1.data.title``) used anywhere in ``value``, in order."""
    out: list[str] = []
    if isinstance(value, str):
        out.extend(m.group(1) for m in _REF.finditer(value))
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(template_refs(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(template_refs(v))
    return out


def full_reference(value: Any) -> str | None:
    """The path when ``value`` is exactly one reference (its resolved value keeps its type)."""
    if isinstance(value, str):
        m = _FULL.match(value)
        return m.group(1) if m else None
    return None


def has_template(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_REF.search(value))
    if isinstance(value, dict):
        return any(has_template(v) for v in value.values())
    if isinstance(value, list):
        return any(has_template(v) for v in value)
    return False


def referenced_steps(value: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(value, str):
        for m in _REF.finditer(value):
            parts = m.group(1).split(".")
            if len(parts) >= 2 and parts[0] == "steps":
                out.add(parts[1])
    elif isinstance(value, dict):
        for v in value.values():
            out |= referenced_steps(v)
    elif isinstance(value, list):
        for v in value:
            out |= referenced_steps(v)
    return out


def resolve(value: Any, scope: dict[str, Any]) -> Any:
    if isinstance(value, str):
        full = _FULL.match(value)
        if full:
            return _lookup(full.group(1), scope)
        return _REF.sub(lambda m: str(_lookup(m.group(1), scope)), value)
    if isinstance(value, dict):
        return {k: resolve(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, scope) for v in value]
    return value
