"""JSON-Schema helpers for providers with a restricted schema dialect (Gemini)."""

from __future__ import annotations

from typing import Any

_ALLOWED = {
    "type",
    "format",
    "description",
    "nullable",
    "enum",
    "items",
    "properties",
    "required",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
}


def _resolve(node: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    if depth > 24:
        return {"type": "object"}
    if isinstance(node, list):
        return [_resolve(n, defs, depth + 1) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = str(node["$ref"]).rsplit("/", 1)[-1]
        return _resolve(defs.get(name, {"type": "object"}), defs, depth + 1)
    return {
        k: _resolve(v, defs, depth + 1)
        for k, v in node.items()
        if k not in ("$defs", "definitions")
    }


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic/JSON-Schema -> the OpenAPI subset Gemini accepts (no $ref/anyOf/title/default/additionalProperties)."""
    defs = {**schema.get("$defs", {}), **schema.get("definitions", {})}
    cleaned = _clean(_resolve(schema, defs))
    return cleaned if isinstance(cleaned, dict) else {"type": "object"}


def _clean(node: Any) -> Any:
    if isinstance(node, list):
        return [_clean(n) for n in node]
    if not isinstance(node, dict):
        return node
    node = dict(node)
    for key in ("anyOf", "oneOf"):
        if key in node:
            raw = node.pop(key)
            options = [o for o in raw if isinstance(o, dict) and o.get("type") != "null"]
            if len(options) == 1:
                merged: dict[str, Any] = dict(_clean(options[0]))
            elif options and all(o.get("type") == "object" for o in options):
                merged = {
                    "type": "object"
                }  # union of objects: accept any object; pydantic validates the real shape
            else:
                merged = {"type": "string"}
            if len(options) != len(raw):
                merged["nullable"] = True
            node = {**node, **merged}
    t = node.get("type")
    if isinstance(t, list):  # ["string", "null"]
        non_null = [x for x in t if x != "null"]
        node["type"] = non_null[0] if non_null else "string"
        if "null" in t:
            node["nullable"] = True
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k not in _ALLOWED:
            continue
        if k == "properties":
            out[k] = {pk: _clean(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _clean(v)
        else:
            out[k] = v
    if out.get("type") == "object" and not out.get("properties"):
        out.pop("properties", None)
    if "required" in out:
        req = [r for r in out["required"] if r in out.get("properties", {})]
        if req:
            out["required"] = req
        else:
            del out["required"]
    return out
