"""Output-schema helpers: one source of truth for what a tool's ``data`` contains.

Every tool declares its output as a pydantic model (``Tool.Output``). Its JSON schema is used for
three things, so they can never drift apart:

* the planner's tool catalogue (``describe``): the model sees the real field names and types;
* plan validation (``resolve_path``): a reference such as ``steps.s2.data.results[0].url`` is
  checked against the schema before anything executes;
* the router's contract check: what a tool actually returned must match what it declared.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict

SCALARS = frozenset({"string", "integer", "number", "boolean"})
_TOKEN = re.compile(r"[^.\[\]]+")


class OutputModel(BaseModel):
    """Base for tool output contracts: undeclared fields are a contract violation."""

    model_config = ConfigDict(extra="forbid")


class PathError(ValueError):
    """A reference path does not exist in a schema."""

    def __init__(self, message: str, available: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.available = available or []


def tokens(path: str) -> list[str]:
    return _TOKEN.findall(path)


def _alternatives(node: dict[str, Any], root: dict[str, Any]) -> list[dict[str, Any]]:
    """Concrete (non-null) schemas a node may be: follows ``$ref`` and unwraps ``anyOf``."""
    seen = 0
    while "$ref" in node and seen < 20:
        name = str(node["$ref"]).rsplit("/", 1)[-1]
        node = root.get("$defs", {}).get(name, {})
        seen += 1
    options = node.get("anyOf") or node.get("oneOf")
    if options:
        out: list[dict[str, Any]] = []
        for opt in options:
            if opt.get("type") == "null":
                continue
            out.extend(_alternatives(opt, root))
        return out or [{}]
    return [node]


def type_of(node: dict[str, Any], root: dict[str, Any]) -> str:
    """'string' | 'integer' | 'number' | 'boolean' | 'array' | 'object' | 'any'."""
    kinds = {str(alt.get("type", "any")) for alt in _alternatives(node, root)}
    if len(kinds) == 1:
        return kinds.pop()
    if kinds <= {"integer", "number"}:
        return "number"
    return "any"


def _step(node: dict[str, Any], root: dict[str, Any], key: str) -> dict[str, Any] | str:
    """One path segment. Returns the child schema, or a leaf type name when the walk ends."""
    errors: list[PathError] = []
    for alt in _alternatives(node, root):
        kind = alt.get("type")
        props: dict[str, Any] = alt.get("properties") or {}
        if kind is None and not props:
            return "any"  # free-form value: nothing to check against
        if kind == "array":
            if key == "length":
                return "integer"
            if key.isdigit():
                return dict(alt.get("items") or {})
            errors.append(
                PathError(f"is a list: use an index such as [0], or .length (not '{key}')")
            )
            continue
        if kind == "string":
            if key == "length":
                return "integer"
            errors.append(PathError(f"is a string and has no field '{key}'"))
            continue
        if kind == "object" or props:
            if key in props:
                return dict(props[key])
            extra = alt.get("additionalProperties")
            if isinstance(extra, dict):
                return dict(extra)  # mapping with arbitrary keys (e.g. path aliases)
            if not props and extra is not False:
                return "any"  # free-form object
            if key == "length":
                return "integer"
            errors.append(PathError(f"has no field '{key}'", sorted(props)))
            continue
        errors.append(PathError(f"is a {kind} and has no field '{key}'"))
    best = max(errors, key=lambda e: len(e.available)) if errors else PathError("cannot be read")
    raise best


def resolve_path(schema: dict[str, Any], path: list[str]) -> str:
    """Leaf type of ``path`` inside ``schema``; raises ``PathError`` when it does not exist."""
    node: dict[str, Any] = schema
    walked: list[str] = []
    for key in path:
        try:
            nxt = _step(node, schema, key)
        except PathError as exc:
            where = ".".join(walked) or "the output"
            raise PathError(f"'{where}' {exc.message}", exc.available) from None
        if isinstance(nxt, str):
            return nxt
        node = nxt
        walked.append(key)
    return type_of(node, schema)


def describe(schema: dict[str, Any], *, depth: int = 3) -> str:
    """Compact, exact field list for the planner: ``url:string, results:[{title:string, ...}]``.

    ``?`` marks a field that is not always present (optional / mode-dependent / may be null).
    """
    return _describe_object(schema, schema, depth)


def _describe_object(node: dict[str, Any], root: dict[str, Any], depth: int) -> str:
    alts = [a for a in _alternatives(node, root) if a.get("properties")]
    if not alts:
        return ""
    alt = alts[0]
    required = set(alt.get("required", []))
    parts = []
    for name, child in alt["properties"].items():
        nullable = any(o.get("type") == "null" for o in child.get("anyOf", []))
        mark = "" if name in required and not nullable else "?"
        parts.append(f"{name}{mark}:{_describe_value(child, root, depth - 1)}")
    return ", ".join(parts)


def _describe_value(node: dict[str, Any], root: dict[str, Any], depth: int) -> str:
    kind = type_of(node, root)
    if kind == "array":
        item = next(iter(_alternatives(node, root)), {}).get("items") or {}
        return f"[{_describe_value(item, root, depth - 1)}]"
    if kind == "object" and depth > 0:
        inner = _describe_object(node, root, depth)
        if inner:
            return "{" + inner + "}"
        extra = next(iter(_alternatives(node, root)), {}).get("additionalProperties")
        if isinstance(extra, dict):
            return "{<key>:" + _describe_value(extra, root, depth - 1) + "}"
    return kind


def example_reference(schema: dict[str, Any]) -> str | None:
    """A valid reference suffix for the catalogue, e.g. ``title`` or ``results[0].url``."""
    alts = [a for a in _alternatives(schema, schema) if a.get("properties")]
    if not alts:
        return None
    required = set(alts[0].get("required", []))
    props: dict[str, Any] = alts[0]["properties"]
    ordered: list[str] = sorted(props, key=lambda n: n not in required)
    for name in ordered:
        if type_of(props[name], schema) in SCALARS:
            return name
    for name in ordered:
        if type_of(props[name], schema) == "array":
            item = next(iter(_alternatives(props[name], schema)), {}).get("items") or {}
            inner = example_reference({**item, "$defs": schema.get("$defs", {})})
            return f"{name}[0].{inner}" if inner else f"{name}.length"
    return None


def contract_problems(model: type[BaseModel] | None, data: dict[str, Any]) -> list[str]:
    """How ``data`` deviates from the declared output model (empty = it matches)."""
    if model is None:
        return []
    from pydantic import ValidationError

    try:
        model.model_validate(data)
    except ValidationError as exc:
        return [
            f"{'.'.join(str(p) for p in e['loc']) or 'data'}: {e['msg']}" for e in exc.errors()[:6]
        ]
    return []
