from __future__ import annotations

import pytest
from app.config.settings import LimitSettings
from app.models.enums import TERMINAL_STATUSES, StepKind, TaskStatus
from app.models.plan import PlanStep
from app.recovery.engine import RecoveryAttempt, RecoveryEngine, Strategy, alternative_args
from app.state.machine import InvalidTransition, can_transition, transition
from app.utils.errors import ErrorCode

S = TaskStatus


# ----------------------------------------------------------------------------- state machine
def test_happy_path_transitions() -> None:
    cur = S.CREATED
    for nxt in (
        S.PLANNING,
        S.EXECUTING,
        S.WAITING_APPROVAL,
        S.EXECUTING,
        S.RECOVERING,
        S.EXECUTING,
        S.VERIFYING,
        S.COMPLETED,
    ):
        cur = transition(cur, nxt)
    assert cur == S.COMPLETED


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES, key=str))
def test_terminal_states_are_final(terminal: TaskStatus) -> None:
    for target in TaskStatus:
        if target != terminal:
            assert not can_transition(terminal, target)
            with pytest.raises(InvalidTransition):
                transition(terminal, target)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (S.CREATED, S.EXECUTING),
        (S.CREATED, S.COMPLETED),
        (S.PLANNING, S.COMPLETED),
        (S.EXECUTING, S.COMPLETED),
        (S.RECOVERING, S.VERIFYING),
        (S.CREATED, S.WAITING_APPROVAL),
    ],
)
def test_illegal_transitions_rejected(a: TaskStatus, b: TaskStatus) -> None:
    with pytest.raises(InvalidTransition):
        transition(a, b)


def test_every_active_state_can_be_cancelled_failed_or_timed_out() -> None:
    for s in TaskStatus:
        if s in TERMINAL_STATUSES or s == S.CREATED:
            continue
        for t in (S.CANCELLED, S.FAILED, S.TIMED_OUT):
            assert can_transition(s, t), (s, t)


# ----------------------------------------------------------------------------- recovery
def step(**kw: object) -> PlanStep:
    base = {
        "id": "s1",
        "title": "Click download",
        "tool": "browser_click",
        "args": {"query": "Download PDF", "role": "button"},
    }
    base.update(kw)
    return PlanStep(**base)  # type: ignore[arg-type]


def decide(
    engine: RecoveryEngine,
    st: PlanStep,
    code: ErrorCode,
    history: list[RecoveryAttempt],
    **kw: object,
):  # type: ignore[no-untyped-def]
    args = {"replans_used": 0, "tool_idempotent": False, "phase": "action"} | kw
    return engine.decide(st, code, "boom", history, **args)  # type: ignore[arg-type]


def run_sequence(
    engine: RecoveryEngine, st: PlanStep, code: ErrorCode, **kw: object
) -> list[Strategy]:
    history: list[RecoveryAttempt] = []
    out: list[Strategy] = []
    for _ in range(10):
        d = decide(engine, st, code, history, **kw)
        out.append(d.strategy)
        history.append(RecoveryAttempt(st.id, d.strategy, d.reason, code.value, "action"))
        if d.strategy is Strategy.FAIL:
            break
    return out


def test_stale_reference_refreshes_then_falls_back() -> None:
    seq = run_sequence(RecoveryEngine(LimitSettings()), step(), ErrorCode.STALE_REFERENCE)
    assert (
        seq[:3] == [Strategy.REFRESH_RETRY, Strategy.REFRESH_RETRY, Strategy.ADAPTIVE]
        and seq[-1] is Strategy.FAIL
    )


