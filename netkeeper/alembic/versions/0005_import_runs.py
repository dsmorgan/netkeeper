"""Import runs and their rows.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-21 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enums are strings plus a CHECK, identical on SQLite and PostgreSQL.
IMPORT_SOURCE_KIND = "source_kind IN ('archive', 'csv')"
IMPORT_STATUS = "status IN ('draft', 'committed', 'rolled_back')"
IMPORT_RESOLUTION = "resolution IN ('matched', 'created', 'candidate', 'skipped')"


def _timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]


def _user_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["user_id"], ["users.id"], name=op.f(f"fk_{table}_user_id_users"), ondelete="CASCADE"
    )


def upgrade() -> None:
    op.create_table(
        "import_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        # The preset the mapping came from; the mapping itself is always stored.
        sa.Column("preset", sa.String(length=100), nullable=True),
        sa.Column("mapping_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("matched_count", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("committed_at", sa.DateTime(), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(IMPORT_SOURCE_KIND, name=op.f("ck_import_runs_import_source_kind")),
        sa.CheckConstraint(IMPORT_STATUS, name=op.f("ck_import_runs_import_status")),
        _user_fk("import_runs"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_runs")),
    )
    op.create_index(op.f("ix_import_runs_user_id"), "import_runs", ["user_id"], unique=False)

    op.create_table(
        "import_rows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw_json", sa.JSON(), nullable=False),
        sa.Column("resolution", sa.String(length=16), nullable=False),
        # SET NULL, not CASCADE: a rollback deletes the contact and the audit row stays.
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("matched_by", sa.String(length=20), nullable=True),
        sa.Column("candidate_ids_json", sa.JSON(), nullable=True),
        sa.Column("decision_json", sa.JSON(), nullable=True),
        sa.Column("changes_json", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(IMPORT_RESOLUTION, name=op.f("ck_import_rows_import_resolution")),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_import_rows_contact_id_contacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["import_runs.id"],
            name=op.f("fk_import_rows_run_id_import_runs"),
            ondelete="CASCADE",
        ),
        _user_fk("import_rows"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_rows")),
        sa.UniqueConstraint(
            "user_id", "run_id", "row_number", name=op.f("uq_import_rows_user_id_run_id_row_number")
        ),
    )
    op.create_index(op.f("ix_import_rows_contact_id"), "import_rows", ["contact_id"], unique=False)
    op.create_index(op.f("ix_import_rows_run_id"), "import_rows", ["run_id"], unique=False)
    op.create_index(op.f("ix_import_rows_user_id"), "import_rows", ["user_id"], unique=False)


def downgrade() -> None:
    # Referencing tables first. Dropping a table drops its indexes.
    op.drop_table("import_rows")
    op.drop_table("import_runs")
