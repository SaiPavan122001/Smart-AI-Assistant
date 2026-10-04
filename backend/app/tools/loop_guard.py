"""Per-task action ledger and loop protection.

Why this exists: before this guard, nothing remembered *which side effects had already happened* in a task.
A step whose action succeeded but whose verification then failed (or a replan that re-listed the same
"open the app" step) simply ran the action again, and a model could alternate open/close indefinitely
until a global ceiling (200 actions / 60 LLM calls / 30 minutes) was hit.

The ledger sits in the ToolRouter, the single choke point every action passes through (planned steps,
adaptive loops, recovery and replans alike), so it cannot be bypassed by the planner or the executor.

1. **Idempotent state-establishing actions** (``Tool.establishes_state``: launch/open). An identical call that
   already succeeded, with no later side effect on the same capability, is *replayed from the ledger*: the
   side effect is NOT executed again and the result says so honestly.
2. **Loop detection** (secondary safety net, deterministic, provider independent): the same side-effecting call
   executed N times in a row; an A,B,A,B,... action cycle (e.g. OPEN, CLOSE, OPEN, CLOSE); or N consecutive tool
   calls with identical call *and* identical result (no state progress). Tripping is sticky: from then on every
   tool call is refused, and the runner stops recovery and re-planning and fails the task with LOOP_DETECTED.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from app.models.enums import EventType
from app.models.tool import ToolSpec
from app.perception.cache import content_hash


class Verdict(StrEnum):
    PROCEED = "proceed"
    REPLAY = "replay"  # identical state-establishing action already done: do not execute again
    BLOCKED = "blocked"  # loop detected: nothing more may run


@dataclass(frozen=True)
class Done:
    fingerprint: str
    tool: str
    summary: str
    data: dict[str, Any]
    artifacts: dict[str, Any]
    seq: int


@dataclass
class Decision:
    verdict: Verdict
    done: Done | None = None
    reason: str = ""


@dataclass
class LoopGuard:
    max_identical: int = 6
    max_cycle_repeats: int = 3
    max_no_progress: int = 5
    tripped: str | None = None
    _seq: int = 0
    _effects: list[tuple[str, str]] = field(
        default_factory=list
    )  # (fingerprint, tool) of executed side effects
    _calls: list[tuple[str, bool, str]] = field(
        default_factory=list
    )  # (fingerprint, ok, result hash)
    _established: dict[str, Done] = field(default_factory=dict)
    _last_effect_seq: dict[str, int] = field(
        default_factory=dict
    )  # capability -> seq of last side effect
    _replays: Counter[str] = field(default_factory=Counter)

    # ------------------------------------------------------------------ before executing
    def check(self, fingerprint: str, spec: ToolSpec) -> Decision:
        if self.tripped:
            return Decision(Verdict.BLOCKED, reason=self.tripped)
        if spec.establishes_state:
            done = self._established.get(fingerprint)
            unchanged = (
                done is not None and self._last_effect_seq.get(spec.capability.value, 0) <= done.seq
            )
            if done is not None and unchanged:
                self._replays[fingerprint] += 1
                if self._replays[fingerprint] > 3:
                    self._trip(
                        f"The agent asked {self._replays[fingerprint]} times to repeat '{spec.name}' even though it had "
                        "already succeeded and nothing changed since."
                    )
                    return Decision(Verdict.BLOCKED, reason=self.tripped or "")
                return Decision(Verdict.REPLAY, done=done)
        return Decision(Verdict.PROCEED)

    # ------------------------------------------------------------------ after executing
    def record(
        self, fingerprint: str, spec: ToolSpec, *, ok: bool, summary: str, data: dict[str, Any], artifacts: dict[str, Any],
    ) -> None:  # fmt: skip
        self._seq += 1
        if not spec.read_only:
            self._effects.append((fingerprint, spec.name))
            self._last_effect_seq[spec.capability.value] = self._seq
            if spec.establishes_state and ok:
                self._established[fingerprint] = Done(
                    fingerprint, spec.name, summary, data, artifacts, self._seq
                )
        self._calls.append((fingerprint, ok, content_hash(summary)))
        del self._effects[:-64], self._calls[:-64]
        self._detect()

    # ------------------------------------------------------------------ detection
    def _detect(self) -> None:
        if self.tripped:
            return
        e = [fp for fp, _ in self._effects]
        names = [n for _, n in self._effects]
        if len(e) >= self.max_identical and len(set(e[-self.max_identical :])) == 1:
            self._trip(
                f"'{names[-1]}' was executed {self.max_identical} times in a row with the same arguments."
            )
            return
        reps = self.max_cycle_repeats
        for k in range(2, 5):
            if len(e) >= k * reps:
                tail = e[-k:]
                if len(set(tail)) > 1 and e[-k * reps :] == tail * reps:
                    self._trip(
                        f"Action cycle detected: {' -> '.join(names[-k:])} repeated {reps} times without reaching the goal."
                    )
                    return
        n = self.max_no_progress
        if len(self._calls) >= n and len(set(self._calls[-n:])) == 1:
            self._trip(f"No progress: the same call returned the same result {n} times in a row.")

    def trip(self, reason: str) -> None:
        """Sticky: from now on every tool call is refused and the runner stops recovery and re-planning."""
        if not self.tripped:
            self.tripped = f"Loop detected: {reason} The task was stopped to avoid repeating actions on your computer."

    _trip = trip

    def stats(self) -> dict[str, Any]:
        return {
            "actions": self._seq,
            "side_effects": len(self._effects),
            "replays": sum(self._replays.values()),
            "tripped": self.tripped,
        }


if TYPE_CHECKING:
    from app.state.repository import Repository
    from app.tools.context import ExecutionContext


async def report_loop(ctx: ExecutionContext, repo: Repository, tool: str | None = None) -> None:
    """Emit the terminal LOOP_DETECTED signal exactly once per task: a timeline event and a durable record."""
    import logging

    from app.state.repository import AgentActionRecord
    from app.utils.errors import ErrorCode
    from app.utils.redact import redact

    reason = ctx.loop_guard.tripped
    if not reason or ctx.extras.get("loop_reported"):
        return
    ctx.extras["loop_reported"] = True
    details = {
        "code": ErrorCode.LOOP_DETECTED.value,
        "reason": reason,
        "tool": tool,
        **ctx.loop_guard.stats(),
    }
    await ctx.event(EventType.LIMIT_REACHED, reason, details)
    try:
        await repo.save_agent_action(
            AgentActionRecord(
                task_id=ctx.task_id,
                step_id=ctx.step_id,
                kind="loop_detected",
                payload=redact(details),
            )
        )
    except Exception:
        logging.getLogger("saa.loop_guard").exception("failed to persist loop_detected")
