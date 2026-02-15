"""Database infrastructure stub.

Exposes:
  - metadata: SQLAlchemy MetaData instance shared across all ORM table definitions.
  - engine:   Async SQLAlchemy engine (initialised lazily via get_engine()).

Table definitions are registered against `metadata` in app/domain/ as the
domain layer is implemented.
"""

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import constants

# Shared MetaData instance. All SQLAlchemy Table objects must be constructed
# with this metadata so that Alembic's autogenerate can discover them.
metadata: MetaData = MetaData()

_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    """Return the singleton async engine, creating it on first call."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            constants.DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
        )
    return _engine
