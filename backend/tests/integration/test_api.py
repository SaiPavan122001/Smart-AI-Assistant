from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from app.api.main import create_app
from app.config.settings import (
    FilesystemSettings,
    LimitSettings,
    LLMSettings,
    SecuritySettings,
    Settings,
)
from app.container import build_container
from app.llm.scripted import ScriptedProvider, structured
from app.state.memory import InMemoryRepository
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Api:
    def __init__(self, client: TestClient, llm: ScriptedProvider, root: Path) -> None:
        self.c = client
        self.llm = llm
        self.root = root

    def create(self, instruction: str = "do the thing") -> dict[str, Any]:
        r = self.c.post("/api/v1/tasks", json={"instruction": instruction}, headers=AUTH)
        assert r.status_code == 201, r.text
        return r.json()  # type: ignore[no-any-return]

    def task(self, task_id: str) -> dict[str, Any]:
        return self.c.get(f"/api/v1/tasks/{task_id}", headers=AUTH).json()  # type: ignore[no-any-return]

    def wait(
        self, task_id: str, pred: Callable[[dict[str, Any]], bool], timeout: float = 20
    ) -> dict[str, Any]:
        end = time.time() + timeout
        t: dict[str, Any] = {}
        while time.time() < end:
            t = self.task(task_id)
            if pred(t):
                return t
            time.sleep(0.05)
        raise AssertionError(f"timeout waiting; last status={t.get('status')}")

    def wait_done(self, task_id: str) -> dict[str, Any]:
        return self.wait(
            task_id, lambda t: t["status"] in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT")
        )

    def pending_approvals(self, timeout: float = 10) -> list[dict[str, Any]]:
        end = time.time() + timeout
        while time.time() < end:
            items = self.c.get("/api/v1/approvals", headers=AUTH).json()
            if items:
                return items  # type: ignore[no-any-return]
            time.sleep(0.05)
        raise AssertionError("no approval appeared")


@pytest.fixture
def api(tmp_path: Path) -> Iterator[Api]:
    desktop, downloads, documents, ws = (
        tmp_path / n for n in ("Desktop", "Downloads", "Documents", "workspace")
    )
    for d in (desktop, downloads, documents, ws):
        d.mkdir()
    settings = Settings(
        env="test",
        filesystem=FilesystemSettings(allowed_roots=[desktop, downloads, documents], workspace=ws),
        limits=LimitSettings(approval_timeout_s=30),
        security=SecuritySettings(
            api_token=TOKEN,
            allowed_origins=["http://localhost:5173"],
            allowed_hosts=["testserver", "localhost"],
        ),
        log_json=False,
    )
    llm = ScriptedProvider()
    container = build_container(
        settings, repo=InMemoryRepository(), llm=llm, enable_browser=False, enable_desktop=False
    )
    app = create_app(container)
    with TestClient(app) as client:
        yield Api(client, llm, tmp_path)


def fs_plan(root: Path) -> dict[str, Any]:
    return {
        "summary": "make a folder",
        "steps": [
            {
                "id": "s1",
                "title": "Create folder",
                "tool": "fs_create",
                "args": {"path": "@Desktop/Reports", "kind": "dir"},
                "rationale": "requested",
                "verify": [{"kind": "dir_exists", "params": {"path": "@Desktop/Reports"}}],
            },
        ],
        "success_criteria": [{"kind": "dir_exists", "params": {"path": "@Desktop/Reports"}}],
    }


# ----------------------------------------------------------------------------- auth + basics
def test_health_open_and_everything_else_requires_token(api: Api) -> None:
    assert api.c.get("/api/v1/health").json()["status"] == "ok"
    assert api.c.get("/api/v1/ready").json()["ready"] is True
    for path in (
        "/api/v1/tasks",
        "/api/v1/permissions",
        "/api/v1/tools",
        "/api/v1/agent/status",
        "/metrics",
    ):
        assert api.c.get(path).status_code == 401, path
        assert api.c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401, path
    assert api.c.post("/api/v1/tasks", json={"instruction": "x"}).status_code == 401
    assert api.c.post("/api/v1/agent/stop").status_code == 401


