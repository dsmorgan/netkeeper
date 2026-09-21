"""The user's own job history.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-21 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enums are strings plus a CHECK, identical on SQLite and PostgreSQL.
SOURCE = "source IN ('sync', 'archive', 'csv', 'manual')"


def upgrade() -> None:
    op.create_table(
        "user_positions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("company", sa.String(length=300), nullable=True),
        sa.Column("company_urn", sa.String(length=200), nullable=True),
        sa.Column("started_on", sa.Date(), nullable=True),
        sa.Column("ended_on", sa.Date(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(SOURCE, name=op.f("ck_user_positions_contact_source")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_positions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_positions")),
    )
    op.create_index(op.f("ix_user_positions_user_id"), "user_positions", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_user_positions_user_id_company"),
        "user_positions",
        ["user_id", "company"],
        unique=False,
    )


def downgrade() -> None:
    # Dropping a table drops its indexes.
    op.drop_table("user_positions")
