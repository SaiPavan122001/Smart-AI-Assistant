"""Migration validation: Alembic head must exactly match the ORM models, and be reversible."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from app.database.models import Base
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import BACKEND, create_database, migrate


async def _drift(url: str) -> list[object]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:

            def diff(sync_conn):  # type: ignore[no-untyped-def]
                ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
                return compare_metadata(ctx, Base.metadata)

            return await conn.run_sync(diff)
    finally:
        await engine.dispose()


async def test_sqlite_migration_matches_models(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    await asyncio.to_thread(migrate, url)
    assert await _drift(url) == []


async def test_postgres_migration_matches_models(pg_url: str | None) -> None:
    if pg_url is None:
        pytest.skip("embedded PostgreSQL unavailable")
    await asyncio.to_thread(migrate, pg_url)
    assert await _drift(pg_url) == []


async def test_migration_is_reversible(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"
    await asyncio.to_thread(migrate, url)

    def down() -> None:
        cfg = Config(str(BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND / "app" / "database" / "migrations"))
        os.environ["SAA_MIGRATION_URL"] = url
        try:
            command.downgrade(cfg, "base")
        finally:
            os.environ.pop("SAA_MIGRATION_URL", None)

    await asyncio.to_thread(down)
    assert await _drift(url) != []  # tables are gone again


_TS = "2026-10-01 10:00:00+00"
_OLD_ROWS = (
    f"INSERT INTO users (id, name, created_at) VALUES ('local', 'local', '{_TS}')",
    "INSERT INTO tasks (id, user_id, instruction, status, paused, usage, created_at, updated_at) "
    f"VALUES ('task_old', 'local', 'made before 0002', 'EXECUTING', false, '{{}}', '{_TS}', '{_TS}')",
    "INSERT INTO plans (id, task_id, version, summary, assumptions, success_criteria, created_at) "
    f"VALUES ('plan_old', 'task_old', 1, 'old plan', '[]', '[]', '{_TS}')",
    "INSERT INTO tool_calls (id, task_id, tool, args, rationale, decision, decision_reason, duration_ms, created_at) "
    f"VALUES ('tc_old', 'task_old', 'fs_move', '{{}}', '', 'allow', '', 0, '{_TS}')",
)


async def _upgrade_with_existing_rows(url: str) -> None:
    """0001 -> 0002 on a database that already holds an unfinished task: nothing is lost and the new
    columns get safe defaults."""
    from app.config.settings import DatabaseSettings
    from app.database.repository import SqlRepository
    from app.database.session import create_engine, create_session_factory
    from app.models.enums import TaskStatus
    from sqlalchemy import text

    await asyncio.to_thread(migrate, url, "0001")
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        for stmt in _OLD_ROWS:
            await conn.execute(text(stmt))
    await engine.dispose()

    await asyncio.to_thread(migrate, url)  # head
    assert await _drift(url) == []
    eng = create_engine(DatabaseSettings(url=url))
    repo = SqlRepository(eng, create_session_factory(eng))
    try:
        task = await repo.get_task("task_old")
        assert task is not None and task.status is TaskStatus.EXECUTING
        assert task.cancel_requested is False and task.durable is None and task.interruption is None
        assert (
            task.plan is not None and task.plan.goal is None and task.plan.answer_template is None
        )
        [call] = await repo.list_tool_calls("task_old")
        assert call.call.fingerprint == ""
    finally:
        await repo.close()


async def test_sqlite_upgrade_keeps_existing_rows(tmp_path: Path) -> None:
    await _upgrade_with_existing_rows(f"sqlite+aiosqlite:///{tmp_path / 'up.db'}")


async def test_postgres_upgrade_keeps_existing_rows(pg_url: str | None) -> None:
    if pg_url is None:
        pytest.skip("embedded PostgreSQL unavailable")
    await _upgrade_with_existing_rows(
        await create_database(pg_url, f"saa_up_{os.urandom(4).hex()}")
    )


async def test_a_task_from_before_the_migration_is_not_offered_resume(tmp_path: Path) -> None:
    """It has a plan but no stored step outputs: restart or cancel, never a guessed resume."""
    from app.config.settings import DatabaseSettings
    from app.database.repository import SqlRepository
    from app.database.session import create_engine, create_session_factory
    from app.models.enums import TaskStatus
    from app.models.recovery import RecoveryOption

    from tests.helpers import make_env
    from tests.restart_helpers import boot, use_repo

    url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
    await _upgrade_with_existing_rows(url)
    eng = create_engine(DatabaseSettings(url=url))
    repo = SqlRepository(eng, create_session_factory(eng))
    try:
        env = use_repo(make_env(tmp_path / "fs"), repo)
        await boot(env)
        task = await repo.get_task("task_old")
        assert task is not None and task.status is TaskStatus.INTERRUPTED and task.interruption
        assert not task.interruption.resumable
        assert task.interruption.options == [
            RecoveryOption.RESTART,
            RecoveryOption.CANCEL,
            RecoveryOption.INSPECT,
        ]
    finally:
        await repo.close()
