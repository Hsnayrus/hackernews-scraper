"""Domain exceptions.

All application exceptions are domain-level. Infrastructure errors (Playwright
crashes, network timeouts, DB driver errors) are caught at the activity boundary
and re-raised as the appropriate domain exception here.

Hierarchy:
    HackerNewsScraperError          — root for all application errors
    ├── BrowserError                — browser lifecycle / Playwright errors
    │   ├── BrowserStartError      — failure to launch or initialise the browser
    │   └── BrowserNavigationError — failure to navigate to a target URL
    ├── ParseError                  — unexpected DOM structure during scraping
    └── PersistenceError            — database persistence errors
        ├── PersistenceTransientError    — transient, safe to retry
        └── PersistenceValidationError   — non-retryable, indicates a bug

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


class BrowserNavigationError(BrowserError):
    """Raised when the browser fails to navigate to a target URL.

    This covers:
    - Network timeout during page.goto()
    - DNS resolution failure
    - Page load timeout (domcontentloaded not reached within timeout)
    - Unexpected page content (captcha, error page, unexpected title)
    - Expected DOM elements not present after navigation

    All cases are transient and retryable — Temporal will apply
    BROWSER_RETRY_POLICY. The activity captures a screenshot before raising
    so that failure state is preserved for post-mortem inspection.
    """


# ---------------------------------------------------------------------------
# Scraping / parsing errors
# ---------------------------------------------------------------------------


class ParseError(HackerNewsScraperError):
    """Raised when the HN DOM structure cannot be parsed as expected.

    This covers:
    - A required element (hn_id, title) is absent from a story row.
    - Zero stories were extractable from an otherwise non-empty page.
    - The page DOM has changed in a way that breaks all selector logic.

    ParseError is always non-retryable: the same DOM will produce the same
    failure on every attempt. The activity wraps this in
    `ApplicationError(non_retryable=True)` before raising to Temporal so
    that the workflow fails immediately rather than exhausting retry budget.
    """


# ---------------------------------------------------------------------------
# Persistence errors
# ---------------------------------------------------------------------------


class PersistenceError(HackerNewsScraperError):
    """Base class for all database persistence errors."""


class PersistenceTransientError(PersistenceError):
    """Transient database error that is safe to retry.

    This covers:
    - Connection pool exhaustion (asyncpg.TooManyConnectionsError)
    - Connection lost mid-operation (asyncpg.InterfaceError)
    - Deadlock or lock timeout (asyncpg.DeadlockDetectedError)
    - Generic SQLAlchemy OperationalError

    Temporal will retry activities raising this exception using the
    configured DB_RETRY_POLICY.
    """


class PersistenceValidationError(PersistenceError):
    """Non-retryable database error indicating a programming or schema bug.

    This covers:
    - Unexpected unique constraint violations (not handled by upsert logic)
    - Foreign key violations
    - Not-null constraint violations on required fields
    - Row not found when an update was expected to match

    The activity wraps this in `ApplicationError(non_retryable=True)` before
    raising to Temporal so the workflow fails immediately.
    """
