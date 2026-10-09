"""Structured user goal: what the request explicitly demands, independent of any model's plan.

The goal is extracted from the user's own words by deterministic code (``app.planner.goal``), stored on
the plan and never produced or edited by a model. A re-plan can add steps but cannot drop, weaken or
rewrite a requirement, and the final verification evaluates every requirement against the real state.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class RequirementKind(StrEnum):
    ARTIFACT = "artifact"  # a file the user asked to have saved (by name, or any saved file)
    MIN_URLS = "min_urls"  # "three resources ... with their URLs": N distinct, observed URLs
    GROUNDED_SUMMARY = "grounded_summary"  # a saved summary must be drawn from what was read


class RequirementStatus(StrEnum):
    MET = "met"
    UNMET = "unmet"
    UNVERIFIED = "unverified"  # could not be checked deterministically: never reported as met


class Requirement(BaseModel):
    id: str
    kind: RequirementKind
    text: str = Field(description="what the request demands, in plain words")
    name: str | None = Field(default=None, description="artifact: file name the user gave")
    path: str | None = Field(default=None, description="artifact: exact path the user gave")
    count: int | None = Field(default=None, ge=1, description="min_urls: how many were requested")
    where: Literal["artifact", "answer"] = "answer"  # min_urls: where the URLs must appear


class Prohibition(BaseModel):
    id: str
    text: str = Field(description="the user's restriction, quoted from the request")
    tools: list[str] = Field(default_factory=list, description="tools that may not run")
    forbid_overwrite: bool = False  # no tool call may replace an existing item


class GoalSpec(BaseModel):
    request: str = ""
    requirements: list[Requirement] = Field(default_factory=list)
    prohibitions: list[Prohibition] = Field(default_factory=list)

    @property
    def forbidden_tools(self) -> set[str]:
        return {t for p in self.prohibitions for t in p.tools}

    @property
    def forbids_overwrite(self) -> bool:
        return any(p.forbid_overwrite for p in self.prohibitions)

    def violation(self, tool: str, args: dict[str, object]) -> str | None:
        """The restriction ``tool(args)`` would break, or None."""
        for p in self.prohibitions:
            if tool in p.tools:
                return f"{p.text}"
            if p.forbid_overwrite and args.get("overwrite") is True:
                return f"{p.text}"
        return None

    def is_empty(self) -> bool:
        return not self.requirements and not self.prohibitions


class RequirementResult(BaseModel):
    """Outcome of one requirement against the real final state."""

    id: str
    kind: str
    text: str
    status: RequirementStatus
    evidence: str = ""
