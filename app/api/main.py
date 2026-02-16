"""FastAPI application entry point.

Routers for /scrape, /stories, and /runs are registered here as they are
implemented. For now the application exposes a single /health endpoint so
the container health check and smoke tests have a stable target.

Application lifecycle:
  1. Startup: Connect to Temporal server, configure logging
  2. Runtime: Handle HTTP requests, trigger workflows via Temporal client
  3. Shutdown: Close Temporal client connection gracefully
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI
from temporalio.client import Client

from app.api.routers import runs_router, scrape_router, stories_router
from app.config import constants


def _configure_logging() -> None:
    """Configure structlog for structured JSON output.

    Sets up stdlib logging at the configured level so that third-party
    libraries (FastAPI, uvicorn, Temporal client) emit through the same
    pipeline as application code. All output is serialised as JSON to stdout.
    """
    log_level = getattr(logging, constants.LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        level=log_level,
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifecycle: startup and shutdown.

    Startup:
      - Configure structured logging
      - Connect to Temporal server
      - Store client in app.state for dependency injection

    Shutdown:
      - Close Temporal client connection gracefully

    The Temporal client is a singleton shared across all requests. This is
    the recommended pattern for production: connection pooling and reuse.
    """
    # Startup
    _configure_logging()

    log = structlog.get_logger().bind(
        service=constants.SERVICE_NAME,
        component="api",
    )

    log.info(
        "api.startup.connecting_temporal",
        temporal_address=constants.TEMPORAL_ADDRESS,
        namespace=constants.TEMPORAL_NAMESPACE,
    )

    try:
        client = await Client.connect(
            constants.TEMPORAL_ADDRESS,
            namespace=constants.TEMPORAL_NAMESPACE,
        )
        app.state.temporal_client = client

        log.info(
            "api.startup.complete",
            temporal_connected=True,
        )

    except Exception as exc:
        log.error(
            "api.startup.failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise

    # Application runs here (yield control to FastAPI)
    yield

    # Shutdown
    log.info("api.shutdown.closing_temporal_client")
    await client.close()
    log.info("api.shutdown.complete")


app = FastAPI(
    title="HackerNews Scraper",
    description="Production-grade HN scraping service.",
    version="0.1.0",
    lifespan=lifespan,
)

# Register routers
app.include_router(scrape_router)
app.include_router(stories_router)
app.include_router(runs_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": constants.SERVICE_NAME}