def test_element_not_found_tries_alternative_selector_before_llm() -> None:
    seq = run_sequence(
        RecoveryEngine(LimitSettings(max_retries_per_step=4)), step(), ErrorCode.ELEMENT_NOT_FOUND
    )
    assert seq[:3] == [Strategy.REFRESH_RETRY, Strategy.ALTERNATIVE, Strategy.ADAPTIVE]


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.APPROVAL_DENIED,
        ErrorCode.APPROVAL_TIMEOUT,
        ErrorCode.INJECTION_BLOCKED,
        ErrorCode.CANCELLED,
        ErrorCode.KILL_SWITCH,
        ErrorCode.LIMIT_EXCEEDED,
    ],
)
def test_final_errors_are_never_recovered(code: ErrorCode) -> None:
    d = decide(RecoveryEngine(LimitSettings()), step(), code, [])
    assert d.strategy is Strategy.FAIL


def test_policy_blocks_allow_one_replan_only() -> None:
    seq = run_sequence(
        RecoveryEngine(LimitSettings()),
        step(tool="fs_create", args={"path": "C:/x"}),
        ErrorCode.PATH_BLOCKED,
    )
    assert seq == [Strategy.REPLAN, Strategy.FAIL]


def test_non_idempotent_actions_are_never_blindly_retried_after_timeout() -> None:
    st = step(tool="fs_move", args={"src": "a", "dst": "b"})
    d = decide(RecoveryEngine(LimitSettings()), st, ErrorCode.TIMEOUT, [], tool_idempotent=False)
    assert d.strategy is Strategy.ADAPTIVE, (
        "inspect state first; repeating a half-applied move is unsafe"
    )
    d = decide(RecoveryEngine(LimitSettings()), st, ErrorCode.TIMEOUT, [], tool_idempotent=True)
    assert d.strategy is Strategy.RETRY and d.delay_s > 0


def test_verification_failure_rechecks_without_rerunning_the_action() -> None:
    d = decide(
        RecoveryEngine(LimitSettings()), step(), ErrorCode.VERIFICATION_FAILED, [], phase="verify"
    )
    assert d.strategy is Strategy.REFRESH_RETRY


def test_adaptive_loops_do_not_nest_and_replans_are_bounded() -> None:
    adaptive = step(kind=StepKind.ADAPTIVE, tool=None, goal="do it", args={})
    d = decide(
        RecoveryEngine(LimitSettings()), adaptive, ErrorCode.COMMAND_FAILED, [], phase="adaptive"
    )
    assert d.strategy is Strategy.REPLAN
    d = decide(
        RecoveryEngine(LimitSettings(max_replans=1)),
        adaptive,
        ErrorCode.COMMAND_FAILED,
        [],
        phase="adaptive",
        replans_used=1,
    )
    assert d.strategy is Strategy.FAIL


def test_budget_exhaustion_stops_retry_loops() -> None:
    eng = RecoveryEngine(LimitSettings(max_retries_per_step=2))
    hist = [RecoveryAttempt("s1", Strategy.RETRY, "r", "timeout", "action")] * 2
    d = decide(eng, step(), ErrorCode.TIMEOUT, hist, tool_idempotent=True)
    assert d.strategy is Strategy.FAIL and "exhausted" in d.reason


def test_every_decision_carries_a_reason() -> None:
    eng = RecoveryEngine(LimitSettings(max_retries_per_step=6))
    for code in ErrorCode:
        for st in (step(), step(kind=StepKind.ADAPTIVE, tool=None, goal="g", args={})):
            for d in [decide(eng, st, code, [])]:
                assert d.reason, (code, d)


def test_alternative_args_progression() -> None:
    a = alternative_args(
        "browser_click", {"ref": "e1", "query": "Download PDF", "role": "button"}, 0
    )
    assert a == {"query": "Download PDF", "role": "button"}  # stale ref dropped
    b = alternative_args("browser_click", a or {}, 0)
    assert b == {"query": "Download PDF"}  # role relaxed
    c = alternative_args("browser_click", b or {}, 0)
    assert c == {"query": "Download"}  # most distinctive word
    assert alternative_args("browser_click", {"query": "Download"}, 0) is None
    assert alternative_args("fs_create", {"path": "x"}, 0) is None
