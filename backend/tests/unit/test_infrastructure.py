"""Smaller building blocks: approvals manager, event bus, caches, meters, URL policy, config."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from app.config.settings import LimitSettings, Settings
from app.models.approval import ApprovalDecision, ApprovalRequest
from app.models.enums import ApprovalScope, ApprovalStatus, Capability, EventType, RiskLevel
from app.models.tool import RiskAssessment, ToolSpec
from app.observability.metrics import Metrics
from app.observability.usage import UsageMeter
from app.orchestration.events import EventBus
from app.perception.cache import ObservationCache, call_key
from app.permissions.approvals import ApprovalManager
from app.permissions.engine import PermissionEngine, TaskGrants
from app.permissions.policy import Action, PermissionPolicy
from app.security.urls import check_url
from app.state.memory import InMemoryRepository
from app.utils.control import CancelToken, KillSwitch, PauseGate
from app.utils.errors import ErrorCode, LimitExceeded, TaskCancelled, UrlViolation


def req(task: str = "t1") -> ApprovalRequest:
    return ApprovalRequest(
        task_id=task,
        tool_call_id="tc",
        tool="fs_delete",
        action="Delete x",
        reason="r",
        resource="x",
        risk=RiskLevel.HIGH,
    )


# ----------------------------------------------------------------------------- approvals
async def test_approval_manager_decisions_and_scopes() -> None:
    m = ApprovalManager(timeout_s=5)
    r = req()
    t = asyncio.create_task(m.request(r, CancelToken()))
    await asyncio.sleep(0.01)
    assert [p.id for p in m.pending("t1")] == [r.id] and m.pending("other") == []
    done = await m.resolve(
        r.id, ApprovalDecision(approved=True, scope=ApprovalScope.TASK, decided_by="alice")
    )
    assert (
        (await t).status == ApprovalStatus.APPROVED
        and done.scope_granted == ApprovalScope.TASK
        and done.decided_by == "alice"
        and done.decided_at
    )
    with pytest.raises(Exception, match="already"):
        await m.resolve(r.id, ApprovalDecision(approved=False))
    r2 = req()
    r2.allowed_scopes = [ApprovalScope.ONCE]
    t2 = asyncio.create_task(m.request(r2, CancelToken()))
    await asyncio.sleep(0.01)
    out = await m.resolve(r2.id, ApprovalDecision(approved=True, scope=ApprovalScope.TASK))
    await t2
    assert out.scope_granted == ApprovalScope.ONCE, (
        "a scope the request did not offer is downgraded"
    )


async def test_approval_expiry_cancel_and_cancel_task() -> None:
    m = ApprovalManager(timeout_s=0.1)
    assert (await m.request(req(), CancelToken())).status == ApprovalStatus.EXPIRED
    m2 = ApprovalManager(timeout_s=5)
    tok = CancelToken()
    t = asyncio.create_task(m2.request(req(), tok))
    await asyncio.sleep(0.02)
    tok.cancel("stop")
    with pytest.raises(TaskCancelled):
        await t
    r = req("t9")
    t3 = asyncio.create_task(m2.request(r, CancelToken()))
    await asyncio.sleep(0.02)
    assert m2.cancel_task("t9") == 1
    assert (await t3).status == ApprovalStatus.CANCELLED and m2.pending() == []


# ----------------------------------------------------------------------------- events
async def test_event_bus_sequences_redacts_persists_and_fans_out() -> None:
    repo = InMemoryRepository()
    bus = EventBus(repo)
    sub_all, sub_one, sub_other = bus.subscribe(), bus.subscribe("t1"), bus.subscribe("t2")
    e1 = await bus.publish(
        "t1", EventType.TASK_CREATED, "m", {"api_key": "sk-ant-abcdefghijklmnop12345", "ok": 1}
    )
    e2 = await bus.publish("t1", EventType.PLAN_CREATED)
    e3 = await bus.publish("t2", EventType.TASK_CREATED)
    assert (
        (e1.seq, e2.seq, e3.seq) == (1, 2, 1)
        and e1.data["api_key"] == "***REDACTED***"
        and e1.data["ok"] == 1
    )
    assert [e.seq for e in await repo.list_events("t1")] == [1, 2]
    assert (
        sub_all.queue.qsize() == 3 and sub_one.queue.qsize() == 2 and sub_other.queue.qsize() == 1
    )
    sub_one.unsubscribe()
    await bus.publish("t1", EventType.TASK_COMPLETED)
    assert sub_one.queue.qsize() == 2 and sub_all.queue.qsize() == 4
    # sequence numbers continue after a restart (new bus over the same repository)
    again = await EventBus(repo).publish("t1", EventType.STATUS_CHANGED)
    assert again.seq == 4


async def test_slow_subscribers_are_dropped_not_blocking() -> None:
    bus = EventBus(InMemoryRepository())
    sub = bus.subscribe("t")
    sub.queue = asyncio.Queue(maxsize=2)
    for _ in range(5):
        await bus.publish("t", EventType.USAGE_UPDATED)
    assert sub.overflowed
    items = []
    async for ev in sub:
        items.append(ev)
    assert len(items) <= 2


# ----------------------------------------------------------------------------- control primitives
async def test_cancel_token_aborts_running_awaitables_and_kill_switch_propagates() -> None:
    kill = KillSwitch()
    tok = CancelToken(kill)

    async def forever() -> None:
        await asyncio.sleep(60)

    t = asyncio.create_task(tok.run(forever()))
    await asyncio.sleep(0.05)
    kill.engage("STOP")
    with pytest.raises(TaskCancelled):
        await asyncio.wait_for(t, 2)
    assert tok.cancelled and tok.reason == "STOP"
    kill.release()
    assert not tok.cancelled
    assert await tok.run(asyncio.sleep(0, result=5)) == 5


async def test_pause_gate_blocks_until_resumed_or_cancelled() -> None:
    gate, tok = PauseGate(), CancelToken()
    gate.pause()
    waiter = asyncio.create_task(gate.wait_if_paused(tok))
    await asyncio.sleep(0.15)
    assert not waiter.done()
    gate.resume()
    await asyncio.wait_for(waiter, 1)
    gate.pause()
    w2 = asyncio.create_task(gate.wait_if_paused(tok))
    await asyncio.sleep(0.05)
    tok.cancel("x")
    with pytest.raises(TaskCancelled):
        await asyncio.wait_for(w2, 1)


# ----------------------------------------------------------------------------- cache + meter
def test_observation_cache_semantics() -> None:
    c = ObservationCache(max_entries=2)
    k = call_key("fs_list", {"path": "x"})
    assert c.get(k, Capability.FILESYSTEM, 5) is None
    c.put(k, Capability.FILESYSTEM, "v")
    assert c.get(k, Capability.FILESYSTEM, 5) == "v" and c.hits == 1
    assert (
        call_key("fs_list", {"path": "x"})
        == call_key("fs_list", {"path": "x"})
        != call_key("fs_list", {"path": "y"})
    )
    c.bump(Capability.BROWSER)
    assert c.get(k, Capability.FILESYSTEM, 5) == "v", "other capabilities do not invalidate"
    c.bump(Capability.FILESYSTEM)
    assert c.get(k, Capability.FILESYSTEM, 5) is None
    c.put("a", Capability.FILESYSTEM, 1)
    c.put("b", Capability.FILESYSTEM, 2)
    c.put("c", Capability.FILESYSTEM, 3)  # bounded
    assert sum(c.get(x, Capability.FILESYSTEM, 5) is not None for x in "abc") == 2
    assert (
        not c.unchanged("s", {"a": 1})
        and c.unchanged("s", {"a": 1})
        and not c.unchanged("s", {"a": 2})
    )


def test_usage_meter_costs_limits_and_metrics() -> None:
    metrics = Metrics()
    m = UsageMeter(
        LimitSettings(max_llm_calls=2, max_actions=1, max_shell_operations=1, max_vision_calls=1),
        metrics=metrics,
        price_in_per_mtok=3.0,
        price_out_per_mtok=15.0,
    )
    cost = m.charge_llm(input_tokens=1_000_000, output_tokens=100_000, latency_ms=250)
    assert cost == pytest.approx(3.0 + 1.5) and m.usage.estimated_cost_usd == pytest.approx(4.5)
    m.charge_tool(shell=True)
    with pytest.raises(LimitExceeded):
        m.guard_action()
    with pytest.raises(LimitExceeded):
        m.guard_shell()
    m.charge_llm(input_tokens=1, output_tokens=1, latency_ms=1)
    with pytest.raises(LimitExceeded) as ei:
        m.guard_llm()
    assert ei.value.code == ErrorCode.LIMIT_EXCEEDED and ei.value.details["cap"] == 2
    m.charge_vision(input_tokens=10, output_tokens=5, latency_ms=10)
    assert m.usage.vision_calls == 1 and m.usage.llm_calls == 2
    m.note_recovery("retry")
    m.note_retry()
    out = metrics.render().decode()
    assert (
        "saa_llm_calls_total" in out
        and "saa_vision_calls_total 1.0" in out
        and 'saa_recoveries_total{strategy="retry"} 1.0' in out
    )
    metrics.observe_task_finished("COMPLETED", 2.0, m.usage)
    metrics.observe_task_finished("FAILED", 4.0, m.usage)
    s = metrics.summary()
    assert (
        s["task_success_rate"] == 0.5
        and s["average_task_duration_s"] == 3.0
        and s["tasks_failed"] == 1
    )


# ----------------------------------------------------------------------------- policy
def spec(cap: Capability = Capability.FILESYSTEM) -> ToolSpec:
    return ToolSpec(name="t", description="d", input_schema={}, capability=cap)


def assess(risk: RiskLevel, category: str = "c", **kw: object) -> RiskAssessment:
    return RiskAssessment(risk=risk, action="a", resource="r", category=category, **kw)  # type: ignore[arg-type]


def test_permission_matrix() -> None:
    e = PermissionEngine()
    g = TaskGrants()
    assert e.decide(spec(), assess(RiskLevel.LOW), grants=g).action == Action.ALLOW
    assert e.decide(spec(), assess(RiskLevel.MEDIUM), grants=g).action == Action.ALLOW
    assert e.decide(spec(), assess(RiskLevel.HIGH), grants=g).action == Action.ASK
    assert e.decide(spec(), assess(RiskLevel.CRITICAL), grants=g).action == Action.ASK
    assert (
        e.decide(spec(), assess(RiskLevel.LOW, hard_deny="never"), grants=g).action == Action.DENY
    )
    g.add("t", "c")
    assert e.decide(spec(), assess(RiskLevel.HIGH), grants=g).via == "task_grant"
    assert (
        e.decide(spec(), assess(RiskLevel.HIGH, scoped_grant_allowed=False), grants=g).action
        == Action.ASK
    )
    assert e.decide(spec(), assess(RiskLevel.HIGH), grants=g, tainted=True).action == Action.ASK
    assert e.decide(spec(), assess(RiskLevel.MEDIUM), grants=g, tainted=True).tainted_escalation
    assert e.decide(spec(), assess(RiskLevel.LOW), grants=g, tainted=True).action == Action.ALLOW, (
        "reads stay cheap"
    )


def test_policy_update_bumps_version_and_normalises() -> None:
    e = PermissionEngine()
    new = e.update_policy(PermissionPolicy(risk_actions={RiskLevel.CRITICAL: Action.ALLOW}))
    assert (
        new.version == 2
        and new.risk_actions[RiskLevel.CRITICAL] == Action.ASK
        and new.risk_actions[RiskLevel.HIGH] == Action.ASK
    )


# ----------------------------------------------------------------------------- URL policy
@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a?b=c",
        "http://docs.spring.io/",
        "https://sub.domain.example.org:8443/x",
    ],
)
def test_public_urls_pass(url: str) -> None:
    assert check_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "file:///etc/passwd",
        "ftp://x.com",
        "javascript:alert(1)",
        "data:text/plain,hi",
        "http://localhost/",
        "http://127.0.0.1:8000",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://10.1.2.3/",
        "http://192.168.0.1/",
        "http://172.16.5.5/",
        "http://169.254.169.254/",
        "http://[::ffff:127.0.0.1]/",
        "http://x.local/",
        "http://svc.internal/",
        "http://u:p@example.com/",
        "http:///nohost",
        "//example.com",
    ],
)
def test_dangerous_urls_are_blocked(url: str) -> None:
    with pytest.raises(UrlViolation):
        check_url(url)


def test_private_network_can_be_enabled_explicitly() -> None:
    assert check_url("http://127.0.0.1:3000/", allow_private_network=True)
    with pytest.raises(UrlViolation):
        check_url("file:///x", allow_private_network=True)  # schemes stay restricted


# ----------------------------------------------------------------------------- config
def test_settings_are_env_driven_validated_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SAA_LLM__PROVIDER", "openai")
    monkeypatch.setenv("SAA_LLM__MODEL", "gpt-x")
    monkeypatch.setenv("SAA_LLM__API_KEY", "sk-verysecret-1234567890abcd")
    monkeypatch.setenv("SAA_DATABASE__URL", "postgresql+asyncpg://u:pw@db/saa")
    monkeypatch.setenv("SAA_LIMITS__MAX_ACTIONS", "7")
    monkeypatch.setenv("SAA_LOG_LEVEL", "debug")
    monkeypatch.setenv("SAA_FILESYSTEM__ALLOWED_ROOTS", f'["{tmp_path.as_posix()}"]')
    s = Settings()
    assert (s.llm.provider, s.llm.model, s.limits.max_actions, s.log_level) == (
        "openai",
        "gpt-x",
        7,
        "DEBUG",
    )
    assert s.llm.api_key is not None and s.llm.api_key.get_secret_value().startswith(
        "sk-verysecret"
    )
    assert "sk-verysecret" not in repr(s) and "sk-verysecret" not in s.model_dump_json()
    assert (
        tmp_path in s.resolved_allowed_roots()
        and s.filesystem.workspace in s.resolved_allowed_roots()
    )
    monkeypatch.setenv("SAA_LLM__TEMPERATURE", "9")
    with pytest.raises(Exception, match="temperature"):
        Settings()


def test_default_roots_are_desktop_downloads_documents() -> None:
    names = {p.name for p in Settings().resolved_allowed_roots()}
    assert {"Desktop", "Downloads", "Documents"} <= names
    _ = os
