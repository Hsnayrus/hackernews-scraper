# Project Guidelines

## Engineering Principles

- Production-first mindset. No prototype shortcuts.
- Explicit error handling everywhere.
- No hidden global state.
- Deterministic Temporal workflows. All side effects in Activities only. Workflows must be replay-safe.

## Temporal Rules

- No non-deterministic calls inside workflows.
- No direct HTTP calls in workflows.
- No database writes in workflows.
- All external I/O must be Activities.

Activities must:

- Have timeouts
- Have retry policy
- Be idempotent

## Playwright Rules

- Use async API only.
- Context isolation per execution.
- Explicit timeouts.
- Proper browser teardown.
- Screenshots on failure.
- Trace capture enabled in debug mode.

## Logging Rules

Structured JSON logging. Always include:

- `request_id`
- `workflow_id`
- `activity_id`
- `correlation_id`

Never log secrets.

## Error Handling

- No bare `except`.
- Domain exceptions only.
- Map infrastructure errors to domain errors.
- Explicit retry classification.

## Code Structure

```text
app/
  workflows/
  activities/
  services/
  domain/
  infra/
  api/
  config/
```

## Testing Requirements

- Unit tests for domain logic.
- Workflow tests using Temporal test environment.
- Activity tests with mocks.
- Playwright integration tests.
- No untested business logic.

## Security

- Secrets via environment variables or secret manager.
- No hardcoded credentials.
- Validate all external input.
- Sandbox browser environment.

## Rule: always use qmd before reading files

Before reading files or exploring directories, always use qmd to search for information in local projects.

Available tools:

- `qmd search “query”` — fast keyword search (BM25)

- `qmd query “query”` — hybrid search with reranking (best quality)

- `qmd vsearch “query”` — semantic vector search

- `qmd get <file>` — retrieve a specific document

Use qmd search for quick lookups and qmd query for complex questions.

Use Read/Glob very sparingly. Ensure that these commands are run first and then understand if Glob/Read is required.
