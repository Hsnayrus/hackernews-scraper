"""Domain exceptions.

All application exceptions are domain-level. Infrastructure errors (Playwright
crashes, network timeouts, DB driver errors) are caught at the activity boundary
and re-raised as the appropriate domain exception here.

Hierarchy:
    HackerNewsScraperError          — root for all application errors
    ├── BrowserError                — browser lifecycle / Playwright errors
    │   └── BrowserStartError      — failure to launch or initialise the browser
    └── (future: PersistenceError, ParseError, …)

Rules:
- No bare `except` anywhere in the codebase — always catch a specific type.
- Infrastructure errors (playwright.Error, asyncpg exceptions, etc.) are mapped
  to domain errors at the activity boundary; callers only see domain errors.
- Temporal retry classification is driven by these types: retryable failures
  are left to propagate (Temporal retries by default); non-retryable failures
  are wrapped in ApplicationError(non_retryable=True) at the call site.
"""

from __future__ import annotations


class HackerNewsScraperError(Exception):
    """Root exception for all application-level errors."""


# ---------------------------------------------------------------------------
# Browser errors
# ---------------------------------------------------------------------------


class BrowserError(HackerNewsScraperError):
    """Base class for all Playwright / browser lifecycle errors."""


class BrowserStartError(BrowserError):
    """Raised when the browser fails to launch or initialise.

    This covers:
    - Playwright binary not found (infra misconfiguration — non-retryable)
    - Chromium process failed to start (transient — retryable)
    - Browser context or page creation failed (transient — retryable)

    The activity is responsible for wrapping this in
    `temporalio.exceptions.ApplicationError(non_retryable=True)` when the
    failure is a hard infra misconfiguration (e.g. missing binary).
    """
