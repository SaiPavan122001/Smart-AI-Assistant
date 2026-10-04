"""Static validation of ``{{steps.<id>.<section>.<path>}}`` references in a plan.

A reference is checked against the *declared output schema* of the step it points at (the tool's
``Output`` / ``Artifacts`` model, or ``AdaptiveOutput`` for an adaptive step) before anything runs.
A field the tool does not produce is rejected at plan time instead of failing during execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.enums import StepKind, StepStatus
from app.models.plan import AdaptiveOutput, PlanStep
from app.tools.registry import ToolRegistry
from app.tools.schema import SCALARS, PathError, resolve_path, tokens, type_of

SECTIONS = ("data", "artifacts", "summary", "ok")
_ADAPTIVE_SCHEMA = AdaptiveOutput.model_json_schema()


@dataclass
class PlanProblem:
    """One validation error, structured so a repair round can act on it precisely."""

    code: str  # unknown_step | unknown_field | type_mismatch | invalid_args | invalid_verification | ...
    where: str  # "s3.args.url", "s2.verify[0]", "success_criteria[1]", "answer_template"
    message: str
    reference: str | None = None
    available: list[str] = field(default_factory=list)  # valid field names at the failing level

    def __str__(self) -> str:
        text = f"{self.where}: {self.message}"
        if self.available:
            text += f" Available fields: {', '.join(self.available)}."
        return text

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "where": self.where, "message": self.message}
        if self.reference:
            out["reference"] = "{{" + self.reference + "}}"
        if self.available:
            out["available_fields"] = self.available
        return out


def _schema_for(
    step: PlanStep, section: str, registry: ToolRegistry
) -> tuple[dict[str, Any] | None, str]:
    """(schema, label) of ``step``'s output section; schema None = nothing can be referenced."""
    if step.kind is StepKind.ADAPTIVE:
        if section == "data":
            return _ADAPTIVE_SCHEMA, "an adaptive step"
        return None, "an adaptive step (it has no fixed artifacts; use data.answer or data.summary)"
    tool = registry.get(step.tool or "")
    if tool is None:
        return None, f"unknown tool '{step.tool}'"
    schema = tool.output_schema() if section == "data" else tool.artifacts_schema()
    return (schema or None), step.tool or ""


def reference_type(
    path: str,
    steps: dict[str, PlanStep],
    registry: ToolRegistry,
    *,
    allowed: set[str],
    where: str,
    unavailable: set[str] | None = None,
) -> tuple[str | None, PlanProblem | None]:
    """Leaf type of a reference ('string', 'integer', 'array', ...), or the problem with it."""
    parts = tokens(path)
    ref = "{{" + path + "}}"
    if len(parts) < 3 or parts[0] != "steps":
        return None, PlanProblem(
            "invalid_reference",
            where,
            f"{ref} is not a valid reference; use steps.<id>.data.<field>",
            path,
        )
    sid, section, rest = parts[1], parts[2], parts[3:]
    step = steps.get(sid)
    if step is None or sid not in allowed:
        return None, PlanProblem(
            "unknown_step",
            where,
            f"{ref} refers to step '{sid}', which is not an earlier step",
            path,
        )
    if step.status in (StepStatus.FAILED, StepStatus.SKIPPED) or sid in (unavailable or ()):
        return None, PlanProblem(
            "unknown_step",
            where,
            f"{ref} refers to step '{sid}', which did not succeed (a revised plan replaces it): it has no output",
            path,
        )
    if section not in SECTIONS:
        return None, PlanProblem(
            "unknown_field",
            where,
            f"{ref}: '{section}' is not an output section",
            path,
            list(SECTIONS[:3]),
        )
    if section in ("summary", "ok"):
        if rest and not (section == "summary" and rest == ["length"]):
            return None, PlanProblem(
                "unknown_field", where, f"{ref}: '{section}' has no sub-fields", path
            )
        return ("integer" if rest else "string" if section == "summary" else "boolean"), None
    schema, label = _schema_for(step, section, registry)
    if schema is None:
        return None, PlanProblem(
            "unknown_field",
            where,
            f"{ref}: step '{sid}' is {label} and declares no {section}",
            path,
        )
    if not rest:
        return "object", None
    try:
        return resolve_path(schema, rest), None
    except PathError as exc:
        return None, PlanProblem(
            "unknown_field",
            where,
            f"{ref} does not exist: in the {section} of {label}, {exc.message}.",
            path,
            exc.available,
        )


def compatible(actual: str, expected: str) -> bool:
    """Can a value of type ``actual`` be passed where ``expected`` is required?"""
    if "any" in (actual, expected) or actual == expected:
        return True
    if expected == "number" and actual == "integer":
        return True
    # numeric / boolean text is coerced by input validation; anything else is not
    return expected in ("integer", "number", "boolean") and actual == "string"


def expected_type(schema: dict[str, Any], key: str) -> str:
    """Declared JSON type of a top-level parameter, 'any' when unknown."""
    prop = (schema.get("properties") or {}).get(key)
    return type_of(prop, schema) if isinstance(prop, dict) else "any"


def embeddable(kind: str) -> bool:
    """May a value of this type be placed inside a sentence / string?"""
    return kind in SCALARS or kind == "any"
