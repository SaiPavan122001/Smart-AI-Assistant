"""Async engine/session factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config.settings import DatabaseSettings


def is_memory_url(url: str) -> bool:
    return url.startswith("memory://")


def create_engine(cfg: DatabaseSettings) -> AsyncEngine:
    url = cfg.url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    kwargs: dict[str, object] = {"echo": cfg.echo}
    if url.startswith("sqlite") and ":memory:" in url:
        kwargs.update(poolclass=StaticPool, connect_args={"check_same_thread": False})
    elif url.startswith("postgresql"):
        kwargs.update(pool_size=10, max_overflow=10, pool_pre_ping=True)
    return create_async_engine(url, **kwargs)  # type: ignore[arg-type]


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
