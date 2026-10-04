"""Verification engine: runs contracts, records evidence, emits events."""

from __future__ import annotations

from typing import Any

from app.models.enums import EventType
from app.models.verification import VerificationResult, VerificationSpec
from app.state.repository import Repository
from app.tools.context import ExecutionContext
from app.verification.checks import run_checker


class VerificationEngine:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    async def run(
        self,
        specs: list[VerificationSpec],
        ctx: ExecutionContext,
        *,
        step_id: str | None = None,
        scope: str = "step",
    ) -> list[VerificationResult]:
        results: list[VerificationResult] = []
        if not specs:
            return results
        await ctx.emit(
            EventType.VERIFICATION_STARTED, f"Verifying {scope}: {len(specs)} check(s)",
            {"scope": scope, "kinds": [s.kind for s in specs]}, step_id=step_id,
        )  # fmt: skip
        for spec in specs:
            ctx.token.raise_if_cancelled()
            params: dict[str, Any] = dict(spec.params)
            wait_s = float(params.pop("wait_s", 0) or 0)
            outcome = await run_checker(spec.kind, params, ctx, wait_s=wait_s)
            res = VerificationResult(
                task_id=ctx.task_id, step_id=step_id, kind=spec.kind, passed=outcome.passed,
                description=spec.description or spec.kind, evidence=outcome.evidence, message=outcome.message,
                polls=outcome.polls, duration_ms=round(outcome.duration_ms, 1), terminal=outcome.terminal,
            )  # fmt: skip
            results.append(res)
            usage = ctx.meter.usage
            usage.verification_attempts += 1
            usage.verification_polls += outcome.polls
            usage.verification_duration_ms += outcome.duration_ms
            if outcome.passed:
                usage.verification_success_ms += outcome.duration_ms
            await self._repo.save_verification(res)
            if not res.passed:
                ctx.meter.usage.verification_failures += 1
                if ctx.services.metrics:
                    ctx.services.metrics.verification_failures.inc()
        failed = [r for r in results if not r.passed]
        await ctx.emit(
            EventType.VERIFICATION_COMPLETED,
            f"Verification {scope}: {len(results) - len(failed)}/{len(results)} passed",
            {
                "scope": scope, "passed": not failed,
                "results": [r.model_dump(mode="json", include={"kind", "passed", "message", "description", "polls", "duration_ms"}) for r in results],
            },
            step_id=step_id,
        )  # fmt: skip
        return results
