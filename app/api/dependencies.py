"""FastAPI dependency injection providers.

Dependencies are injected via function parameters using `Depends()`. This
pattern enables:
  - Clean separation between infrastructure (Temporal client) and handlers
  - Easy testing (swap real client for mock)
  - Explicit lifecycle management (client stored in app.state)

Example usage:
    @router.post("/scrape")
    async def trigger_scrape(
        client: Annotated[Client, Depends(get_temporal_client)]
    ):
        # Use client here
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from temporalio.client import Client


def get_temporal_client(request: Request) -> Client:
    """Dependency that provides the singleton Temporal client.

    The client is created once at application startup (in the lifespan
    context manager) and stored in `app.state.temporal_client`. This
    dependency retrieves it from app state.

    Args:
        request: FastAPI request object (injected automatically).

    Returns:
        The singleton Temporal client instance.

    Raises:
        HTTPException: 503 Service Unavailable if client not initialized.
    """
    client: Client | None = getattr(request.app.state, "temporal_client", None)

    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Temporal client not initialized. Service is starting or shutting down.",
        )

    return client


# Type alias for use in route handler signatures
TemporalClientDep = Annotated[Client, Depends(get_temporal_client)]
