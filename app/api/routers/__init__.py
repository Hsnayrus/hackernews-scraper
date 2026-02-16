"""API routers.

Each router module defines endpoints for a specific domain:
  - scrape: Workflow triggering endpoints (POST /scrape)
  - stories: Story query endpoints (GET /stories)
  - runs: Scrape run metadata endpoints (GET /runs)
"""

from app.api.routers.runs import router as runs_router
from app.api.routers.scrape import router as scrape_router
from app.api.routers.stories import router as stories_router

__all__ = ["scrape_router", "stories_router", "runs_router"]
