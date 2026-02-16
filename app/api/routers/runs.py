"""Scrape runs query endpoint.

Exposes:
    GET /runs — Return scrape run execution metadata.
"""

from __future__ import annotations

import sqlalchemy.exc
import structlog
from fastapi import APIRouter, HTTPException, Query, status

from app.api.dependencies import ScrapeRunRepoDep
from app.api.models import RunsResponse, ScrapeRunResponse
from app.config import constants
from app.domain.exceptions import PersistenceTransientError

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get(
    "",
    response_model=RunsResponse,
    status_code=status.HTTP_200_OK,
    summary="List scrape run history",
    description=(
        "Returns metadata about previous ScrapeHackerNewsWorkflow executions, "
        "ordered by start time descending (most recent first). "
        "Use `limit` to cap the result count."
    ),
)
async def list_runs(
    repo: ScrapeRunRepoDep,
    limit: int = Query(default=50, ge=1, le=200, description="Maximum runs to return."),
) -> RunsResponse:
    """Return scrape run execution history, most recent first.

    Args:
        repo:  ScrapeRunRepository (injected).
        limit: Maximum number of runs to return (1–200).

    Returns:
        RunsResponse containing a list of runs and their count.

    Raises:
        HTTPException 503: Transient database error.
        HTTPException 500: Unexpected database error.
    """
    log = structlog.get_logger().bind(
        service=constants.SERVICE_NAME,
        endpoint="/runs",
        limit=limit,
    )

    log.info("api.runs.request", status="starting")

    try:
        runs = await repo.list(limit=limit)
    except PersistenceTransientError as exc:
        log.error(
            "api.runs.db_transient_error",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable. Please retry.",
        ) from exc
    except sqlalchemy.exc.SQLAlchemyError as exc:
        log.error(
            "api.runs.db_error",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected database error occurred.",
        ) from exc

    run_responses = [
        ScrapeRunResponse(
            id=r.id,
            workflow_id=r.workflow_id,
            started_at=r.started_at,
            finished_at=r.finished_at,
            status=r.status.value,
            stories_scraped=r.stories_scraped,
            error_message=r.error_message,
        )
        for r in runs
    ]

    log.info(
        "api.runs.response",
        status="completed",
        count=len(run_responses),
    )

    return RunsResponse(runs=run_responses, count=len(run_responses))
