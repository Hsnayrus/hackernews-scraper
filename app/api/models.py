"""API request and response models.

Pydantic models for FastAPI endpoint validation and OpenAPI schema generation.
These models are separate from domain models to maintain clean architecture:

    - Domain models (app.domain.models) represent business entities
    - API models (this module) represent HTTP contracts

If the shapes happen to align, API models may wrap or reference domain models.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# POST /scrape
# ---------------------------------------------------------------------------


class ScrapeRequest(BaseModel):
    """Request body for POST /scrape endpoint.

    Triggers a new ScrapeHackerNewsWorkflow execution.
    """

    num_stories: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Number of top stories to scrape from Hacker News. "
            "If not provided, defaults to SCRAPE_TOP_N from environment config."
        ),
    )


class ScrapeResponse(BaseModel):
    """Response body for POST /scrape endpoint.

    Returned immediately after workflow is started (fire-and-forget pattern).
    """

    workflow_id: str = Field(
        description="Unique Temporal workflow execution ID. Use this to track workflow progress."
    )
    status: str = Field(
        description="Workflow execution status. Always 'STARTED' for successful requests."
    )


# ---------------------------------------------------------------------------
# GET /stories
# ---------------------------------------------------------------------------


class StoryResponse(BaseModel):
    """Response body for a single story in GET /stories."""

    id: uuid.UUID
    hn_id: str = Field(description="Hacker News item ID.")
    title: str
    url: Optional[str] = Field(default=None, description="External URL. None for Ask/Show HN posts.")
    rank: int = Field(description="Front-page rank at time of scrape (1-indexed).")
    points: int
    author: str
    comments_count: int
    top_comment: Optional[str] = Field(
        default=None,
        description=(
            "The top comment from the story's HN page. "
            "None if the story has no comments or comment scraping failed."
        ),
    )
    scraped_at: datetime
    created_at: datetime


class StoriesResponse(BaseModel):
    """Response envelope for GET /stories."""

    stories: list[StoryResponse]
    count: int = Field(description="Number of stories returned.")


# ---------------------------------------------------------------------------
# GET /runs
# ---------------------------------------------------------------------------


class ScrapeRunResponse(BaseModel):
    """Response body for a single scrape run in GET /runs."""

    id: uuid.UUID
    workflow_id: str = Field(description="Temporal workflow execution ID.")
    started_at: datetime
    finished_at: Optional[datetime] = None
    status: str = Field(description="Lifecycle status: PENDING, RUNNING, COMPLETED, or FAILED.")
    stories_scraped: Optional[int] = None
    error_message: Optional[str] = None


class RunsResponse(BaseModel):
    """Response envelope for GET /runs."""

    runs: list[ScrapeRunResponse]
    count: int = Field(description="Number of runs returned.")
