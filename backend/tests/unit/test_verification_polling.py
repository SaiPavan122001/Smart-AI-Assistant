"""Verification efficiency: immediate first check, short bounded polling, early exits, metrics.

Regression for Step 7 / F3: a check that could never pass polled ~30 s per attempt.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.config.settings import LimitSettings
from app.llm.scripted import structured
from app.models.enums import TaskStatus
from app.models.verification import VerificationSpec
from app.verification import checks
from app.verification.checks import run_checker
from app.verification.engine import VerificationEngine

from tests.helpers import Env, make_env, make_harness


async def timed(coro: Any) -> tuple[Any, float]:
    start = time.monotonic()
    out = await coro
    return out, time.monotonic() - start


# ============================================================================ 15. early success
async def test_passing_check_returns_on_the_immediate_first_check(env: Env) -> None:
    (env.desktop / "a.txt").write_text("x")
    out, took = await timed(
        run_checker("file_exists", {"path": "@Desktop/a.txt"}, env.ctx(), wait_s=30)
    )
    assert out.passed and out.polls == 1 and took < 0.5, "no sleep before the first check"


async def test_polling_stops_as_soon_as_the_condition_holds(env: Env) -> None:
    async def appear() -> None:
        await asyncio.sleep(0.4)
        (env.desktop / "late.txt").write_text("x")

    bg = asyncio.create_task(appear())
    out, took = await timed(
        run_checker("file_exists", {"path": "@Desktop/late.txt"}, env.ctx(), wait_s=30)
    )
    await bg
    assert out.passed and out.polls > 1
    assert 0.3 < took < 2.5, f"early success exit, not a fixed 30 s wait (took {took:.2f}s)"
    assert out.duration_ms >= 300


# ============================================================================ 16. early terminal failure
@pytest.mark.parametrize(
    ("kind", "params"),
    [
        ("value_check", {"actual": 3, "op": "equals", "expected": 4}),  # a recorded value never changes
        ("browser_text", {"contains": "x"}),  # no browser page: nothing to observe
        ("browser_text", {}),  # invalid contract: required parameter missing
        ("browser_text", {"contains": "x", "text": "y"}),  # invalid contract: unknown parameter
        ("file_exists", {"path": "C:/Windows/System32/x"}),  # outside the allowed roots
        ("no_such_kind", {}),
        ("window_exists", {"title_contains": "x"}),  # desktop verification unavailable
    ],
)  # fmt: skip
async def test_a_result_that_is_already_known_is_not_polled(
    env: Env, kind: str, params: dict[str, Any]
) -> None:
    out, took = await timed(run_checker(kind, params, env.ctx(), wait_s=30))
    assert not out.passed and out.polls <= 1 and took < 0.5, (kind, took, out.message)
    if kind != "value_check":
        assert out.terminal, "recovery must not re-check this"


# ============================================================================ 17. timeout
async def test_an_unmet_condition_times_out_after_the_requested_wait(env: Env) -> None:
    out, took = await timed(
        run_checker("file_exists", {"path": "@Desktop/never.txt"}, env.ctx(), wait_s=0.6)
    )
    assert not out.passed and not out.terminal and out.polls >= 2
    assert 0.5 <= took < 1.5
    # no wait requested -> exactly one check
    out0, took0 = await timed(run_checker("file_exists", {"path": "@Desktop/never.txt"}, env.ctx()))
    assert not out0.passed and out0.polls == 1 and took0 < 0.3


# ============================================================================ 18. no unnecessary 30 s poll
async def test_wait_is_capped_far_below_30_seconds(tmp_path: Path) -> None:
    env = make_env(tmp_path, limits=LimitSettings(verification_max_wait_s=0.5))
    out, took = await timed(
        run_checker("file_exists", {"path": "@Desktop/never.txt"}, env.ctx(), wait_s=30)
    )
    assert not out.passed and took < 1.5, f"capped by limits.verification_max_wait_s ({took:.2f}s)"
    assert LimitSettings().verification_max_wait_s == 10.0
    assert checks.poll_cap("download_complete", env.ctx()) == 30.0, "only downloads may wait 30 s"
    assert checks.poll_cap("browser_text", env.ctx()) == 0.5


async def test_a_settled_page_that_does_not_match_stops_the_poll(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live F3: `browser_text` on content that never appears polled ~30 s per attempt."""
    monkeypatch.setattr(checks, "STABLE_EXIT_S", 0.4)
    reads = 0

    class Sess:
        has_page = True

        async def read(self, *, mode: str, selector: str | None, max_chars: int) -> dict[str, str]:
            nonlocal reads
            reads += 1
            return {"title": "Example Domain", "text": "static page", "url": "https://example.com/"}

    env.services.browser = SimpleNamespace(peek=lambda _t: Sess())  # type: ignore[assignment]
    out, took = await timed(
        run_checker("browser_text", {"contains": "never there"}, env.ctx(), wait_s=30)
    )
    assert not out.passed and took < 2.0, f"stopped once the page was stable ({took:.2f}s)"
    assert 2 <= reads <= 8, "backoff polling, not a tight loop"


async def test_backoff_bounds_the_number_of_polls(env: Env) -> None:
    out, _ = await timed(
        run_checker("file_exists", {"path": "@Desktop/never.txt"}, env.ctx(), wait_s=2.0)
    )
    assert 3 <= out.polls <= 6, f"0.15 -> 0.3 -> 0.6 -> 1.0 s backoff, got {out.polls} polls"


# ============================================================================ metrics
async def test_verification_metrics_are_recorded(env: Env) -> None:
    (env.desktop / "a.txt").write_text("x")
    ctx = env.ctx()
    results = await VerificationEngine(env.repo).run(
        [
            VerificationSpec(kind="file_exists", params={"path": "@Desktop/a.txt"}),
            VerificationSpec(kind="file_exists", params={"path": "@Desktop/no.txt", "wait_s": 0.4}),
        ],
        ctx,
    )
    usage = ctx.meter.usage
    assert [r.passed for r in results] == [True, False]
    assert usage.verification_attempts == 2
    assert usage.verification_polls == sum(r.polls for r in results) >= 3
    assert usage.verification_duration_ms >= 350
    assert 0 <= usage.verification_success_ms < 200, "time to success of the passing check only"
    assert usage.verification_failures == 1
    assert results[0].polls == 1 and results[1].polls >= 2 and results[1].duration_ms >= 350


async def test_final_verification_is_skipped_when_a_required_step_already_failed(
    tmp_path: Path,
) -> None:
    """Early terminal exit at task level: the outcome is known, so nothing polls for 30 s."""
    env = make_env(tmp_path, limits=LimitSettings(max_replans=0, approval_timeout_s=5))
    plan = {
        "summary": "x",
        "steps": [
            {"id": "s1", "title": "s1", "tool": "fs_move", "rationale": "r",
             "args": {"src": "@Desktop/a.txt", "dst": "C:/Windows/a.txt"}},
        ],
        "success_criteria": [
            {"kind": "file_exists", "params": {"path": "@Desktop/never.txt", "wait_s": 30}}
        ],
    }  # fmt: skip
    (env.desktop / "a.txt").write_text("x")
    task, took = await timed(make_harness(env, [structured(plan)]).run("move it"))
    assert task.status is TaskStatus.FAILED and took < 5
    assert task.usage.verification_attempts == 0, "the final contract was not polled"
