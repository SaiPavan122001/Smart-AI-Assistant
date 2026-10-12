"""What a finished (or stopped) task actually did, step by step and requirement by requirement."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.goal import RequirementResult, RequirementStatus


class StepReport(BaseModel):
    id: str
    title: str
    status: str
    summary: str = ""


class CompletionReport(BaseModel):
    """Built from the final state only. Lists are bounded; texts are short, redacted summaries."""

    completed: list[StepReport] = Field(default_factory=list)
    failed: list[StepReport] = Field(default_factory=list)
    interrupted: list[StepReport] = Field(
        default_factory=list,
        description="was running when the task ended: its action may have completed",
    )
    not_run: list[StepReport] = Field(default_factory=list)
    requirements: list[RequirementResult] = Field(default_factory=list)
    effects: list[str] = Field(
        default_factory=list, description="side-effecting tool calls that succeeded (not undone)"
    )
    replans: int = 0
    recoveries: int = 0

    @property
    def met(self) -> list[RequirementResult]:
        return [r for r in self.requirements if r.status is RequirementStatus.MET]

    @property
    def unmet(self) -> list[RequirementResult]:
        return [r for r in self.requirements if r.status is RequirementStatus.UNMET]

    @property
    def unverified(self) -> list[RequirementResult]:
        return [r for r in self.requirements if r.status is RequirementStatus.UNVERIFIED]

    def progress_text(self) -> str:
        """Plain account for the user. Says what was done, what was not, what is unchecked."""
        lines: list[str] = []
        if self.completed:
            lines.append("Done: " + "; ".join(s.title for s in self.completed[:8]) + ".")
        if self.failed:
            lines.append(
                "Failed: "
                + "; ".join(f"{s.title} ({s.summary[:100]})" for s in self.failed[:4])
                + "."
            )
        if self.interrupted:
            lines.append(
                "Interrupted (may have partly completed): "
                + "; ".join(s.title for s in self.interrupted[:4])
                + "."
            )
        if self.not_run:
            lines.append("Not run: " + "; ".join(s.title for s in self.not_run[:6]) + ".")
        if self.requirements:
            lines.append(
                "Requirements: "
                + "; ".join(
                    f"{'✓' if r.status is RequirementStatus.MET else '✗' if r.status is RequirementStatus.UNMET else '?'} {r.text}"
                    for r in self.requirements
                )
                + "."
            )
        if self.effects:
            lines.append("Completed actions were not undone.")
        return " ".join(lines)
