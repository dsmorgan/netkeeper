"""Contacts carry the enrichment NotFound streak (spec 9.8, P2-07).

``li_not_found_count`` is how many enrichment visits in a row found no profile,
``li_not_found_since`` when that streak began, and ``li_not_found_at`` the
latest. Three across at least 14 days marks the profile gone.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-23 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A NOT NULL column on a table that may already hold rows needs a database
    # default to fill them; the model declares the same one (see 0003).
    op.add_column(
        "contacts",
        sa.Column("li_not_found_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("contacts", sa.Column("li_not_found_since", sa.DateTime(), nullable=True))
    op.add_column("contacts", sa.Column("li_not_found_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("contacts", "li_not_found_at")
    op.drop_column("contacts", "li_not_found_since")
    op.drop_column("contacts", "li_not_found_count")
