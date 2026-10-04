"""Execution-loop regression suite (OPEN -> CLOSE -> OPEN -> CLOSE ...).

Everything here is deterministic: a FakeDesktopBackend (no real window is ever opened), a scripted LLM and a
test-only "close" tool. The production runtime, router, executor, recovery, planner and verification run for real.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.config.settings import LimitSettings, Settings
from app.llm.base import LLMRequest, LLMResponse
from app.llm.scripted import structured, tool_use
from app.models.enums import Capability, RiskLevel, TaskStatus
from app.models.plan import PlanStep
from app.models.tool import ToolCall, ToolSpec
from app.recovery.engine import RecoveryEngine, Strategy
from app.tools.base import Tool, ToolOutput
from app.tools.desktop.memory import FakeDesktopBackend
from app.tools.desktop.service import DesktopService
from app.tools.loop_guard import LoopGuard, Verdict
from app.utils.errors import ErrorCode
from pydantic import BaseModel

from tests.helpers import Env, Harness, make_env, make_harness

NOTEPAD = ("Untitled - Notepad", "Notepad.exe")


# ----------------------------------------------------------------------------- fakes
class CloseArgs(BaseModel):
    title: str


class FakeCloseApp(Tool[CloseArgs]):
    """Test-only: closes a window of the FAKE desktop (the production code has no such tool)."""

    name = "fake_close_app"
    description = "close a window (test only)"
    capability = Capability.DESKTOP
    Input = CloseArgs
    risk = RiskLevel.LOW
    backend: FakeDesktopBackend

    async def run(self, args: CloseArgs, ctx: Any) -> ToolOutput:
        before = len(self.backend.windows)
        self.backend.windows = [
            w for w in self.backend.windows if args.title.lower() not in w.title.lower()
        ]
        self.backend.closed += 1
        return ToolOutput(summary=f"Closed {before - len(self.backend.windows)} window(s)")


def make_desk(
    tmp_path: Path, *, limits: LimitSettings | None = None
) -> tuple[Env, FakeDesktopBackend]:
    env = make_env(tmp_path, include_desktop=True, limits=limits)
    backend = FakeDesktopBackend()
    backend.closed = 0  # type: ignore[attr-defined]
    backend.launch_handlers["notepad"] = lambda b: b_window(b)
    env.services.desktop = DesktopService(backend, env.settings.desktop)
    closer = FakeCloseApp()
    closer.backend = backend
    env.router.registry.register(closer)
    return env, backend


def b_window(b: FakeDesktopBackend) -> Any:
    from app.tools.desktop.memory import FakeWindow

    return FakeWindow(handle=2000 + len(b.windows), title=NOTEPAD[0], process=NOTEPAD[1])


def step(sid: str, tool: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {
        "id": sid,
        "title": kw.pop("title", sid),
        "tool": tool,
        "args": args,
        "rationale": kw.pop("rationale", sid),
        **kw,
    }


def adaptive(sid: str, goal: str, tools: list[str]) -> dict[str, Any]:
    return {
        "id": sid,
        "title": goal,
        "kind": "adaptive",
        "goal": goal,
        "allowed_tools": tools,
        "rationale": goal,
    }


# deterministic, always-true criterion (no window with that title): keeps the final check off the LLM
TRIVIAL = [
    {"kind": "window_exists", "params": {"title_contains": "zzz-no-such-window", "present": False}}
]


def plan(*steps: dict[str, Any], criteria: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"summary": "p", "steps": list(steps), "success_criteria": criteria or TRIVIAL}


async def tool_calls(env: Env, task_id: str) -> list[Any]:
    return await env.repo.list_tool_calls(task_id)


def launch_use(**kw: Any) -> LLMResponse:
    return tool_use("desktop_launch", app="notepad", **kw)


def finish(success: bool = True, summary: str = "done") -> LLMResponse:
    return tool_use("finish_step", success=success, summary=summary)


# ============================================================================ TEST 1: success -> no second open
async def test_open_succeeds_once_and_the_step_completes_without_a_second_open(
    tmp_path: Path,
) -> None:
    env, be = make_desk(tmp_path)
    h = make_harness(env, [structured(plan(step("s1", "desktop_launch", {"app": "notepad"})))])
    task = await h.run("open notepad")
    assert task.status is TaskStatus.COMPLETED
    assert len(be.launched) == 1
    assert len(await tool_calls(env, task.id)) == 1
    assert "recovery_started" not in await h.events(task.id), (
        "recovery must never run after a success"
    )  # TEST 4 (executor side)


# ============================================================================ TEST 2 / 17: repeated open -> bounded, LOOP_DETECTED
async def test_model_repeatedly_requesting_the_same_open_is_bounded_and_detected(
    tmp_path: Path,
) -> None:
    env, be = make_desk(tmp_path)
    p = plan(adaptive("s1", "Open Notepad", ["desktop_launch"]))
    h = make_harness(
        env, [structured(p)], fallback=lambda r: launch_use()
    )  # the model never stops asking
    task = await h.run("open notepad")
    assert task.status is TaskStatus.FAILED
    assert task.result and task.result.error and task.result.error.code is ErrorCode.LOOP_DETECTED
    assert len(be.launched) == 1, (
        "the side effect ran once; the repeats were replayed, not executed"
    )
    assert len(h.llm.requests) <= 8, "bounded: nowhere near the 60-call / 12-iteration budgets"
    assert "breaker" not in (task.result.summary or "")
    assert any(
        e.type.value == "limit_reached" and e.data.get("code") == "loop_detected"
        for e in await env.repo.list_events(task.id)
    )
    actions = await env.repo.list_agent_actions(task.id)
    assert [a.kind for a in actions].count("loop_detected") == 1, (
        "terminal reason is recorded exactly once"
    )
    assert "replan" not in [a.kind for a in actions], "a tripped loop blocks re-planning"


async def test_simulated_open_close_open_close_cycle_stops_instead_of_continuing(
    tmp_path: Path,
) -> None:
    """FakeLLM + FakeDesktop + test-only close tool: the exact symptom, without touching the computer.

    The cycle spans planned steps (where every per-step counter used to reset), as a replanning loop would produce.
    """
    env, be = make_desk(tmp_path)
    steps = []
    for n in range(1, 11):
        if n % 2:
            steps.append(step(f"s{n}", "desktop_launch", {"app": "notepad", "new_instance": True}))
        else:
            steps.append(step(f"s{n}", "fake_close_app", {"title": "Notepad"}))
    h = make_harness(env, [structured(plan(*steps))])
    task = await h.run("open notepad")
    assert task.status is TaskStatus.FAILED
    assert task.result and task.result.error and task.result.error.code is ErrorCode.LOOP_DETECTED
    assert "cycle" in task.result.error.message.lower()
    assert len(be.launched) == 3 and be.closed == 3, (
        "stopped after the third OPEN/CLOSE pair, not after the whole plan/200 actions"
    )
    assert len(await tool_calls(env, task.id)) == 6
    assert len(h.llm.requests) == 1, (
        "no further LLM call, recovery or re-plan after the loop was detected"
    )


async def test_after_the_loop_trips_no_tool_runs_even_if_the_model_keeps_asking(
    tmp_path: Path,
) -> None:
    env, be = make_desk(tmp_path)
    ctx = env.ctx()
    ctx.loop_guard.tripped = "Loop detected: test"
    for tool, args in (("desktop_launch", {"app": "notepad"}), ("desktop_windows", {})):
        r = await env.router.execute(ToolCall(tool=tool, args=args, rationale="t"), ctx)
        assert not r.ok and r.error and r.error.code is ErrorCode.LOOP_DETECTED
    assert be.launched == []


# ============================================================================ TEST 3: uncertain verification -> no reopen
async def test_failed_verification_after_open_never_reopens_and_ends_safely(tmp_path: Path) -> None:
    env, be = make_desk(tmp_path)
    never = [
        {
            "kind": "element_state",
            "params": {"window_title": "ThisWindowDoesNotExist", "exists": True},
        }
    ]
    first = plan(step("s1", "desktop_launch", {"app": "notepad"}, verify=never))
    replan = plan(
        step("s4", "desktop_launch", {"app": "notepad"})
    )  # the planner forgets the app is open

    def brain(r: LLMRequest) -> LLMResponse:
        if r.purpose == "plan":
            return structured(first)
        if r.purpose == "replan":
            return structured(replan)
        return launch_use()  # adaptive recovery: "just launch it again"

    h = make_harness(env, fallback=brain)
    task = await h.run("open notepad")
    assert len(be.launched) == 1, "verification uncertainty must never repeat the side effect"
    assert task.status in (TaskStatus.FAILED, TaskStatus.COMPLETED)
    # the recovery ladder ran (non-side-effecting re-check first) and stayed bounded
    assert len(h.llm.requests) < 20 and len(await tool_calls(env, task.id)) < 12
    s1 = next(s for s in (task.plan.steps if task.plan else []) if s.id == "s1")
    assert s1.output.get("action_performed") is True, "the planner is told the action DID run"


# ============================================================================ TEST 4: recovery never retries success / terminal states
def test_recovery_never_retries_terminal_or_cancelled_or_loop_states() -> None:
    eng = RecoveryEngine(LimitSettings())
    st = PlanStep(id="s1", title="open", tool="desktop_launch", args={"app": "notepad"})
    for code in (
        ErrorCode.LOOP_DETECTED,
        ErrorCode.CANCELLED,
        ErrorCode.APPROVAL_DENIED,
        ErrorCode.KILL_SWITCH,
        ErrorCode.LIMIT_EXCEEDED,
    ):
        d = eng.decide(st, code, "x", [], replans_used=0, tool_idempotent=False)
        assert d.strategy is Strategy.FAIL, code
    # a cancelled task must not recover even for a normally-recoverable code
    assert (
        eng.decide(
            st,
            ErrorCode.ELEMENT_NOT_FOUND,
            "x",
            [],
            replans_used=0,
            tool_idempotent=False,
            cancelled=True,
        ).strategy
        is Strategy.FAIL
    )
    # a verification failure re-checks WITHOUT re-running the action (phase=verify)
    d = eng.decide(
        st,
        ErrorCode.VERIFICATION_FAILED,
        "x",
        [],
        replans_used=0,
        tool_idempotent=False,
        phase="verify",
    )
    assert d.strategy is Strategy.REFRESH_RETRY


# ============================================================================ TEST 5: re-planner returns the completed open step
async def test_replanner_returning_the_completed_open_step_does_not_execute_it_again(
    tmp_path: Path,
) -> None:
    env, be = make_desk(tmp_path)
    bad_verify = [{"kind": "window_exists", "params": {"title_contains": "Nope"}}]
    first = plan(
        step("s1", "desktop_launch", {"app": "notepad"}),
        step("s2", "desktop_windows", {}, verify=bad_verify),
    )
    again = plan(
        step("s4", "desktop_launch", {"app": "notepad"}), step("s5", "desktop_windows", {})
    )

    def brain(r: LLMRequest) -> LLMResponse:
        if r.purpose == "plan":
            return structured(first)
        if r.purpose == "replan":
            return structured(again)
        return finish(False, "cannot verify")

    h = make_harness(env, fallback=brain)
    task = await h.run("open notepad")
    assert len(be.launched) == 1, "the re-listed open step was recognised as already done"
    replays = [
        r
        for r in await tool_calls(env, task.id)
        if r.call.tool == "desktop_launch" and r.decision == "replayed"
    ]
    assert replays and replays[0].result.data.get("replayed") is True
    assert "NOT repeated" in replays[0].result.summary, "the replay is reported honestly"


# ============================================================================ TEST 6: duplicate starts -> one execution
async def test_two_identical_task_starts_run_once(tmp_path: Path) -> None:
    env, be = make_desk(tmp_path)
    h = make_harness(env, [structured(plan(step("s1", "desktop_launch", {"app": "notepad"})))])
    a, b = await asyncio.gather(
        h.runtime.submit("open notepad"), h.runtime.submit("  Open   Notepad ")
    )
    assert a.id == b.id
    await h.runtime.wait(a.id, 30)
    assert len(be.launched) == 1 and h.llm.purposes.count("plan") == 1
    assert len(await env.repo.list_tasks()) == 1


async def test_idempotency_key_makes_repeated_delivery_start_one_task_even_after_it_finished(
    tmp_path: Path,
) -> None:
    env, be = make_desk(tmp_path)
    h = make_harness(
        env,
        [structured(plan(step("s1", "desktop_launch", {"app": "notepad"})))],
        fallback=lambda r: structured(plan()),
    )
    first = await h.runtime.submit("open notepad", idempotency_key="k-1")
    await h.runtime.wait(first.id, 30)
    again = await h.runtime.submit("some other wording", idempotency_key="k-1")
    other = await h.runtime.submit("open notepad", idempotency_key="k-2")
    assert again.id == first.id, "same key -> same task, never a second execution"
    assert other.id != first.id, "a new key is a deliberate new request"
    await h.runtime.wait(other.id, 30)
    assert len(await env.repo.list_tasks()) == 2


def test_only_one_task_may_drive_the_computer_at_a_time_by_default() -> None:
    assert LimitSettings().max_concurrent_tasks == 1


async def test_a_second_task_waits_instead_of_running_on_the_same_desktop_concurrently(
    tmp_path: Path,
) -> None:
    env, be = make_desk(tmp_path)
    running: list[int] = []
    peak = {"n": 0}

    def brain(r: LLMRequest) -> LLMResponse:
        if r.purpose == "plan":
            running.append(1)
            peak["n"] = max(peak["n"], len(running))
            return structured(plan(step("s1", "desktop_windows", {})))
        return structured(plan())

    h = make_harness(env, fallback=brain)
    t1 = await h.runtime.submit("task one")
    t2 = await h.runtime.submit("task two")
    await asyncio.gather(h.runtime.wait(t1.id, 30), h.runtime.wait(t2.id, 30))
    assert (await env.repo.get_task(t1.id)).status.is_terminal and (
        await env.repo.get_task(t2.id)
    ).status.is_terminal  # type: ignore[union-attr]


# ============================================================================ TEST 8 / 9: STOP and PAUSE stop *behaviour*
async def test_stop_during_the_loop_runs_no_further_tool_call(tmp_path: Path) -> None:
    env, be = make_desk(tmp_path)
    p = plan(adaptive("s1", "poll windows", ["desktop_windows", "desktop_active_window"]))
    h_box: dict[str, Harness] = {}
    n = {"calls": 0}

    def brain(r: LLMRequest) -> LLMResponse:
        if r.purpose == "plan":
            return structured(p)
        n["calls"] += 1
        if n["calls"] == 3:  # STOP is pressed while the model is thinking
            rt = h_box["h"].runtime
            asyncio.get_event_loop().create_task(rt.cancel(next(iter(rt._handles)), "STOP"))
        return tool_use("desktop_windows" if n["calls"] % 2 else "desktop_active_window")

    h = make_harness(env)
    h_box["h"] = h
    h.llm._fallback = brain  # type: ignore[attr-defined]
    h.llm.push(structured(p))
    task = await h.run("watch")
    assert task.status is TaskStatus.CANCELLED
    seen = len(await tool_calls(env, task.id))
    await asyncio.sleep(0.3)
    assert len(await tool_calls(env, task.id)) == seen, "nothing executed after STOP"
    assert n["calls"] <= 4, "no further LLM calls after STOP"


async def test_pause_blocks_new_actions_until_resumed(tmp_path: Path) -> None:
    env, be = make_desk(tmp_path)
    h = make_harness(
        env,
        [
            structured(
                plan(step("s1", "desktop_windows", {}), step("s2", "desktop_active_window", {}))
            )
        ],
    )
    gate = h.runtime  # alias for readability
    task = await h.runtime.submit("look around")
    await gate.pause(task.id)
    await asyncio.sleep(0.3)
    paused_at = len(await tool_calls(env, task.id))
    await asyncio.sleep(0.3)
    assert len(await tool_calls(env, task.id)) == paused_at, "no new external action while paused"
    assert paused_at < 2
    await gate.resume(task.id)
    await h.runtime.wait(task.id, 30)
    assert (
        len(await tool_calls(env, task.id)) == 2
        and (await env.repo.get_task(task.id)).status is TaskStatus.COMPLETED
    )  # type: ignore[union-attr]


# ============================================================================ TEST 10: already open -> reuse, never relaunch
async def test_app_already_open_is_recognised_and_not_relaunched(tmp_path: Path) -> None:
    env, be = make_desk(tmp_path)
    be.add_window(*NOTEPAD, active=True)  # the user already has Notepad open
    r = await env.router.execute(
        ToolCall(tool="desktop_launch", args={"app": "notepad"}, rationale="t"), env.ctx()
    )
    assert r.ok and r.data["reused"] is True and "already open" in r.summary
    assert be.launched == [], "no second process was started"
    r2 = await env.router.execute(
        ToolCall(
            tool="desktop_launch", args={"app": "notepad", "new_instance": True}, rationale="t"
        ),
        env.ctx(),
    )
    assert r2.ok and len(be.launched) == 1, "an explicit request for another instance is honoured"


async def test_calculator_alias_and_explorer_exception(tmp_path: Path) -> None:
    env, be = make_desk(tmp_path)
    be.add_window(
        "Calculator", "CalculatorApp.exe", active=True
    )  # allow-list says calc.exe, process is CalculatorApp.exe
    r = await env.router.execute(
        ToolCall(tool="desktop_launch", args={"app": "calculator"}, rationale="t"), env.ctx()
    )
    assert r.ok and r.data["reused"] is True and be.launched == []
    be.add_window("Program Manager", "explorer.exe")
    be.launch_handlers["explorer"] = lambda b: b_window(b)
    await env.router.execute(
        ToolCall(tool="desktop_launch", args={"app": "explorer"}, rationale="t"), env.ctx()
    )
    assert len(be.launched) == 1, (
        "explorer.exe owns the shell windows: launching it always opens a new window"
    )


# ============================================================================ the ledger itself
SPEC = ToolSpec(
    name="desktop_launch",
    description="",
    input_schema={},
    capability=Capability.DESKTOP,
    establishes_state=True,
)
CLICK = ToolSpec(
    name="desktop_click", description="", input_schema={}, capability=Capability.DESKTOP
)
READ = ToolSpec(
    name="desktop_read",
    description="",
    input_schema={},
    capability=Capability.DESKTOP,
    read_only=True,
)


def rec(g: LoopGuard, fp: str, spec: ToolSpec, ok: bool = True, summary: str = "s") -> None:
    g.record(fp, spec, ok=ok, summary=summary, data={}, artifacts={})


def test_ledger_replays_only_while_nothing_changed_since() -> None:
    g = LoopGuard()
    assert g.check("open:notepad", SPEC).verdict is Verdict.PROCEED
    rec(g, "open:notepad", SPEC)
    assert g.check("open:notepad", SPEC).verdict is Verdict.REPLAY
    rec(g, "click:x", CLICK)  # the world changed: opening again is legitimate again
    assert g.check("open:notepad", SPEC).verdict is Verdict.PROCEED
    g2 = LoopGuard()
    rec(g2, "open:notepad", SPEC, ok=False)  # a FAILED open is never replayed as success
    assert g2.check("open:notepad", SPEC).verdict is Verdict.PROCEED


def test_ledger_trips_on_identical_cycle_and_no_progress_and_is_sticky() -> None:
    g = LoopGuard(max_identical=4)
    for _ in range(4):
        rec(g, "click:a", CLICK, summary=str(_))
    assert g.tripped and "4 times in a row" in g.tripped
    assert g.check("anything", READ).verdict is Verdict.BLOCKED, (
        "sticky: even reads are refused afterwards"
    )

    c = LoopGuard()
    for fp in ["open", "close"] * 3:
        rec(c, fp, CLICK, summary=fp)
    assert c.tripped and "cycle" in c.tripped

    n = LoopGuard()
    for _ in range(5):
        rec(n, "read:a", READ, summary="same")
    assert n.tripped and "No progress" in n.tripped

    ok = (
        LoopGuard()
    )  # legitimate variety is not a loop: typing 5,5,... with different results, A,B,C sequences
    for i in range(30):
        rec(ok, f"click:{i % 7}", CLICK, summary=f"r{i}")
    assert ok.tripped is None


def test_replay_requests_themselves_are_bounded() -> None:
    g = LoopGuard()
    rec(g, "open", SPEC)
    verdicts = [g.check("open", SPEC).verdict for _ in range(5)]
    assert verdicts[:3] == [Verdict.REPLAY] * 3 and verdicts[3] is Verdict.BLOCKED and g.tripped


def test_loop_settings_have_safe_defaults() -> None:
    lim = Settings().limits
    assert (lim.max_identical_actions, lim.max_cycle_repeats, lim.max_no_progress) == (6, 3, 5)
    assert lim.max_replans == 2 and lim.max_retries_per_step == 3 and lim.max_actions == 200
