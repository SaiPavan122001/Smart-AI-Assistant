"""Per-task usage meter: LLM/vision/tool accounting plus resource-limit enforcement."""

from __future__ import annotations

from app.config.settings import LimitSettings
from app.models.task import UsageStats
from app.observability.metrics import Metrics
from app.utils.errors import ErrorCode, LimitExceeded

# LLMRequest.purpose -> the UsageStats counter it is accounted under.
_PURPOSE_COUNTERS = {
    "plan": "planner_calls",
    "replan": "replanner_calls",
    "repair": "repair_calls",
    "step": "step_calls",
    "verify": "judge_calls",
    "answer": "answer_generation_calls",
}


class UsageMeter:
    def __init__(
        self,
        limits: LimitSettings,
        *,
        metrics: Metrics | None = None,
        price_in_per_mtok: float = 0.0,
        price_out_per_mtok: float = 0.0,
    ) -> None:
        self.usage = UsageStats()
        self.limits = limits
        self._metrics = metrics
        self._price_in = price_in_per_mtok
        self._price_out = price_out_per_mtok

    # ---- guards (raise LimitExceeded *before* the resource is consumed) ----
    def guard_llm(self) -> None:
        cap = self.limits.max_llm_calls
        if 0 < self.usage.llm_budget < cap:
            cap = self.usage.llm_budget  # task-aware budget derived from the plan
        if self.usage.llm_calls >= cap:
            raise self._limit("llm calls", cap)

    def guard_vision(self) -> None:
        if self.usage.vision_calls >= self.limits.max_vision_calls:
            raise self._limit("vision calls", self.limits.max_vision_calls)

    def guard_action(self) -> None:
        if self.usage.actions >= self.limits.max_actions:
            raise self._limit("actions", self.limits.max_actions)

    def guard_shell(self) -> None:
        if self.usage.shell_operations >= self.limits.max_shell_operations:
            raise self._limit("shell operations", self.limits.max_shell_operations)

    @staticmethod
    def _limit(what: str, cap: int) -> LimitExceeded:
        return LimitExceeded(
            f"Resource limit reached: maximum {what} ({cap}) for this task",
            code=ErrorCode.LIMIT_EXCEEDED,
            details={"limit": what, "cap": cap},
        )

    # ---- charging ----
    def charge_llm(
        self, *, input_tokens: int, output_tokens: int, latency_ms: float, purpose: str = "agent"
    ) -> float:
        u = self.usage
        u.llm_calls += 1
        counter = _PURPOSE_COUNTERS.get(purpose)
        if counter:
            setattr(u, counter, getattr(u, counter) + 1)
        u.input_tokens += input_tokens
        u.output_tokens += output_tokens
        u.llm_latency_ms += latency_ms
        cost = (input_tokens * self._price_in + output_tokens * self._price_out) / 1_000_000
        u.estimated_cost_usd += cost
        if self._metrics:
            self._metrics.llm_calls.labels(purpose=purpose).inc()
            self._metrics.llm_latency.observe(latency_ms / 1000)
            self._metrics.tokens.labels(direction="input").inc(input_tokens)
            self._metrics.tokens.labels(direction="output").inc(output_tokens)
            self._metrics.cost.inc(cost)
        return cost

    def charge_vision(self, *, input_tokens: int, output_tokens: int, latency_ms: float) -> None:
        self.usage.vision_calls += 1
        self.charge_llm(
            input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=latency_ms,
            purpose="vision",
        )  # fmt: skip
        # a vision call is also an LLM call for cost purposes, but count it separately for llm_calls
        self.usage.llm_calls -= 1
        if self._metrics:
            self._metrics.vision_calls.inc()

    def charge_tool(self, *, shell: bool = False, action: bool = True) -> None:
        self.usage.tool_calls += 1
        if action:
            self.usage.actions += 1
        if shell:
            self.usage.shell_operations += 1

    def note_retry(self) -> None:
        self.usage.retries += 1

    def note_recovery(self, strategy: str) -> None:
        self.usage.recoveries += 1
        if self._metrics:
            self._metrics.recoveries.labels(strategy=strategy).inc()
