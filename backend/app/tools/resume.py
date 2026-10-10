"""What a resumed task already knows about actions from before the interruption.

Built by ``app.orchestration.recovery`` from the persisted tool-call records and consulted by the
``ToolRouter``, the single path to the computer, so neither a plan, an adaptive loop nor recovery
can repeat an action that already happened or blindly retry one whose outcome is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.tool import ToolResult


@dataclass
class ResumeLedger:
    # fingerprint -> results of side-effecting calls that completed (or were confirmed done) in the
    # interrupted step before the process stopped. Each is replayed once instead of being executed again.
    done: dict[str, list[ToolResult]] = field(default_factory=dict)
    # fingerprint -> why the outcome of an in-flight action is unknown. Such a call always needs the
    # user's approval, whatever the policy, a trusted operation or a task grant would allow.
    uncertain: dict[str, str] = field(default_factory=dict)
    # The still-open records of those actions (ToolCallRecord): closed once the action has run again.
    uncertain_records: dict[str, list[Any]] = field(default_factory=dict)

    def take(self, fingerprint: str) -> ToolResult | None:
        results = self.done.get(fingerprint)
        if not results:
            return None
        result = results.pop(0)
        if not results:
            del self.done[fingerprint]
        return result

    def add_done(self, fingerprint: str, result: ToolResult) -> None:
        self.done.setdefault(fingerprint, []).append(result)
