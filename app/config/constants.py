"""Constants module.

All configuration values are sourced exclusively from environment variables.
This module is the single gateway between the environment and the codebase:

    Environment variables
            │
            ▼
    app.config.constants     ← os.environ["KEY"]
            │
            ▼
    All other modules        ← import from app.config.constants

Rules:
- No module outside this file may call os.environ directly.
- os.environ["KEY"] is used (not .get) so that a missing variable raises
  KeyError at import time, causing a hard startup failure rather than a
  silent runtime error.
"""

import os

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DB_HOST: str = os.environ["DB_HOST"]
DB_PORT: str = os.environ["DB_PORT"]
DB_NAME: str = os.environ["DB_NAME"]
DB_USER: str = os.environ["DB_USER"]
DB_PASSWORD: str = os.environ["DB_PASSWORD"]

# Async SQLAlchemy URL (asyncpg driver) — used by the application at runtime.
DATABASE_URL: str = (
    f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# Synchronous SQLAlchemy URL (psycopg2 driver) — used by Alembic migrations only.
DATABASE_SYNC_URL: str = (
    f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# ---------------------------------------------------------------------------
# Temporal
# ---------------------------------------------------------------------------

TEMPORAL_HOST: str = os.environ["TEMPORAL_HOST"]
TEMPORAL_PORT: str = os.environ["TEMPORAL_PORT"]

# Convenience: combined address string expected by the Temporal SDK client.
TEMPORAL_ADDRESS: str = f"{TEMPORAL_HOST}:{TEMPORAL_PORT}"

TEMPORAL_NAMESPACE: str = os.environ["TEMPORAL_NAMESPACE"]
TEMPORAL_TASK_QUEUE: str = os.environ["TEMPORAL_TASK_QUEUE"]

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

SERVICE_NAME: str = os.environ["SERVICE_NAME"]
LOG_LEVEL: str = os.environ["LOG_LEVEL"]

# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

HN_BASE_URL: str = os.environ["HN_BASE_URL"]
SCRAPE_TOP_N: int = int(os.environ["SCRAPE_TOP_N"])
