from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from app.config.settings import DatabaseSettings
from app.database.repository import SqlRepository
from app.database.session import create_engine, create_session_factory
from app.state.memory import InMemoryRepository
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND = Path(__file__).resolve().parents[2]


def migrate(url: str, revision: str = "head") -> None:
    """Run the real Alembic migrations (the same path production uses)."""
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "app" / "database" / "migrations"))
    old = os.environ.get("SAA_MIGRATION_URL")
    os.environ["SAA_MIGRATION_URL"] = url
    try:
        command.upgrade(cfg, revision)
    finally:
        if old is None:
            os.environ.pop("SAA_MIGRATION_URL", None)
        else:
            os.environ["SAA_MIGRATION_URL"] = old


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str | None]:
    """A real PostgreSQL via the embedded `pgserver` package (skipped when unavailable)."""
    try:
        import pgserver
    except ImportError:
        yield None
        return
    data = tempfile.mkdtemp(prefix="saa-pg-")
    try:
        server = pgserver.get_server(data, cleanup_mode="stop")
        uri = server.get_uri()
    except Exception:
        yield None
        return
    try:
        yield uri.replace("postgresql://", "postgresql+asyncpg://", 1)
    finally:
        server.cleanup()


@pytest.fixture(params=["memory", "sqlite", "postgres"])
async def repo(
    request: pytest.FixtureRequest, tmp_path: Path, pg_url: str | None
) -> AsyncIterator[Any]:
    kind = request.param
    if kind == "memory":
        yield InMemoryRepository()
        return
    if kind == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'saa.db'}"
    else:
        if pg_url is None:
            pytest.skip("embedded PostgreSQL unavailable")
        url = pg_url
    await asyncio.to_thread(migrate, url)
    engine = create_engine(DatabaseSettings(url=url))
    repository = SqlRepository(engine, create_session_factory(engine))
    try:
        yield repository
    finally:
        await repository.close()


async def create_database(pg_url: str, name: str) -> str:
    """A new, empty database on the embedded PostgreSQL server; returns its URL."""
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()
    return make_url(pg_url).set(database=name).render_as_string(hide_password=False)


@pytest.fixture(params=["memory", "sqlite", "postgres"])
async def own_repo(
    request: pytest.FixtureRequest, tmp_path: Path, pg_url: str | None
) -> AsyncIterator[Any]:
    """Like ``repo``, but PostgreSQL gets a database of its own: for tests that scan every task
    (startup recovery) or leave unfinished tasks behind, which must not leak into other tests."""
    kind = request.param
    if kind == "memory":
        yield InMemoryRepository()
        return
    if kind == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'own.db'}"
    else:
        if pg_url is None:
            pytest.skip("embedded PostgreSQL unavailable")
        url = await create_database(pg_url, f"saa_{os.urandom(6).hex()}")
    await asyncio.to_thread(migrate, url)
    engine = create_engine(DatabaseSettings(url=url))
    repository = SqlRepository(engine, create_session_factory(engine))
    try:
        yield repository
    finally:
        await repository.close()
