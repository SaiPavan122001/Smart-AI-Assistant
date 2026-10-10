"""A REAL process that starts a task and is then killed by the test (``proc.kill()``).

    python -m tests.crash_driver <database url> <root folder> model|tool

``model``: the task hangs in a model call after three file steps completed.
``tool``:  the task hangs inside ``fs_move`` right after the file was moved, before its result is stored.

It prints ``TASK <id>`` and then ``READY`` when it has reached that point. It never exits by itself.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from app.config.settings import DatabaseSettings
from app.database.repository import SqlRepository
from app.database.session import create_engine, create_session_factory
from app.llm.scripted import structured

from tests.helpers import make_env
from tests.restart_helpers import MOVE, boot, move_plan, use_repo


def _hang(*_: Any) -> Any:
    print("READY", flush=True)
    time.sleep(3600)


async def main(url: str, root: str, where: str) -> None:
    env = make_env(Path(root))
    engine = create_engine(DatabaseSettings(url=url))
    use_repo(env, SqlRepository(engine, create_session_factory(engine)))
    plan = move_plan()
    if where == "model":
        plan["steps"].append(
            {
                "id": "s4", "title": "Confirm", "kind": "adaptive", "depends_on": ["s3"],
                "rationale": "wrap up", "goal": "Confirm the note is in the folder",
                "allowed_tools": ["fs_list"],
            }
        )  # fmt: skip
    else:
        tool = env.router.registry.get("fs_move")
        assert tool is not None
        original = tool.run

        async def moved_then_hang(args: Any, ctx: Any) -> Any:
            await original(args, ctx)
            _hang()

        tool.run = moved_then_hang  # type: ignore[method-assign]
    h = await boot(env, [structured(plan), _hang])
    task = await h.runtime.submit(MOVE)
    print(f"TASK {task.id}", flush=True)
    await h.runtime.wait(task.id, 3600)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
