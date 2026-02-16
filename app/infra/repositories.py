"""Database repository layer.

Repositories provide typed, async methods for reading and writing domain
models to Postgres via SQLAlchemy Core. They do not log and do not contain
business logic — they are pure data access objects.

Responsibilities:
  - Construct and execute SQL statements.
  - Map result rows to domain model instances.
  - Let SQLAlchemy exceptions propagate to callers (activities) which then
    classify them as PersistenceTransientError or PersistenceValidationError.

What repositories do NOT do:
  - They do not catch exceptions.
  - They do not log.
  - They do not own transactions (each method is one atomic transaction via
    get_connection(), which uses engine.begin()).

Classes:
    StoryRepository      — upsert_many(), list()
    ScrapeRunRepository  — create(), update(), list(), get_by_workflow_id()
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.domain.models import ScrapeRun, ScrapeRunStatus, Story
from app.infra.db import get_connection
from app.infra.tables import scrape_runs_table, stories_table


def _row_to_story(row: sa.engine.Row) -> Story:  # type: ignore[type-arg]
    """Map a SQLAlchemy result row to a Story domain model."""
    return Story(
        id=row.id,
        hn_id=row.hn_id,
        title=row.title,
        url=row.url,
        rank=row.rank,
        points=row.points,
        author=row.author,
        comments_count=row.comments_count,
        scraped_at=row.scraped_at,
        created_at=row.created_at,
    )


# type: ignore[type-arg]
def _row_to_scrape_run(row: sa.engine.Row) -> ScrapeRun:
    """Map a SQLAlchemy result row to a ScrapeRun domain model."""
    return ScrapeRun(
        id=row.id,
        workflow_id=row.workflow_id,
        started_at=row.started_at,
        finished_at=row.finished_at,
        status=ScrapeRunStatus(row.status),
        stories_scraped=row.stories_scraped,
        error_message=row.error_message,
    )


# ---------------------------------------------------------------------------
# StoryRepository
# ---------------------------------------------------------------------------


class StoryRepository:
    """Data access layer for the ``stories`` table."""

    async def upsert_many(self, stories: list[Story]) -> int:
        """Upsert a list of stories, keyed on ``hn_id``.

        On conflict, all mutable fields are updated except ``id`` and
        ``created_at`` (which preserve the original insert values).

        Args:
            stories: Non-empty list of Story domain models.

        Returns:
            Number of rows affected (inserted + updated).

        Raises:
            sqlalchemy.exc.SQLAlchemyError: Propagated to caller for
                classification into PersistenceTransientError or
                PersistenceValidationError.
        """
        if not stories:
            return 0

        values = [
            {
                "id": story.id,
                "hn_id": story.hn_id,
                "title": story.title,
                "url": story.url,
                "rank": story.rank,
                "points": story.points,
                "author": story.author,
                "comments_count": story.comments_count,
                "scraped_at": story.scraped_at,
                "created_at": story.created_at,
            }
            for story in stories
        ]

        stmt = pg_insert(stories_table).values(values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_stories_hn_id",
            set_={
                "title": stmt.excluded.title,
                "url": stmt.excluded.url,
                "rank": stmt.excluded.rank,
                "points": stmt.excluded.points,
                "author": stmt.excluded.author,
                "comments_count": stmt.excluded.comments_count,
                "scraped_at": stmt.excluded.scraped_at,
                # created_at is intentionally excluded: preserves first-seen time.
                # id is intentionally excluded: preserves original UUID.
            },
        )

        async with get_connection() as conn:
            result = await conn.execute(stmt)

        return result.rowcount

    async def list(
        self,
        limit: int = 50,
        min_points: Optional[int] = None,
        rank_min: Optional[int] = None,
        rank_max: Optional[int] = None,
    ) -> list[Story]:
        """Return stories ordered by rank ascending.

        Args:
            limit:      Maximum number of stories to return (1-10000).
            min_points: If provided, exclude stories with fewer points.
            rank_min:   If provided, only return stories with rank >= this value.
            rank_max:   If provided, only return stories with rank <= this value.

        Returns:
            List of Story domain models, ordered by rank ascending.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: Propagated to caller.
        """
        stmt = sa.select(stories_table).order_by(stories_table.c.rank.asc())

        if min_points is not None:
            stmt = stmt.where(stories_table.c.points >= min_points)

        if rank_min is not None:
            stmt = stmt.where(stories_table.c.rank >= rank_min)

        if rank_max is not None:
            stmt = stmt.where(stories_table.c.rank <= rank_max)

        stmt = stmt.limit(limit)

        async with get_connection() as conn:
            result = await conn.execute(stmt)

        return [_row_to_story(row) for row in result.fetchall()]


# ---------------------------------------------------------------------------
# ScrapeRunRepository
# ---------------------------------------------------------------------------


class ScrapeRunRepository:
    """Data access layer for the ``scrape_runs`` table."""

    async def create(self, workflow_id: str) -> ScrapeRun:
        """Insert a new scrape run record with status=PENDING.

        Idempotent: if a row with the same ``workflow_id`` already exists
        (Temporal activity retry after a transient failure post-INSERT),
        the existing row is returned unchanged.

        Args:
            workflow_id: Temporal workflow execution ID.

        Returns:
            The newly created (or pre-existing) ScrapeRun record.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: Propagated to caller.
        """
        run_id = uuid.uuid4()
        started_at = datetime.now(tz=timezone.utc)

        stmt = pg_insert(scrape_runs_table).values(
            id=run_id,
            workflow_id=workflow_id,
            started_at=started_at,
            finished_at=None,
            status=ScrapeRunStatus.PENDING.value,
            stories_scraped=None,
            error_message=None,
        )
        # ON CONFLICT: no-op update (workflow_id = EXCLUDED.workflow_id) ensures
        # RETURNING always emits the row — whether just inserted or pre-existing.
        stmt = stmt.on_conflict_do_update(
            constraint="uq_scrape_runs_workflow_id",
            set_={"workflow_id": stmt.excluded.workflow_id},
        ).returning(scrape_runs_table)

        async with get_connection() as conn:
            result = await conn.execute(stmt)

        row = result.fetchone()
        # row is always non-None: ON CONFLICT DO UPDATE + RETURNING guarantees a row.
        assert row is not None, "INSERT ... ON CONFLICT DO UPDATE RETURNING returned no row"  # noqa: S101
        return _row_to_scrape_run(row)

    async def update(
        self,
        run_id: uuid.UUID,
        status: ScrapeRunStatus,
        finished_at: datetime,
        stories_scraped: Optional[int],
        error_message: Optional[str],
    ) -> ScrapeRun:
        """Update the final state of a scrape run.

        Idempotent: applying the same update twice yields the same result.

        Args:
            run_id:          UUID of the scrape run row to update.
            status:          Final lifecycle status (COMPLETED or FAILED).
            finished_at:     UTC timestamp of workflow completion.
            stories_scraped: Count of upserted stories (None if failed).
            error_message:   Human-readable error (None if succeeded).

        Returns:
            Updated ScrapeRun with all fields including the original started_at.

        Raises:
            PersistenceValidationError: If no row matched ``run_id`` (bug).
            sqlalchemy.exc.SQLAlchemyError: Other DB errors, propagated to caller.
        """
        from app.domain.exceptions import PersistenceValidationError  # local import avoids circular

        stmt = (
            sa.update(scrape_runs_table)
            .where(scrape_runs_table.c.id == run_id)
            .values(
                finished_at=finished_at,
                status=status.value,
                stories_scraped=stories_scraped,
                error_message=error_message,
            )
            .returning(scrape_runs_table)
        )

        async with get_connection() as conn:
            result = await conn.execute(stmt)

        row = result.fetchone()
        if row is None:
            raise PersistenceValidationError(
                f"scrape_run not found for update: run_id={run_id}"
            )

        return _row_to_scrape_run(row)

    async def list(
        self,
        limit: int = 50,
        status: Optional[ScrapeRunStatus] = None,
    ) -> list[ScrapeRun]:
        """Return scrape runs ordered by started_at descending (most recent first).

        Args:
            limit:  Maximum number of runs to return (1–200).
            status: If provided, only return runs with this lifecycle status.

        Returns:
            List of ScrapeRun domain models.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: Propagated to caller.
        """
        stmt = (
            sa.select(scrape_runs_table)
            .order_by(scrape_runs_table.c.started_at.desc())
        )

        if status is not None:
            stmt = stmt.where(scrape_runs_table.c.status == status.value)

        stmt = stmt.limit(limit)

        async with get_connection() as conn:
            result = await conn.execute(stmt)

        return [_row_to_scrape_run(row) for row in result.fetchall()]

    async def get_by_workflow_id(self, workflow_id: str) -> Optional[ScrapeRun]:
        """Return the scrape run associated with a Temporal workflow execution ID.

        Args:
            workflow_id: Temporal workflow execution ID (as returned by POST /scrape).

        Returns:
            ScrapeRun domain model if found, None otherwise.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: Propagated to caller.
        """
        stmt = sa.select(scrape_runs_table).where(
            scrape_runs_table.c.workflow_id == workflow_id
        )

        async with get_connection() as conn:
            result = await conn.execute(stmt)

        row = result.fetchone()
        return _row_to_scrape_run(row) if row is not None else None
