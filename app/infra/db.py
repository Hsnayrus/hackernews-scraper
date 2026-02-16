"""Database infrastructure.

Exposes:
  - metadata:        SQLAlchemy MetaData instance shared across all table
                     definitions and Alembic autogenerate.
  - get_engine():    Returns the singleton async engine (lazy init).
  - get_connection(): Async context manager that yields a transactional
                     AsyncConnection from the engine pool.

Usage in repositories:
    async with get_connection() as conn:
        result = await conn.execute(stmt)
        # Connection is committed and returned to pool on clean exit.
        # Rolled back automatically on exception.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

# Shared MetaData instance. All SQLAlchemy Table objects must be constructed
# with this metadata so that Alembic's autogenerate can discover them.
metadata: MetaData = MetaData()

_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    """Return the singleton async engine, creating it on first call.

    The engine manages the connection pool. It is intentionally a module-level
    singleton — creating a new engine per request would bypass pooling.

    constants is imported here (not at module level) so that importing this
    module for its `metadata` object alone — e.g. from Alembic env.py in an
    init container — does not trigger the full env-var validation in constants.
    """
    from app.config import constants  # lazy import — see docstring

    global _engine
    if _engine is None:
        _engine = create_async_engine(
            constants.DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
            # Conservative pool size for a single-worker deployment.
            # Increase if multiple concurrent activities need DB access.
            pool_size=5,
            max_overflow=10,
        )
    return _engine


@asynccontextmanager
async def get_connection() -> AsyncGenerator[AsyncConnection, None]:
    """Yield a transactional AsyncConnection from the engine pool.

    The connection is automatically committed on clean exit and rolled back
    if an exception is raised, then returned to the pool in both cases.

    Usage:
        async with get_connection() as conn:
            await conn.execute(insert_stmt)
            # auto-committed here

    Raises:
        Any SQLAlchemy exception propagated from the driver. Callers
        (repositories) are responsible for mapping these to domain exceptions.
    """
    async with get_engine().begin() as conn:
        yield conn
