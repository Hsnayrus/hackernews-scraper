"""Global pytest configuration.

Sets required environment variables at module level so that
``app.config.constants`` can be imported without raising ``KeyError``.

``app.config.constants`` reads ``os.environ["KEY"]`` (not ``.get``) at import
time. ``conftest.py`` files are loaded by pytest *before* test modules are
collected or imported, which makes this the only reliable injection point
for mandatory env vars.

Rules:
- Do NOT import from ``app.*`` here — constants must not be imported until
  after the env vars below have been applied.
- Use ``setdefault`` so that real env vars set by CI/CD or the developer's
  shell are not clobbered.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Mandatory environment variables consumed by app.config.constants
# ---------------------------------------------------------------------------

_TEST_ENV: dict[str, str] = {
    # Database
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DB_NAME": "hackernews_test",
    "DB_USER": "test_user",
    "DB_PASSWORD": "test_password",
    # Temporal
    "TEMPORAL_HOST": "localhost",
    "TEMPORAL_PORT": "7233",
    "TEMPORAL_NAMESPACE": "default",
    "TEMPORAL_TASK_QUEUE": "hn-scraper-test",
    # Observability
    "SERVICE_NAME": "hackernews-scraper-test",
    "LOG_LEVEL": "ERROR",
    # Scraper
    "HN_BASE_URL": "https://news.ycombinator.com",
    "SCRAPE_TOP_N": "30",
    # Browser (all optional in production, but explicit here for determinism)
    "BROWSER_HEADLESS": "true",
    "BROWSER_TIMEOUT_MS": "5000",
    "BROWSER_VIEWPORT_WIDTH": "1280",
    "BROWSER_VIEWPORT_HEIGHT": "800",
    "BROWSER_SCREENSHOT_DIR": "/tmp",
}

for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)
