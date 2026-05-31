"""Async SQLAlchemy engine + session.

The engine URL comes from settings, so the same code runs on SQLite (default,
single file) or Postgres (set DATABASE_URL). Models call create_all on startup;
swap in Alembic when the schema starts to evolve in production.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
engine = create_async_engine(_settings.database_url, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


# Columns added after the initial release. create_all only creates *missing
# tables*, not missing columns, so we ALTER existing tables best-effort. This is
# a stopgap until the schema warrants Alembic; ADD COLUMN is cheap and safe on
# both SQLite and Postgres, and a duplicate-column error just means it's already
# there.
_ADDED_COLUMNS = (
    ("api_keys", "rate_limit_per_min", "INTEGER"),
)


async def init_db() -> None:
    # Import models so they register on Base.metadata before create_all.
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    for table, column, coltype in _ADDED_COLUMNS:
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                )
        except Exception:
            pass  # column already exists


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
