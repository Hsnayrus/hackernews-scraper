"""Database persistence activities.

These activities handle all database writes for the scraping workflow:
    - Creating scrape run records (create_scrape_run_activity)
    - Upserting scraped stories (upsert_stories_activity)
    - Updating scrape run final status (update_scrape_run_activity)

Each activity:
  - Has an explicit start_to_close_timeout set in the workflow.
  - Has a RetryPolicy (DB_RETRY_POLICY) set in the workflow.
  - Is idempotent: safe to re-execute on Temporal retry.
  - Maps SQLAlchemy / asyncpg infrastructure errors to domain exceptions
    so that Temporal retry classification is driven by domain types.
  - Wraps non-retryable domain errors in ApplicationError(non_retryable=True)
    to prevent exhausting retry budget on unrecoverable failures.

Error classification:
    PersistenceTransientError  → retryable  (connection issues, deadlocks)
    PersistenceValidationError → non-retryable (row not found, constraint bug)
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy.exc
import structlog
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import constants
from app.domain.exceptions import PersistenceTransientError, PersistenceValidationError
from app.domain.models import ScrapeRun, ScrapeRunStatus, Story
from app.infra.repositories import ScrapeRunRepository, StoryRepository


def _classify_sqlalchemy_error(
    exc: sqlalchemy.exc.SQLAlchemyError,
) -> PersistenceTransientError | PersistenceValidationError:
    """Map a SQLAlchemy exception to a domain persistence exception.

    Classification:
        IntegrityError  → PersistenceValidationError (non-retryable: constraint bug)
        OperationalError → PersistenceTransientError (retryable: connection / deadlock)
        All others       → PersistenceTransientError (conservative: retry is safe)
    """
    if isinstance(exc, sqlalchemy.exc.IntegrityError):
        return PersistenceValidationError(str(exc))
    return PersistenceTransientError(str(exc))


class PersistenceActivities:
    """Temporal activity class for database persistence operations.

    Repositories are instantiated once per worker process and reused across
    activity invocations. They are stateless (no per-request mutable state)
    so sharing is safe.
    """

    def __init__(self) -> None:
        self._story_repo = StoryRepository()
        self._scrape_run_repo = ScrapeRunRepository()

    @activity.defn(name="create_scrape_run_activity")
    async def create_scrape_run_activity(self, workflow_id: str) -> ScrapeRun:
        """Create a new scrape run record with status=PENDING.

        Idempotent: if a row for ``workflow_id`` already exists (Temporal retry
        after a transient failure), the existing row is returned unchanged.

        Args:
            workflow_id: Temporal workflow execution ID.

        Returns:
            Newly created (or pre-existing) ScrapeRun record.

        Raises:
            ApplicationError(non_retryable=False): PersistenceTransientError —
                Temporal will retry per DB_RETRY_POLICY.
            ApplicationError(non_retryable=True): PersistenceValidationError —
                Temporal fails the workflow immediately.
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

        try:
            scrape_run = await self._scrape_run_repo.create(workflow_id=workflow_id)
        except PersistenceValidationError as exc:
            log.error(
                "persistence.create_run.validation_error",
                status="failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise ApplicationError(str(exc), non_retryable=True) from exc
        except PersistenceTransientError as exc:
            log.warning(
                "persistence.create_run.transient_error",
                status="retrying",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise
        except sqlalchemy.exc.SQLAlchemyError as exc:
            domain_exc = _classify_sqlalchemy_error(exc)
            log.warning(
                "persistence.create_run.db_error",
                status="retrying" if isinstance(domain_exc, PersistenceTransientError) else "failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            if isinstance(domain_exc, PersistenceValidationError):
                raise ApplicationError(str(domain_exc), non_retryable=True) from exc
            raise domain_exc from exc

        duration_ms = int((time.monotonic() - started_at) * 1000)
        log.info(
            "persistence.create_run.completed",
            status="completed",
            scrape_run_id=str(scrape_run.id),
            run_status=scrape_run.status.value,
            duration_ms=duration_ms,
        )

        return scrape_run

    @activity.defn(name="upsert_stories_activity")
    async def upsert_stories_activity(self, stories: list[Story]) -> int:
        """Upsert a list of stories into the database.

        Idempotent: re-running with the same stories produces the same DB state.
        Existing stories are updated with the latest scraped values; ``created_at``
        and ``id`` are preserved from the original insert.

        Args:
            stories: List of Story domain models to persist.

        Returns:
            Number of rows affected (inserted + updated).

        Raises:
            ApplicationError(non_retryable=False): PersistenceTransientError.
            ApplicationError(non_retryable=True): PersistenceValidationError.
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

        try:
            upserted_count = await self._story_repo.upsert_many(stories=stories)
        except PersistenceValidationError as exc:
            log.error(
                "persistence.upsert_stories.validation_error",
                status="failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise ApplicationError(str(exc), non_retryable=True) from exc
        except PersistenceTransientError as exc:
            log.warning(
                "persistence.upsert_stories.transient_error",
                status="retrying",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise
        except sqlalchemy.exc.SQLAlchemyError as exc:
            domain_exc = _classify_sqlalchemy_error(exc)
            log.warning(
                "persistence.upsert_stories.db_error",
                status="retrying" if isinstance(domain_exc, PersistenceTransientError) else "failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            if isinstance(domain_exc, PersistenceValidationError):
                raise ApplicationError(str(domain_exc), non_retryable=True) from exc
            raise domain_exc from exc

        duration_ms = int((time.monotonic() - started_at) * 1000)
        log.info(
            "persistence.upsert_stories.completed",
            status="completed",
            upserted_count=upserted_count,
            duration_ms=duration_ms,
        )

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

        Idempotent: applying the same update twice yields the same DB state.

        Args:
            run_id:          UUID of the scrape run to update.
            status:          Final status string (COMPLETED or FAILED).
            stories_scraped: Count of upserted stories (None if failed).
            error_message:   Human-readable error description (None if succeeded).

        Returns:
            Updated ScrapeRun record with all fields including original started_at.

        Raises:
            ApplicationError(non_retryable=True): Row not found (PersistenceValidationError)
                or unexpected constraint error.
            ApplicationError(non_retryable=False): Transient DB error.
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
        started_at_ts = time.monotonic()

        try:
            scrape_run = await self._scrape_run_repo.update(
                run_id=run_id,
                status=ScrapeRunStatus(status),
                finished_at=datetime.now(tz=timezone.utc),
                stories_scraped=stories_scraped,
                error_message=error_message,
            )
        except PersistenceValidationError as exc:
            log.error(
                "persistence.update_run.validation_error",
                status="failed",
                target_run_id=str(run_id),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise ApplicationError(str(exc), non_retryable=True) from exc
        except PersistenceTransientError as exc:
            log.warning(
                "persistence.update_run.transient_error",
                status="retrying",
                target_run_id=str(run_id),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise
        except sqlalchemy.exc.SQLAlchemyError as exc:
            domain_exc = _classify_sqlalchemy_error(exc)
            log.warning(
                "persistence.update_run.db_error",
                status="retrying" if isinstance(domain_exc, PersistenceTransientError) else "failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            if isinstance(domain_exc, PersistenceValidationError):
                raise ApplicationError(str(domain_exc), non_retryable=True) from exc
            raise domain_exc from exc

        duration_ms = int((time.monotonic() - started_at_ts) * 1000)
        log.info(
            "persistence.update_run.completed",
            status="completed",
            scrape_run_id=str(scrape_run.id),
            run_status=scrape_run.status.value,
            stories_scraped=scrape_run.stories_scraped,
            duration_ms=duration_ms,
        )

        return scrape_run