def test_untrusted_host_is_rejected(api: Api) -> None:
    r = api.c.get("/api/v1/health", headers={"Host": "evil.example.com"})
    assert r.status_code == 400


def test_validation_errors_are_structured(api: Api) -> None:
    r = api.c.post("/api/v1/tasks", json={"instruction": ""}, headers=AUTH)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_input"
    assert api.c.get("/api/v1/tasks/nope", headers=AUTH).status_code == 404
    assert "x-request-id" in api.c.get("/api/v1/health").headers


def test_tools_endpoint_exposes_metadata(api: Api) -> None:
    tools = {t["name"]: t for t in api.c.get("/api/v1/tools", headers=AUTH).json()}
    assert {"fs_delete", "terminal_execute_safe", "verify_task_result"} <= set(tools)
    t = tools["fs_delete"]
    for key in (
        "input_schema",
        "capability",
        "risk",
        "approval",
        "timeout_s",
        "retry",
        "idempotent",
        "audit",
    ):
        assert key in t


# ----------------------------------------------------------------------------- task lifecycle
def test_task_runs_to_completion_with_timeline(api: Api) -> None:
    api.llm.push(structured(fs_plan(api.root)))
    t = api.create("Create a Reports folder on my desktop")
    done = api.wait_done(t["id"])
    assert done["status"] == "COMPLETED" and done["result"]["verified"] is True
    assert (api.root / "Desktop" / "Reports").is_dir()
    events = api.c.get(f"/api/v1/tasks/{t['id']}/events", headers=AUTH).json()
    types = [e["type"] for e in events]
    assert types[0] == "task_created" and types[-1] == "task_completed"
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    assert {
        "planning_started",
        "plan_created",
        "tool_selected",
        "action_completed",
        "verification_completed",
    } <= set(types)
    assert all(e["correlation"]["task_id"] == t["id"] for e in events)
    assert (
        api.c.get(f"/api/v1/tasks/{t['id']}/events?after_seq=3", headers=AUTH).json()[0]["seq"] == 4
    )
    listed = api.c.get("/api/v1/tasks", headers=AUTH).json()
    assert listed["items"][0]["id"] == t["id"] and listed["items"][0]["status"] == "COMPLETED"
    assert (
        api.c.get(f"/api/v1/tasks/{t['id']}/tool-calls", headers=AUTH).json()[0]["call"]["tool"]
        == "fs_create"
    )
    assert api.c.get(f"/api/v1/tasks/{t['id']}/verifications", headers=AUTH).json()
    cps = api.c.get(f"/api/v1/tasks/{t['id']}/checkpoints", headers=AUTH).json()
    assert [c["kind"] for c in cps] == ["checkpoint"] * len(cps) and cps
    assert cps[-1]["payload"]["remaining"] == [] and cps[-1]["payload"]["completed"] == ["s1"]
    assert "checkpoint_saved" in types
    assert api.c.get("/api/v1/tasks/nope/checkpoints", headers=AUTH).status_code == 404
    decisions = api.c.get(f"/api/v1/permissions/decisions?task_id={t['id']}", headers=AUTH).json()
    assert decisions and decisions[0]["action"] == "allow"
    metrics = api.c.get("/metrics", headers=AUTH).text
    assert "saa_tasks_total" in metrics and "saa_llm_calls_total" in metrics
    assert api.c.get("/api/v1/metrics/summary", headers=AUTH).json()["tasks_completed"] == 1


