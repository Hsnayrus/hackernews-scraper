"""Add top_comment column to stories table.

Revision ID: 002
Revises: 001
Create Date: 2026-02-18

Adds:
    stories.top_comment — TEXT column storing the top comment from each story's HN page.

Notes:
    - Column is nullable (stories may have no comments).
    - No unique constraint needed (multiple stories can have same comment text).
    - Existing rows will have top_comment = NULL until next scrape.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column("top_comment", sa.TEXT, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stories", "top_comment")
