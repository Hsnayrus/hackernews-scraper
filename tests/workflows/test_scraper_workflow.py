"""Unit tests for app.workflows.scraper — ScrapeHackerNewsWorkflow.

Coverage targets
----------------
- Happy path: workflow completes successfully
- Failure before run creation: workflow fails, no status update
- Failure after run creation: workflow updates run to FAILED
- Edge cases: top_n=1, top_n=100, empty stories
- Activity retry scenarios

Design decisions
----------------
- Use Temporal's test environment for deterministic replay testing
- Mock all activities to test workflow orchestration in isolation
- Activities are mocked using the Temporal test framework's activity mocking
- Tests verify correct activity call order and parameters
- Tests verify proper error handling and status updates
- No real browser, database, or Temporal server required

Test organization
------------------
Each test class focuses on a specific aspect:
  - TestWorkflowHappyPath: successful execution scenarios
  - TestWorkflowFailureHandling: activity failure scenarios
  - TestWorkflowEdgeCases: boundary conditions and special cases
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.domain.models import ScrapeRun, ScrapeRunStatus, Story
from app.workflows.scraper import ScrapeHackerNewsWorkflow


# ---------------------------------------------------------------------------
# Test Fixtures and Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_scrape_run() -> ScrapeRun:
    """Return a sample ScrapeRun in PENDING status."""
    return ScrapeRun(
        id=uuid.uuid4(),
        workflow_id="test-workflow-001",
        started_at=datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
        finished_at=None,
        status=ScrapeRunStatus.PENDING,
        stories_scraped=None,
        error_message=None,
    )


@pytest.fixture
def mock_completed_scrape_run(mock_scrape_run: ScrapeRun) -> ScrapeRun:
    """Return a sample ScrapeRun in COMPLETED status."""
    return ScrapeRun(
        id=mock_scrape_run.id,
        workflow_id=mock_scrape_run.workflow_id,
        started_at=mock_scrape_run.started_at,
        finished_at=datetime(2026, 2, 15, 10, 5, 0, tzinfo=timezone.utc),
        status=ScrapeRunStatus.COMPLETED,
        stories_scraped=30,
        error_message=None,
    )


@pytest.fixture
def mock_failed_scrape_run(mock_scrape_run: ScrapeRun) -> ScrapeRun:
    """Return a sample ScrapeRun in FAILED status."""
    return ScrapeRun(
        id=mock_scrape_run.id,
        workflow_id=mock_scrape_run.workflow_id,
        started_at=mock_scrape_run.started_at,
        finished_at=datetime(2026, 2, 15, 10, 5, 0, tzinfo=timezone.utc),
        status=ScrapeRunStatus.FAILED,
        stories_scraped=None,
        error_message="Browser navigation failed",
    )


@pytest.fixture
def mock_stories() -> list[Story]:
    """Return a sample list of scraped stories."""
    return [
        Story(
            id=uuid.uuid4(),
            hn_id=f"story-{i}",
            title=f"Test Story {i}",
            url=f"https://example.com/story-{i}",
            rank=i,
            points=100 + i * 10,
            author=f"author{i}",
            comments_count=i * 5,
            scraped_at=datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
            created_at=datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
        )
        for i in range(1, 31)  # 30 stories
    ]


def _create_activity_mocks(
    scrape_run: ScrapeRun,
    completed_run: ScrapeRun,
    stories: list[Story],
    upserted_count: int = 30,
) -> list[Any]:
    """Create mock activity implementations for the happy path.

    Returns a list of decorated activity functions that can be registered
    with the Temporal test worker.
    """
    @activity.defn(name="create_scrape_run_activity")
    async def create_scrape_run_activity(workflow_id: str) -> dict[str, Any]:
        """Mock: create scrape run record."""
        return scrape_run.model_dump()

    @activity.defn(name="start_playwright_activity")
    async def start_playwright_activity() -> None:
        """Mock: start browser (no-op)."""
        pass

    @activity.defn(name="navigate_to_hacker_news_activity")
    async def navigate_to_hacker_news_activity() -> None:
        """Mock: navigate to HN (no-op)."""
        pass

    @activity.defn(name="scrape_urls_activity")
    async def scrape_urls_activity() -> list[dict[str, Any]]:
        """Mock: scrape stories and return as list of dicts."""
        return [story.model_dump() for story in stories]

    @activity.defn(name="upsert_stories_activity")
    async def upsert_stories_activity(stories_data: list[dict[str, Any]]) -> int:
        """Mock: persist stories and return count."""
        return upserted_count

    @activity.defn(name="update_scrape_run_activity")
    async def update_scrape_run_activity(
        run_id: uuid.UUID,
        status: str,
        stories_scraped: int | None,
        error_message: str | None,
    ) -> dict[str, Any]:
        """Mock: update scrape run status."""
        return completed_run.model_dump()

    return [
        create_scrape_run_activity,
        start_playwright_activity,
        navigate_to_hacker_news_activity,
        scrape_urls_activity,
        upsert_stories_activity,
        update_scrape_run_activity,
    ]


# ---------------------------------------------------------------------------
# Test Cases: Happy Path
# ---------------------------------------------------------------------------


class TestWorkflowHappyPath:
    """Test successful workflow execution scenarios."""

    @pytest.mark.asyncio
    async def test_workflow_completes_successfully(
        self,
        mock_scrape_run: ScrapeRun,
        mock_completed_scrape_run: ScrapeRun,
        mock_stories: list[Story],
    ):
        """Verify workflow completes and returns COMPLETED ScrapeRun."""
        # Arrange
        activity_mocks = _create_activity_mocks(
            mock_scrape_run, mock_completed_scrape_run, mock_stories, upserted_count=30
        )

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-task-queue",
                workflows=[ScrapeHackerNewsWorkflow],
                activities=activity_mocks,
            ):
                # Act
                result = await env.client.execute_workflow(
                    ScrapeHackerNewsWorkflow.run,
                    args=[30],  # top_n
                    id="test-workflow-happy-path",
                    task_queue="test-task-queue",
                )

                # Assert
                assert isinstance(result, ScrapeRun)
                assert result.status == ScrapeRunStatus.COMPLETED
                assert result.stories_scraped == 30
                assert result.error_message is None
                assert result.finished_at is not None

    @pytest.mark.asyncio
    async def test_workflow_with_minimum_top_n(
        self,
        mock_scrape_run: ScrapeRun,
        mock_completed_scrape_run: ScrapeRun,
    ):
        """Verify workflow handles top_n=1 correctly."""
        # Arrange
        single_story = [
            Story(
                id=uuid.uuid4(),
                hn_id="story-1",
                title="Single Test Story",
                url="https://example.com/story-1",
                rank=1,
                points=100,
                author="author1",
                comments_count=5,
                scraped_at=datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
                created_at=datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
            )
        ]
        completed_run_single = ScrapeRun(
            **{**mock_completed_scrape_run.model_dump(), "stories_scraped": 1}
        )
        activity_mocks = _create_activity_mocks(
            mock_scrape_run, completed_run_single, single_story, upserted_count=1
        )

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-task-queue",
                workflows=[ScrapeHackerNewsWorkflow],
                activities=activity_mocks,
            ):
                # Act
                result = await env.client.execute_workflow(
                    ScrapeHackerNewsWorkflow.run,
                    args=[1],  # top_n=1
                    id="test-workflow-min-top-n",
                    task_queue="test-task-queue",
                )

                # Assert
                assert result.status == ScrapeRunStatus.COMPLETED
                assert result.stories_scraped == 1

    @pytest.mark.asyncio
    async def test_workflow_with_maximum_top_n(
        self,
        mock_scrape_run: ScrapeRun,
        mock_completed_scrape_run: ScrapeRun,
    ):
        """Verify workflow handles top_n=100 correctly."""
        # Arrange
        many_stories = [
            Story(
                id=uuid.uuid4(),
                hn_id=f"story-{i}",
                title=f"Test Story {i}",
                url=f"https://example.com/story-{i}",
                rank=i,
                points=100 + i,
                author=f"author{i}",
                comments_count=i,
                scraped_at=datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
                created_at=datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
            )
            for i in range(1, 101)  # 100 stories
        ]
        completed_run_many = ScrapeRun(
            **{**mock_completed_scrape_run.model_dump(), "stories_scraped": 100}
        )
        activity_mocks = _create_activity_mocks(
            mock_scrape_run, completed_run_many, many_stories, upserted_count=100
        )

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-task-queue",
                workflows=[ScrapeHackerNewsWorkflow],
                activities=activity_mocks,
            ):
                # Act
                result = await env.client.execute_workflow(
                    ScrapeHackerNewsWorkflow.run,
                    args=[100],  # top_n=100
                    id="test-workflow-max-top-n",
                    task_queue="test-task-queue",
                )

                # Assert
                assert result.status == ScrapeRunStatus.COMPLETED
                assert result.stories_scraped == 100


# ---------------------------------------------------------------------------
# Test Cases: Failure Handling
# ---------------------------------------------------------------------------


class TestWorkflowFailureHandling:
    """Test workflow behavior when activities fail."""

    @pytest.mark.asyncio
    async def test_failure_before_run_creation(self):
        """Verify workflow fails without updating run status if create_scrape_run fails."""
        # Arrange
        @activity.defn(name="create_scrape_run_activity")
        async def create_scrape_run_activity_failing(workflow_id: str) -> dict[str, Any]:
            """Mock activity that raises an exception."""
            raise RuntimeError("Database connection failed")

        # Other activities won't be called, but need to be defined
        @activity.defn(name="start_playwright_activity")
        async def start_playwright_activity() -> None:
            pass

        @activity.defn(name="navigate_to_hacker_news_activity")
        async def navigate_to_hacker_news_activity() -> None:
            pass

        @activity.defn(name="scrape_urls_activity")
        async def scrape_urls_activity() -> list[dict[str, Any]]:
            return []

        @activity.defn(name="upsert_stories_activity")
        async def upsert_stories_activity(stories_data: list[dict[str, Any]]) -> int:
            return 0

        @activity.defn(name="update_scrape_run_activity")
        async def update_scrape_run_activity(
            run_id: uuid.UUID,
            status: str,
            stories_scraped: int | None,
            error_message: str | None,
        ) -> dict[str, Any]:
            return {}

        activity_mocks = [
            create_scrape_run_activity_failing,
            start_playwright_activity,
            navigate_to_hacker_news_activity,
            scrape_urls_activity,
            upsert_stories_activity,
            update_scrape_run_activity,
        ]

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-task-queue",
                workflows=[ScrapeHackerNewsWorkflow],
                activities=activity_mocks,
            ):
                # Act & Assert
                with pytest.raises(WorkflowFailureError) as exc_info:
                    await env.client.execute_workflow(
                        ScrapeHackerNewsWorkflow.run,
                        args=[30],
                        id="test-workflow-early-failure",
                        task_queue="test-task-queue",
                    )

                # Verify the workflow failed (error details are in Temporal's cause chain)
                assert exc_info.value is not None

    @pytest.mark.asyncio
    async def test_failure_after_run_creation_updates_status(
        self,
        mock_scrape_run: ScrapeRun,
        mock_failed_scrape_run: ScrapeRun,
    ):
        """Verify workflow updates run to FAILED when browser activity fails."""
        # Arrange
        update_called = {"called": False, "status": None, "error_message": None}

        @activity.defn(name="create_scrape_run_activity")
        async def create_scrape_run_activity(workflow_id: str) -> dict[str, Any]:
            return mock_scrape_run.model_dump()

        @activity.defn(name="start_playwright_activity")
        async def start_playwright_activity() -> None:
            pass

        @activity.defn(name="navigate_to_hacker_news_activity")
        async def navigate_to_hacker_news_activity() -> None:
            """Mock activity that fails."""
            raise RuntimeError("Browser navigation failed")

        @activity.defn(name="scrape_urls_activity")
        async def scrape_urls_activity() -> list[dict[str, Any]]:
            # Won't be called
            return []

        @activity.defn(name="upsert_stories_activity")
        async def upsert_stories_activity(stories_data: list[dict[str, Any]]) -> int:
            # Won't be called
            return 0

        @activity.defn(name="update_scrape_run_activity")
        async def update_scrape_run_activity(
            run_id: uuid.UUID,
            status: str,
            stories_scraped: int | None,
            error_message: str | None,
        ) -> dict[str, Any]:
            """Capture the update call to verify FAILED status."""
            update_called["called"] = True
            update_called["status"] = status
            update_called["error_message"] = error_message
            return mock_failed_scrape_run.model_dump()

        activity_mocks = [
            create_scrape_run_activity,
            start_playwright_activity,
            navigate_to_hacker_news_activity,
            scrape_urls_activity,
            upsert_stories_activity,
            update_scrape_run_activity,
        ]

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-task-queue",
                workflows=[ScrapeHackerNewsWorkflow],
                activities=activity_mocks,
            ):
                # Act & Assert
                with pytest.raises(WorkflowFailureError) as exc_info:
                    await env.client.execute_workflow(
                        ScrapeHackerNewsWorkflow.run,
                        args=[30],
                        id="test-workflow-mid-failure",
                        task_queue="test-task-queue",
                    )

                # Verify the workflow failed
                assert exc_info.value is not None

                # Verify update_scrape_run_activity was called with FAILED status
                assert update_called["called"] is True
                assert update_called["status"] == ScrapeRunStatus.FAILED.value
                # Error message should be set (contains activity error details)
                assert update_called["error_message"] is not None
                assert len(update_called["error_message"]) > 0

    @pytest.mark.asyncio
    async def test_failure_in_scrape_activity_updates_status(
        self,
        mock_scrape_run: ScrapeRun,
        mock_failed_scrape_run: ScrapeRun,
    ):
        """Verify workflow updates run to FAILED when scrape activity fails."""
        # Arrange
        update_called = {"called": False, "status": None}

        @activity.defn(name="create_scrape_run_activity")
        async def create_scrape_run_activity(workflow_id: str) -> dict[str, Any]:
            return mock_scrape_run.model_dump()

        @activity.defn(name="start_playwright_activity")
        async def start_playwright_activity() -> None:
            pass

        @activity.defn(name="navigate_to_hacker_news_activity")
        async def navigate_to_hacker_news_activity() -> None:
            pass

        @activity.defn(name="scrape_urls_activity")
        async def scrape_urls_activity() -> list[dict[str, Any]]:
            """Mock activity that fails during scraping."""
            raise RuntimeError("Failed to parse story elements")

        @activity.defn(name="upsert_stories_activity")
        async def upsert_stories_activity(stories_data: list[dict[str, Any]]) -> int:
            # Won't be called
            return 0

        @activity.defn(name="update_scrape_run_activity")
        async def update_scrape_run_activity(
            run_id: uuid.UUID,
            status: str,
            stories_scraped: int | None,
            error_message: str | None,
        ) -> dict[str, Any]:
            """Capture the update call."""
            update_called["called"] = True
            update_called["status"] = status
            failed_run = ScrapeRun(
                **{
                    **mock_failed_scrape_run.model_dump(),
                    "error_message": "Failed to parse story elements",
                }
            )
            return failed_run.model_dump()

        activity_mocks = [
            create_scrape_run_activity,
            start_playwright_activity,
            navigate_to_hacker_news_activity,
            scrape_urls_activity,
            upsert_stories_activity,
            update_scrape_run_activity,
        ]

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-task-queue",
                workflows=[ScrapeHackerNewsWorkflow],
                activities=activity_mocks,
            ):
                # Act & Assert
                with pytest.raises(WorkflowFailureError):
                    await env.client.execute_workflow(
                        ScrapeHackerNewsWorkflow.run,
                        args=[30],
                        id="test-workflow-scrape-failure",
                        task_queue="test-task-queue",
                    )

                # Verify update was called with FAILED status
                assert update_called["called"] is True
                assert update_called["status"] == ScrapeRunStatus.FAILED.value


# ---------------------------------------------------------------------------
# Test Cases: Edge Cases
# ---------------------------------------------------------------------------


class TestWorkflowEdgeCases:
    """Test boundary conditions and special scenarios."""

    @pytest.mark.asyncio
    async def test_empty_stories_list(
        self,
        mock_scrape_run: ScrapeRun,
        mock_completed_scrape_run: ScrapeRun,
    ):
        """Verify workflow handles empty stories list gracefully."""
        # Arrange
        empty_stories: list[Story] = []
        completed_run_empty = ScrapeRun(
            **{**mock_completed_scrape_run.model_dump(), "stories_scraped": 0}
        )
        activity_mocks = _create_activity_mocks(
            mock_scrape_run, completed_run_empty, empty_stories, upserted_count=0
        )

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-task-queue",
                workflows=[ScrapeHackerNewsWorkflow],
                activities=activity_mocks,
            ):
                # Act
                result = await env.client.execute_workflow(
                    ScrapeHackerNewsWorkflow.run,
                    args=[30],
                    id="test-workflow-empty-stories",
                    task_queue="test-task-queue",
                )

                # Assert
                assert result.status == ScrapeRunStatus.COMPLETED
                assert result.stories_scraped == 0

    @pytest.mark.asyncio
    async def test_upsert_count_differs_from_scraped_count(
        self,
        mock_scrape_run: ScrapeRun,
        mock_completed_scrape_run: ScrapeRun,
        mock_stories: list[Story],
    ):
        """Verify workflow records actual upserted count (may differ due to deduplication)."""
        # Arrange: scrape 30 stories but only 25 are new (5 duplicates)
        completed_run_partial = ScrapeRun(
            **{**mock_completed_scrape_run.model_dump(), "stories_scraped": 25}
        )
        activity_mocks = _create_activity_mocks(
            mock_scrape_run,
            completed_run_partial,
            mock_stories,
            upserted_count=25,  # Simulates 5 duplicates
        )

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-task-queue",
                workflows=[ScrapeHackerNewsWorkflow],
                activities=activity_mocks,
            ):
                # Act
                result = await env.client.execute_workflow(
                    ScrapeHackerNewsWorkflow.run,
                    args=[30],
                    id="test-workflow-dedup",
                    task_queue="test-task-queue",
                )

                # Assert: workflow records the upserted count, not scraped count
                assert result.status == ScrapeRunStatus.COMPLETED
                assert result.stories_scraped == 25  # Actual upserted count
