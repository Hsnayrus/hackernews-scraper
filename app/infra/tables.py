"""SQLAlchemy Core table definitions.

All table objects are registered against the shared ``metadata`` instance from
``app.infra.db`` so that Alembic's autogenerate can discover them and the
repository layer can reference them for queries.

No ORM declarative mapping is used. Domain models (Pydantic) are hydrated
manually from query result rows inside the repository layer, keeping the
domain layer free of SQLAlchemy concerns.

Tables:
    stories      — Scraped Hacker News story records (unique key: hn_id)
    scrape_runs  — Workflow execution metadata (unique key: workflow_id)
"""

from __future__ import annotations

import sqlalchemy as sa

from app.infra.db import metadata

# ---------------------------------------------------------------------------
# stories
# ---------------------------------------------------------------------------

stories_table: sa.Table = sa.Table(
    "stories",
    metadata,
    sa.Column(
        "id",
        sa.UUID(as_uuid=True),
        primary_key=True,
        # Python-side uuid.uuid4() is always passed on insert; server_default
        # is a safety net only (e.g. direct SQL inserts outside the app).
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    ),
    sa.Column("hn_id", sa.VARCHAR(64), nullable=False),
    sa.Column("title", sa.TEXT, nullable=False),
    sa.Column("url", sa.TEXT, nullable=True),
    sa.Column("rank", sa.INTEGER, nullable=False),
    sa.Column("points", sa.INTEGER, nullable=False),
    sa.Column("author", sa.VARCHAR(255), nullable=False),
    sa.Column("comments_count", sa.INTEGER, nullable=False),
    sa.Column("top_comment", sa.TEXT, nullable=True),
    sa.Column(
        "scraped_at",
        sa.TIMESTAMP(timezone=True),
        nullable=False,
    ),
    sa.Column(
        "created_at",
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        # Python-side datetime is always passed; server_default covers
        # direct SQL inserts only.
        server_default=sa.text("now()"),
    ),
    sa.UniqueConstraint("hn_id", name="uq_stories_hn_id"),
)

# ---------------------------------------------------------------------------
# scrape_runs
# ---------------------------------------------------------------------------

scrape_runs_table: sa.Table = sa.Table(
    "scrape_runs",
    metadata,
    sa.Column(
        "id",
        sa.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    ),
    sa.Column("workflow_id", sa.VARCHAR(255), nullable=False),
    sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
    # VARCHAR to match ScrapeRunStatus enum values: PENDING, RUNNING, COMPLETED, FAILED
    sa.Column("status", sa.VARCHAR(32), nullable=False),
    sa.Column("stories_scraped", sa.INTEGER, nullable=True),
    sa.Column("error_message", sa.TEXT, nullable=True),
    sa.UniqueConstraint("workflow_id", name="uq_scrape_runs_workflow_id"),
)
