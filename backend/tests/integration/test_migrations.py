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

from tests.integration.conftest import BACKEND, migrate


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
