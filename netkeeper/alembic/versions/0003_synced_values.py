"""Contacts keep the last synced value of every LinkedIn field.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-20 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A NOT NULL column on a table that may already hold rows needs a database
    # default to fill them, and SQLite cannot drop one afterwards without batch
    # mode, so it stays. '{}' is a JSON object literal on SQLite and PostgreSQL
    # alike, and the model declares the same default.
    op.add_column(
        "contacts",
        sa.Column("synced_values", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("contacts", "synced_values")
