"""API request and response models.

Pydantic models for FastAPI endpoint validation and OpenAPI schema generation.
These models are separate from domain models to maintain clean architecture:

    - Domain models (app.domain.models) represent business entities
    - API models (this module) represent HTTP contracts

If the shapes happen to align, API models may wrap or reference domain models.
"""

from typing import Optional

from pydantic import BaseModel, Field


class ScrapeRequest(BaseModel):
    """Request body for POST /scrape endpoint.

    Triggers a new ScrapeHackerNewsWorkflow execution.
    """

    num_stories: Optional[int] = Field(
        default=None,
        ge=1,
        le=120,
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
