"""Scrape runs query endpoint.

Exposes:
    GET /runs                   — Return scrape run execution metadata.
    GET /runs/{workflow_id}     — Return a single run by Temporal workflow ID.
"""

from __future__ import annotations

from typing import Optional

import sqlalchemy.exc
import structlog
from fastapi import APIRouter, HTTPException, Path, Query, status

from app.api.dependencies import ScrapeRunRepoDep
from app.api.models import RunsResponse, ScrapeRunResponse
from app.config import constants
from app.domain.exceptions import PersistenceTransientError
from app.domain.models import ScrapeRun, ScrapeRunStatus

router = APIRouter(prefix="/runs", tags=["runs"])


def _to_run_response(r: ScrapeRun) -> ScrapeRunResponse:
    """Map a ScrapeRun domain model to its API response shape."""
    return ScrapeRunResponse(
        id=r.id,
        workflow_id=r.workflow_id,
        started_at=r.started_at,
        finished_at=r.finished_at,
        status=r.status.value,
        stories_scraped=r.stories_scraped,
        error_message=r.error_message,
    )


@router.get(
    "",
    response_model=RunsResponse,
    status_code=status.HTTP_200_OK,
    summary="List scrape run history",
    description=(
        "Returns metadata about previous ScrapeHackerNewsWorkflow executions, "
        "ordered by start time descending (most recent first). "
        "Use `limit` to cap the result count and `status` to filter by lifecycle state."
    ),
)
async def list_runs(
    repo: ScrapeRunRepoDep,
    limit: int = Query(default=50, ge=1, le=200, description="Maximum runs to return."),
    status_filter: Optional[ScrapeRunStatus] = Query(
        default=None,
        alias="status",
        description="Filter by lifecycle status: PENDING, RUNNING, COMPLETED, or FAILED.",
    ),
) -> RunsResponse:
    """Return scrape run execution history, most recent first.

    Args:
        repo:          ScrapeRunRepository (injected).
        limit:         Maximum number of runs to return (1–200).
        status_filter: If provided, only return runs in this lifecycle state.

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
        status_filter=status_filter.value if status_filter else None,
    )

    log.info("api.runs.request", status="starting")

    try:
        runs = await repo.list(limit=limit, status=status_filter)
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

    run_responses = [_to_run_response(r) for r in runs]

    log.info(
        "api.runs.response",
        status="completed",
        count=len(run_responses),
    )

    return RunsResponse(runs=run_responses, count=len(run_responses))


@router.get(
    "/{workflow_id}",
    response_model=ScrapeRunResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a scrape run by workflow ID",
    description=(
        "Returns the scrape run associated with a Temporal workflow execution ID. "
        "The workflow ID is returned by POST /scrape when a new run is triggered."
    ),
)
async def get_run(
    repo: ScrapeRunRepoDep,
    workflow_id: str = Path(description="Temporal workflow execution ID returned by POST /scrape."),
) -> ScrapeRunResponse:
    """Return a single scrape run by its Temporal workflow execution ID.

    Args:
        repo:        ScrapeRunRepository (injected).
        workflow_id: Temporal workflow execution ID (e.g. ``scrape-hn-<uuid>``).

    Returns:
        ScrapeRunResponse for the matched run.

    Raises:
        HTTPException 404: No run found for the given workflow_id.
        HTTPException 503: Transient database error.
        HTTPException 500: Unexpected database error.
    """
    log = structlog.get_logger().bind(
        service=constants.SERVICE_NAME,
        endpoint="/runs/{workflow_id}",
        workflow_id=workflow_id,
    )

    log.info("api.runs.get_by_workflow_id.request", status="starting")

    try:
        run = await repo.get_by_workflow_id(workflow_id)
    except PersistenceTransientError as exc:
        log.error(
            "api.runs.get_by_workflow_id.db_transient_error",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable. Please retry.",
        ) from exc
    except sqlalchemy.exc.SQLAlchemyError as exc:
        log.error(
            "api.runs.get_by_workflow_id.db_error",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected database error occurred.",
        ) from exc

    if run is None:
        log.warning(
            "api.runs.get_by_workflow_id.not_found",
            workflow_id=workflow_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No scrape run found for workflow_id '{workflow_id}'.",
        )

    log.info(
        "api.runs.get_by_workflow_id.response",
        status="completed",
        run_status=run.status.value,
    )

    return _to_run_response(run)
