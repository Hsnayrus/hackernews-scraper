# HackerNews Scraper

Production-grade Hacker News scraping service using Python 3.12, Playwright, Temporal, and Postgres.

## Prerequisites

- Docker and Docker Compose v2
- Python 3.12+ (for local development only)
- [uv](https://docs.astral.sh/uv/) (Python package manager)

---

## Running with Docker Compose

### 1. Copy and configure environment variables

All required variables are documented in [.env.example](.env.example).

```bash
cp .env.example .env
```

The default values in `.env.example` are production-ready for local Docker Compose usage.
You typically **do not need to modify** `.env` unless you want to change ports or resource limits.

### 2. Start all services

```bash
docker compose up --build -d
```

This single command:

- Builds Docker images for `api`, `worker`, and `migrate` services
- Starts all infrastructure (Postgres, Temporal)
- Runs database migrations automatically via the `migrate` init container
- Registers the Temporal namespace via `temporal-admin-setup`
- Starts the API and worker services

**Services started:**

| Service                | Description                                      | Port(s)       |
|------------------------|--------------------------------------------------|---------------|
| `postgres`             | Postgres 16 datastore                            | 5432          |
| `temporal`             | Temporal server (auto-setup mode)                | 7233          |
| `temporal-ui`          | Temporal Web UI for monitoring workflows         | 8080          |
| `temporal-admin-setup` | Init container: registers namespace (runs once)  | —             |
| `migrate`              | Init container: runs `alembic upgrade head`      | —             |
| `api`                  | FastAPI HTTP service                             | 8000          |
| `worker`               | Temporal worker with Playwright                  | —             |

**Startup behavior:**

- Temporal auto-setup creates its own schemas in Postgres on first boot (10-30s)
- The `migrate` service waits for Postgres health, then applies application migrations
- The `api` and `worker` services start only after migrations complete successfully
- Both `api` and `worker` use `restart: on-failure` to tolerate Temporal's startup delay

### 3. Verify the system is running

**Check API health:**

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{
  "status": "ok",
  "service": "api"
}
```

**Access Temporal UI:**

Open [http://localhost:8080](http://localhost:8080) in your browser to monitor workflows, view execution history, and inspect activity logs.

**Check running containers:**

```bash
docker compose ps
```

All services except init containers (`migrate`, `temporal-admin-setup`) should show `Up` status.

---

## Local Development

### Install dependencies

```bash
uv sync --group dev
```

This creates `.venv/` and installs all production and dev dependencies.
`uv.lock` is generated on first run and should be committed to version control.

### Set required environment variables

```bash
export DB_HOST=localhost
export DB_PORT=5432
export DB_NAME=hackernews
export DB_USER=hn_user
export DB_PASSWORD=changeme
export TEMPORAL_HOST=localhost
export TEMPORAL_PORT=7233
export TEMPORAL_NAMESPACE=default
export TEMPORAL_TASK_QUEUE=hn-scraper-queue
export SERVICE_NAME=local
export LOG_LEVEL=INFO
export HN_BASE_URL=https://news.ycombinator.com
export SCRAPE_TOP_N=30
```

### Run tests

```bash
pytest
```

### Type checking

```bash
mypy app/
```

### Linting

```bash
ruff check app/ tests/
```

---

## Architecture

```text
app/
├── workflows/   # Temporal workflow definitions (deterministic — no I/O)
├── activities/  # All side effects: scraping, DB writes, logging
├── services/    # Reusable business logic invoked by activities
├── domain/      # Pure Pydantic domain models — no infrastructure deps
├── infra/       # SQLAlchemy engine, Temporal client, browser factory
├── api/         # FastAPI application and routers
└── config/
    └── constants.py  # Single gateway: reads os.environ, codebase imports from here
```

### Configuration Flow

```text
Host environment variables
        │
        ▼
app/config/constants.py   (os.environ["KEY"] — raises KeyError if missing)
        │
        ▼
All modules import from app.config.constants
(no module calls os.environ directly)
```

### Key Design Decisions

- **Deterministic workflows**: Temporal workflows contain zero I/O, zero randomness, and never call `datetime.now()`. All side effects are activities.
- **Idempotent writes**: Stories are upserted by `hn_id` using `ON CONFLICT DO UPDATE`.
- **Fail-fast config**: Missing environment variables raise `KeyError` at import time, not mid-request.
- **Browser isolation**: Each scrape run gets a fresh Playwright browser context.
- **Top comment enrichment**: After scraping front-page stories, the workflow visits each story's HN comments page and captures the top comment. Comment scraping failures are non-fatal — stories are persisted with `top_comment: null` rather than failing the workflow.

---

## API Endpoints

### `GET /health`

Service health check.

```bash
curl http://localhost:8000/health
```

Response:

```json
{
  "status": "ok",
  "service": "api"
}
```

---

### `POST /scrape`

Triggers a new Hacker News scraping workflow via Temporal.

**Scrape with default settings (top 30 stories):**

```bash
curl -X POST http://localhost:8000/scrape
```

**Scrape a custom number of stories:**

```bash
curl -X POST http://localhost:8000/scrape \
  -H "Content-Type: application/json" \
  -d '{"num_stories": 50}'
```

Response:

```json
{
  "workflow_id": "scrape-2026-02-15T10:30:45Z-a7b3c9d2",
  "status": "STARTED"
}
```

**Workflow execution:**

- Returns immediately with workflow ID (non-blocking)
- Workflow runs asynchronously in the Temporal worker
- Monitor progress in Temporal UI: [http://localhost:8080](http://localhost:8080)

**What happens during scrape:**

1. Creates a new scrape run record in `scrape_runs` table
2. Launches Playwright browser in headless mode
3. Navigates to <https://news.ycombinator.com>
4. Scrapes top 30 stories (configurable via `SCRAPE_TOP_N`)
5. For each story, navigates to its HN comments page and scrapes the top comment
6. Upserts stories (with top comments) into `stories` table (idempotent by `hn_id`)
7. Updates scrape run status to `COMPLETED` or `FAILED`

---

### `GET /stories`

Returns stored stories from the database.

**Query all stories (most recent first):**

```bash
curl http://localhost:8000/stories
```

**Limit results:**

```bash
curl "http://localhost:8000/stories?limit=10"
```

**Filter by minimum points:**

```bash
curl "http://localhost:8000/stories?min_points=100"
```

**Filter by rank range:**

```bash
curl "http://localhost:8000/stories?rank_min=1&rank_max=10"
```

**Combine filters:**

```bash
curl "http://localhost:8000/stories?limit=5&min_points=200&rank_min=1&rank_max=30"
```

Response:

```json
{
  "stories": [
    {
      "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "hn_id": "39876543",
      "title": "Show HN: I built a thing",
      "url": "https://example.com/my-project",
      "rank": 1,
      "points": 420,
      "author": "pg",
      "comments_count": 137,
      "top_comment": "This is the top comment text from the HN discussion thread.",
      "scraped_at": "2026-02-15T10:30:45.123456",
      "created_at": "2026-02-15T10:30:45.123456"
    }
  ],
  "count": 1
}
```

---

### `GET /runs`

Returns metadata about previous scrape executions.

**Query all runs (most recent first):**

```bash
curl http://localhost:8000/runs
```

**Limit results:**

```bash
curl "http://localhost:8000/runs?limit=10"
```

**Filter by status:**

```bash
curl "http://localhost:8000/runs?status=COMPLETED"
```

**Combine filters:**

```bash
curl "http://localhost:8000/runs?limit=5&status=FAILED"
```

Response:

```json
{
  "runs": [
    {
      "id": "d4e5f6a7-b8c9-0d1e-2f3a-4b5c6d7e8f9a",
      "workflow_id": "scrape-hn-20260215-103045-abc123",
      "started_at": "2026-02-15T10:30:45.000000",
      "finished_at": "2026-02-15T10:31:23.000000",
      "status": "COMPLETED",
      "stories_scraped": 30,
      "error_message": null
    }
  ],
  "count": 1
}
```

**Status values:**

- `PENDING` — scrape run record created, workflow not yet started
- `RUNNING` — scraping workflow in progress
- `COMPLETED` — all stories scraped and persisted successfully
- `FAILED` — workflow terminated with unrecoverable error (see `error_message`)
