"""Contacts carry a needs-review mark (spec 9.8, 10.2; P2-08, #184).

``needs_review_at`` is set when the app created the contact from a card on
the connections page (the DOM fallback) rather than from a source that names
the person by URN. It is cleared when the person confirms the contact or a
Voyager sync attaches a URN to it. Every existing contact came from a source
the app already trusts, so the column is added ``NULL`` for all of them.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-24 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("needs_review_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("contacts", "needs_review_at")
