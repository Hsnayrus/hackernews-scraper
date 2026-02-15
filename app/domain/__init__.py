"""Domain layer public API.

Import domain types from here rather than from app.domain.models directly.
This keeps the internal module structure free to change without breaking callers.
"""

from app.domain.exceptions import BrowserError, BrowserStartError, HackerNewsScraperError
from app.domain.models import ScrapeRun, ScrapeRunStatus, Story

__all__ = [
    # Models
    "ScrapeRun",
    "ScrapeRunStatus",
    "Story",
    # Exceptions
    "HackerNewsScraperError",
    "BrowserError",
    "BrowserStartError",
]
