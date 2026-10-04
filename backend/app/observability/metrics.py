"""Prometheus metrics on a dedicated registry (no hidden global state)."""

from __future__ import annotations

import threading

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

from app.models.task import UsageStats


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry
        self.tasks = Counter("saa_tasks_total", "Tasks by final status", ["status"], registry=r)
        self.task_duration = Histogram(
            "saa_task_duration_seconds", "Task wall-clock duration", registry=r,
            buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800),
        )  # fmt: skip
        self.llm_calls = Counter("saa_llm_calls_total", "LLM calls", ["purpose"], registry=r)
        self.llm_latency = Histogram(
            "saa_llm_latency_seconds", "LLM call latency", registry=r,
            buckets=(0.25, 0.5, 1, 2, 5, 10, 30, 60),
        )  # fmt: skip
        self.tokens = Counter("saa_llm_tokens_total", "LLM tokens", ["direction"], registry=r)
        self.vision_calls = Counter("saa_vision_calls_total", "Vision calls", registry=r)
        self.tool_calls = Counter(
            "saa_tool_calls_total", "Tool calls", ["tool", "capability", "outcome"], registry=r
        )
        self.tool_latency = Histogram(
            "saa_tool_latency_seconds", "Tool latency", ["capability"], registry=r,
            buckets=(0.01, 0.05, 0.25, 1, 5, 15, 60),
        )  # fmt: skip
        self.approvals = Counter("saa_approvals_total", "Approvals", ["decision"], registry=r)
        self.recoveries = Counter(
            "saa_recoveries_total", "Recovery attempts", ["strategy"], registry=r
        )
        self.browser_failures = Counter(
            "saa_browser_failures_total", "Browser failures", registry=r
        )
        self.desktop_failures = Counter(
            "saa_desktop_failures_total", "Desktop failures", registry=r
        )
        self.verification_failures = Counter(
            "saa_verification_failures_total", "Verification failures", registry=r
        )
        self.policy_denials = Counter(
            "saa_policy_denials_total", "Policy denials", ["reason"], registry=r
        )
        self.injection_findings = Counter(
            "saa_injection_findings_total", "Prompt-injection findings", ["severity"], registry=r
        )
        self.cost = Counter("saa_estimated_cost_usd_total", "Estimated LLM cost (USD)", registry=r)

        self._lock = threading.Lock()
        self._finished = {"COMPLETED": 0, "FAILED": 0, "CANCELLED": 0, "TIMED_OUT": 0}
        self._duration_sum = 0.0
        self._llm_latency_sum = 0.0
        self._llm_calls = 0

    def observe_task_finished(self, status: str, duration_s: float, usage: UsageStats) -> None:
        self.tasks.labels(status=status).inc()
        self.task_duration.observe(duration_s)
        with self._lock:
            self._finished[status] = self._finished.get(status, 0) + 1
            self._duration_sum += duration_s
            self._llm_latency_sum += usage.llm_latency_ms
            self._llm_calls += usage.llm_calls

    def summary(self) -> dict[str, float | int]:
        with self._lock:
            total = sum(self._finished.values())
            ok = self._finished["COMPLETED"]
            return {
                "tasks_finished": total,
                "task_success_rate": (ok / total) if total else 0.0,
                "task_failure_rate": ((total - ok) / total) if total else 0.0,
                "average_task_duration_s": (self._duration_sum / total) if total else 0.0,
                "average_llm_latency_ms": (
                    self._llm_latency_sum / self._llm_calls if self._llm_calls else 0.0
                ),
                **{f"tasks_{k.lower()}": v for k, v in self._finished.items()},
            }

    def render(self) -> bytes:
        return generate_latest(self.registry)