def test_no_llm_configured_fails_honestly(tmp_path: Path) -> None:
    settings = Settings(
        env="test", llm=LLMSettings(provider="none"),
        filesystem=FilesystemSettings(allowed_roots=[tmp_path], workspace=tmp_path / "w"),
        security=SecuritySettings(api_token=TOKEN, allowed_hosts=["testserver"]), log_json=False,
    )  # fmt: skip
    container = build_container(
        settings, repo=InMemoryRepository(), enable_browser=False, enable_desktop=False
    )
    with TestClient(create_app(container)) as client:
        assert client.get("/api/v1/ready").json()["checks"]["llm_configured"] is False
        t = client.post("/api/v1/tasks", json={"instruction": "x"}, headers=AUTH).json()
        d: dict[str, Any] = {}
        for _ in range(100):
            d = client.get(f"/api/v1/tasks/{t['id']}", headers=AUTH).json()
            if d["status"] == "FAILED":
                break
            time.sleep(0.05)
        assert d["status"] == "FAILED" and "No LLM provider" in d["result"]["error"]["message"]


# ----------------------------------------------------------------------------- approvals
def delete_plan() -> dict[str, Any]:
    return {
        "summary": "delete the old file",
        "steps": [
            {
                "id": "s1",
                "title": "Delete old.txt",
                "tool": "fs_delete",
                "args": {"path": "@Desktop/old.txt"},
                "rationale": "User asked to clean up old.txt",
                "verify": [{"kind": "file_absent", "params": {"path": "@Desktop/old.txt"}}],
            },
        ],
        "success_criteria": [{"kind": "file_absent", "params": {"path": "@Desktop/old.txt"}}],
    }


