"""Stories query endpoint.

Exposes:
    GET /stories — Return stored stories with optional filtering.
"""

from __future__ import annotations

from typing import Optional

import sqlalchemy.exc
import structlog
from fastapi import APIRouter, HTTPException, Query, status

from app.api.dependencies import StoryRepoDep
from app.api.models import StoriesResponse, StoryResponse
from app.config import constants
from app.domain.exceptions import PersistenceTransientError

router = APIRouter(prefix="/stories", tags=["stories"])


@router.get(
    "",
    response_model=StoriesResponse,
    status_code=status.HTTP_200_OK,
    summary="List scraped stories",
    description=(
        "Returns stored Hacker News stories ordered by rank ascending. "
        "Use `limit` to cap the result count, `min_points` to filter by score, "
        "and `rank_min` / `rank_max` to restrict to a rank range (inclusive)."
    ),
)
async def list_stories(
    repo: StoryRepoDep,
    limit: int = Query(default=1000, ge=1, le=1000,
                       description="Maximum stories to return."),
    min_points: Optional[int] = Query(
        default=None, ge=0, description="Exclude stories with fewer points than this."
    ),
    rank_min: Optional[int] = Query(
        default=None, ge=1, description="Only return stories with rank >= this value."
    ),
    rank_max: Optional[int] = Query(
        default=None, ge=1, description="Only return stories with rank <= this value."
    ),
) -> StoriesResponse:
    """Return stored stories, ordered by rank ascending.

    Args:
        repo:       StoryRepository (injected).
        limit:      Maximum number of stories to return (1–200).
        min_points: Minimum points filter (inclusive).
        rank_min:   Lower bound of rank range filter (inclusive).
        rank_max:   Upper bound of rank range filter (inclusive).

    Returns:
        StoriesResponse containing a list of stories and their count.

    Raises:
        HTTPException 400: rank_min is greater than rank_max.
        HTTPException 503: Transient database error.
        HTTPException 500: Unexpected database error.
    """
    if rank_min is not None and rank_max is not None and rank_min > rank_max:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"rank_min ({rank_min}) must not be greater than rank_max ({rank_max}).",
        )

    log = structlog.get_logger().bind(
        service=constants.SERVICE_NAME,
        endpoint="/stories",
        limit=limit,
        min_points=min_points,
        rank_min=rank_min,
        rank_max=rank_max,
    )

    log.info("api.stories.request", status="starting")

    try:
        stories = await repo.list(
            limit=limit,
            min_points=min_points,
            rank_min=rank_min,
            rank_max=rank_max,
        )
    except PersistenceTransientError as exc:
        log.error(
            "api.stories.db_transient_error",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable. Please retry.",
        ) from exc
    except sqlalchemy.exc.SQLAlchemyError as exc:
        log.error(
            "api.stories.db_error",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected database error occurred.",
        ) from exc

    story_responses = [
        StoryResponse(
            id=s.id,
            hn_id=s.hn_id,
            title=s.title,
            url=s.url,
            rank=s.rank,
            points=s.points,
            author=s.author,
            comments_count=s.comments_count,
            scraped_at=s.scraped_at,
            created_at=s.created_at,
        )
        for s in stories
    ]

    log.info(
        "api.stories.response",
        status="completed",
        count=len(story_responses),
    )

    return StoriesResponse(stories=story_responses, count=len(story_responses))
