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
    1. start_playwright_activity      ← this module (initialises browser)
    2. navigate_to_hacker_news_activity
    3. scrape_urls_activity
    4. navigate_to_next_page_activity

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
    Error as PlaywrightError,
    Page,
    Playwright,
    async_playwright,
)
from temporalio import activity
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from app.config import constants
from app.domain.exceptions import BrowserNavigationError, BrowserStartError

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


# ---------------------------------------------------------------------------
# Activity class
# ---------------------------------------------------------------------------


class BrowserActivities:
    """Stateful Temporal activity class managing the Playwright browser lifecycle.

    One instance of this class is passed to `temporalio.worker.Worker` at
    startup. Temporal calls activity methods on that instance, so all methods
    share browser state via `self`.

    This class must never be instantiated more than once per worker process.
    """

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # -------------------------------------------------------------------------
    # Public activities
    # -------------------------------------------------------------------------

    @activity.defn(name="start_playwright_activity")
    async def start_playwright_activity(self) -> bool:
        """Launch Playwright and open a headless Chromium browser.

        This is the explicit first activity in the scrape workflow. Subsequent
        activities call `_ensure_browser()` internally, but calling this first
        makes the browser initialisation step visible in the workflow history
        and Temporal UI.

        Returns:
            True when the browser is ready.

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
            await self._ensure_browser(log=log)
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
        browser is transparently relaunched if state was lost.

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

        log.info("navigation.starting", status="starting", url=constants.HN_BASE_URL)
        started_at = time.monotonic()

        try:
            await self._ensure_browser(log=log)
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

        assert self._page is not None  # guaranteed by _ensure_browser

        try:
            response = await self._page.goto(
                constants.HN_BASE_URL,
                wait_until="domcontentloaded",
                timeout=constants.BROWSER_TIMEOUT_MS,
            )
        except PlaywrightError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                info.activity_type, info.workflow_id
            )
            log.error(
                "navigation.failed",
                status="failed",
                reason="goto_error",
                url=constants.HN_BASE_URL,
                error=str(exc),
                screenshot_path=str(screenshot_path) if screenshot_path else None,
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
                info.activity_type, info.workflow_id
            )
            log.error(
                "navigation.failed",
                status="failed",
                reason="http_error",
                url=constants.HN_BASE_URL,
                http_status=response.status,
                screenshot_path=str(screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"Unexpected HTTP {response.status} from {constants.HN_BASE_URL}"
            )

        # Verify page identity: title must contain "Hacker News".
        try:
            title = await self._page.title()
        except PlaywrightError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                info.activity_type, info.workflow_id
            )
            log.error(
                "navigation.failed",
                status="failed",
                reason="title_read_error",
                error=str(exc),
                screenshot_path=str(screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"Could not read page title after navigation: {exc}"
            ) from exc

        if "Hacker News" not in title:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                info.activity_type, info.workflow_id
            )
            log.error(
                "navigation.failed",
                status="failed",
                reason="unexpected_page",
                page_title=title,
                url=constants.HN_BASE_URL,
                screenshot_path=str(screenshot_path) if screenshot_path else None,
                duration_ms=duration_ms,
            )
            raise BrowserNavigationError(
                f"Unexpected page title '{title}' — expected 'Hacker News'. "
                "Site may be returning a captcha or error page."
            )

        # Verify at least one story row is present in the DOM.
        try:
            await self._page.wait_for_selector(
                ".athing",
                state="attached",
                timeout=constants.BROWSER_TIMEOUT_MS,
            )
        except PlaywrightError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            screenshot_path = await self._capture_screenshot(
                info.activity_type, info.workflow_id
            )
            log.error(
                "navigation.failed",
                status="failed",
                reason="no_stories_found",
                error=str(exc),
                screenshot_path=str(screenshot_path) if screenshot_path else None,
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

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    async def _ensure_browser(
        self,
        log: Optional[structlog.types.FilteringBoundLogger] = None,
    ) -> None:
        """Guarantee that a live, ready browser is available on self.

        Fast path: browser is already connected → returns immediately.
        Slow path: browser is missing or disconnected → launches a fresh one.

        This is called by every activity that needs the browser. It is the
        mechanism that makes the activity chain resilient to worker restarts:
        if the worker process was killed between activities, `self._browser`
        will be None on the new process and this method relaunches cleanly.

        Args:
            log: Bound structlog logger from the calling activity. If None
                 a fresh unbound logger is used (for direct calls in tests).

        Raises:
            ApplicationError(non_retryable=True): Playwright binary not found.
            BrowserStartError: Any other launch failure.
        """
        if self._browser is not None and self._browser.is_connected():
            return  # fast path — browser is alive

        if log is None:
            log = structlog.get_logger()

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

        try:
            self._context = await self._browser.new_context(
                viewport={
                    "width": constants.BROWSER_VIEWPORT_WIDTH,
                    "height": constants.BROWSER_VIEWPORT_HEIGHT,
                },
            )
            self._context.set_default_timeout(constants.BROWSER_TIMEOUT_MS)

            self._page = await self._context.new_page()
            self._page.set_default_timeout(constants.BROWSER_TIMEOUT_MS)
        except PlaywrightError as exc:
            # Context/page creation failed — tear down the browser we just
            # launched so we leave no orphaned processes.
            await self._teardown_silently(log=log)
            raise BrowserStartError(
                f"Failed to create browser context or page: {exc}"
            ) from exc

        log.info(
            "browser.launched",
            headless=constants.BROWSER_HEADLESS,
            viewport_width=constants.BROWSER_VIEWPORT_WIDTH,
            viewport_height=constants.BROWSER_VIEWPORT_HEIGHT,
        )

    async def _teardown_silently(
        self,
        log: Optional[structlog.types.FilteringBoundLogger] = None,
    ) -> None:
        """Close all browser resources, swallowing errors.

        Called during error recovery and by stop_playwright_activity. Errors
        are logged but not raised — a failed teardown must not mask the
        original error.
        """
        if log is None:
            log = structlog.get_logger()

        for resource_name, close_coro_factory in (
            ("page", lambda: self._page.close() if self._page else None),
            ("context", lambda: self._context.close() if self._context else None),
            ("browser", lambda: self._browser.close() if self._browser else None),
            ("playwright", lambda: self._playwright.stop() if self._playwright else None),
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

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    async def _capture_screenshot(self, activity_name: str, workflow_id: str) -> Optional[Path]:
        """Save a failure screenshot and return its path, or None on failure.

        Best-effort: errors are swallowed so that screenshot capture never
        masks the original exception.
        """
        if self._page is None:
            return None

        screenshot_path = (
            Path(constants.BROWSER_SCREENSHOT_DIR)
            / f"hn_scraper_{activity_name}_{workflow_id}_{int(time.time())}.png"
        )
        try:
            await self._page.screenshot(path=str(screenshot_path))
            return screenshot_path
        except Exception:  # noqa: BLE001
            return None
