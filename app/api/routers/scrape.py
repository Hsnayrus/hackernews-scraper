"""Scrape workflow triggering endpoints.

This module provides the POST /scrape endpoint for triggering new
ScrapeHackerNewsWorkflow executions via the Temporal client.
"""

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, HTTPException, status
from temporalio.client import WorkflowFailureError
from temporalio.service import RPCError

from app.api.dependencies import TemporalClientDep
from app.api.models import ScrapeRequest, ScrapeResponse
from app.config import constants

router = APIRouter(prefix="/scrape", tags=["scrape"])


def _generate_workflow_id() -> str:
    """Generate a unique workflow ID for a new scrape execution.

    Format: scrape-{iso_timestamp}-{short_uuid}
    Example: scrape-2026-02-15T10:30:45Z-a7b3c9d2

    The timestamp provides sortability and human-readability.
    The UUID suffix ensures uniqueness even for concurrent requests.

    Returns:
        A unique workflow ID string.
    """
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    short_uuid = str(uuid.uuid4())[:8]
    return f"scrape-{timestamp}-{short_uuid}"


@router.post(
    "",
    response_model=ScrapeResponse,
    status_code=status.HTTP_200_OK,
    summary="Trigger a new scraping workflow",
    description=(
        "Starts a new ScrapeHackerNewsWorkflow execution via Temporal. "
        "Returns immediately with the workflow ID (fire-and-forget pattern). "
        "Use the workflow ID to track execution progress in the Temporal UI "
        "or query scrape run status via the /runs endpoint."
    ),
)
async def trigger_scrape(
    request: ScrapeRequest,
    client: TemporalClientDep,
) -> ScrapeResponse:
    """Trigger a new Hacker News scraping workflow.

    This endpoint validates the request, generates a unique workflow ID,
    and starts the ScrapeHackerNewsWorkflow on the Temporal task queue.
    It returns immediately without waiting for workflow completion.

    Args:
        request: Request body containing optional num_stories parameter.
        client: Temporal client (injected dependency).

    Returns:
        Response containing workflow_id and status="STARTED".

    Raises:
        HTTPException 422: Invalid num_stories value (not 1-120).
        HTTPException 500: Temporal workflow start failed.
        HTTPException 503: Temporal service unavailable.
    """
    log = structlog.get_logger().bind(
        service=constants.SERVICE_NAME,
        endpoint="/scrape",
    )

    # Determine number of stories to scrape (use default if not provided)
    num_stories = request.num_stories if request.num_stories is not None else constants.SCRAPE_TOP_N

    # Generate unique workflow ID
    workflow_id = _generate_workflow_id()

    log.info(
        "api.scrape.request",
        workflow_id=workflow_id,
        num_stories=num_stories,
    )

    try:
        # Start workflow execution (fire-and-forget)
        handle = await client.start_workflow(
            workflow="ScrapeHackerNewsWorkflow",
            arg=num_stories,
            id=workflow_id,
            task_queue=constants.TEMPORAL_TASK_QUEUE,
            execution_timeout=timedelta(minutes=10),
        )

        log.info(
            "api.scrape.workflow_started",
            workflow_id=workflow_id,
            run_id=handle.first_execution_run_id,
            num_stories=num_stories,
        )

        return ScrapeResponse(
            workflow_id=workflow_id,
            status="STARTED",
        )

    except RPCError as exc:
        # Temporal server communication error (connection refused, timeout, etc.)
        log.error(
            "api.scrape.temporal_rpc_error",
            workflow_id=workflow_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Temporal service unavailable: {exc}",
        ) from exc

    except WorkflowFailureError as exc:
        # Workflow start failed (should be rare for start_workflow)
        log.error(
            "api.scrape.workflow_start_failed",
            workflow_id=workflow_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start workflow: {exc}",
        ) from exc

    except Exception as exc:
        # Unexpected error
        log.error(
            "api.scrape.unexpected_error",
            workflow_id=workflow_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while starting the workflow.",
        ) from exc
