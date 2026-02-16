# =============================================================================
# Stage: base — installs all Python dependencies, copies app source.
#   Both api and worker extend this stage.
#   uv is copied from the official uv image; no pip is used.
# =============================================================================
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # uv: pre-compile .py → .pyc at install time for faster container startup
    UV_COMPILE_BYTECODE=1 \
    # uv: do not use a local cache directory (keeps Docker layers clean)
    UV_NO_CACHE=1 \
    # uv: copy files instead of hardlinking (required inside Docker)
    UV_LINK_MODE=copy

# Pull the uv binary from the official image rather than installing via pip.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# gcc and python3-dev are required to build C extensions (e.g. asyncpg) when
# pre-built wheels are not available for the target platform.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifest first so Docker can cache the uv install layer
# independently of application source changes.
COPY pyproject.toml .

# Install production dependencies only into the system Python.
# [dependency-groups] (dev) are intentionally excluded.
RUN uv pip install --system .

# Copy application source after dependencies to maximise layer cache hits.
COPY app/ ./app/

# =============================================================================
# Stage: migrate — runs Alembic migrations as a one-shot init container.
#   Based on base (no browser binaries needed).
# =============================================================================
FROM base AS migrate

COPY alembic/ ./alembic/
COPY alembic.ini .

CMD ["alembic", "upgrade", "head"]

# =============================================================================
# Stage: api — serves the FastAPI application.
#   Does NOT include Playwright browser binaries.
# =============================================================================
FROM base AS api

EXPOSE 8000

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# =============================================================================
# Stage: worker — runs the Temporal worker and Playwright scraping.
#   Playwright browsers are installed on top of the base stage.
# =============================================================================
FROM base AS worker

# playwright install is intentionally run AFTER uv install (base stage) so
# that browser binary layer is separate from Python dependency layer.
RUN playwright install --with-deps chromium

CMD ["python", "-m", "app.worker"]
