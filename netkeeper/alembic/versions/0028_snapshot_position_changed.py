"""A contact snapshot says whether it records a position change (#286, spec 9.8).

``contact_snapshots.position_changed`` is true when the change the row records
replaced a non-empty ``current_title`` or ``current_company`` with a different
value. The dashboard's "changed jobs" card counts these rows. A snapshot holds
the values before the change only, so a row written before this revision cannot
say what replaced them: existing rows take false.

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-30 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "contact_snapshots",
        sa.Column("position_changed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("contact_snapshots", "position_changed")
