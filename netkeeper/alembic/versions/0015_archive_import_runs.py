"""An archive import records its run: per-table counts and the interactions it wrote (#132).

``report_json`` keeps the per-table counts an archive import reports, which
the CSV-shaped count columns cannot hold. ``created_json`` keeps the ids of
rows the run created that no ``import_rows`` row accounts for (an archive
run's interactions), so a rollback can delete them. Both are ``NULL`` for
every run that exists already: none of them is an archive run.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-26 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("import_runs", sa.Column("report_json", sa.JSON(), nullable=True))
    op.add_column("import_runs", sa.Column("created_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("import_runs", "created_json")
    op.drop_column("import_runs", "report_json")
