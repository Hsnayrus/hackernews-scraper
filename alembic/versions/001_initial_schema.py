"""Initial schema: stories and scrape_runs tables.

Revision ID: 001
Revises:
Create Date: 2026-02-15

Creates:
    stories      — Scraped Hacker News story records.
    scrape_runs  — Temporal workflow execution metadata.

Notes:
    - gen_random_uuid() requires pgcrypto or Postgres 13+ (built-in).
    - All timestamps are WITH TIME ZONE for unambiguous UTC storage.
    - Unique constraints are named explicitly to allow future ALTER operations.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # stories
    # ------------------------------------------------------------------
    op.create_table(
        "stories",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
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
        sa.Column("scraped_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("hn_id", name="uq_stories_hn_id"),
    )

    # ------------------------------------------------------------------
    # scrape_runs
    # ------------------------------------------------------------------
    op.create_table(
        "scrape_runs",
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
        sa.Column("status", sa.VARCHAR(32), nullable=False),
        sa.Column("stories_scraped", sa.INTEGER, nullable=True),
        sa.Column("error_message", sa.TEXT, nullable=True),
        sa.UniqueConstraint("workflow_id", name="uq_scrape_runs_workflow_id"),
    )


def downgrade() -> None:
    op.drop_table("scrape_runs")
    op.drop_table("stories")
