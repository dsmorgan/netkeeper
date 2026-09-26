"""Message templates, versioned (spec 8.5, 11.1; item P3-03).

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-26 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enums are strings plus a CHECK, identical on SQLite and PostgreSQL.
TEMPLATE_CHANNEL = "channel IN ('email', 'linkedin')"


def upgrade() -> None:
    op.create_table(
        "templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        # Save-time lint issues: rule, severity, part, message, and field for each.
        sa.Column("lint_json", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        # The version this row replaced; the newest row of a chain has nothing pointing at it.
        sa.Column("previous_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(TEMPLATE_CHANNEL, name=op.f("ck_templates_template_channel")),
        sa.ForeignKeyConstraint(
            ["previous_id"],
            ["templates.id"],
            name=op.f("fk_templates_previous_id_templates"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_templates_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_templates")),
        sa.UniqueConstraint(
            "user_id", "previous_id", name=op.f("uq_templates_user_id_previous_id")
        ),
    )
    op.create_index(op.f("ix_templates_user_id"), "templates", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_table("templates")
