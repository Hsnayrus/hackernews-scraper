"""Alembic migration environment.

DATABASE_SYNC_URL is constructed directly from the DB-specific environment
variables (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD).

Intentionally does NOT import app.config.constants — that module requires all
application env vars (including Temporal config) to be present at import time,
which is not the case when running migrations in an isolated init container.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.infra.db import metadata

DATABASE_SYNC_URL: str = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)

# Import tables module so Table() constructors execute and register against
# metadata. Without this import, metadata is empty at autogenerate time.
import app.infra.tables  # noqa: F401, E402

alembic_config = context.config

if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

# MetaData is shared with all SQLAlchemy Table definitions so that
# autogenerate can detect schema changes automatically.
target_metadata = metadata


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (generates SQL script)."""
    context.configure(
        url=DATABASE_SYNC_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection."""
    connectable = engine_from_config(
        {"sqlalchemy.url": DATABASE_SYNC_URL},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
