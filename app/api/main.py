"""FastAPI application entry point.

Routers for /scrape, /stories, and /runs are registered here as they are
implemented. For now the application exposes a single /health endpoint so
the container health check and smoke tests have a stable target.
"""

from fastapi import FastAPI

from app.config import constants

app = FastAPI(
    title="HackerNews Scraper",
    description="Production-grade HN scraping service.",
    version="0.1.0",
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": constants.SERVICE_NAME}
