"""Unit tests for app.domain.exceptions — exception hierarchy and behaviour.

Coverage targets
----------------
- Full inheritance hierarchy (all domain exceptions trace back to HackerNewsScraperError)
- Exception messages are preserved via super().__init__
- Every concrete exception is catchable as its parent type
- Every concrete exception is catchable as HackerNewsScraperError (root)
"""

from __future__ import annotations

import pytest

from app.domain.exceptions import (
    BrowserError,
    BrowserNavigationError,
    BrowserStartError,
    HackerNewsScraperError,
    ParseError,
    PersistenceError,
    PersistenceTransientError,
    PersistenceValidationError,
)


# ---------------------------------------------------------------------------
# TestExceptionHierarchy
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    """Verify the declared inheritance chain matches the docstring diagram."""

    def test_root_inherits_exception(self) -> None:
        assert issubclass(HackerNewsScraperError, Exception)

    # --- BrowserError branch ---

    def test_browser_error_inherits_root(self) -> None:
        assert issubclass(BrowserError, HackerNewsScraperError)

    def test_browser_start_error_inherits_browser_error(self) -> None:
        assert issubclass(BrowserStartError, BrowserError)

    def test_browser_navigation_error_inherits_browser_error(self) -> None:
        assert issubclass(BrowserNavigationError, BrowserError)

    def test_browser_start_error_inherits_root(self) -> None:
        assert issubclass(BrowserStartError, HackerNewsScraperError)

    def test_browser_navigation_error_inherits_root(self) -> None:
        assert issubclass(BrowserNavigationError, HackerNewsScraperError)

    # --- ParseError branch ---

    def test_parse_error_inherits_root(self) -> None:
        assert issubclass(ParseError, HackerNewsScraperError)

    # --- PersistenceError branch ---

    def test_persistence_error_inherits_root(self) -> None:
        assert issubclass(PersistenceError, HackerNewsScraperError)

    def test_persistence_transient_inherits_persistence_error(self) -> None:
        assert issubclass(PersistenceTransientError, PersistenceError)

    def test_persistence_validation_inherits_persistence_error(self) -> None:
        assert issubclass(PersistenceValidationError, PersistenceError)

    def test_persistence_transient_inherits_root(self) -> None:
        assert issubclass(PersistenceTransientError, HackerNewsScraperError)

    def test_persistence_validation_inherits_root(self) -> None:
        assert issubclass(PersistenceValidationError, HackerNewsScraperError)

    def test_browser_errors_do_not_inherit_persistence_error(self) -> None:
        assert not issubclass(BrowserStartError, PersistenceError)
        assert not issubclass(BrowserNavigationError, PersistenceError)

    def test_parse_error_does_not_inherit_browser_error(self) -> None:
        assert not issubclass(ParseError, BrowserError)

    def test_persistence_errors_do_not_inherit_browser_error(self) -> None:
        assert not issubclass(PersistenceTransientError, BrowserError)
        assert not issubclass(PersistenceValidationError, BrowserError)


# ---------------------------------------------------------------------------
# TestExceptionMessages
# ---------------------------------------------------------------------------


class TestExceptionMessages:
    """Verify that message strings survive construction unchanged."""

    def test_browser_start_error_preserves_message(self) -> None:
        msg = "Cannot launch Chromium: binary not found at /usr/bin/chromium"
        exc = BrowserStartError(msg)
        assert str(exc) == msg

    def test_browser_navigation_error_preserves_message(self) -> None:
        msg = "Timeout navigating to https://news.ycombinator.com after 30s"
        exc = BrowserNavigationError(msg)
        assert str(exc) == msg

    def test_parse_error_preserves_message(self) -> None:
        msg = "Missing title element (.titleline > a) for story id=42"
        exc = ParseError(msg)
        assert str(exc) == msg

    def test_persistence_transient_error_preserves_message(self) -> None:
        msg = "Connection pool exhausted: all 5 connections are in use"
        exc = PersistenceTransientError(msg)
        assert str(exc) == msg

    def test_persistence_validation_error_preserves_message(self) -> None:
        msg = "scrape_run not found for update: run_id=abc123"
        exc = PersistenceValidationError(msg)
        assert str(exc) == msg

    def test_empty_message_is_preserved(self) -> None:
        exc = ParseError("")
        assert str(exc) == ""


# ---------------------------------------------------------------------------
# TestExceptionCatchability
# ---------------------------------------------------------------------------


class TestExceptionCatchability:
    """Verify every concrete exception can be caught at each ancestor type."""

    def test_browser_start_error_caught_as_browser_error(self) -> None:
        with pytest.raises(BrowserError):
            raise BrowserStartError("test")

    def test_browser_navigation_error_caught_as_browser_error(self) -> None:
        with pytest.raises(BrowserError):
            raise BrowserNavigationError("test")

    def test_browser_start_error_caught_as_root(self) -> None:
        with pytest.raises(HackerNewsScraperError):
            raise BrowserStartError("test")

    def test_browser_navigation_error_caught_as_root(self) -> None:
        with pytest.raises(HackerNewsScraperError):
            raise BrowserNavigationError("test")

    def test_parse_error_caught_as_root(self) -> None:
        with pytest.raises(HackerNewsScraperError):
            raise ParseError("test")

    def test_persistence_transient_caught_as_persistence_error(self) -> None:
        with pytest.raises(PersistenceError):
            raise PersistenceTransientError("test")

    def test_persistence_validation_caught_as_persistence_error(self) -> None:
        with pytest.raises(PersistenceError):
            raise PersistenceValidationError("test")

    def test_persistence_transient_caught_as_root(self) -> None:
        with pytest.raises(HackerNewsScraperError):
            raise PersistenceTransientError("test")

    def test_persistence_validation_caught_as_root(self) -> None:
        with pytest.raises(HackerNewsScraperError):
            raise PersistenceValidationError("test")

    def test_all_concrete_exceptions_caught_as_root(self) -> None:
        concrete_exceptions = [
            BrowserStartError("test"),
            BrowserNavigationError("test"),
            ParseError("test"),
            PersistenceTransientError("test"),
            PersistenceValidationError("test"),
        ]
        for exc in concrete_exceptions:
            with pytest.raises(HackerNewsScraperError):
                raise exc

    def test_persistence_transient_not_caught_as_validation(self) -> None:
        with pytest.raises(PersistenceTransientError):
            try:
                raise PersistenceTransientError("transient")
            except PersistenceValidationError:
                pass  # should NOT be caught here

    def test_persistence_validation_not_caught_as_transient(self) -> None:
        with pytest.raises(PersistenceValidationError):
            try:
                raise PersistenceValidationError("validation")
            except PersistenceTransientError:
                pass  # should NOT be caught here
