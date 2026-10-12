"""NEXT-D over HTTP: a task's file journal is inspectable, and a rollback happens only on request."""

from __future__ import annotations

from app.llm.scripted import structured

from tests.integration.test_api import AUTH, Api, api, fs_plan  # noqa: F401  (api is a fixture)


def test_journal_and_rollback_of_a_finished_task(api: Api) -> None:  # noqa: F811
    api.llm.push(structured(fs_plan(api.root)))
    t = api.create("make a Reports folder on the desktop")
    assert api.wait_done(t["id"])["status"] == "COMPLETED"
    folder = api.root / "Desktop" / "Reports"
    assert folder.is_dir()

    view = api.c.get(f"/api/v1/tasks/{t['id']}/journal", headers=AUTH).json()
    assert view["can_roll_back"] and view["open"] == 1
    [entry] = view["entries"]
    assert (
        entry["op"] == "create_dir" and entry["state"] == "applied" and entry["path"] == str(folder)
    )

    r = api.c.post(f"/api/v1/tasks/{t['id']}/rollback", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["view"]["counts"] == {"undone": 1} and body["errors"] == [] and not folder.exists()
    assert api.task(t["id"])["status"] == "COMPLETED", (
        "a rollback does not rewrite the task's outcome"
    )

    again = api.c.post(f"/api/v1/tasks/{t['id']}/rollback", headers=AUTH)  # idempotent
    assert again.status_code == 200 and again.json()["attempted"] == 0
    assert again.json()["view"]["reason"] == "nothing left to undo"


def test_rollback_needs_the_token_and_an_existing_task(api: Api) -> None:  # noqa: F811
    assert api.c.post("/api/v1/tasks/task_nope/rollback").status_code == 401
    assert api.c.post("/api/v1/tasks/task_nope/rollback", headers=AUTH).status_code == 404
    assert api.c.get("/api/v1/tasks/task_nope/journal", headers=AUTH).status_code == 404
