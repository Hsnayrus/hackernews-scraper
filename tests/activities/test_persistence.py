"""Unit tests for app.activities.persistence — PersistenceActivities.

Coverage targets
----------------
- _classify_sqlalchemy_error:
    IntegrityError → PersistenceValidationError (non-retryable)
    OperationalError → PersistenceTransientError (retryable)
    Generic SQLAlchemyError → PersistenceTransientError (conservative default)
    DatabaseError → PersistenceTransientError

- create_scrape_run_activity:
    Success: returns ScrapeRun from repo
    PersistenceValidationError → ApplicationError(non_retryable=True)
    PersistenceTransientError → propagates as-is (Temporal retries)
    SQLAlchemy IntegrityError → ApplicationError(non_retryable=True)
    SQLAlchemy OperationalError → PersistenceTransientError

- upsert_stories_activity:
    Success: returns upserted count
    Empty stories list: returns 0
    PersistenceValidationError → ApplicationError(non_retryable=True)
    PersistenceTransientError → propagates as-is
    SQLAlchemy IntegrityError → ApplicationError(non_retryable=True)
    SQLAlchemy OperationalError → PersistenceTransientError

- update_scrape_run_activity:
    Success COMPLETED: returns updated ScrapeRun
    Success FAILED: returns ScrapeRun with error_message
    PersistenceValidationError (row not found) → ApplicationError(non_retryable=True)
    PersistenceTransientError → propagates as-is
    Status string is converted to ScrapeRunStatus enum for the repo call
    SQLAlchemy IntegrityError → ApplicationError(non_retryable=True)
    SQLAlchemy OperationalError → PersistenceTransientError

Design decisions
----------------
- activity.info() is patched at app.activities.persistence.activity.info
- Repository methods are replaced with AsyncMock directly on the instance
  (repositories have no __init__ that would require patching)
- structlog is patched to suppress log output noise in test runs
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy.exc
from temporalio.exceptions import ApplicationError

from app.activities.persistence import PersistenceActivities, _classify_sqlalchemy_error
from app.domain.exceptions import PersistenceTransientError, PersistenceValidationError
from app.domain.models import ScrapeRun, ScrapeRunStatus, Story


# ---------------------------------------------------------------------------
# Test helpers / factories
# ---------------------------------------------------------------------------


def _make_activity_info(
    *,
    activity_type: str = "test_activity",
    workflow_id: str = "wf-test-001",
    workflow_run_id: str = "run-test-001",
    activity_id: str = "act-test-001",
) -> MagicMock:
    """Return a mock that satisfies the temporalio.activity.Info interface."""
    info = MagicMock()
    info.activity_type = activity_type
    info.workflow_id = workflow_id
    info.workflow_run_id = workflow_run_id
    info.activity_id = activity_id
    return info


def _make_scrape_run(
    status: ScrapeRunStatus = ScrapeRunStatus.PENDING,
    workflow_id: str = "wf-test-001",
    stories_scraped: int | None = None,
    error_message: str | None = None,
) -> ScrapeRun:
    return ScrapeRun(
        id=uuid.uuid4(),
        workflow_id=workflow_id,
        started_at=datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
        status=status,
        stories_scraped=stories_scraped,
        error_message=error_message,
    )


def _make_story(rank: int = 1, hn_id: str = "12345") -> Story:
    return Story(
        hn_id=hn_id,
        title=f"Test Story {rank}",
        url="https://example.com",
        rank=rank,
        points=100 + rank,
        author="testuser",
        comments_count=10 + rank,
    )


# ---------------------------------------------------------------------------
# TestClassifySQLAlchemyError
# ---------------------------------------------------------------------------


class TestClassifySQLAlchemyError:
    """Tests for the _classify_sqlalchemy_error module-level helper."""

    def test_integrity_error_maps_to_validation_error(self) -> None:
        exc = sqlalchemy.exc.IntegrityError("stmt", {}, Exception("constraint violation"))
        result = _classify_sqlalchemy_error(exc)
        assert isinstance(result, PersistenceValidationError)

    def test_integrity_error_message_propagated(self) -> None:
        orig = sqlalchemy.exc.IntegrityError("stmt", {}, Exception("unique constraint"))
        result = _classify_sqlalchemy_error(orig)
        # The domain exception wraps the original error string
        assert str(orig) in str(result)

    def test_operational_error_maps_to_transient_error(self) -> None:
        exc = sqlalchemy.exc.OperationalError("stmt", {}, Exception("connection refused"))
        result = _classify_sqlalchemy_error(exc)
        assert isinstance(result, PersistenceTransientError)

    def test_generic_sqlalchemy_error_maps_to_transient_error(self) -> None:
        """Conservative fallback: unknown SQLAlchemy errors → transient (safe to retry)."""
        exc = sqlalchemy.exc.SQLAlchemyError("some unknown database problem")
        result = _classify_sqlalchemy_error(exc)
        assert isinstance(result, PersistenceTransientError)

    def test_database_error_maps_to_transient_error(self) -> None:
        """DatabaseError is a subclass of SQLAlchemyError but not IntegrityError → transient."""
        exc = sqlalchemy.exc.DatabaseError("stmt", {}, Exception("db error"))
        result = _classify_sqlalchemy_error(exc)
        assert isinstance(result, PersistenceTransientError)

    def test_programming_error_maps_to_transient_error(self) -> None:
        """ProgrammingError is a DBAPIError but not IntegrityError → transient."""
        exc = sqlalchemy.exc.ProgrammingError("stmt", {}, Exception("syntax error"))
        result = _classify_sqlalchemy_error(exc)
        assert isinstance(result, PersistenceTransientError)

    def test_result_is_always_a_domain_exception(self) -> None:
        for exc_class in (
            sqlalchemy.exc.IntegrityError,
            sqlalchemy.exc.OperationalError,
            sqlalchemy.exc.SQLAlchemyError,
        ):
            exc = (
                exc_class("stmt", {}, Exception("e"))
                if issubclass(exc_class, sqlalchemy.exc.DBAPIError)
                else exc_class("e")
            )
            result = _classify_sqlalchemy_error(exc)
            from app.domain.exceptions import PersistenceError
            assert isinstance(result, PersistenceError)


# ---------------------------------------------------------------------------
# TestCreateScrapeRunActivity
# ---------------------------------------------------------------------------


class TestCreateScrapeRunActivity:
    """Tests for PersistenceActivities.create_scrape_run_activity."""

    @pytest.fixture()
    def activities(self) -> PersistenceActivities:
        return PersistenceActivities()

    @pytest.fixture()
    def mock_info(self) -> MagicMock:
        return _make_activity_info(activity_type="create_scrape_run_activity")

    async def test_returns_scrape_run_on_success(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        """Activity returns the ScrapeRun returned by the repository."""
        expected_run = _make_scrape_run()
        activities._scrape_run_repo.create = AsyncMock(return_value=expected_run)

        with patch("app.activities.persistence.activity.info", return_value=mock_info):
            result = await activities.create_scrape_run_activity("wf-test-001")

        assert result == expected_run

    def test_repo_called_with_correct_workflow_id(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        """Verify the workflow_id is passed through to the repository unchanged."""
        # This is a synchronous test verifying call arguments via sync inspection
        expected_run = _make_scrape_run(workflow_id="specific-wf-id")
        mock_create = AsyncMock(return_value=expected_run)
        activities._scrape_run_repo.create = mock_create

        import asyncio

        async def _run() -> None:
            with patch("app.activities.persistence.activity.info", return_value=mock_info):
                await activities.create_scrape_run_activity("specific-wf-id")

        asyncio.get_event_loop().run_until_complete(_run())
        mock_create.assert_awaited_once_with(workflow_id="specific-wf-id")

    async def test_raises_non_retryable_application_error_on_validation_error(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        """PersistenceValidationError → ApplicationError(non_retryable=True)."""
        activities._scrape_run_repo.create = AsyncMock(
            side_effect=PersistenceValidationError("constraint violation")
        )

        with (
            patch("app.activities.persistence.activity.info", return_value=mock_info),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activities.create_scrape_run_activity("wf-test-001")

        assert exc_info.value.non_retryable is True

    async def test_reraises_transient_error_unchanged(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        """PersistenceTransientError propagates as-is (Temporal will retry via DB_RETRY_POLICY)."""
        activities._scrape_run_repo.create = AsyncMock(
            side_effect=PersistenceTransientError("connection pool exhausted")
        )

        with (
            patch("app.activities.persistence.activity.info", return_value=mock_info),
            pytest.raises(PersistenceTransientError),
        ):
            await activities.create_scrape_run_activity("wf-test-001")

    async def test_wraps_sqlalchemy_integrity_error_as_non_retryable(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        """Direct SQLAlchemy IntegrityError (bypassing classification) → ApplicationError."""
        exc = sqlalchemy.exc.IntegrityError("stmt", {}, Exception("unique violation"))
        activities._scrape_run_repo.create = AsyncMock(side_effect=exc)

        with (
            patch("app.activities.persistence.activity.info", return_value=mock_info),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activities.create_scrape_run_activity("wf-test-001")

        assert exc_info.value.non_retryable is True

    async def test_wraps_sqlalchemy_operational_error_as_transient(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        """Direct SQLAlchemy OperationalError → PersistenceTransientError (retryable)."""
        exc = sqlalchemy.exc.OperationalError("stmt", {}, Exception("connection refused"))
        activities._scrape_run_repo.create = AsyncMock(side_effect=exc)

        with (
            patch("app.activities.persistence.activity.info", return_value=mock_info),
            pytest.raises(PersistenceTransientError),
        ):
            await activities.create_scrape_run_activity("wf-test-001")

    async def test_wraps_generic_sqlalchemy_error_as_transient(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        """Generic SQLAlchemyError → PersistenceTransientError (conservative fallback)."""
        exc = sqlalchemy.exc.SQLAlchemyError("unknown db error")
        activities._scrape_run_repo.create = AsyncMock(side_effect=exc)

        with (
            patch("app.activities.persistence.activity.info", return_value=mock_info),
            pytest.raises(PersistenceTransientError),
        ):
            await activities.create_scrape_run_activity("wf-test-001")


# ---------------------------------------------------------------------------
# TestUpsertStoriesActivity
# ---------------------------------------------------------------------------


class TestUpsertStoriesActivity:
    """Tests for PersistenceActivities.upsert_stories_activity."""

    @pytest.fixture()
    def activities(self) -> PersistenceActivities:
        return PersistenceActivities()

    @pytest.fixture()
    def mock_info(self) -> MagicMock:
        return _make_activity_info(activity_type="upsert_stories_activity")

    async def test_returns_upserted_count_on_success(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        stories = [_make_story(i, hn_id=str(i)) for i in range(1, 4)]
        activities._story_repo.upsert_many = AsyncMock(return_value=3)

        with patch("app.activities.persistence.activity.info", return_value=mock_info):
            result = await activities.upsert_stories_activity(stories)

        assert result == 3

    async def test_passes_stories_list_to_repo(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        stories = [_make_story(1, hn_id="abc"), _make_story(2, hn_id="def")]
        mock_upsert = AsyncMock(return_value=2)
        activities._story_repo.upsert_many = mock_upsert

        with patch("app.activities.persistence.activity.info", return_value=mock_info):
            await activities.upsert_stories_activity(stories)

        mock_upsert.assert_awaited_once_with(stories=stories)

    async def test_empty_stories_list_returns_zero(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        """Empty list is a valid input — zero stories results in zero upserts."""
        activities._story_repo.upsert_many = AsyncMock(return_value=0)

        with patch("app.activities.persistence.activity.info", return_value=mock_info):
            result = await activities.upsert_stories_activity([])

        assert result == 0

    async def test_partial_upsert_when_duplicates_exist(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        """When 5 out of 30 stories are duplicates, upserted count may differ from input count."""
        stories = [_make_story(i, hn_id=str(i)) for i in range(1, 31)]
        activities._story_repo.upsert_many = AsyncMock(return_value=25)

        with patch("app.activities.persistence.activity.info", return_value=mock_info):
            result = await activities.upsert_stories_activity(stories)

        assert result == 25

    async def test_raises_non_retryable_on_validation_error(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        activities._story_repo.upsert_many = AsyncMock(
            side_effect=PersistenceValidationError("unexpected constraint violation")
        )

        with (
            patch("app.activities.persistence.activity.info", return_value=mock_info),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activities.upsert_stories_activity([_make_story()])

        assert exc_info.value.non_retryable is True

    async def test_reraises_transient_error(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        activities._story_repo.upsert_many = AsyncMock(
            side_effect=PersistenceTransientError("DB connection pool exhausted")
        )

        with (
            patch("app.activities.persistence.activity.info", return_value=mock_info),
            pytest.raises(PersistenceTransientError),
        ):
            await activities.upsert_stories_activity([_make_story()])

    async def test_wraps_integrity_error_as_non_retryable(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        exc = sqlalchemy.exc.IntegrityError("stmt", {}, Exception("violation"))
        activities._story_repo.upsert_many = AsyncMock(side_effect=exc)

        with (
            patch("app.activities.persistence.activity.info", return_value=mock_info),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activities.upsert_stories_activity([_make_story()])

        assert exc_info.value.non_retryable is True

    async def test_wraps_operational_error_as_transient(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        exc = sqlalchemy.exc.OperationalError("stmt", {}, Exception("connection refused"))
        activities._story_repo.upsert_many = AsyncMock(side_effect=exc)

        with (
            patch("app.activities.persistence.activity.info", return_value=mock_info),
            pytest.raises(PersistenceTransientError),
        ):
            await activities.upsert_stories_activity([_make_story()])

    async def test_wraps_generic_sqlalchemy_error_as_transient(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        exc = sqlalchemy.exc.SQLAlchemyError("unknown error")
        activities._story_repo.upsert_many = AsyncMock(side_effect=exc)

        with (
            patch("app.activities.persistence.activity.info", return_value=mock_info),
            pytest.raises(PersistenceTransientError),
        ):
            await activities.upsert_stories_activity([_make_story()])


# ---------------------------------------------------------------------------
# TestUpdateScrapeRunActivity
# ---------------------------------------------------------------------------


class TestUpdateScrapeRunActivity:
    """Tests for PersistenceActivities.update_scrape_run_activity."""

    @pytest.fixture()
    def activities(self) -> PersistenceActivities:
        return PersistenceActivities()

    @pytest.fixture()
    def mock_info(self) -> MagicMock:
        return _make_activity_info(activity_type="update_scrape_run_activity")

    async def test_returns_updated_scrape_run_on_completed(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        run_id = uuid.uuid4()
        completed_run = _make_scrape_run(
            status=ScrapeRunStatus.COMPLETED, stories_scraped=30, workflow_id="wf-001"
        )
        activities._scrape_run_repo.update = AsyncMock(return_value=completed_run)

        with patch("app.activities.persistence.activity.info", return_value=mock_info):
            result = await activities.update_scrape_run_activity(
                run_id=run_id,
                status=ScrapeRunStatus.COMPLETED.value,
                stories_scraped=30,
                error_message=None,
            )

        assert result == completed_run
        assert result.status == ScrapeRunStatus.COMPLETED
        assert result.stories_scraped == 30

    async def test_returns_updated_scrape_run_on_failed(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        run_id = uuid.uuid4()
        failed_run = _make_scrape_run(
            status=ScrapeRunStatus.FAILED,
            error_message="Browser navigation failed",
        )
        activities._scrape_run_repo.update = AsyncMock(return_value=failed_run)

        with patch("app.activities.persistence.activity.info", return_value=mock_info):
            result = await activities.update_scrape_run_activity(
                run_id=run_id,
                status=ScrapeRunStatus.FAILED.value,
                stories_scraped=None,
                error_message="Browser navigation failed",
            )

        assert result.status == ScrapeRunStatus.FAILED
        assert result.error_message == "Browser navigation failed"

    async def test_passes_correct_status_enum_to_repo(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        """Status string "COMPLETED" must be converted to ScrapeRunStatus.COMPLETED for the repo."""
        run_id = uuid.uuid4()
        completed_run = _make_scrape_run(status=ScrapeRunStatus.COMPLETED)
        mock_update = AsyncMock(return_value=completed_run)
        activities._scrape_run_repo.update = mock_update

        with patch("app.activities.persistence.activity.info", return_value=mock_info):
            await activities.update_scrape_run_activity(
                run_id=run_id,
                status="COMPLETED",
                stories_scraped=30,
                error_message=None,
            )

        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs["status"] == ScrapeRunStatus.COMPLETED
        assert call_kwargs["run_id"] == run_id
        assert call_kwargs["stories_scraped"] == 30
        assert call_kwargs["error_message"] is None

    async def test_passes_finished_at_timestamp_to_repo(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        """Activity always passes a timezone-aware finished_at to the repo."""
        run_id = uuid.uuid4()
        completed_run = _make_scrape_run(status=ScrapeRunStatus.COMPLETED)
        mock_update = AsyncMock(return_value=completed_run)
        activities._scrape_run_repo.update = mock_update

        with patch("app.activities.persistence.activity.info", return_value=mock_info):
            await activities.update_scrape_run_activity(
                run_id=run_id,
                status="COMPLETED",
                stories_scraped=10,
                error_message=None,
            )

        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs["finished_at"].tzinfo is not None

    async def test_raises_non_retryable_when_run_not_found(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        """PersistenceValidationError (row not found) → ApplicationError(non_retryable=True)."""
        run_id = uuid.uuid4()
        activities._scrape_run_repo.update = AsyncMock(
            side_effect=PersistenceValidationError(
                f"scrape_run not found for update: run_id={run_id}"
            )
        )

        with (
            patch("app.activities.persistence.activity.info", return_value=mock_info),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activities.update_scrape_run_activity(
                run_id=run_id,
                status=ScrapeRunStatus.COMPLETED.value,
                stories_scraped=30,
                error_message=None,
            )

        assert exc_info.value.non_retryable is True

    async def test_reraises_transient_error(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        run_id = uuid.uuid4()
        activities._scrape_run_repo.update = AsyncMock(
            side_effect=PersistenceTransientError("deadlock detected")
        )

        with (
            patch("app.activities.persistence.activity.info", return_value=mock_info),
            pytest.raises(PersistenceTransientError),
        ):
            await activities.update_scrape_run_activity(
                run_id=run_id,
                status=ScrapeRunStatus.COMPLETED.value,
                stories_scraped=30,
                error_message=None,
            )

    async def test_wraps_integrity_error_as_non_retryable(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        run_id = uuid.uuid4()
        exc = sqlalchemy.exc.IntegrityError("stmt", {}, Exception("constraint"))
        activities._scrape_run_repo.update = AsyncMock(side_effect=exc)

        with (
            patch("app.activities.persistence.activity.info", return_value=mock_info),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activities.update_scrape_run_activity(
                run_id=run_id,
                status="COMPLETED",
                stories_scraped=30,
                error_message=None,
            )

        assert exc_info.value.non_retryable is True

    async def test_wraps_operational_error_as_transient(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        run_id = uuid.uuid4()
        exc = sqlalchemy.exc.OperationalError("stmt", {}, Exception("conn refused"))
        activities._scrape_run_repo.update = AsyncMock(side_effect=exc)

        with (
            patch("app.activities.persistence.activity.info", return_value=mock_info),
            pytest.raises(PersistenceTransientError),
        ):
            await activities.update_scrape_run_activity(
                run_id=run_id,
                status="COMPLETED",
                stories_scraped=30,
                error_message=None,
            )

    async def test_failed_status_string_converted_correctly(
        self, activities: PersistenceActivities, mock_info: MagicMock
    ) -> None:
        """Status string "FAILED" must be converted to ScrapeRunStatus.FAILED for the repo."""
        run_id = uuid.uuid4()
        failed_run = _make_scrape_run(status=ScrapeRunStatus.FAILED)
        mock_update = AsyncMock(return_value=failed_run)
        activities._scrape_run_repo.update = mock_update

        with patch("app.activities.persistence.activity.info", return_value=mock_info):
            await activities.update_scrape_run_activity(
                run_id=run_id,
                status="FAILED",
                stories_scraped=None,
                error_message="Browser crashed",
            )

        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs["status"] == ScrapeRunStatus.FAILED
        assert call_kwargs["error_message"] == "Browser crashed"
        assert call_kwargs["stories_scraped"] is None
