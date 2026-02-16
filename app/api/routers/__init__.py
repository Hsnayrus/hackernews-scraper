"""API routers.

Each router module defines endpoints for a specific domain:
  - scrape: Workflow triggering endpoints
  - stories: Story query endpoints (future)
  - runs: Scrape run metadata endpoints (future)
"""

from app.api.routers.scrape import router as scrape_router

__all__ = ["scrape_router"]
