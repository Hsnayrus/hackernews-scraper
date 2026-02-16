"""Temporal worker entry point.

Invoked as:  python -m app.worker

Configures structured JSON logging, connects to the Temporal server, registers
all activity and workflow implementations, and begins polling the task queue.
"""

import asyncio
import logging

import structlog
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from app.activities.browser import BrowserActivities
from app.activities.persistence import PersistenceActivities
from app.config import constants
from app.workflows.scraper import ScrapeHackerNewsWorkflow


def _configure_logging() -> None:
    """Configure structlog for structured JSON output.

    Sets up stdlib logging at the configured level so that third-party
    libraries (Temporal SDK, uvicorn, asyncpg) emit through the same pipeline
    as application code. All output is serialised as JSON to stdout.
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


async def main() -> None:
    _configure_logging()

    log = structlog.get_logger().bind(
        service=constants.SERVICE_NAME,
        component="worker",
        task_queue=constants.TEMPORAL_TASK_QUEUE,
    )

    log.info(
        "worker.connecting",
        temporal_address=constants.TEMPORAL_ADDRESS,
        namespace=constants.TEMPORAL_NAMESPACE,
    )

    client = await Client.connect(
        constants.TEMPORAL_ADDRESS,
        namespace=constants.TEMPORAL_NAMESPACE,
        data_converter=pydantic_data_converter,
    )

    browser_activities = BrowserActivities()
    persistence_activities = PersistenceActivities()

    log.info("worker.starting")

    worker = Worker(
        client,
        task_queue=constants.TEMPORAL_TASK_QUEUE,
        workflows=[ScrapeHackerNewsWorkflow],
        activities=[
            # Browser activities
            browser_activities.start_playwright_activity,
            browser_activities.navigate_to_hacker_news_activity,
            browser_activities.scrape_urls_activity,
            # Database persistence activities
            persistence_activities.create_scrape_run_activity,
            persistence_activities.upsert_stories_activity,
            persistence_activities.update_scrape_run_activity,
        ],
    )

    log.info("worker.polling")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