def test_approval_request_is_specific_and_deny_blocks_the_action(api: Api) -> None:
    victim = api.root / "Desktop" / "old.txt"
    victim.write_text("precious")
    api.llm.push(structured(delete_plan()))
    t = api.create("Delete old.txt from my desktop")
    ap = api.pending_approvals()[0]
    # specific, human-readable approval content
    assert (
        ap["tool"] == "fs_delete" and str(victim) in ap["action"] and str(victim) in ap["resource"]
    )
    assert ap["reason"] == "User asked to clean up old.txt"
    assert ap["risk"] == 3 and ap["risk_explanation"] and ap["status"] == "PENDING"
    assert api.task(t["id"])["status"] == "WAITING_APPROVAL"

    r = api.c.post(f"/api/v1/approvals/{ap['id']}/deny", json={"note": "no thanks"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "DENIED"
    done = api.wait_done(t["id"])
    assert victim.exists() and victim.read_text() == "precious", "denied action must not happen"
    assert done["status"] == "FAILED"
    types = [e["type"] for e in api.c.get(f"/api/v1/tasks/{t['id']}/events", headers=AUTH).json()]
    assert (
        "approval_required" in types
        and "approval_denied" in types
        and "approval_granted" not in types
    )
    stored = api.c.get(f"/api/v1/tasks/{t['id']}/approvals", headers=AUTH).json()
    assert stored[0]["status"] == "DENIED" and stored[0]["decision_note"] == "no thanks"
    # a second decision on the same request is refused
    assert api.c.post(
        f"/api/v1/approvals/{ap['id']}/approve", json={}, headers=AUTH
    ).status_code in (404, 422)


def test_approve_executes_the_action(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    trashed: list[str] = []
    monkeypatch.setattr("send2trash.send2trash", lambda p: (trashed.append(p), Path(p).unlink())[0])
    victim = api.root / "Desktop" / "old.txt"
    victim.write_text("x")
    api.llm.push(structured(delete_plan()))
    t = api.create("Delete old.txt")
    ap = api.pending_approvals()[0]
    assert (
        api.c.post(
            f"/api/v1/approvals/{ap['id']}/approve", json={"scope": "once"}, headers=AUTH
        ).json()["status"]
        == "APPROVED"
    )
    done = api.wait_done(t["id"])
    assert done["status"] == "COMPLETED" and not victim.exists() and trashed == [str(victim)]


# ----------------------------------------------------------------------------- emergency stop
def test_emergency_stop_cancels_running_task_and_blocks_new_ones(api: Api) -> None:
    plan = {
        "summary": "run a slow script",
        "steps": [
            {
                "id": "s1",
                "title": "Sleep",
                "tool": "terminal_execute_safe",
                "args": {"command": 'python -c "import time; time.sleep(60)"'},
                "rationale": "demo",
            }
        ],
    }
    api.llm.push(structured(plan))
    t = api.create("run the slow thing")
    ap = api.pending_approvals()[0]  # python -c needs approval (arbitrary code)
    api.c.post(f"/api/v1/approvals/{ap['id']}/approve", json={}, headers=AUTH)
    api.wait(t["id"], lambda d: d["status"] == "EXECUTING")
    time.sleep(0.5)
    started = time.time()
    r = api.c.post("/api/v1/agent/stop", headers=AUTH)
    assert r.json()["kill_switch_engaged"] is True and t["id"] in r.json()["stopped_tasks"]
    done = api.wait_done(t["id"])
    assert done["status"] == "CANCELLED" and time.time() - started < 8
    # new work is refused until released
    blocked = api.c.post("/api/v1/tasks", json={"instruction": "again"}, headers=AUTH)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "kill_switch_active"
    assert api.c.get("/api/v1/agent/status", headers=AUTH).json()["kill_switch_engaged"] is True
    api.c.post("/api/v1/agent/release", headers=AUTH)
    api.llm.push(structured(fs_plan(api.root)))
    assert (
        api.c.post("/api/v1/tasks", json={"instruction": "again"}, headers=AUTH).status_code == 201
    )


def test_cancel_task_while_waiting_for_approval(api: Api) -> None:
    (api.root / "Desktop" / "old.txt").write_text("x")
    api.llm.push(structured(delete_plan()))
    t = api.create("delete")
    api.pending_approvals()
    assert api.c.post(f"/api/v1/tasks/{t['id']}/cancel", headers=AUTH).status_code == 202
    done = api.wait_done(t["id"])
    assert done["status"] == "CANCELLED" and (api.root / "Desktop" / "old.txt").exists()
    assert (
        api.c.post(f"/api/v1/tasks/{t['id']}/cancel", headers=AUTH).status_code == 422
    )  # already terminal


# ----------------------------------------------------------------------------- permissions
def test_permissions_roundtrip_and_guardrails(api: Api) -> None:
    pol = api.c.get("/api/v1/permissions", headers=AUTH).json()
    assert pol["risk_actions"]["3"] == "ask"
    pol["risk_actions"]["4"] = "allow"  # attempt to auto-allow CRITICAL
    pol["tool_overrides"] = (
        {"browser_open": "deny"}
        if "browser_open" in {t["name"] for t in api.c.get("/api/v1/tools", headers=AUTH).json()}
        else {}
    )
    updated = api.c.put("/api/v1/permissions", json=pol, headers=AUTH).json()
    assert updated["risk_actions"]["4"] == "ask", "CRITICAL can never be auto-allowed"
    assert updated["version"] == pol["version"] + 1
    bad = dict(pol, tool_overrides={"no_such_tool": "deny"})
    assert api.c.put("/api/v1/permissions", json=bad, headers=AUTH).status_code == 422


# ----------------------------------------------------------------------------- websocket
def test_websocket_replays_and_streams_events(api: Api) -> None:
    api.llm.push(structured(fs_plan(api.root)))
    t = api.create("make folder")
    api.wait_done(t["id"])
    with api.c.websocket_connect(
        f"/api/v1/ws/tasks/{t['id']}", subprotocols=["saa.v1", f"bearer.{TOKEN}"]
    ) as ws:
        seen = []
        while True:
            ev = ws.receive_json()
            seen.append(ev)
            if ev["type"] == "task_completed":
                break
    assert seen[0]["type"] == "task_created" and [e["seq"] for e in seen] == sorted(
        e["seq"] for e in seen
    )
    with api.c.websocket_connect(
        f"/api/v1/ws/tasks/{t['id']}?after_seq={seen[-2]['seq']}&token={TOKEN}"
    ) as ws:
        assert (
            ws.receive_json()["seq"] == seen[-1]["seq"]
        )  # resumed exactly after the last seen event


def test_websocket_rejects_bad_token_and_foreign_origin(api: Api) -> None:
    for kwargs in (
        {"subprotocols": ["saa.v1", "bearer.wrong"]},
        {
            "subprotocols": ["saa.v1", f"bearer.{TOKEN}"],
            "headers": {"origin": "https://evil.example"},
        },
    ):
        with (
            pytest.raises(WebSocketDisconnect),
            api.c.websocket_connect("/api/v1/ws/events", **kwargs) as ws,
        ):  # type: ignore[arg-type]
            ws.receive_json()


def test_websocket_live_stream_for_running_task(api: Api) -> None:
    (api.root / "Desktop" / "old.txt").write_text("x")
    api.llm.push(structured(delete_plan()))
    with api.c.websocket_connect(
        "/api/v1/ws/events", subprotocols=["saa.v1", f"bearer.{TOKEN}"]
    ) as ws:
        t = api.create("delete old")
        got: list[str] = []
        while "approval_required" not in got:
            got.append(ws.receive_json()["type"])
        ap = api.pending_approvals()[0]
        api.c.post(f"/api/v1/approvals/{ap['id']}/deny", json={}, headers=AUTH)
        while "task_failed" not in got:
            got.append(ws.receive_json()["type"])
    assert got.index("approval_required") < got.index("approval_denied") < got.index("task_failed")
    assert t["id"]


# ----------------------------------------------------------------------------- execution-loop regressions
def test_repeated_delivery_of_the_same_start_request_runs_one_task(api: Api) -> None:
    """POST retries / double clicks carry the same Idempotency-Key: exactly one task, one plan."""
    api.llm.push(structured(fs_plan(api.root)))
    hdr = {**AUTH, "Idempotency-Key": "click-1"}
    a = api.c.post("/api/v1/tasks", json={"instruction": "make folder"}, headers=hdr).json()
    b = api.c.post("/api/v1/tasks", json={"instruction": "make folder"}, headers=hdr).json()
    assert a["id"] == b["id"]
    api.wait_done(a["id"])
    c = api.c.post(
        "/api/v1/tasks", json={"instruction": "make folder"}, headers=hdr
    ).json()  # a late retry
    assert c["id"] == a["id"]
    assert api.c.get("/api/v1/tasks", headers=AUTH).json()["items"].__len__() == 1
    assert api.llm.purposes.count("plan") == 1


def test_websocket_reconnects_only_replay_state_and_never_execute(api: Api) -> None:
    api.llm.push(structured(fs_plan(api.root)))
    t = api.create("make folder")
    api.wait_done(t["id"])
    llm_calls = len(api.llm.requests)
    tools_before = api.c.get(f"/api/v1/tasks/{t['id']}/tool-calls", headers=AUTH).json()
    for _ in range(3):  # reconnect storm, from the start and from the middle
        for q in ("", "?after_seq=3"):
            with api.c.websocket_connect(
                f"/api/v1/ws/tasks/{t['id']}{q}", subprotocols=["saa.v1", f"bearer.{TOKEN}"]
            ) as ws:
                assert ws.receive_json()["task_id"] == t["id"]
    with api.c.websocket_connect("/api/v1/ws/events", subprotocols=["saa.v1", f"bearer.{TOKEN}"]):
        pass
    assert len(api.llm.requests) == llm_calls, "replay must not call the model"
    assert api.c.get(f"/api/v1/tasks/{t['id']}/tool-calls", headers=AUTH).json() == tools_before
    assert len(api.c.get("/api/v1/tasks", headers=AUTH).json()["items"]) == 1, (
        "no second task was created"
    )
