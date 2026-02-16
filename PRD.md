# Hacker News Scraper

> Production-Ready Scraping Service using Python, Playwright, Temporal, and Postgres

## 1. Overview

### Objective

Build a reliable scraping service that extracts top stories from Hacker News and stores them in Postgres.

The system must demonstrate:

- Durable workflow orchestration (Temporal)
- Deterministic workflow design
- Proper activity isolation
- Idempotent database writes
- Production-grade logging
- Clean architecture
- Containerized deployment using Docker Compose

## 2. Problem Statement

We need a reliable way to:

- Scrape the top N stories from Hacker News
- Persist structured story data in Postgres
- Avoid duplicates
- Handle transient failures gracefully
- Support manual and scheduled execution
- Maintain clear observability into each scrape run

The system should be production-ready, not a simple script.

## 3. Scope

### In Scope

- Scrape top 30 stories from the Hacker News homepage
- Extract structured story metadata
- Persist stories into Postgres
- Avoid duplicate inserts
- Record scrape execution metadata
- Expose API endpoints for triggering and querying data
- Use Temporal workflows for orchestration
- Run all services via Docker Compose

### Out of Scope

- Frontend UI
- Horizontal scaling across multiple machines
- Multi-tenant support
- Authentication/authorization
- Cloud deployment

## 4. Functional Requirements

### 4.1 Scraping

The system must extract:

- Story rank
- Story title
- Story URL
- Hacker News item ID
- Points
- Author
- Number of comments

### 4.2 Persistence

- Stories must be stored in Postgres.
- Duplicate stories must not be inserted.
- Use `hn_id` as a unique identifier.
- Support upsert semantics.

### 4.3 Workflow Execution

Scraping must be orchestrated using Temporal.

Workflow must:

- Trigger scraping activity
- Persist results
- Record execution metadata

Workflow must be deterministic. All side effects must be implemented as activities.

### 4.4 API

The system must expose:

#### `POST /scrape`

Triggers a new scraping workflow.

Response:

```json
{
  "workflow_id": "...",
  "status": "STARTED"
}
```

#### `GET /stories`

Returns stored stories.

Optional filters:

- `limit`
- `min_points`

#### `GET /runs`

Returns metadata about previous scrape runs.

## 5. Non-Functional Requirements

| Category        | Requirement                               |
| --------------- | ----------------------------------------- |
| Reliability     | Workflow survives worker restart          |
| Determinism     | No non-deterministic logic in workflows   |
| Idempotency     | Duplicate stories not inserted            |
| Observability   | Structured JSON logging                   |
| Isolation       | Browser context per scrape run            |
| Maintainability | Clean architecture separation             |
| Reproducibility | Full system runs via Docker Compose       |

## 6. System Architecture

### Components

- API Service (FastAPI)
- Temporal Server
- Temporal Worker
- Postgres
- Playwright runtime

### Docker Compose Services

```text
docker-compose.yml
├── api
├── worker
├── temporal
└── postgres
```

All services must:

- Run in isolated containers
- Communicate over Docker network
- Use environment variables for configuration

## 7. Workflow Design

### Primary Workflow: `ScrapeHackerNewsWorkflow`

**Trigger:**

- Manual API call
- Optional scheduled cron execution

**Steps:**

1. Create scrape run record
2. Execute browser scraping activitye
3. Parse story data
4. Upsert stories into Postgres
5. Update scrape run status

### Sample Activities

These are the sample activities along which we need to build our app:

- StartPlaywrightActivity
- NavigateToHackerNewsActivity
- ScrapeURLsActivity
- NavigateToNextPageActivity

## 8. Activity Boundaries

Activities must handle all side effects:

- Playwright browser execution
- HTML parsing
- Database writes
- Logging of execution metadata

Workflows must only orchestrate activities.

## 9. Data Model

### Table: `stories`

| Column           | Type               |
| ---------------- | ------------------ |
| `id`             | `UUID`             |
| `hn_id`          | `VARCHAR` (unique) |
| `title`          | `TEXT`             |
| `url`            | `TEXT`             |
| `rank`           | `INTEGER`          |
| `points`         | `INTEGER`          |
| `author`         | `VARCHAR`          |
| `comments_count` | `INTEGER`          |
| `scraped_at`     | `TIMESTAMP`        |
| `created_at`     | `TIMESTAMP`        |

Constraint: `UNIQUE(hn_id)`

### Table: `scrape_runs`

| Column            | Type        |
| ----------------- | ----------- |
| `id`              | `UUID`      |
| `workflow_id`     | `VARCHAR`   |
| `started_at`      | `TIMESTAMP` |
| `finished_at`     | `TIMESTAMP` |
| `status`          | `VARCHAR`   |
| `stories_scraped` | `INTEGER`   |
| `error_message`   | `TEXT`      |

## 10. Idempotency Strategy

- Use `hn_id` as unique identifier.
- Implement Postgres `ON CONFLICT DO UPDATE`.
- Activities must be retry-safe.
- Workflow ID uniquely identifies scrape run.

## 11. Failure Handling

### Retry Policy

- Max attempts: `3`
- Exponential backoff
- Timeout per activity

### Failure Types

| Failure              | Handling        |
| -------------------- | --------------- |
| Network timeout      | Retry           |
| Browser crash        | Retry           |
| DB transient error   | Retry           |
| Parsing error        | Fail workflow   |

## 12. Playwright Requirements

- Use async API
- Headless execution
- New browser context per run
- Explicit timeouts
- Proper teardown
- Capture screenshot on failure

## 13. Observability

Structured JSON logging must include:

- `timestamp`
- `service`
- `workflow_id`
- `run_id`
- `activity_name`
- `status`
- `duration_ms`

Logs must not include sensitive data.

## 14. Testing Strategy

- Unit tests for parsing logic
- Activity tests with mocked Playwright
- Workflow tests using Temporal test environment
- Integration test verifying scrape → database persistence

## 15. Deliverables

- Source code repository
- Docker Compose configuration
- README with setup instructions
