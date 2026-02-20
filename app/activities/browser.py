"""Browser activity class.

All Playwright browser activities are methods on `BrowserActivities`. A single
instance of this class is registered with the Temporal Worker so that all
methods share `self` — the only correct way to share an in-memory browser
instance across Temporal activities within one worker process.

State layout:
    BrowserActivities instance (self)
    ├── _playwright: Playwright | None
    ├── _browser:    Browser | None
    ├── _context:    BrowserContext | None
    └── _page:       Page | None

Activity chain (in workflow order):
    1. start_playwright_activity           ← this module (initialises browser)
    2. navigate_to_hacker_news_activity    ← navigates to HN page 1
    3. scrape_urls_activity                ← scrapes current page
    4. navigate_to_next_page_activity      ← navigates to page N (when top_n > 30)
       [3–4 repeat for each additional page]
    5. scrape_top_comment_activity         ← scrapes top comment for each story
    6. cleanup_browser_context_activity    ← tears down workflow's context

Worker-restart resilience (Option A):
    Every activity that needs the browser calls `await self._ensure_browser()`
    before use. If the worker restarted between activities and state was lost,
    `_ensure_browser()` transparently relaunches the browser. `start_playwright
    _activity` is still the explicit first step in the workflow; it just isn't
    the only entry point for browser initialisation.

Retry / timeout constants are exported so workflows can import them directly
rather than duplicating the values.
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path
from typing import Optional

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Error as PlaywrightError,
    Page,
    Playwright,
    async_playwright,
)
from temporalio import activity
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from app.config import constants
from app.domain.exceptions import BrowserNavigationError, BrowserStartError, ParseError
from app.domain.models import Story

# ---------------------------------------------------------------------------
# Activity execution options
# Imported by the workflow when calling workflow.execute_activity_method().
# ---------------------------------------------------------------------------

#: Retry policy for all browser activities.
BROWSER_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
)

#: Time budget for a single attempt of start_playwright_activity.
BROWSER_START_TIMEOUT = timedelta(minutes=1)

#: Time budget for a single attempt of navigate_to_hacker_news_activity.
#: Covers DNS resolution + TCP handshake + TLS + server response + DOM parse.
NAVIGATE_TIMEOUT = timedelta(minutes=1)

#: Time budget for a single attempt of scrape_urls_activity.
#: Covers DOM querying and parsing for up to SCRAPE_TOP_N story rows.
SCRAPE_TIMEOUT = timedelta(minutes=2)

#: Time budget for a single attempt of navigate_to_next_page_activity.
#: Same budget as NAVIGATE_TIMEOUT — identical network operation, different URL.
NAVIGATE_TO_NEXT_PAGE_TIMEOUT = NAVIGATE_TIMEOUT

#: Time budget for a single attempt of cleanup_browser_context_activity.
#: Covers closing the browser context and page for this workflow.
CLEANUP_TIMEOUT = timedelta(seconds=30)

#: Time budget for a single attempt of scrape_top_comment_activity.
#: Covers navigation to comment page + DOM parsing + application-level retries.
SCRAPE_COMMENT_TIMEOUT = timedelta(seconds=30)

#: Number of stories Hacker News renders per results page.
#: This is a fixed property of the HN site, not a configurable parameter.
HN_STORIES_PER_PAGE: int = 30


# ---------------------------------------------------------------------------
# Activity class
# ---------------------------------------------------------------------------


class BrowserActivities:
    """Stateful Temporal activity class managing the Playwright browser lifecycle.

    One instance of this class is passed to `temporalio.worker.Worker` at
    startup. Temporal calls activity methods on that instance, so all methods
    share browser state via `self`.

    Browser isolation strategy:
        - One shared `_browser` instance (lightweight, expensive to launch)
        - Per-workflow `_contexts` and `_pages` keyed by `workflow_id`
        - This ensures concurrent workflows never interfere with each other

    This class must never be instantiated more than once per worker process.
    """

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        # Per-workflow isolation: each workflow gets its own context and page
        self._contexts: dict[str, BrowserContext] = {}
        self._pages: dict[str, Page] = {}

    # -------------------------------------------------------------------------
    # Public activities
    # -------------------------------------------------------------------------

    @activity.defn(name="start_playwright_activity")
    async def start_playwright_activity(self) -> bool:
        """Launch Playwright and create a workflow-specific browser context.

        This is the explicit first activity in the scrape workflow. It creates
        an isolated browser context for this workflow, ensuring concurrent
        workflows never interfere with each other. Subsequent activities call
        `_ensure_browser()` internally, but calling this first makes the
        browser initialisation step visible in the workflow history and
        Temporal UI.

        Returns:
            True when the workflow's browser context is ready.

        Raises:
            ApplicationError(non_retryable=True): Playwright binary not found
                (infrastructure misconfiguration — retrying won't help).
            BrowserStartError: Any other launch failure (retryable).
        """
        info = activity.info()
        log = structlog.get_logger().bind(
            service=constants.SERVICE_NAME,
            activity_name=info.activity_type,
            workflow_id=info.workflow_id,
            run_id=info.workflow_run_id,
            activity_id=info.activity_id,
        )

        log.info("browser_activity.starting", status="starting")
        started_at = time.monotonic()

        try:
            await self._ensure_browser(workflow_id=info.workflow_id, log=log)
        except ApplicationError:
            # Non-retryable infra error — let it propagate as-is.
            raise
        except BrowserStartError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            log.error(
                "browser_activity.failed",
                status="failed",
                error=str(exc),
                duration_ms=duration_ms,
            )
            raise

        duration_ms = int((time.monotonic() - started_at) * 1000)
        log.info(
            "browser_activity.completed",
            status="completed",
            duration_ms=duration_ms,
        )
        return True

    @activity.defn(name="navigate_to_hacker_news_activity")
    async def navigate_to_hacker_news_activity(self) -> bool:
        """Navigate the browser to the Hacker News homepage and verify the page loaded.

        This is the second activity in the scrape workflow. It navigates to
        `constants.HN_BASE_URL`, waits for the DOM to be ready, and verifies
        that the page is a valid HN front page by asserting:
          1. The page title contains "Hacker News".
          2. At least one story row (CSS selector `.athing`) is present.

        `_ensure_browser()` is called at entry so the activity is resilient to
        worker restart between `start_playwright_activity` and this step — the
        browser context is transparently recreated if state was lost.

        Returns:
            True when the browser is positioned on a loaded HN front page.

        Raises:
            ApplicationError(non_retryable=True): Playwright binary not found
                (propagated from _ensure_browser — infra misconfiguration).
            BrowserStartError: Browser could not be (re-)launched (retryable).
            BrowserNavigationError: Navigation or page verification failed
                (retryable). A failure screenshot is captured before raising.
        """
        info = activity.info()
        log = structlog.get_logger().bind(
            service=constants.SERVICE_NAME,
            activity_name=info.activity_type,
            workflow_id=info.workflow_id,
            run_id=info.workflow_run_id,
            activity_id=info.activity_id,
        )

        log.info("navigation.starting", status="starting",
                 url=constants.HN_BASE_URL)
        started_at = time.monotonic()

        try:
            context, page = await self._ensure_browser(
                workflow_id=info.workflow_id, log=log
            )
        except ApplicationError:
            raise
        except BrowserStartError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            log.error(
                "navigation.failed",
                status="failed",
                reason="browser_unavailable",
                error=str(exc),
                duration_ms=duration_ms,
            )
            raise

        try:
            response = await page.goto(
                constants.HN_BASE_URL,
                wait_until="domcontentloaded",
                timeout=constants.BROWSER_TIMEOUT_MS,
            )
        except PlaywrightError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                page, info.activity_type, info.workflow_id
            )
            log.error(
                "navigation.failed",
                status="failed",
                reason="goto_error",
                url=constants.HN_BASE_URL,
                error=str(exc),
                screenshot_path=str(
                    screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"Failed to navigate to {constants.HN_BASE_URL}: {exc}"
            ) from exc

        # An HTTP error response (4xx / 5xx) does not raise in Playwright —
        # check the status code explicitly so we surface it clearly.
        if response is not None and not response.ok:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                page, info.activity_type, info.workflow_id
            )
            log.error(
                "navigation.failed",
                status="failed",
                reason="http_error",
                url=constants.HN_BASE_URL,
                http_status=response.status,
                screenshot_path=str(
                    screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"Unexpected HTTP {response.status} from {constants.HN_BASE_URL}"
            )

        # Verify page identity: title must contain "Hacker News".
        try:
            title = await page.title()
        except PlaywrightError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                page, info.activity_type, info.workflow_id
            )
            log.error(
                "navigation.failed",
                status="failed",
                reason="title_read_error",
                error=str(exc),
                screenshot_path=str(
                    screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"Could not read page title after navigation: {exc}"
            ) from exc

        if "Hacker News" not in title:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                page, info.activity_type, info.workflow_id
            )
            log.error(
                "navigation.failed",
                status="failed",
                reason="unexpected_page",
                page_title=title,
                url=constants.HN_BASE_URL,
                screenshot_path=str(
                    screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"Unexpected page title '{title}' — expected 'Hacker News'. "
                "Site may be returning a captcha or error page."
            )

        # Verify at least one story row is present in the DOM.
        try:
            await page.wait_for_selector(
                ".athing",
                state="attached",
                timeout=constants.BROWSER_TIMEOUT_MS,
            )
        except PlaywrightError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                page, info.activity_type, info.workflow_id
            )
            log.error(
                "navigation.failed",
                status="failed",
                reason="no_stories_found",
                error=str(exc),
                screenshot_path=str(
                    screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"No story rows (.athing) found on {constants.HN_BASE_URL} "
                f"within timeout: {exc}"
            ) from exc

        duration_ms = int((time.monotonic() - started_at) * 1000)
        log.info(
            "navigation.completed",
            status="completed",
            url=constants.HN_BASE_URL,
            page_title=title,
            duration_ms=duration_ms,
        )
        return True

    @activity.defn(name="scrape_urls_activity")
    async def scrape_urls_activity(self, top_n: int) -> list[Story]:
        """Scrape the top N stories from the currently loaded HN front page.

        Third activity in the scrape workflow. Assumes the browser is already
        positioned on a loaded HN page (left by navigate_to_hacker_news_activity)
        and calls _ensure_browser() for worker-restart resilience.

        Args:
            top_n: Number of top stories to scrape.

        Returns:
            list[Story] — parsed stories, serialised by Temporal as JSON.

        Raises:
            ApplicationError(non_retryable=True): Playwright binary missing
                (infra misconfiguration) or DOM parse failure.
            BrowserStartError: Browser could not be re-launched (retryable).
            BrowserNavigationError: Playwright DOM access failed (retryable).
        """
        info = activity.info()
        log = structlog.get_logger().bind(
            service=constants.SERVICE_NAME,
            activity_name=info.activity_type,
            workflow_id=info.workflow_id,
            run_id=info.workflow_run_id,
            activity_id=info.activity_id,
        )

        log.info(
            "scrape.starting",
            status="starting",
            url=constants.HN_BASE_URL,
            expected=top_n,
        )
        started_at = time.monotonic()

        try:
            _, page = await self._ensure_browser(workflow_id=info.workflow_id, log=log)
        except ApplicationError:
            raise
        except BrowserStartError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            log.error(
                "scrape.failed",
                status="failed",
                reason="browser_unavailable",
                error=str(exc),
                duration_ms=duration_ms,
            )
            raise

        try:
            stories = await self._extract_stories(page=page, top_n=top_n, log=log)
        except ApplicationError:
            raise
        except (BrowserNavigationError, ParseError) as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                page, info.activity_type, info.workflow_id
            )
            log.error(
                "scrape.failed",
                status="failed",
                reason=type(exc).__name__,
                error=str(exc),
                screenshot_path=str(
                    screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            if isinstance(exc, ParseError):
                raise ApplicationError(str(exc), non_retryable=True) from exc
            raise

        duration_ms = int((time.monotonic() - started_at) * 1000)
        log.info(
            "scrape.completed",
            status="completed",
            stories_count=len(stories),
            duration_ms=duration_ms,
        )
        return stories

    @activity.defn(name="navigate_to_next_page_activity")
    async def navigate_to_next_page_activity(self, page_number: int) -> bool:
        """Navigate the browser to the specified Hacker News results page.

        Fourth activity in the scrape workflow. Navigates directly to
        ``{HN_BASE_URL}?p={page_number}``, waits for the DOM to be ready, and
        verifies the page is a valid HN front page with story rows.

        Called only when ``top_n > HN_STORIES_PER_PAGE``. Page 1 is loaded by
        ``navigate_to_hacker_news_activity``; this activity is invoked for
        pages 2, 3, … in sequence, once per additional page needed.

        ``_ensure_browser()`` is called at entry so the activity is resilient to
        worker restart between page transitions.

        Args:
            page_number: 1-indexed HN page to navigate to. Must be >= 2.

        Returns:
            True when the browser is positioned on the loaded target page.
            False when the current page has no ``a.morelink`` element, meaning
            HN has no further pages to offer — the caller should stop
            pagination without treating this as an error.

        Raises:
            ApplicationError(non_retryable=True): ``page_number < 2``
                (contract violation — caller logic error) or Playwright binary
                not found (infra misconfiguration).
            BrowserStartError: Browser could not be (re-)launched (retryable).
            BrowserNavigationError: Navigation or page verification failed
                (retryable). A failure screenshot is captured before raising.
        """
        info = activity.info()
        log = structlog.get_logger().bind(
            service=constants.SERVICE_NAME,
            activity_name=info.activity_type,
            workflow_id=info.workflow_id,
            run_id=info.workflow_run_id,
            activity_id=info.activity_id,
        )

        if page_number < 2:
            raise ApplicationError(
                f"navigate_to_next_page_activity requires page_number >= 2, "
                f"got {page_number}",
                non_retryable=True,
            )

        target_url = f"{constants.HN_BASE_URL}?p={page_number}"
        log.info(
            "pagination.starting",
            status="starting",
            url=target_url,
            page_number=page_number,
        )
        started_at = time.monotonic()

        try:
            _, page = await self._ensure_browser(workflow_id=info.workflow_id, log=log)
        except ApplicationError:
            raise
        except BrowserStartError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            log.error(
                "pagination.failed",
                status="failed",
                reason="browser_unavailable",
                page_number=page_number,
                error=str(exc),
                duration_ms=duration_ms,
            )
            raise

        # Check whether the current page exposes a "More" link before
        # navigating away.  HN renders <a class="morelink"> at the bottom of
        # every page that has a successor.  Its absence means we've reached the
        # last available page — return False so the workflow stops pagination
        # without treating this as an error.
        try:
            more_link = await page.query_selector("a.morelink")
        except PlaywrightError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                page, info.activity_type, info.workflow_id
            )
            log.error(
                "pagination.failed",
                status="failed",
                reason="morelink_query_error",
                page_number=page_number,
                error=str(exc),
                screenshot_path=str(
                    screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"Failed to query 'a.morelink' on HN page {page_number - 1}: {exc}"
            ) from exc

        if more_link is None:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            log.info(
                "pagination.no_more_pages",
                status="completed",
                reason="no_morelink",
                page_number=page_number,
                duration_ms=duration_ms,
            )
            return False

        try:
            response = await page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=constants.BROWSER_TIMEOUT_MS,
            )
        except PlaywrightError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                page, info.activity_type, info.workflow_id
            )
            log.error(
                "pagination.failed",
                status="failed",
                reason="goto_error",
                url=target_url,
                page_number=page_number,
                error=str(exc),
                screenshot_path=str(
                    screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"Failed to navigate to HN page {page_number} ({target_url}): {exc}"
            ) from exc

        if response is not None and not response.ok:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                page, info.activity_type, info.workflow_id
            )
            log.error(
                "pagination.failed",
                status="failed",
                reason="http_error",
                url=target_url,
                page_number=page_number,
                http_status=response.status,
                screenshot_path=str(
                    screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"Unexpected HTTP {response.status} from HN page {page_number} "
                f"({target_url})"
            )

        try:
            title = await page.title()
        except PlaywrightError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                page, info.activity_type, info.workflow_id
            )
            log.error(
                "pagination.failed",
                status="failed",
                reason="title_read_error",
                page_number=page_number,
                error=str(exc),
                screenshot_path=str(
                    screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"Could not read page title after navigating to HN page "
                f"{page_number}: {exc}"
            ) from exc

        if "Hacker News" not in title:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                page, info.activity_type, info.workflow_id
            )
            log.error(
                "pagination.failed",
                status="failed",
                reason="unexpected_page",
                page_title=title,
                url=target_url,
                page_number=page_number,
                screenshot_path=str(
                    screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"Unexpected page title '{title}' on HN page {page_number} — "
                "expected 'Hacker News'. Site may be returning a captcha or "
                "error page."
            )

        try:
            await page.wait_for_selector(
                ".athing",
                state="attached",
                timeout=constants.BROWSER_TIMEOUT_MS,
            )
        except PlaywrightError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                page, info.activity_type, info.workflow_id
            )
            log.error(
                "pagination.failed",
                status="failed",
                reason="no_stories_found",
                url=target_url,
                page_number=page_number,
                error=str(exc),
                screenshot_path=str(
                    screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"No story rows (.athing) found on HN page {page_number} "
                f"({target_url}) within timeout: {exc}"
            ) from exc

        duration_ms = int((time.monotonic() - started_at) * 1000)
        log.info(
            "pagination.completed",
            status="completed",
            url=target_url,
            page_number=page_number,
            page_title=title,
            duration_ms=duration_ms,
        )
        return True

    @activity.defn(name="scrape_top_comment_activity")
    async def scrape_top_comment_activity(self, hn_id: str) -> Optional[str]:
        """Scrape the top (first displayed) comment from a given HN story's page.

        Navigates the workflow's shared page to the story's comment page,
        extracts the first comment, and truncates to the configured character
        limit. Transient failures are retried by Temporal via the
        BROWSER_RETRY_POLICY configured at the workflow call site.

        Context-level isolation (one BrowserContext per workflow_id) is
        sufficient — no per-story page creation is needed because comment
        scraping runs sequentially after all front-page scraping is complete
        and the shared page is idle.

        If the story has no comments, returns None (not an error). If the story
        is deleted or not found, raises a non-retryable error.

        Args:
            hn_id: Hacker News item ID (e.g., "12345678").

        Returns:
            Top comment text (truncated to TOP_COMMENT_MAX_CHARS), or None if
            the story has no comments.

        Raises:
            ApplicationError(non_retryable=True): Story not found (404), parse
                error (DOM structure changed), or Playwright binary missing.
            BrowserStartError: Browser could not be launched (retryable).
            BrowserNavigationError: Navigation failed (retryable via Temporal
                retry policy).
        """
        info = activity.info()
        log = structlog.get_logger().bind(
            service=constants.SERVICE_NAME,
            activity_name=info.activity_type,
            workflow_id=info.workflow_id,
            run_id=info.workflow_run_id,
            activity_id=info.activity_id,
            hn_id=hn_id,
        )

        log.info(
            "comment_scrape.starting",
            status="starting",
            url=f"{constants.HN_BASE_URL}/item?id={hn_id}",
        )
        started_at = time.monotonic()

        # Use the workflow's shared page — context-level isolation is sufficient.
        try:
            _, page = await self._ensure_browser(
                workflow_id=info.workflow_id, log=log
            )
        except ApplicationError:
            raise
        except BrowserStartError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            log.error(
                "comment_scrape.failed",
                status="failed",
                reason="browser_unavailable",
                error=str(exc),
                duration_ms=duration_ms,
            )
            raise

        # Navigate to comment page
        target_url = f"{constants.HN_BASE_URL}/item?id={hn_id}"
        try:
            response = await page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=constants.BROWSER_TIMEOUT_MS,
            )
        except PlaywrightError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                page, info.activity_type, info.workflow_id
            )
            log.error(
                "comment_scrape.failed",
                status="failed",
                reason="navigation_error",
                error=str(exc),
                screenshot_path=str(screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"Navigation failed for story {hn_id}: {exc}"
            ) from exc

        # Check for 404 (story deleted/not found)
        if response and response.status == 404:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            log.error(
                "comment_scrape.failed",
                status="failed",
                reason="story_not_found",
                http_status=404,
                duration_ms=duration_ms,
            )
            raise ApplicationError(
                f"Story {hn_id} not found (HTTP 404)",
                non_retryable=True,
            )

        # Check for unexpected HTTP errors
        if response and not response.ok:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                page, info.activity_type, info.workflow_id
            )
            error_msg = f"HTTP {response.status} from {target_url}"
            log.error(
                "comment_scrape.failed",
                status="failed",
                reason="http_error",
                http_status=response.status,
                error=error_msg,
                screenshot_path=str(screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(error_msg)

        # Verify page loaded correctly
        try:
            title = await page.title()
            if "Hacker News" not in title:
                duration_ms = int((time.monotonic() - started_at) * 1000)
                screenshot_path = await self._capture_screenshot(
                    page, info.activity_type, info.workflow_id
                )
                error_msg = f"Unexpected page title '{title}' for story {hn_id}"
                log.error(
                    "comment_scrape.failed",
                    status="failed",
                    reason="unexpected_page",
                    page_title=title,
                    screenshot_path=str(screenshot_path) if screenshot_path else None,
                    duration_ms=duration_ms,
                )
                raise BrowserNavigationError(error_msg)
        except PlaywrightError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            log.error(
                "comment_scrape.failed",
                status="failed",
                reason="title_read_error",
                error=str(exc),
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"Could not read page title for story {hn_id}: {exc}"
            ) from exc

        # Extract top comment
        comment_text = await self._extract_top_comment(
            page=page, hn_id=hn_id, log=log
        )

        if comment_text is None:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            log.info(
                "comment_scrape.completed",
                status="completed",
                result="no_comments",
                duration_ms=duration_ms,
            )
            return None

        original_length = len(comment_text)
        if original_length > constants.TOP_COMMENT_MAX_CHARS:
            comment_text = comment_text[: constants.TOP_COMMENT_MAX_CHARS]
            truncated = True
        else:
            truncated = False

        duration_ms = int((time.monotonic() - started_at) * 1000)
        log.info(
            "comment_scrape.completed",
            status="completed",
            comment_length=len(comment_text),
            original_length=original_length,
            truncated=truncated,
            duration_ms=duration_ms,
        )
        return comment_text

    @activity.defn(name="cleanup_browser_context_activity")
    async def cleanup_browser_context_activity(self) -> bool:
        """Clean up this workflow's browser context and page.

        Final activity in the scrape workflow. Called in the workflow's finally
        block to ensure the context is always cleaned up, even if the workflow
        fails. This prevents memory leaks from accumulating contexts.

        Idempotent: safe to call multiple times. If the context doesn't exist
        (already cleaned or never created), this is a no-op.

        Returns:
            True when cleanup succeeds (context closed) or is a no-op
            (context already gone).

        Raises:
            BrowserStartError: Context or page close failed (retryable).
                Temporal will retry cleanup to ensure resources are released.
        """
        info = activity.info()
        log = structlog.get_logger().bind(
            service=constants.SERVICE_NAME,
            activity_name=info.activity_type,
            workflow_id=info.workflow_id,
            run_id=info.workflow_run_id,
            activity_id=info.activity_id,
        )

        log.info("cleanup.starting", status="starting")
        started_at = time.monotonic()

        try:
            await self._cleanup_workflow_context(workflow_id=info.workflow_id, log=log)
        except BrowserStartError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            log.error(
                "cleanup.failed",
                status="failed",
                error=str(exc),
                duration_ms=duration_ms,
            )
            raise

        duration_ms = int((time.monotonic() - started_at) * 1000)
        log.info(
            "cleanup.completed",
            status="completed",
            duration_ms=duration_ms,
        )
        return True

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    async def _extract_stories(
        self,
        page: Page,
        top_n: int,
        log: Optional[structlog.types.FilteringBoundLogger] = None,
    ) -> list[Story]:
        """Locate all story rows on the current page and parse each one.

        Waits for .athing rows to be attached to the DOM, then processes up to
        top_n rows. Individual rows that fail to parse are skipped with a
        warning; if no stories are extracted at all, ParseError is raised.

        Args:
            page: Playwright Page object for this workflow.
            top_n: Maximum number of stories to extract.
            log: Bound structlog logger. A fresh unbound logger is used if None.

        Returns:
            Non-empty list of Story domain models.

        Raises:
            BrowserNavigationError: Playwright DOM access failed (retryable).
            ParseError: Zero stories could be extracted from the page.
        """
        if log is None:
            log = structlog.get_logger()

        try:
            await page.wait_for_selector(
                ".athing",
                state="attached",
                timeout=constants.BROWSER_TIMEOUT_MS,
            )
            rows = await page.query_selector_all("tr.athing")
        except PlaywrightError as exc:
            raise BrowserNavigationError(
                f"Failed to locate story rows on HN page: {exc}"
            ) from exc

        stories: list[Story] = []
        for rank, row in enumerate(rows[:top_n], start=1):
            try:
                story = await self._parse_story_row(page, row, fallback_rank=rank)
                stories.append(story)
            except ParseError as exc:
                log.warning("scrape.story_skipped", rank=rank, reason=str(exc))
            except PlaywrightError as exc:
                raise BrowserNavigationError(
                    f"Playwright error while parsing story at rank {rank}: {exc}"
                ) from exc

        if not stories:
            raise ParseError(
                "Extracted zero stories from the HN page — "
                "the DOM structure may have changed."
            )
        return stories

    async def _parse_story_row(
        self,
        page: Page,
        row: ElementHandle,
        fallback_rank: int,
    ) -> Story:
        """Parse a single HN story row element into a Story domain model.

        Extracts hn_id, rank, title, and url from the supplied tr.athing row,
        then delegates subtext extraction to _parse_subtext. Required fields
        hn_id and title raise ParseError when absent; optional fields default.

        Args:
            page: Playwright Page object for this workflow.
            row: ElementHandle for the tr.athing story row.
            fallback_rank: 1-indexed page position, used if span.rank is absent.

        Returns:
            A populated, immutable Story domain model.

        Raises:
            ParseError: hn_id or title are missing or empty (non-retryable).
        """
        hn_id = await row.get_attribute("id")
        if not hn_id:
            raise ParseError(
                f"Story row at rank {fallback_rank} is missing the id attribute."
            )

        rank_el = await row.query_selector("span.rank")
        rank_text = (await rank_el.inner_text()).rstrip(".").strip() if rank_el else ""
        rank = int(rank_text) if rank_text.isdigit() else fallback_rank

        title_el = await row.query_selector(".titleline > a")
        if title_el is None:
            raise ParseError(
                f"Story {hn_id} is missing its title element (.titleline > a)."
            )
        title = (await title_el.inner_text()).strip()
        if not title:
            raise ParseError(f"Story {hn_id} has an empty title string.")

        href = (await title_el.get_attribute("href")) or ""
        url: Optional[str] = None if href.startswith(
            "item?id=") else href or None

        points, author, comments_count = await self._parse_subtext(page, hn_id)

        return Story(
            hn_id=hn_id,
            rank=rank,
            title=title,
            url=url,
            points=points,
            author=author,
            comments_count=comments_count,
        )

    async def _parse_subtext(self, page: Page, hn_id: str) -> tuple[int, str, int]:
        """Extract points, author, and comments count from a story's subtext row.

        The subtext row is the <tr> immediately following tr.athing#{hn_id}.
        Missing optional fields (common for job posts) default gracefully:
        points=0, author="", comments_count=0.

        Args:
            page: Playwright Page object for this workflow.
            hn_id: The HN item ID used to locate the adjacent subtext row.

        Returns:
            Tuple of (points, author, comments_count).
        """
        # Use attribute selector instead of ID selector to handle numeric IDs
        subtext = await page.query_selector(
            f"tr.athing[id='{hn_id}'] + tr td.subtext"
        )
        if subtext is None:
            return 0, "", 0

        score_el = await subtext.query_selector("span.score")
        points = 0
        if score_el:
            score_text = (await score_el.inner_text()).strip()
            first_token = score_text.split()[0] if score_text else ""
            points = int(first_token) if first_token.isdigit() else 0

        author_el = await subtext.query_selector("a.hnuser")
        author = (await author_el.inner_text()).strip() if author_el else ""

        links = await subtext.query_selector_all("a")
        comments_count = 0
        if links:
            raw = (await links[-1].inner_text()).replace("\xa0", " ").strip()
            first_word = raw.split()[0] if raw else ""
            comments_count = int(first_word) if first_word.isdigit() else 0

        return points, author, comments_count

    async def _extract_top_comment(
        self,
        page: Page,
        hn_id: str,
        log: Optional[structlog.types.FilteringBoundLogger] = None,
    ) -> Optional[str]:
        """Extract the top (first displayed) comment from the current HN item page.

        Uses CSS selectors to locate the first comment element in the comment tree.
        HN's comment structure:
            .comment-tree
                └─ .athing.comtr (first comment)
                    └─ .commtext (comment text)

        Args:
            page: Playwright Page object positioned on an HN item page.
            hn_id: The HN item ID (for logging purposes).
            log: Bound structlog logger. If None, a fresh unbound logger is used.

        Returns:
            Comment text as a string, or None if no comments exist.

        Raises:
            ApplicationError(non_retryable=True): Parse error (DOM structure changed).
            PlaywrightError: DOM query failed (retryable).
        """
        if log is None:
            log = structlog.get_logger()

        # Wait for comment tree to be attached (if it exists)
        # Don't wait too long—if there are no comments, the element won't exist
        try:
            await page.wait_for_selector(
                ".comment-tree",
                state="attached",
                timeout=5000,  # Short timeout—if no comments, fail fast
            )
        except PlaywrightError:
            # Comment tree doesn't exist—story has no comments
            log.debug(
                "comment_scrape.no_comment_tree",
                hn_id=hn_id,
                reason="comment_tree_not_found",
            )
            return None

        # Find the first comment element
        # HN structure: .comment-tree contains .athing.comtr elements (each is a comment)
        try:
            first_comment = await page.query_selector(".comment-tree .athing.comtr")
        except PlaywrightError as exc:
            raise ApplicationError(
                f"Failed to query comment elements for story {hn_id}: {exc}",
                non_retryable=True,
            ) from exc

        if first_comment is None:
            # Comment tree exists but has no comments (edge case)
            log.debug(
                "comment_scrape.no_comments",
                hn_id=hn_id,
                reason="comment_tree_empty",
            )
            return None

        # Extract the comment text from .commtext
        try:
            comment_el = await first_comment.query_selector(".commtext")
        except PlaywrightError as exc:
            raise ApplicationError(
                f"Failed to query .commtext for story {hn_id}: {exc}",
                non_retryable=True,
            ) from exc

        if comment_el is None:
            # Comment element exists but has no text element (structural change?)
            raise ApplicationError(
                f"Comment element missing .commtext for story {hn_id}. "
                "HN DOM structure may have changed.",
                non_retryable=True,
            )

        # Get the text content
        try:
            comment_text = await comment_el.inner_text()
        except PlaywrightError as exc:
            raise ApplicationError(
                f"Failed to extract text from .commtext for story {hn_id}: {exc}",
                non_retryable=True,
            ) from exc

        # Strip whitespace and return
        comment_text = comment_text.strip()
        if not comment_text:
            # Empty comment (edge case—user submitted blank comment?)
            log.debug(
                "comment_scrape.empty_comment",
                hn_id=hn_id,
                reason="comment_text_empty",
            )
            return None

        return comment_text

    async def _ensure_browser(
        self,
        workflow_id: str,
        log: Optional[structlog.types.FilteringBoundLogger] = None,
    ) -> tuple[BrowserContext, Page]:
        """Guarantee that a live, ready browser context is available for this workflow.

        Fast path: workflow's context already exists → returns immediately.
        Slow path: context missing or browser disconnected → launches/recreates.

        This is called by every activity that needs the browser. It is the
        mechanism that makes the activity chain resilient to worker restarts:
        if the worker process was killed between activities, `self._browser`
        will be None on the new process and this method relaunches cleanly.

        Isolation: Each workflow_id gets its own BrowserContext and Page,
        ensuring concurrent workflows never interfere with each other.

        Args:
            workflow_id: Temporal workflow ID for context isolation.
            log: Bound structlog logger from the calling activity. If None
                 a fresh unbound logger is used (for direct calls in tests).

        Returns:
            Tuple of (BrowserContext, Page) isolated to this workflow.

        Raises:
            ApplicationError(non_retryable=True): Playwright binary not found.
            BrowserStartError: Any other launch failure.
        """
        if log is None:
            log = structlog.get_logger()

        # -----------------------------------------------------------------------
        # Step 1: Ensure shared browser is running
        # -----------------------------------------------------------------------
        if self._browser is None or not self._browser.is_connected():
            log.info("browser.launching", headless=constants.BROWSER_HEADLESS)

            # Tear down any half-open state from a previous failed attempt.
            await self._teardown_silently(log=log)

            try:
                self._playwright = await async_playwright().start()
            except Exception as exc:
                # Playwright's own ImportError / FileNotFoundError when the binary
                # is missing surfaces as a generic Exception from async_playwright().
                # Treat this as non-retryable infra misconfiguration.
                if "executable" in str(exc).lower() or "not found" in str(exc).lower():
                    raise ApplicationError(
                        f"Playwright binary not found — run 'playwright install chromium': {exc}",
                        non_retryable=True,
                    ) from exc
                raise BrowserStartError(
                    f"Failed to start Playwright runtime: {exc}"
                ) from exc

            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=constants.BROWSER_HEADLESS,
                )
            except PlaywrightError as exc:
                raise BrowserStartError(
                    f"Failed to launch Chromium: {exc}"
                ) from exc

            log.info(
                "browser.launched",
                headless=constants.BROWSER_HEADLESS,
            )

        # -----------------------------------------------------------------------
        # Step 2: Get or create workflow-specific context
        # -----------------------------------------------------------------------
        if workflow_id in self._contexts:
            log.debug(
                "browser.context_reused",
                workflow_id=workflow_id,
                total_contexts=len(self._contexts),
            )
            return self._contexts[workflow_id], self._pages[workflow_id]

        # Create new context for this workflow
        try:
            context = await self._browser.new_context(
                viewport={
                    "width": constants.BROWSER_VIEWPORT_WIDTH,
                    "height": constants.BROWSER_VIEWPORT_HEIGHT,
                },
            )
            context.set_default_timeout(constants.BROWSER_TIMEOUT_MS)

            page = await context.new_page()
            page.set_default_timeout(constants.BROWSER_TIMEOUT_MS)
        except PlaywrightError as exc:
            # Context/page creation failed — if this was the first workflow,
            # tear down the browser we just launched.
            if not self._contexts:
                await self._teardown_silently(log=log)
            raise BrowserStartError(
                f"Failed to create browser context or page for workflow {workflow_id}: {exc}"
            ) from exc

        self._contexts[workflow_id] = context
        self._pages[workflow_id] = page

        log.info(
            "browser.context_created",
            workflow_id=workflow_id,
            total_contexts=len(self._contexts),
            viewport_width=constants.BROWSER_VIEWPORT_WIDTH,
            viewport_height=constants.BROWSER_VIEWPORT_HEIGHT,
        )

        return context, page

    async def _cleanup_workflow_context(
        self,
        workflow_id: str,
        log: Optional[structlog.types.FilteringBoundLogger] = None,
    ) -> None:
        """Close and remove a specific workflow's browser context.

        Called by cleanup_browser_context_activity when a workflow completes.
        Raises exceptions so cleanup failures can be retried.

        Args:
            workflow_id: Temporal workflow ID whose context should be cleaned up.
            log: Bound structlog logger. If None, a fresh unbound logger is used.

        Raises:
            BrowserStartError: Context or page close failed (retryable).
        """
        if log is None:
            log = structlog.get_logger()

        # Idempotent: if context doesn't exist, cleanup already happened
        if workflow_id not in self._contexts:
            log.debug(
                "browser.context_already_cleaned",
                workflow_id=workflow_id,
            )
            return

        # Close page first, then context
        try:
            page = self._pages.get(workflow_id)
            if page:
                await page.close()
        except PlaywrightError as exc:
            raise BrowserStartError(
                f"Failed to close page for workflow {workflow_id}: {exc}"
            ) from exc

        try:
            context = self._contexts.get(workflow_id)
            if context:
                await context.close()
        except PlaywrightError as exc:
            raise BrowserStartError(
                f"Failed to close context for workflow {workflow_id}: {exc}"
            ) from exc

        # Remove from dictionaries
        self._pages.pop(workflow_id, None)
        self._contexts.pop(workflow_id, None)

        log.info(
            "browser.context_cleaned",
            workflow_id=workflow_id,
            remaining_contexts=len(self._contexts),
        )

    async def _teardown_silently(
        self,
        log: Optional[structlog.types.FilteringBoundLogger] = None,
    ) -> None:
        """Close all browser resources, swallowing errors.

        Called during error recovery and worker shutdown. Closes all
        workflow contexts, the shared browser, and Playwright runtime.
        Errors are logged but not raised — a failed teardown must not
        mask the original error.
        """
        if log is None:
            log = structlog.get_logger()

        # Close all workflow-specific pages and contexts
        for workflow_id in list(self._pages.keys()):
            try:
                page = self._pages.get(workflow_id)
                if page:
                    await page.close()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "browser.teardown_error",
                    resource="page",
                    workflow_id=workflow_id,
                    error=str(exc),
                )

        for workflow_id in list(self._contexts.keys()):
            try:
                context = self._contexts.get(workflow_id)
                if context:
                    await context.close()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "browser.teardown_error",
                    resource="context",
                    workflow_id=workflow_id,
                    error=str(exc),
                )

        self._pages.clear()
        self._contexts.clear()

        # Close shared browser and playwright
        for resource_name, close_coro_factory in (
            ("browser", lambda: self._browser.close() if self._browser else None),
            ("playwright", lambda: self._playwright.stop()
             if self._playwright else None),
        ):
            coro = close_coro_factory()
            if coro is not None:
                try:
                    await coro
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "browser.teardown_error",
                        resource=resource_name,
                        error=str(exc),
                    )

        self._browser = None
        self._playwright = None

    async def _capture_screenshot(
        self,
        page: Page,
        activity_name: str,
        workflow_id: str,
    ) -> Optional[Path]:
        """Save a failure screenshot and return its path, or None on failure.

        Best-effort: errors are swallowed so that screenshot capture never
        masks the original exception.

        Args:
            page: Playwright Page object for this workflow.
            activity_name: Name of the activity that failed.
            workflow_id: Temporal workflow ID.

        Returns:
            Path to screenshot file, or None if capture failed.
        """
        screenshot_path = (
            Path(constants.BROWSER_SCREENSHOT_DIR)
            / f"hn_scraper_{activity_name}_{workflow_id}_{int(time.time())}.png"
        )
        try:
            await page.screenshot(path=str(screenshot_path))
            return screenshot_path
        except Exception:  # noqa: BLE001
            return None
