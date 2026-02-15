"""Unit tests for app.activities.browser — BrowserActivities.

Coverage targets
----------------
- ``start_playwright_activity`` — the Temporal activity entry-point.
- ``_ensure_browser``           — internal browser lifecycle helper.
- ``_teardown_silently``        — resource cleanup helper.
- ``_capture_screenshot``       — best-effort screenshot helper.

Design decisions
----------------
- No real Playwright process is launched. Every Playwright object
  (``Playwright``, ``Browser``, ``BrowserContext``, ``Page``) is replaced
  with ``AsyncMock`` / ``MagicMock``.
- ``async_playwright`` is patched at ``app.activities.browser.async_playwright``
  (the name as it exists *inside* the module under test).
- ``activity.info`` is patched at ``app.activities.browser.activity.info``
  so ``start_playwright_activity`` does not require a live Temporal runtime.
- ``structlog`` is patched per test-class so log assertions are possible and
  test output stays clean.
- ``_ensure_browser`` is patched with ``AsyncMock`` when testing
  ``start_playwright_activity`` in isolation (separating activity contract
  from browser lifecycle).
- All helpers accept an explicit ``log`` parameter, so tests inject a
  ``MagicMock`` logger directly instead of patching structlog globally.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from playwright.async_api import Error as PlaywrightError
from temporalio.exceptions import ApplicationError

from app.activities.browser import BrowserActivities
from app.domain.exceptions import BrowserStartError


# ---------------------------------------------------------------------------
# Shared helpers / factories
# ---------------------------------------------------------------------------


def _make_activity_info(
    *,
    activity_type: str = "start_playwright_activity",
    workflow_id: str = "wf-test-001",
    workflow_run_id: str = "run-test-001",
    activity_id: str = "act-test-001",
) -> MagicMock:
    """Return a mock that looks like a ``temporalio.activity.Info`` object."""
    info = MagicMock()
    info.activity_type = activity_type
    info.workflow_id = workflow_id
    info.workflow_run_id = workflow_run_id
    info.activity_id = activity_id
    return info


def _make_mock_logger() -> MagicMock:
    """Return a MagicMock that satisfies the structlog bound-logger interface."""
    logger = MagicMock()
    logger.bind.return_value = logger  # .bind() returns itself for chaining
    return logger


def _make_playwright_stack() -> tuple[MagicMock, AsyncMock, AsyncMock, AsyncMock]:
    """Build a fully wired fake Playwright stack for _ensure_browser tests.

    Returns
    -------
    (mock_ap_cm, mock_pw, mock_browser, mock_context, mock_page)
    where ``mock_ap_cm`` is the object returned by ``async_playwright()``.
    """
    mock_page: AsyncMock = AsyncMock()
    mock_page.set_default_timeout = MagicMock()  # synchronous call

    mock_context: AsyncMock = AsyncMock()
    mock_context.set_default_timeout = MagicMock()  # synchronous call
    mock_context.new_page = AsyncMock(return_value=mock_page)

    mock_browser: AsyncMock = AsyncMock()
    mock_browser.is_connected = MagicMock(return_value=True)
    mock_browser.new_context = AsyncMock(return_value=mock_context)

    mock_pw: AsyncMock = AsyncMock()
    mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)

    # async_playwright() returns a context-manager-like object whose .start()
    # is the async entry point used in the production code.
    mock_ap_cm = MagicMock()
    mock_ap_cm.start = AsyncMock(return_value=mock_pw)

    return mock_ap_cm, mock_pw, mock_browser, mock_context, mock_page


# ===========================================================================
# TestStartPlaywrightActivity
# ===========================================================================


class TestStartPlaywrightActivity:
    """Tests for ``BrowserActivities.start_playwright_activity``.

    ``_ensure_browser`` is patched with an ``AsyncMock`` in every test so
    that the activity's contract (error handling, return value, logging) is
    verified in isolation from the browser lifecycle.
    """

    @pytest.fixture()
    def activities(self) -> BrowserActivities:
        return BrowserActivities()

    @pytest.fixture()
    def mock_info(self) -> MagicMock:
        return _make_activity_info()

    # ------------------------------------------------------------------
    # Success path
    # ------------------------------------------------------------------

    async def test_returns_true_on_success(
        self, activities: BrowserActivities, mock_info: MagicMock
    ) -> None:
        """Activity returns ``True`` when _ensure_browser succeeds."""
        with (
            patch("app.activities.browser.activity.info", return_value=mock_info),
            patch.object(activities, "_ensure_browser", new_callable=AsyncMock),
        ):
            result = await activities.start_playwright_activity()

        assert result is True

    async def test_calls_ensure_browser_exactly_once(
        self, activities: BrowserActivities, mock_info: MagicMock
    ) -> None:
        """Activity delegates browser initialisation to _ensure_browser once."""
        with (
            patch("app.activities.browser.activity.info", return_value=mock_info),
            patch.object(
                activities, "_ensure_browser", new_callable=AsyncMock
            ) as mock_ensure,
        ):
            await activities.start_playwright_activity()

        mock_ensure.assert_awaited_once()

    async def test_logs_starting_and_completed_on_success(
        self, activities: BrowserActivities, mock_info: MagicMock
    ) -> None:
        """Activity emits 'starting' then 'completed' log events on success."""
        mock_logger = _make_mock_logger()

        with (
            patch("app.activities.browser.activity.info", return_value=mock_info),
            patch("app.activities.browser.structlog") as mock_structlog,
            patch.object(activities, "_ensure_browser", new_callable=AsyncMock),
        ):
            mock_structlog.get_logger.return_value = mock_logger
            await activities.start_playwright_activity()

        log_calls = [c.args[0] for c in mock_logger.info.call_args_list]
        assert "browser_activity.starting" in log_calls
        assert "browser_activity.completed" in log_calls

    # ------------------------------------------------------------------
    # ApplicationError (non-retryable) path
    # ------------------------------------------------------------------

    async def test_reraises_application_error_without_wrapping(
        self, activities: BrowserActivities, mock_info: MagicMock
    ) -> None:
        """Non-retryable ApplicationError from _ensure_browser propagates as-is."""
        original_exc = ApplicationError(
            "Playwright binary not found", non_retryable=True
        )
        with (
            patch("app.activities.browser.activity.info", return_value=mock_info),
            patch.object(
                activities,
                "_ensure_browser",
                new_callable=AsyncMock,
                side_effect=original_exc,
            ),
        ):
            with pytest.raises(ApplicationError) as exc_info:
                await activities.start_playwright_activity()

        # Must be the exact same instance — no rewrapping.
        assert exc_info.value is original_exc
        assert exc_info.value.non_retryable is True

    async def test_application_error_does_not_log_failed(
        self, activities: BrowserActivities, mock_info: MagicMock
    ) -> None:
        """ApplicationError is not logged as a failure (it propagates immediately)."""
        mock_logger = _make_mock_logger()
        original_exc = ApplicationError("binary missing", non_retryable=True)

        with (
            patch("app.activities.browser.activity.info", return_value=mock_info),
            patch("app.activities.browser.structlog") as mock_structlog,
            patch.object(
                activities,
                "_ensure_browser",
                new_callable=AsyncMock,
                side_effect=original_exc,
            ),
        ):
            mock_structlog.get_logger.return_value = mock_logger
            with pytest.raises(ApplicationError):
                await activities.start_playwright_activity()

        logged_events = [c.args[0] for c in mock_logger.error.call_args_list]
        assert "browser_activity.failed" not in logged_events

    # ------------------------------------------------------------------
    # BrowserStartError (retryable) path
    # ------------------------------------------------------------------

    async def test_reraises_browser_start_error(
        self, activities: BrowserActivities, mock_info: MagicMock
    ) -> None:
        """BrowserStartError propagates as a retryable domain exception."""
        original_exc = BrowserStartError("Chromium failed to start")
        with (
            patch("app.activities.browser.activity.info", return_value=mock_info),
            patch.object(
                activities,
                "_ensure_browser",
                new_callable=AsyncMock,
                side_effect=original_exc,
            ),
        ):
            with pytest.raises(BrowserStartError) as exc_info:
                await activities.start_playwright_activity()

        assert exc_info.value is original_exc

    async def test_browser_start_error_logs_failed_event(
        self, activities: BrowserActivities, mock_info: MagicMock
    ) -> None:
        """BrowserStartError causes a 'failed' log event with the error message."""
        mock_logger = _make_mock_logger()

        with (
            patch("app.activities.browser.activity.info", return_value=mock_info),
            patch("app.activities.browser.structlog") as mock_structlog,
            patch.object(
                activities,
                "_ensure_browser",
                new_callable=AsyncMock,
                side_effect=BrowserStartError("launch error"),
            ),
        ):
            mock_structlog.get_logger.return_value = mock_logger
            with pytest.raises(BrowserStartError):
                await activities.start_playwright_activity()

        logged_events = [c.args[0] for c in mock_logger.error.call_args_list]
        assert "browser_activity.failed" in logged_events

    async def test_browser_start_error_log_includes_error_string(
        self, activities: BrowserActivities, mock_info: MagicMock
    ) -> None:
        """The failed log includes the exception message as the 'error' kwarg."""
        mock_logger = _make_mock_logger()
        error_message = "unique-chromium-error-xyz"

        with (
            patch("app.activities.browser.activity.info", return_value=mock_info),
            patch("app.activities.browser.structlog") as mock_structlog,
            patch.object(
                activities,
                "_ensure_browser",
                new_callable=AsyncMock,
                side_effect=BrowserStartError(error_message),
            ),
        ):
            mock_structlog.get_logger.return_value = mock_logger
            with pytest.raises(BrowserStartError):
                await activities.start_playwright_activity()

        error_call = mock_logger.error.call_args
        assert error_call.kwargs.get("error") == error_message

    async def test_browser_start_error_log_includes_duration_ms(
        self, activities: BrowserActivities, mock_info: MagicMock
    ) -> None:
        """The failed log includes a non-negative 'duration_ms' kwarg."""
        mock_logger = _make_mock_logger()

        with (
            patch("app.activities.browser.activity.info", return_value=mock_info),
            patch("app.activities.browser.structlog") as mock_structlog,
            patch.object(
                activities,
                "_ensure_browser",
                new_callable=AsyncMock,
                side_effect=BrowserStartError("err"),
            ),
        ):
            mock_structlog.get_logger.return_value = mock_logger
            with pytest.raises(BrowserStartError):
                await activities.start_playwright_activity()

        error_call = mock_logger.error.call_args
        duration_ms = error_call.kwargs.get("duration_ms")
        assert isinstance(duration_ms, int)
        assert duration_ms >= 0

    # ------------------------------------------------------------------
    # Logger context binding
    # ------------------------------------------------------------------

    async def test_logger_bound_with_activity_context_fields(
        self, activities: BrowserActivities, mock_info: MagicMock
    ) -> None:
        """Logger is bound with service, activity_name, workflow_id, run_id, activity_id."""
        mock_logger = _make_mock_logger()

        with (
            patch("app.activities.browser.activity.info", return_value=mock_info),
            patch("app.activities.browser.structlog") as mock_structlog,
            patch.object(activities, "_ensure_browser", new_callable=AsyncMock),
        ):
            mock_structlog.get_logger.return_value = mock_logger
            await activities.start_playwright_activity()

        bind_kwargs: dict[str, Any] = mock_logger.bind.call_args.kwargs
        assert bind_kwargs["workflow_id"] == mock_info.workflow_id
        assert bind_kwargs["run_id"] == mock_info.workflow_run_id
        assert bind_kwargs["activity_id"] == mock_info.activity_id
        assert bind_kwargs["activity_name"] == mock_info.activity_type


# ===========================================================================
# TestEnsureBrowser
# ===========================================================================


class TestEnsureBrowser:
    """Tests for ``BrowserActivities._ensure_browser``.

    ``async_playwright`` is patched at the module level. Each test builds its
    own fake Playwright stack via ``_make_playwright_stack``.
    """

    @pytest.fixture()
    def activities(self) -> BrowserActivities:
        return BrowserActivities()

    @pytest.fixture()
    def log(self) -> MagicMock:
        return _make_mock_logger()

    # ------------------------------------------------------------------
    # Fast path — browser already alive
    # ------------------------------------------------------------------

    async def test_fast_path_returns_immediately_when_connected(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """No re-launch when _browser is set and is_connected() is True."""
        connected_browser = MagicMock()
        connected_browser.is_connected.return_value = True
        activities._browser = connected_browser

        with patch("app.activities.browser.async_playwright") as mock_ap:
            await activities._ensure_browser(log=log)

        mock_ap.assert_not_called()

    async def test_fast_path_does_not_alter_existing_state(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """Existing browser/context/page references are preserved on fast path."""
        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = True
        mock_context = MagicMock()
        mock_page = MagicMock()

        activities._browser = mock_browser
        activities._context = mock_context
        activities._page = mock_page

        with patch("app.activities.browser.async_playwright"):
            await activities._ensure_browser(log=log)

        assert activities._browser is mock_browser
        assert activities._context is mock_context
        assert activities._page is mock_page

    # ------------------------------------------------------------------
    # Slow path — browser is None
    # ------------------------------------------------------------------

    async def test_slow_path_launches_browser_when_none(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """Full launch sequence runs when _browser is None."""
        mock_ap_cm, mock_pw, mock_browser, mock_context, mock_page = (
            _make_playwright_stack()
        )

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            await activities._ensure_browser(log=log)

        assert activities._playwright is mock_pw
        assert activities._browser is mock_browser
        assert activities._context is mock_context
        assert activities._page is mock_page

    async def test_slow_path_calls_chromium_launch(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """chromium.launch() is called with the configured headless flag."""
        mock_ap_cm, mock_pw, _, _, _ = _make_playwright_stack()

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            await activities._ensure_browser(log=log)

        mock_pw.chromium.launch.assert_awaited_once()

    async def test_slow_path_creates_context_with_viewport(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """new_context() is called with the viewport dimensions from constants."""
        from app.config import constants

        mock_ap_cm, _, mock_browser, _, _ = _make_playwright_stack()

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            await activities._ensure_browser(log=log)

        call_kwargs = mock_browser.new_context.call_args.kwargs
        viewport = call_kwargs.get("viewport", {})
        assert viewport.get("width") == constants.BROWSER_VIEWPORT_WIDTH
        assert viewport.get("height") == constants.BROWSER_VIEWPORT_HEIGHT

    async def test_slow_path_sets_default_timeout_on_context(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """set_default_timeout is called on the context."""
        from app.config import constants

        mock_ap_cm, _, _, mock_context, _ = _make_playwright_stack()

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            await activities._ensure_browser(log=log)

        mock_context.set_default_timeout.assert_called_once_with(
            constants.BROWSER_TIMEOUT_MS
        )

    async def test_slow_path_sets_default_timeout_on_page(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """set_default_timeout is called on the page."""
        from app.config import constants

        mock_ap_cm, _, _, _, mock_page = _make_playwright_stack()

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            await activities._ensure_browser(log=log)

        mock_page.set_default_timeout.assert_called_once_with(
            constants.BROWSER_TIMEOUT_MS
        )

    # ------------------------------------------------------------------
    # Slow path — browser disconnected
    # ------------------------------------------------------------------

    async def test_slow_path_relaunches_when_browser_disconnected(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """Re-launch occurs when _browser is set but is_connected() is False."""
        stale_browser = MagicMock()
        stale_browser.is_connected.return_value = False
        stale_browser.close = AsyncMock()
        activities._browser = stale_browser

        mock_ap_cm, mock_pw, mock_fresh_browser, _, _ = _make_playwright_stack()

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            await activities._ensure_browser(log=log)

        assert activities._browser is mock_fresh_browser

    # ------------------------------------------------------------------
    # Playwright start failures
    # ------------------------------------------------------------------

    async def test_playwright_start_binary_not_found_executable_in_message(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """'executable' in error message → non-retryable ApplicationError."""
        mock_ap_cm = MagicMock()
        mock_ap_cm.start = AsyncMock(
            side_effect=Exception("executable not found on PATH")
        )

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            with pytest.raises(ApplicationError) as exc_info:
                await activities._ensure_browser(log=log)

        assert exc_info.value.non_retryable is True

    async def test_playwright_start_binary_not_found_not_found_in_message(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """'not found' in error message (case-insensitive) → non-retryable ApplicationError."""
        mock_ap_cm = MagicMock()
        mock_ap_cm.start = AsyncMock(
            side_effect=Exception("Playwright binary NOT FOUND")
        )

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            with pytest.raises(ApplicationError) as exc_info:
                await activities._ensure_browser(log=log)

        assert exc_info.value.non_retryable is True

    async def test_playwright_start_generic_exception_raises_browser_start_error(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """A generic Exception from async_playwright().start() → retryable BrowserStartError."""
        mock_ap_cm = MagicMock()
        mock_ap_cm.start = AsyncMock(side_effect=Exception("unexpected runtime failure"))

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            with pytest.raises(BrowserStartError):
                await activities._ensure_browser(log=log)

    async def test_playwright_start_generic_exception_preserves_cause(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """BrowserStartError wraps the original generic exception as __cause__."""
        original = Exception("underlying cause")
        mock_ap_cm = MagicMock()
        mock_ap_cm.start = AsyncMock(side_effect=original)

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            with pytest.raises(BrowserStartError) as exc_info:
                await activities._ensure_browser(log=log)

        assert exc_info.value.__cause__ is original

    async def test_playwright_start_failure_does_not_set_playwright_state(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """If async_playwright().start() fails, self._playwright remains None."""
        mock_ap_cm = MagicMock()
        mock_ap_cm.start = AsyncMock(side_effect=Exception("fail"))

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            with pytest.raises((ApplicationError, BrowserStartError)):
                await activities._ensure_browser(log=log)

        assert activities._playwright is None

    # ------------------------------------------------------------------
    # Chromium launch failures
    # ------------------------------------------------------------------

    async def test_chromium_launch_failure_raises_browser_start_error(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """PlaywrightError from chromium.launch() → BrowserStartError."""
        mock_ap_cm, mock_pw, _, _, _ = _make_playwright_stack()
        mock_pw.chromium.launch = AsyncMock(
            side_effect=PlaywrightError("Chromium crashed")
        )

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            with pytest.raises(BrowserStartError) as exc_info:
                await activities._ensure_browser(log=log)

        assert "Failed to launch Chromium" in str(exc_info.value)

    async def test_chromium_launch_failure_preserves_cause(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """BrowserStartError wraps the original PlaywrightError as __cause__."""
        original = PlaywrightError("Chromium crashed")
        mock_ap_cm, mock_pw, _, _, _ = _make_playwright_stack()
        mock_pw.chromium.launch = AsyncMock(side_effect=original)

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            with pytest.raises(BrowserStartError) as exc_info:
                await activities._ensure_browser(log=log)

        assert exc_info.value.__cause__ is original

    # ------------------------------------------------------------------
    # Context creation failures
    # ------------------------------------------------------------------

    async def test_context_creation_failure_raises_browser_start_error(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """PlaywrightError from new_context() → BrowserStartError."""
        mock_ap_cm, _, mock_browser, _, _ = _make_playwright_stack()
        mock_browser.new_context = AsyncMock(
            side_effect=PlaywrightError("context creation failed")
        )

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            with pytest.raises(BrowserStartError) as exc_info:
                await activities._ensure_browser(log=log)

        assert "Failed to create browser context or page" in str(exc_info.value)

    async def test_context_creation_failure_calls_teardown(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """Teardown is called after new_context() raises so no orphaned browser.

        ``_ensure_browser`` always calls ``_teardown_silently`` once at slow-path
        start (to clean half-open state from a previous attempt), and a second
        time inside the exception handler for context/page creation failure.
        Total expected await count: 2.
        """
        mock_ap_cm, _, mock_browser, _, _ = _make_playwright_stack()
        mock_browser.new_context = AsyncMock(
            side_effect=PlaywrightError("context creation failed")
        )

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            with patch.object(
                activities, "_teardown_silently", new_callable=AsyncMock
            ) as mock_teardown:
                with pytest.raises(BrowserStartError):
                    await activities._ensure_browser(log=log)

        # Once for initial cleanup of half-open state, once for failure cleanup.
        assert mock_teardown.await_count == 2

    async def test_context_creation_failure_clears_state(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """After new_context() failure and teardown, all state refs are None."""
        mock_ap_cm, _, mock_browser, _, _ = _make_playwright_stack()
        mock_browser.new_context = AsyncMock(
            side_effect=PlaywrightError("context creation failed")
        )

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            with pytest.raises(BrowserStartError):
                await activities._ensure_browser(log=log)

        assert activities._playwright is None
        assert activities._browser is None
        assert activities._context is None
        assert activities._page is None

    # ------------------------------------------------------------------
    # Page creation failures
    # ------------------------------------------------------------------

    async def test_page_creation_failure_raises_browser_start_error(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """PlaywrightError from new_page() → BrowserStartError."""
        mock_ap_cm, _, _, mock_context, _ = _make_playwright_stack()
        mock_context.new_page = AsyncMock(
            side_effect=PlaywrightError("page creation failed")
        )

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            with pytest.raises(BrowserStartError) as exc_info:
                await activities._ensure_browser(log=log)

        assert "Failed to create browser context or page" in str(exc_info.value)

    async def test_page_creation_failure_calls_teardown(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """Teardown is called after new_page() raises so no orphaned browser.

        Same dual-call logic as the context failure case: initial cleanup at
        slow-path start, plus failure-path cleanup. Total expected count: 2.
        """
        mock_ap_cm, _, _, mock_context, _ = _make_playwright_stack()
        mock_context.new_page = AsyncMock(
            side_effect=PlaywrightError("page creation failed")
        )

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            with patch.object(
                activities, "_teardown_silently", new_callable=AsyncMock
            ) as mock_teardown:
                with pytest.raises(BrowserStartError):
                    await activities._ensure_browser(log=log)

        # Once for initial cleanup of half-open state, once for failure cleanup.
        assert mock_teardown.await_count == 2

    async def test_page_creation_failure_clears_state(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """After new_page() failure and teardown, all state refs are None."""
        mock_ap_cm, _, _, mock_context, _ = _make_playwright_stack()
        mock_context.new_page = AsyncMock(
            side_effect=PlaywrightError("page creation failed")
        )

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            with pytest.raises(BrowserStartError):
                await activities._ensure_browser(log=log)

        assert activities._playwright is None
        assert activities._browser is None
        assert activities._context is None
        assert activities._page is None

    # ------------------------------------------------------------------
    # Default logger (log=None)
    # ------------------------------------------------------------------

    async def test_uses_default_structlog_logger_when_log_is_none(
        self, activities: BrowserActivities
    ) -> None:
        """Passing log=None does not raise; structlog.get_logger() is used instead."""
        mock_ap_cm, _, _, _, _ = _make_playwright_stack()

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            # Should not raise even though no log param was provided.
            await activities._ensure_browser(log=None)

    # ------------------------------------------------------------------
    # Idempotency — calling _ensure_browser twice
    # ------------------------------------------------------------------

    async def test_second_call_is_no_op_when_browser_still_connected(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """Calling _ensure_browser a second time while the browser is connected
        is a no-op (fast path)."""
        mock_ap_cm, _, mock_browser, _, _ = _make_playwright_stack()

        with patch("app.activities.browser.async_playwright", return_value=mock_ap_cm):
            await activities._ensure_browser(log=log)
            launch_call_count_after_first = mock_browser.new_context.await_count

            await activities._ensure_browser(log=log)
            launch_call_count_after_second = mock_browser.new_context.await_count

        # No additional context was created on the second call.
        assert launch_call_count_after_second == launch_call_count_after_first


# ===========================================================================
# TestTeardownSilently
# ===========================================================================


class TestTeardownSilently:
    """Tests for ``BrowserActivities._teardown_silently``.

    Resources are injected directly onto the ``BrowserActivities`` instance
    before calling the method.
    """

    @pytest.fixture()
    def activities(self) -> BrowserActivities:
        return BrowserActivities()

    @pytest.fixture()
    def log(self) -> MagicMock:
        return _make_mock_logger()

    def _inject_full_stack(
        self, activities: BrowserActivities
    ) -> tuple[AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
        """Set all four resources on ``activities`` and return the mocks."""
        mock_page: AsyncMock = AsyncMock()
        mock_context: AsyncMock = AsyncMock()
        mock_browser: AsyncMock = AsyncMock()
        mock_playwright: AsyncMock = AsyncMock()

        activities._page = mock_page
        activities._context = mock_context
        activities._browser = mock_browser
        activities._playwright = mock_playwright

        return mock_page, mock_context, mock_browser, mock_playwright

    # ------------------------------------------------------------------
    # Happy path — all resources present
    # ------------------------------------------------------------------

    async def test_closes_all_resources_when_fully_initialised(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """All four .close()/.stop() coroutines are awaited."""
        mock_page, mock_context, mock_browser, mock_playwright = (
            self._inject_full_stack(activities)
        )

        await activities._teardown_silently(log=log)

        mock_page.close.assert_awaited_once()
        mock_context.close.assert_awaited_once()
        mock_browser.close.assert_awaited_once()
        mock_playwright.stop.assert_awaited_once()

    async def test_clears_all_references_after_full_teardown(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """All instance references are set to None after successful teardown."""
        self._inject_full_stack(activities)

        await activities._teardown_silently(log=log)

        assert activities._page is None
        assert activities._context is None
        assert activities._browser is None
        assert activities._playwright is None

    async def test_teardown_order_page_context_browser_playwright(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """Resources are closed in correct order: page → context → browser → playwright.

        This validates the teardown sequence so inner resources are released
        before outer ones.
        """
        closed_order: list[str] = []
        mock_page = AsyncMock()
        mock_context = AsyncMock()
        mock_browser = AsyncMock()
        mock_playwright = AsyncMock()

        mock_page.close.side_effect = lambda: closed_order.append("page")
        mock_context.close.side_effect = lambda: closed_order.append("context")
        mock_browser.close.side_effect = lambda: closed_order.append("browser")
        mock_playwright.stop.side_effect = lambda: closed_order.append("playwright")

        activities._page = mock_page
        activities._context = mock_context
        activities._browser = mock_browser
        activities._playwright = mock_playwright

        await activities._teardown_silently(log=log)

        assert closed_order == ["page", "context", "browser", "playwright"]

    # ------------------------------------------------------------------
    # All resources are None — no-op
    # ------------------------------------------------------------------

    async def test_no_op_when_all_resources_are_none(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """No errors raised and nothing called when all resources are None."""
        # Default state of BrowserActivities has all resources as None.
        await activities._teardown_silently(log=log)
        # No assertion needed beyond "it doesn't raise".

    async def test_state_still_none_after_no_op_teardown(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """References remain None after teardown of an uninitialised instance."""
        await activities._teardown_silently(log=log)

        assert activities._page is None
        assert activities._context is None
        assert activities._browser is None
        assert activities._playwright is None

    # ------------------------------------------------------------------
    # Partial initialisation
    # ------------------------------------------------------------------

    async def test_only_closes_resources_that_are_set(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """Only existing (non-None) resources are closed."""
        mock_page = AsyncMock()
        mock_playwright = AsyncMock()

        activities._page = mock_page
        activities._playwright = mock_playwright
        # _context and _browser remain None

        await activities._teardown_silently(log=log)

        mock_page.close.assert_awaited_once()
        mock_playwright.stop.assert_awaited_once()

    async def test_partial_teardown_clears_all_references(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """All references set to None even when only some resources existed."""
        activities._page = AsyncMock()
        activities._playwright = AsyncMock()

        await activities._teardown_silently(log=log)

        assert activities._page is None
        assert activities._playwright is None

    # ------------------------------------------------------------------
    # Error swallowing — each resource
    # ------------------------------------------------------------------

    async def test_page_close_error_is_swallowed(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """An error in page.close() does not propagate; remaining resources are closed."""
        mock_page, mock_context, mock_browser, mock_playwright = (
            self._inject_full_stack(activities)
        )
        mock_page.close.side_effect = Exception("page close blew up")

        await activities._teardown_silently(log=log)  # must not raise

        mock_context.close.assert_awaited_once()
        mock_browser.close.assert_awaited_once()
        mock_playwright.stop.assert_awaited_once()

    async def test_context_close_error_is_swallowed(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """An error in context.close() does not propagate; remaining resources are closed."""
        mock_page, mock_context, mock_browser, mock_playwright = (
            self._inject_full_stack(activities)
        )
        mock_context.close.side_effect = Exception("context close blew up")

        await activities._teardown_silently(log=log)  # must not raise

        mock_page.close.assert_awaited_once()
        mock_browser.close.assert_awaited_once()
        mock_playwright.stop.assert_awaited_once()

    async def test_browser_close_error_is_swallowed(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """An error in browser.close() does not propagate; playwright is still stopped."""
        mock_page, mock_context, mock_browser, mock_playwright = (
            self._inject_full_stack(activities)
        )
        mock_browser.close.side_effect = Exception("browser close blew up")

        await activities._teardown_silently(log=log)  # must not raise

        mock_playwright.stop.assert_awaited_once()

    async def test_playwright_stop_error_is_swallowed(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """An error in playwright.stop() does not propagate."""
        mock_page, mock_context, mock_browser, mock_playwright = (
            self._inject_full_stack(activities)
        )
        mock_playwright.stop.side_effect = Exception("playwright stop blew up")

        await activities._teardown_silently(log=log)  # must not raise

    async def test_all_errors_swallowed_and_state_cleared(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """Even when every resource raises on close, all refs are set to None."""
        mock_page, mock_context, mock_browser, mock_playwright = (
            self._inject_full_stack(activities)
        )
        mock_page.close.side_effect = Exception("page error")
        mock_context.close.side_effect = Exception("context error")
        mock_browser.close.side_effect = Exception("browser error")
        mock_playwright.stop.side_effect = Exception("playwright error")

        await activities._teardown_silently(log=log)  # must not raise

        assert activities._page is None
        assert activities._context is None
        assert activities._browser is None
        assert activities._playwright is None

    # ------------------------------------------------------------------
    # Error logging
    # ------------------------------------------------------------------

    async def test_swallowed_page_error_is_logged_as_warning(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """A close error on 'page' is logged as a warning with the resource name."""
        self._inject_full_stack(activities)
        activities._page.close.side_effect = Exception("page close error")

        await activities._teardown_silently(log=log)

        log.warning.assert_called()
        warning_kwargs = log.warning.call_args.kwargs
        assert warning_kwargs.get("resource") == "page"

    async def test_swallowed_playwright_error_is_logged_as_warning(
        self, activities: BrowserActivities, log: MagicMock
    ) -> None:
        """A stop error on 'playwright' is logged as a warning with the resource name."""
        self._inject_full_stack(activities)
        activities._playwright.stop.side_effect = Exception("playwright stop error")

        await activities._teardown_silently(log=log)

        log.warning.assert_called()
        warning_kwargs = log.warning.call_args.kwargs
        assert warning_kwargs.get("resource") == "playwright"

    # ------------------------------------------------------------------
    # Default logger (log=None)
    # ------------------------------------------------------------------

    async def test_uses_default_structlog_logger_when_log_is_none(
        self, activities: BrowserActivities
    ) -> None:
        """Passing log=None does not raise; structlog.get_logger() is used instead."""
        self._inject_full_stack(activities)
        # Should not raise even though no log param was provided.
        await activities._teardown_silently(log=None)


# ===========================================================================
# TestCaptureScreenshot
# ===========================================================================


class TestCaptureScreenshot:
    """Tests for ``BrowserActivities._capture_screenshot``.

    This is a best-effort helper; it must never raise.
    """

    @pytest.fixture()
    def activities(self) -> BrowserActivities:
        return BrowserActivities()

    # ------------------------------------------------------------------
    # page is None
    # ------------------------------------------------------------------

    async def test_returns_none_when_page_is_none(
        self, activities: BrowserActivities
    ) -> None:
        """Returns None immediately when there is no live page."""
        result = await activities._capture_screenshot("my_activity", "wf-000")
        assert result is None

    # ------------------------------------------------------------------
    # Screenshot succeeds
    # ------------------------------------------------------------------

    async def test_returns_path_on_success(
        self, activities: BrowserActivities
    ) -> None:
        """Returns a Path object when screenshot() succeeds."""
        activities._page = AsyncMock()
        activities._page.screenshot = AsyncMock()

        result = await activities._capture_screenshot("my_activity", "wf-001")

        assert result is not None
        assert isinstance(result, Path)

    async def test_path_contains_activity_name(
        self, activities: BrowserActivities
    ) -> None:
        """Returned path embeds the activity_name for identification."""
        activities._page = AsyncMock()
        activities._page.screenshot = AsyncMock()

        result = await activities._capture_screenshot("scrape_urls_activity", "wf-002")

        assert result is not None
        assert "scrape_urls_activity" in result.name

    async def test_path_contains_workflow_id(
        self, activities: BrowserActivities
    ) -> None:
        """Returned path embeds the workflow_id for traceability."""
        activities._page = AsyncMock()
        activities._page.screenshot = AsyncMock()

        result = await activities._capture_screenshot("my_activity", "unique-wf-xyz")

        assert result is not None
        assert "unique-wf-xyz" in result.name

    async def test_path_has_png_extension(
        self, activities: BrowserActivities
    ) -> None:
        """Screenshot file is always a .png."""
        activities._page = AsyncMock()
        activities._page.screenshot = AsyncMock()

        result = await activities._capture_screenshot("my_activity", "wf-003")

        assert result is not None
        assert result.suffix == ".png"

    async def test_screenshot_written_to_configured_directory(
        self, activities: BrowserActivities
    ) -> None:
        """Screenshot path parent equals BROWSER_SCREENSHOT_DIR constant."""
        from app.config import constants

        activities._page = AsyncMock()
        activities._page.screenshot = AsyncMock()

        result = await activities._capture_screenshot("my_activity", "wf-004")

        assert result is not None
        assert str(result.parent) == constants.BROWSER_SCREENSHOT_DIR

    async def test_screenshot_called_with_string_path(
        self, activities: BrowserActivities
    ) -> None:
        """page.screenshot() receives the path as a string (Playwright requirement)."""
        mock_page = AsyncMock()
        mock_page.screenshot = AsyncMock()
        activities._page = mock_page

        await activities._capture_screenshot("my_activity", "wf-005")

        call_kwargs = mock_page.screenshot.call_args.kwargs
        assert isinstance(call_kwargs.get("path"), str)

    # ------------------------------------------------------------------
    # Screenshot fails — exception swallowed
    # ------------------------------------------------------------------

    async def test_returns_none_when_screenshot_raises(
        self, activities: BrowserActivities
    ) -> None:
        """page.screenshot() raising must not propagate; returns None instead."""
        activities._page = AsyncMock()
        activities._page.screenshot = AsyncMock(
            side_effect=Exception("screenshot disk full")
        )

        result = await activities._capture_screenshot("my_activity", "wf-006")

        assert result is None

    async def test_playwright_error_on_screenshot_returns_none(
        self, activities: BrowserActivities
    ) -> None:
        """PlaywrightError from screenshot() is also swallowed, returning None."""
        activities._page = AsyncMock()
        activities._page.screenshot = AsyncMock(
            side_effect=PlaywrightError("target closed")
        )

        result = await activities._capture_screenshot("my_activity", "wf-007")

        assert result is None
