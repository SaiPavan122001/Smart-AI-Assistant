from __future__ import annotations

from app.llm.scripted import structured
from app.models.enums import TaskStatus

from tests.helpers import Env, make_harness


def plan_draft() -> dict:
    return {
        "summary": "Create a folder, write a note, move it into the folder",
        "steps": [
            {
                "id": "s1",
                "title": "Create folder",
                "tool": "fs_create",
                "args": {"path": "@Desktop/Spring Docs", "kind": "dir"},
                "rationale": "Folder requested by the user",
                "verify": [{"kind": "dir_exists", "params": {"path": "@Desktop/Spring Docs"}}],
            },
            {
                "id": "s2",
                "title": "Write note",
                "tool": "fs_create",
                "args": {"path": "@Downloads/note.txt", "content": "hello"},
                "rationale": "Create the file to move",
            },
            {
                "id": "s3",
                "title": "Move note into folder",
                "tool": "fs_move",
                "depends_on": ["s1", "s2"],
                "args": {"src": "{{steps.s2.artifacts.path}}", "dst": "@Desktop/Spring Docs"},
                "rationale": "Move the file as requested",
            },
        ],
        "success_criteria": [
            {
                "kind": "file_moved",
                "params": {"src": "@Downloads/note.txt", "dst": "@Desktop/Spring Docs/note.txt"},
            }
        ],
    }


async def test_create_move_verify(env: Env) -> None:
    h = make_harness(env, [structured(plan_draft())])
    task = await h.run("Create a folder Spring Docs on my desktop and move note.txt there")
    assert task.status == TaskStatus.COMPLETED, task.result
    assert (env.desktop / "Spring Docs" / "note.txt").read_text() == "hello"
    assert not (env.downloads / "note.txt").exists()
    assert task.result and task.result.verified
    assert h.llm.purposes == ["plan"], "deterministic steps must not call the LLM"
    ev = await h.events(task.id)
    for needed in (
        "task_created",
        "planning_started",
        "plan_created",
        "action_completed",
        "verification_completed",
        "task_completed",
    ):
        assert needed in ev
    assert task.usage.llm_calls == 1
