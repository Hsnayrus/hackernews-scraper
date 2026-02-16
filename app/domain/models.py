"""Domain models.

Pure data layer — no infrastructure, no configuration, no I/O.
Every other layer imports from here; this module imports nothing internal.

Pydantic v2 is used for:
  - Field validation at construction time
  - JSON serialisation (Temporal data converter, API responses)
  - OpenAPI schema generation (FastAPI)

All models are frozen (immutable). Temporal workflows must never mutate
objects from workflow history; frozen models enforce this at the type level.

Temporal serialisation notes:
  - UUID fields serialise to/from strings automatically (Pydantic v2 default)
  - datetime fields serialise to ISO-8601 strings automatically
  - No custom DataConverter required
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    """Return the current UTC time.

    Defined as a module-level function so it can be used as a default_factory
    inside Pydantic models. Must never be called from within a Temporal
    Workflow — only from Activities (where side effects are allowed).
    """
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ScrapeRunStatus(str, Enum):
    """Lifecycle states for a single scrape workflow execution.

    Inherits from str so that JSON serialisation produces the raw value
    ("PENDING", "RUNNING", …) without a custom encoder.

      PENDING  → the scrape run record has been created, work not yet started
      RUNNING  → the scraping activity is in progress
      COMPLETED → all stories scraped and persisted successfully
      FAILED   → the workflow terminated with an unrecoverable error
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Story
# ---------------------------------------------------------------------------


class Story(BaseModel):
    """A single Hacker News story extracted from the front page.

    `hn_id` is the canonical business key — all deduplication and upsert
    logic must key on this field, not on `id`.

    `url` is Optional because Ask HN / Show HN submissions have no external
    URL; the HN item URL can be used as a fallback by the scraping activity.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    hn_id: str = Field(
        description="Hacker News item ID (e.g. '12345678'). Unique business key."
    )
    title: str = Field(description="Story headline as displayed on the HN front page.")
    url: Optional[str] = Field(
        default=None,
        description=(
            "External URL the story links to. None for Ask HN / Show HN posts "
            "that have no external link."
        ),
    )
    rank: int = Field(ge=1, description="Position on the front page (1-indexed).")
    points: int = Field(ge=0, description="Upvote count at time of scrape.")
    author: str = Field(description="HN username of the submitter.")
    comments_count: int = Field(
        ge=0, description="Number of comments at time of scrape."
    )
    scraped_at: datetime = Field(
        default_factory=_utcnow,
        description="UTC timestamp when this story was scraped.",
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        description="UTC timestamp when this record was first persisted.",
    )


# ---------------------------------------------------------------------------
# ScrapeRun
# ---------------------------------------------------------------------------


class ScrapeRun(BaseModel):
    """Execution metadata for one invocation of ScrapeHackerNewsWorkflow.

    A ScrapeRun is created at workflow start (status=PENDING) and updated
    at workflow completion (status=COMPLETED or FAILED). Because the model
    is frozen, updates are expressed by constructing a new instance with
    `model_copy(update={...})`.

    `workflow_id` maps 1-to-1 with the Temporal workflow execution ID,
    enabling correlation between Temporal UI/logs and the database record.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    workflow_id: str = Field(
        description="Temporal workflow execution ID. Unique per run."
    )
    started_at: datetime = Field(
        default_factory=_utcnow,
        description="UTC timestamp when the workflow started.",
    )
    finished_at: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp when the workflow completed or failed. None while running.",
    )
    status: ScrapeRunStatus = Field(
        default=ScrapeRunStatus.PENDING,
        description="Current lifecycle state of this scrape run.",
    )
    stories_scraped: Optional[int] = Field(
        default=None,
        ge=0,
        description="Number of stories successfully upserted. None until completion.",
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Human-readable error description. None unless status=FAILED.",
    )
