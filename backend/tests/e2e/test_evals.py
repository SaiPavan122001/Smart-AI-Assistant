"""The evaluation suite as a regression gate (thresholds live in evals/thresholds.json)."""

from __future__ import annotations

import pytest
from evals.run import aggregate, browser_available, check_thresholds, run_one
from evals.tasks import all_tasks


@pytest.fixture(scope="module")
def _sandbox_recycle_bin() -> None:
    """Evaluations must never touch the real Recycle Bin."""
    import shutil
    from pathlib import Path

    import send2trash

    original = send2trash.send2trash
    send2trash.send2trash = lambda p: Path(p).unlink() if Path(p).is_file() else shutil.rmtree(p)  # type: ignore[assignment]
    yield
    send2trash.send2trash = original  # type: ignore[assignment]


@pytest.mark.parametrize("task", all_tasks(), ids=lambda t: t.id)
async def test_eval_task(task, _sandbox_recycle_bin) -> None:  # type: ignore[no-untyped-def]
    ok = await browser_available() if "browser" in task.needs else False
    result = await run_one(task, ok)
    if result.get("skipped"):
        pytest.skip(result["skipped"])
    failing = {
        k: v
        for k, v in result.items()
        if k
        in (
            "completion",
            "tool_selection",
            "execution",
            "verification_correctness",
            "recovery_success",
            "vision_usage_ok",
            "approvals_ok",
            "unnecessary_llm_calls",
            "security_violations",
            "permission_bypasses",
            "injection_resisted",
            "attempted_tools",
        )
        and v in (False, [], 0) is False
    }
    assert result["passed"], failing or result


def test_threshold_logic() -> None:
    good = {
        "completion_rate": 1.0,
        "tool_selection_rate": 1.0,
        "execution_rate": 1.0,
        "verification_correctness_rate": 1.0,
        "recovery_success_rate": 1.0,
        "injection_resistance_rate": 1.0,
        "unnecessary_llm_calls": 0,
        "security_violations": 0,
        "permission_bypasses": 0,
        "total_vision_calls": 0,
    }
    assert check_thresholds(good) == []
    assert check_thresholds({**good, "permission_bypasses": 1}) and check_thresholds(
        {**good, "completion_rate": 0.9}
    )
    assert (
        aggregate([{"id": "x", "category": "c", "skipped": "no browser", "passed": None}])[
            "tasks_skipped"
        ]
        == 1
    )
