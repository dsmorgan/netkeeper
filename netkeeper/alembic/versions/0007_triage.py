"""The triage decision log.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-21 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enums are strings plus a CHECK, identical on SQLite and PostgreSQL.
DECISION_KIND = "kind IN ('decide', 'preferred_name', 'bulk_met')"


def upgrade() -> None:
    op.create_table(
        "triage_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        # Field name to value-as-text, for the fields the decision touched.
        sa.Column("before_state", sa.JSON(), nullable=False),
        sa.Column("after_state", sa.JSON(), nullable=False),
        sa.Column("batch_id", sa.String(length=32), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        # When undo spent the row; NULL is "still on the stack".
        sa.Column("undone_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(DECISION_KIND, name=op.f("ck_triage_decisions_triage_decision_kind")),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_triage_decisions_contact_id_contacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_triage_decisions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_triage_decisions")),
    )
    op.create_index(
        op.f("ix_triage_decisions_contact_id"), "triage_decisions", ["contact_id"], unique=False
    )
    op.create_index(
        op.f("ix_triage_decisions_user_id"), "triage_decisions", ["user_id"], unique=False
    )
    # The undo stack: the newest row of this user that is not spent yet.
    op.create_index(
        "ix_triage_decisions_user_id_undone_at_id",
        "triage_decisions",
        ["user_id", "undone_at", "id"],
        unique=False,
    )
    # Undoing a bulk apply gathers its rows by batch.
    op.create_index(
        "ix_triage_decisions_user_id_batch_id",
        "triage_decisions",
        ["user_id", "batch_id"],
        unique=False,
    )


def downgrade() -> None:
    # Dropping a table drops its indexes.
    op.drop_table("triage_decisions")
