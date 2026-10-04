"""Alembic environment (async engine; URL from application settings)."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config.settings import get_settings
from app.database.models import Base
from app.database.session import create_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _engine() -> AsyncEngine:
    override = os.environ.get("SAA_MIGRATION_URL")
    settings = get_settings()
    if override:
        settings = settings.model_copy(deep=True)
        settings.database.url = override
    return create_engine(settings.database)


def run_migrations_offline() -> None:
    url = os.environ.get("SAA_MIGRATION_URL") or get_settings().database.url
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async() -> None:
    engine = _engine()
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    # Allow programmatic callers (tests, startup) that already run inside an event loop.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run_async())
    else:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(lambda: asyncio.run(run_async())).result()
