"""NEXT-B over HTTP: an interrupted task is shown with its options, and only an explicit request
resumes, restarts or cancels it."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.api.main import create_app
from app.config.settings import FilesystemSettings, LimitSettings, SecuritySettings, Settings
from app.container import build_container
from app.llm.scripted import ScriptedProvider, structured
from app.models.enums import StepStatus, TaskStatus
from app.models.plan import Plan, PlanStep
from app.models.recovery import DurableState
from app.models.task import Task
from app.models.tool import ToolCall
from app.models.verification import VerificationSpec
from app.perception.cache import call_key
from app.state.memory import InMemoryRepository
from app.state.repository import ToolCallRecord
from fastapi.testclient import TestClient

from tests.integration.test_api import AUTH, TOKEN, Api, fs_plan


def _settings(root: Path) -> Settings:
    desktop, downloads, documents, ws = (
        root / n for n in ("Desktop", "Downloads", "Documents", "workspace")
    )
    for d in (desktop, downloads, documents, ws):
        d.mkdir(exist_ok=True)
    return Settings(
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


async def _left_behind(repo: InMemoryRepository, root: Path) -> Task:
    """What a dead process left in storage: step 1 done, step 2 (a move) started, no result."""
    src, dst = root / "Documents" / "a.txt", root / "Documents" / "b.txt"
    src.write_text("data")
    task = Task(instruction="Rename a.txt to b.txt in my documents")
    task.status = TaskStatus.EXECUTING
    task.current_step_id = "s2"
    task.plan = Plan(
        task_id=task.id,
        steps=[
            PlanStep(id="s1", title="Look", tool="fs_list", args={"path": "@Documents"}, status=StepStatus.SUCCEEDED),
            PlanStep(id="s2", title="Rename", tool="fs_move", args={"src": str(src), "dst": str(dst)}, status=StepStatus.RUNNING),
        ],
        success_criteria=[
            VerificationSpec(kind="file_moved", params={"src": str(src), "dst": str(dst)})
        ],
    )  # fmt: skip
    task.durable = DurableState(
        step_data={"s1": {"data": {}, "artifacts": {}, "summary": "listed", "ok": True}}
    )
    await repo.save_task(task)
    args = {"src": str(src), "dst": str(dst)}
    call = ToolCall(
        tool="fs_move", args=args, task_id=task.id, step_id="s2", fingerprint=call_key("fs_move", args)
    )  # fmt: skip
    await repo.save_tool_call(task.id, ToolCallRecord(call=call, decision="executing"))
    return task


@pytest.fixture
def left(tmp_path: Path) -> Iterator[tuple[Api, str]]:
    import asyncio

    settings = _settings(tmp_path)
    repo = InMemoryRepository()
    task = asyncio.run(_left_behind(repo, tmp_path))
    llm = ScriptedProvider()
    container = build_container(
        settings, repo=repo, llm=llm, enable_browser=False, enable_desktop=False
    )
    with TestClient(create_app(container)) as client:  # startup runs the recovery
        yield Api(client, llm, tmp_path), task.id


def _recovery(api: Api, task_id: str) -> Any:
    return api.c.get(f"/api/v1/tasks/{task_id}/recovery", headers=AUTH)


def _recover(api: Api, task_id: str, action: str) -> Any:
    return api.c.post(f"/api/v1/tasks/{task_id}/recover", json={"action": action}, headers=AUTH)


def test_startup_marks_the_task_interrupted_and_explains_it(left: tuple[Api, str]) -> None:
    api, tid = left
    task = api.task(tid)
    assert task["status"] == "INTERRUPTED" and task["result"] is None
    r = _recovery(api, tid)
    assert r.status_code == 200, r.text
    body = r.json()
    found = body["interruption"]
    assert found["resumable"] is True and found["options"][0] == "resume"
    assert found["completed_steps"] == ["s1"] and found["remaining_steps"] == ["s2"]
    [action] = found["in_flight"]
    assert action["tool"] == "fs_move" and action["certainty"] == "confirmed_not_done"
    assert [s["id"] for s in body["report"]["completed"]] == ["s1"]
    assert [s["id"] for s in body["report"]["interrupted"]] == ["s2"]
    assert api.c.get("/api/v1/agent/status", headers=AUTH).json()["active_tasks"] == []
    types = [e["type"] for e in api.c.get(f"/api/v1/tasks/{tid}/events", headers=AUTH).json()]
    assert types == ["task_interrupted"]


def test_recovery_endpoints_require_the_token_and_an_interrupted_task(
    left: tuple[Api, str],
) -> None:
    api, tid = left
    assert api.c.get(f"/api/v1/tasks/{tid}/recovery").status_code == 401
    assert api.c.post(f"/api/v1/tasks/{tid}/recover", json={"action": "resume"}).status_code == 401
    assert _recover(api, tid, "explode").status_code == 422
    assert _recovery(api, "task_nope").status_code == 404
    api.llm.push(structured(fs_plan(api.root)))
    other = api.wait_done(api.create("Create a Reports folder on my desktop")["id"])
    for r in (_recovery(api, other["id"]), _recover(api, other["id"], "resume")):
        assert r.status_code == 422 and "only an interrupted task" in r.json()["error"]["message"]
    assert api.task(tid)["status"] == "INTERRUPTED", "nothing above touched the interrupted task"


def test_resume_continues_and_the_inspection_reflects_the_current_files(
    left: tuple[Api, str],
) -> None:
    api, tid = left
    docs = api.root / "Documents"
    (docs / "a.txt").rename(docs / "b.txt")  # the user finished the rename by hand meanwhile
    assert (
        _recovery(api, tid).json()["interruption"]["in_flight"][0]["certainty"] == "confirmed_done"
    )
    r = _recover(api, tid, "resume")
    assert r.status_code == 202, r.text
    done = api.wait_done(tid)
    assert done["status"] == "COMPLETED"
    assert (docs / "b.txt").read_text() == "data" and not (docs / "a.txt").exists()
    events = api.c.get(f"/api/v1/tasks/{tid}/events", headers=AUTH).json()
    started = [e for e in events if e["type"] == "action_started"]
    assert started == [], "the move was confirmed done: it was replayed, not executed"
    assert _recover(api, tid, "resume").status_code == 422, "a finished task cannot be resumed"


def test_cancel_and_restart_are_explicit_choices(left: tuple[Api, str]) -> None:
    api, tid = left
    api.llm.push(structured(fs_plan(api.root)))
    r = _recover(api, tid, "restart")
    assert r.status_code == 202, r.text
    new = r.json()
    assert new["id"] != tid and new["restarted_from"] == tid
    old = api.task(tid)
    assert old["status"] == "CANCELLED" and old["result"]["answer_status"] == "WITHHELD"
    assert "Restarted as a new task" in old["result"]["summary"]
    assert api.wait_done(new["id"])["status"] == "COMPLETED"
    assert (api.root / "Documents" / "a.txt").exists(), "the old task's unfinished move never ran"


def test_cancel_through_either_endpoint_ends_an_interrupted_task(left: tuple[Api, str]) -> None:
    api, tid = left
    r = _recover(api, tid, "cancel")
    assert r.status_code == 202 and r.json()["status"] == "CANCELLED"
    again = api.c.post(f"/api/v1/tasks/{tid}/cancel", headers=AUTH)
    assert again.status_code == 422  # already cancelled
    result = api.task(tid)["result"]
    assert result["verified"] is False and result["outcome"] == "CANCELLED"
    assert "not undone" in result["answer"]
