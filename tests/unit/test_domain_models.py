"""Unit tests for app.domain.models — Story, ScrapeRun, ScrapeRunStatus.

Coverage targets
----------------
- ScrapeRunStatus: enum values, str subclass, construction from string
- Story: creation, UUID generation, optional url, frozen immutability,
         field constraints (rank >= 1, points >= 0, comments_count >= 0),
         timezone-aware default timestamps
- ScrapeRun: creation, default fields (PENDING status, None finished_at),
             frozen immutability, stories_scraped >= 0 constraint,
             completed and failed states
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.domain.models import ScrapeRun, ScrapeRunStatus, Story


# ---------------------------------------------------------------------------
# TestScrapeRunStatus
# ---------------------------------------------------------------------------


class TestScrapeRunStatus:
    """Tests for the ScrapeRunStatus string enum."""

    def test_pending_value(self) -> None:
        assert ScrapeRunStatus.PENDING == "PENDING"

    def test_running_value(self) -> None:
        assert ScrapeRunStatus.RUNNING == "RUNNING"

    def test_completed_value(self) -> None:
        assert ScrapeRunStatus.COMPLETED == "COMPLETED"

    def test_failed_value(self) -> None:
        assert ScrapeRunStatus.FAILED == "FAILED"

    def test_is_str_subclass_for_json_serialisation(self) -> None:
        """Inherits from str so Pydantic serialises as the raw string without a custom encoder."""
        for status in ScrapeRunStatus:
            assert isinstance(status, str)

    def test_construction_from_string(self) -> None:
        assert ScrapeRunStatus("PENDING") is ScrapeRunStatus.PENDING
        assert ScrapeRunStatus("RUNNING") is ScrapeRunStatus.RUNNING
        assert ScrapeRunStatus("COMPLETED") is ScrapeRunStatus.COMPLETED
        assert ScrapeRunStatus("FAILED") is ScrapeRunStatus.FAILED

    def test_invalid_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            ScrapeRunStatus("UNKNOWN")

    def test_all_four_members_exist(self) -> None:
        members = {s.value for s in ScrapeRunStatus}
        assert members == {"PENDING", "RUNNING", "COMPLETED", "FAILED"}


# ---------------------------------------------------------------------------
# TestStory — helpers
# ---------------------------------------------------------------------------


def _make_story(**overrides: object) -> Story:
    defaults: dict[str, object] = {
        "hn_id": "12345678",
        "title": "Show HN: My Weekend Project",
        "url": "https://example.com/project",
        "rank": 1,
        "points": 150,
        "author": "hackeruser",
        "comments_count": 42,
    }
    defaults.update(overrides)
    return Story(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TestStory
# ---------------------------------------------------------------------------


class TestStoryCreation:
    """Test Story construction with valid inputs."""

    def test_all_required_fields_stored(self) -> None:
        story = _make_story()
        assert story.hn_id == "12345678"
        assert story.title == "Show HN: My Weekend Project"
        assert story.url == "https://example.com/project"
        assert story.rank == 1
        assert story.points == 150
        assert story.author == "hackeruser"
        assert story.comments_count == 42

    def test_auto_generates_uuid_id(self) -> None:
        story = _make_story()
        assert isinstance(story.id, uuid.UUID)

    def test_two_instances_have_different_ids(self) -> None:
        s1 = _make_story()
        s2 = _make_story()
        assert s1.id != s2.id

    def test_custom_id_is_preserved(self) -> None:
        custom_id = uuid.uuid4()
        story = _make_story(id=custom_id)
        assert story.id == custom_id

    def test_url_is_none_for_ask_hn_posts(self) -> None:
        """Ask HN / Show HN posts without an external link have url=None."""
        story = _make_story(url=None)
        assert story.url is None

    def test_scraped_at_defaults_to_utc_aware(self) -> None:
        story = _make_story()
        assert story.scraped_at.tzinfo is not None
        assert story.scraped_at.tzinfo == timezone.utc

    def test_created_at_defaults_to_utc_aware(self) -> None:
        story = _make_story()
        assert story.created_at.tzinfo is not None
        assert story.created_at.tzinfo == timezone.utc

    def test_custom_timestamps_are_accepted(self) -> None:
        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        story = _make_story(scraped_at=ts, created_at=ts)
        assert story.scraped_at == ts
        assert story.created_at == ts


class TestStoryImmutability:
    """Test that Story is frozen (immutable after construction)."""

    def test_cannot_mutate_rank(self) -> None:
        story = _make_story()
        with pytest.raises(Exception):  # ValidationError on pydantic v2 frozen models
            story.rank = 2  # type: ignore[misc]

    def test_cannot_mutate_title(self) -> None:
        story = _make_story()
        with pytest.raises(Exception):
            story.title = "Changed Title"  # type: ignore[misc]

    def test_cannot_mutate_url(self) -> None:
        story = _make_story()
        with pytest.raises(Exception):
            story.url = "https://changed.example.com"  # type: ignore[misc]


class TestStoryFieldConstraints:
    """Test Pydantic field-level validation on Story."""

    def test_rank_must_be_at_least_1(self) -> None:
        with pytest.raises(ValidationError):
            _make_story(rank=0)

    def test_rank_of_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_story(rank=0)

    def test_negative_rank_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_story(rank=-1)

    def test_rank_of_1_is_valid(self) -> None:
        story = _make_story(rank=1)
        assert story.rank == 1

    def test_large_rank_is_valid(self) -> None:
        story = _make_story(rank=9999)
        assert story.rank == 9999

    def test_negative_points_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_story(points=-1)

    def test_zero_points_is_valid(self) -> None:
        story = _make_story(points=0)
        assert story.points == 0

    def test_negative_comments_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_story(comments_count=-1)

    def test_zero_comments_count_is_valid(self) -> None:
        story = _make_story(comments_count=0)
        assert story.comments_count == 0


# ---------------------------------------------------------------------------
# TestScrapeRun — helpers
# ---------------------------------------------------------------------------


def _make_scrape_run(**overrides: object) -> ScrapeRun:
    defaults: dict[str, object] = {
        "workflow_id": "scrape-2026-02-15T10:00:00Z-abc12345",
    }
    defaults.update(overrides)
    return ScrapeRun(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TestScrapeRun
# ---------------------------------------------------------------------------


class TestScrapeRunCreation:
    """Test ScrapeRun construction with valid inputs."""

    def test_workflow_id_stored(self) -> None:
        run = _make_scrape_run()
        assert run.workflow_id == "scrape-2026-02-15T10:00:00Z-abc12345"

    def test_auto_generates_uuid_id(self) -> None:
        run = _make_scrape_run()
        assert isinstance(run.id, uuid.UUID)

    def test_two_instances_have_different_ids(self) -> None:
        r1 = _make_scrape_run()
        r2 = _make_scrape_run()
        assert r1.id != r2.id

    def test_default_status_is_pending(self) -> None:
        run = _make_scrape_run()
        assert run.status == ScrapeRunStatus.PENDING

    def test_default_finished_at_is_none(self) -> None:
        run = _make_scrape_run()
        assert run.finished_at is None

    def test_default_stories_scraped_is_none(self) -> None:
        run = _make_scrape_run()
        assert run.stories_scraped is None

    def test_default_error_message_is_none(self) -> None:
        run = _make_scrape_run()
        assert run.error_message is None

    def test_started_at_defaults_to_utc_aware(self) -> None:
        run = _make_scrape_run()
        assert run.started_at.tzinfo is not None
        assert run.started_at.tzinfo == timezone.utc

    def test_completed_run_with_all_fields(self) -> None:
        finished = datetime(2026, 2, 15, 10, 5, 0, tzinfo=timezone.utc)
        run = ScrapeRun(
            workflow_id="wf-completed",
            finished_at=finished,
            status=ScrapeRunStatus.COMPLETED,
            stories_scraped=30,
        )
        assert run.status == ScrapeRunStatus.COMPLETED
        assert run.stories_scraped == 30
        assert run.finished_at == finished
        assert run.error_message is None

    def test_failed_run_with_error_message(self) -> None:
        run = ScrapeRun(
            workflow_id="wf-failed",
            status=ScrapeRunStatus.FAILED,
            error_message="Browser navigation timed out after 60s",
            finished_at=datetime(2026, 2, 15, 10, 2, 0, tzinfo=timezone.utc),
        )
        assert run.status == ScrapeRunStatus.FAILED
        assert run.error_message == "Browser navigation timed out after 60s"
        assert run.stories_scraped is None

    def test_running_status_accepted(self) -> None:
        run = _make_scrape_run(status=ScrapeRunStatus.RUNNING)
        assert run.status == ScrapeRunStatus.RUNNING


class TestScrapeRunImmutability:
    """Test that ScrapeRun is frozen (immutable after construction)."""

    def test_cannot_mutate_status(self) -> None:
        run = _make_scrape_run()
        with pytest.raises(Exception):
            run.status = ScrapeRunStatus.COMPLETED  # type: ignore[misc]

    def test_cannot_mutate_workflow_id(self) -> None:
        run = _make_scrape_run()
        with pytest.raises(Exception):
            run.workflow_id = "changed-id"  # type: ignore[misc]

    def test_cannot_mutate_stories_scraped(self) -> None:
        run = _make_scrape_run()
        with pytest.raises(Exception):
            run.stories_scraped = 100  # type: ignore[misc]


class TestScrapeRunFieldConstraints:
    """Test Pydantic field-level validation on ScrapeRun."""

    def test_negative_stories_scraped_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_scrape_run(stories_scraped=-1)

    def test_zero_stories_scraped_is_valid(self) -> None:
        run = _make_scrape_run(stories_scraped=0)
        assert run.stories_scraped == 0

    def test_large_stories_scraped_is_valid(self) -> None:
        run = _make_scrape_run(stories_scraped=1000)
        assert run.stories_scraped == 1000
