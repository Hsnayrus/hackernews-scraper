# HackerNews Scraper

Production-grade Hacker News scraping service using Python 3.12, Playwright, Temporal, and Postgres.

## Prerequisites

- Docker and Docker Compose v2
- Python 3.12+ (for local development only)

---

## Running with Docker Compose

### 1. Set environment variables

All required variables are documented in [.env.example](.env.example).

```bash
cp .env.example .env
# Edit .env with your values
```

Or export them directly into your shell — Docker Compose reads from both.

### 2. Start all services

```bash
docker compose up --build
```

This starts four services:

| Service    | Description                              | Port  |
|------------|------------------------------------------|-------|
| `postgres`  | Postgres 16 datastore                    | 5432  |
| `temporal`  | Temporal server (auto-setup)             | 7233  |
| `api`       | FastAPI HTTP service                     | 8000  |
| `worker`    | Temporal worker with Playwright          | —     |

Temporal auto-setup creates its own schema in Postgres on first boot (10-30s).
The `api` and `worker` services will restart automatically until Temporal is ready.

### 3. Run database migrations

```bash
docker compose exec api alembic upgrade head
```

### 4. Verify

```bash
curl http://localhost:8000/health
```

---

## Local Development

### Install dependencies

```bash
pip install -e ".[dev]"
```

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

```
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

```
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

---

## API Endpoints

| Method | Path       | Description                  |
|--------|------------|------------------------------|
| `GET`  | `/health`  | Service health check         |
| `POST` | `/scrape`  | Trigger a new scrape workflow |
| `GET`  | `/stories` | Query stored stories          |
| `GET`  | `/runs`    | Query scrape run history      |
