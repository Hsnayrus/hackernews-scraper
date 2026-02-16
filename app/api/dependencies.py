"""FastAPI dependency injection providers.

Dependencies are injected via function parameters using `Depends()`. This
pattern enables:
  - Clean separation between infrastructure and route handlers
  - Easy test substitution (swap real repo/client for mock)
  - Explicit lifecycle management (Temporal client stored in app.state)

Example usage:
    @router.get("/stories")
    async def list_stories(
        repo: StoryRepoDep,
    ):
        return await repo.list()
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from temporalio.client import Client

from app.infra.repositories import ScrapeRunRepository, StoryRepository


# ---------------------------------------------------------------------------
# Temporal client
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Database repositories
#
# Repositories are stateless — instantiating one per request is cheap and
# ensures no cross-request state leakage. The underlying AsyncEngine (and
# its connection pool) is a module-level singleton shared across all requests.
# ---------------------------------------------------------------------------


def get_story_repository() -> StoryRepository:
    """Dependency that provides a StoryRepository instance."""
    return StoryRepository()


def get_scrape_run_repository() -> ScrapeRunRepository:
    """Dependency that provides a ScrapeRunRepository instance."""
    return ScrapeRunRepository()


# Type aliases for use in route handler signatures
StoryRepoDep = Annotated[StoryRepository, Depends(get_story_repository)]
ScrapeRunRepoDep = Annotated[ScrapeRunRepository, Depends(get_scrape_run_repository)]
