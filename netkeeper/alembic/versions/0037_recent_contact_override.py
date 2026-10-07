"""An enrollment records an override of the recent-contact guard (#446).

Enrollment skips a contact someone contacted within the campaign's
``contacted_within_days_guard``. A person may override that one guard for chosen
contacts, and ``enrollments`` gains what the override leaves behind:

- ``recent_contact_override_at``: when the person overrode it, for display.
- ``recent_contact_cutoff``: the newest outbound contact the guard saw then, the
  contact the person chose to set aside. At every step fire, outbound contact
  dated at or before it no longer counts as recent; anything dated after it does,
  even contact recorded later with a date before the override.
- ``recent_contact_override_by``: the id of the user who overrode it. It has no
  foreign key, because ``user_id`` must stay the table's only key to ``users``.

No existing data changes. Every existing enrollment went through the guard, so
none of these is set.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-06 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: Final = "enrollments"
COLUMNS: Final = (
    "recent_contact_override_at",
    "recent_contact_override_by",
    "recent_contact_cutoff",
)


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("recent_contact_override_at", sa.DateTime(), nullable=True))
    op.add_column(TABLE, sa.Column("recent_contact_override_by", sa.Integer(), nullable=True))
    op.add_column(TABLE, sa.Column("recent_contact_cutoff", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        for column in reversed(COLUMNS):
            batch.drop_column(column)
