"""Database persistence activities (STUB IMPLEMENTATION).

These activities handle all database writes for the scraping workflow:
    - Creating scrape run records
    - Upserting stories
    - Updating scrape run status

IMPORTANT: This is a stub implementation for testing the workflow.
The activities log their inputs but do not actually persist to the database.
Replace this with real database repository calls once the infra layer is ready.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog
from temporalio import activity

from app.config import constants
from app.domain.models import ScrapeRun, ScrapeRunStatus, Story


# ---------------------------------------------------------------------------
# Stub Activities
# ---------------------------------------------------------------------------


class PersistenceActivities:
    """Temporal activity class for database persistence operations.

    STUB: These methods currently just log and return mock data.
    Replace with real database repository calls in production.
    """

    @activity.defn(name="create_scrape_run_activity")
    async def create_scrape_run_activity(self, workflow_id: str) -> ScrapeRun:
        """Create a new scrape run record with status=PENDING.

        Args:
            workflow_id: Temporal workflow execution ID.

        Returns:
            Newly created ScrapeRun record.

        STUB: Returns a mock ScrapeRun without persisting to database.
        """
        info = activity.info()
        log = structlog.get_logger().bind(
            service=constants.SERVICE_NAME,
            activity_name=info.activity_type,
            workflow_id=info.workflow_id,
            run_id=info.workflow_run_id,
            activity_id=info.activity_id,
        )

        log.info(
            "persistence.create_run.starting",
            status="starting",
            target_workflow_id=workflow_id,
        )
        started_at = time.monotonic()

        # STUB: Create mock scrape run record
        scrape_run = ScrapeRun(
            id=uuid.uuid4(),
            workflow_id=workflow_id,
            started_at=datetime.now(tz=timezone.utc),
            finished_at=None,
            status=ScrapeRunStatus.PENDING,
            stories_scraped=None,
            error_message=None,
        )

        duration_ms = int((time.monotonic() - started_at) * 1000)
        log.info(
            "persistence.create_run.completed",
            status="completed",
            run_id=str(scrape_run.id),
            run_status=scrape_run.status.value,
            duration_ms=duration_ms,
        )

        # TODO: Replace with actual database insert via repository
        # scrape_run = await scrape_run_repository.create(scrape_run)

        return scrape_run

    @activity.defn(name="upsert_stories_activity")
    async def upsert_stories_activity(self, stories: list[Story]) -> int:
        """Upsert a list of stories into the database.

        Args:
            stories: List of Story domain models to persist.

        Returns:
            Number of stories upserted (inserted or updated).

        STUB: Logs the stories but does not persist to database.
        """
        info = activity.info()
        log = structlog.get_logger().bind(
            service=constants.SERVICE_NAME,
            activity_name=info.activity_type,
            workflow_id=info.workflow_id,
            run_id=info.workflow_run_id,
            activity_id=info.activity_id,
        )

        log.info(
            "persistence.upsert_stories.starting",
            status="starting",
            stories_count=len(stories),
        )
        started_at = time.monotonic()

        # STUB: Log story details
        for story in stories[:3]:  # Log first 3 for brevity
            log.info(
                "persistence.story_sample",
                hn_id=story.hn_id,
                rank=story.rank,
                title=story.title[:50] + "..." if len(story.title) > 50 else story.title,
                points=story.points,
                author=story.author,
                comments_count=story.comments_count,
            )

        upserted_count = len(stories)

        duration_ms = int((time.monotonic() - started_at) * 1000)
        log.info(
            "persistence.upsert_stories.completed",
            status="completed",
            upserted_count=upserted_count,
            duration_ms=duration_ms,
        )

        # TODO: Replace with actual database upsert via repository
        # upserted_count = await story_repository.upsert_many(stories)

        return upserted_count

    @activity.defn(name="update_scrape_run_activity")
    async def update_scrape_run_activity(
        self,
        run_id: uuid.UUID,
        status: str,
        stories_scraped: Optional[int],
        error_message: Optional[str],
    ) -> ScrapeRun:
        """Update a scrape run record with final status and metadata.

        Args:
            run_id: UUID of the scrape run to update.
            status: Final status (COMPLETED or FAILED).
            stories_scraped: Number of stories successfully persisted (None if failed).
            error_message: Error description (None if succeeded).

        Returns:
            Updated ScrapeRun record.

        STUB: Returns a mock updated ScrapeRun without persisting to database.
        """
        info = activity.info()
        log = structlog.get_logger().bind(
            service=constants.SERVICE_NAME,
            activity_name=info.activity_type,
            workflow_id=info.workflow_id,
            run_id=info.workflow_run_id,
            activity_id=info.activity_id,
        )

        log.info(
            "persistence.update_run.starting",
            status="starting",
            target_run_id=str(run_id),
            target_status=status,
            stories_scraped=stories_scraped,
        )
        started_at = time.monotonic()

        # STUB: Create mock updated scrape run record
        scrape_run = ScrapeRun(
            id=run_id,
            workflow_id=info.workflow_id,
            started_at=datetime.now(tz=timezone.utc),  # Would come from DB
            finished_at=datetime.now(tz=timezone.utc),
            status=ScrapeRunStatus(status),
            stories_scraped=stories_scraped,
            error_message=error_message,
        )

        duration_ms = int((time.monotonic() - started_at) * 1000)
        log.info(
            "persistence.update_run.completed",
            status="completed",
            run_id=str(scrape_run.id),
            run_status=scrape_run.status.value,
            duration_ms=duration_ms,
        )

        # TODO: Replace with actual database update via repository
        # scrape_run = await scrape_run_repository.update(
        #     run_id=run_id,
        #     finished_at=datetime.now(tz=timezone.utc),
        #     status=ScrapeRunStatus(status),
        #     stories_scraped=stories_scraped,
        #     error_message=error_message,
        # )

        return scrape_run
