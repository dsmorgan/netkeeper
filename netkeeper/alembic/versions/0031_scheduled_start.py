"""A campaign's scheduled start, and a step's own time of day (#338).

Campaign sends are no longer limited to a window. Instead:

- ``campaigns.starts_at`` (nullable) is the scheduled start. The engine sends
  nothing for a campaign before it, and nothing for an active or paused campaign
  that has none.
- ``campaign_steps.send_time`` (nullable, ``HH:MM``) is a step's explicit local
  time of day. NULL means the step aims for the next suggested send slot after
  its delay.

Existing data: every campaign that was ever activated (``active``, ``paused``,
``completed`` or ``archived``) gets ``starts_at`` set to its activation time,
``approved_at``, which the review gate writes in the transaction that activates
it. A campaign activated before the review gate recorded that (0024) has no
``approved_at``; it gets its ``created_at``. Either is in the past, so a campaign
that was already sending keeps sending, and one that was not still sends nothing
before its enrollments are due. A ``draft`` or ``reviewing`` campaign keeps NULL:
activation sets it. Every step keeps NULL, the suggested slot.

``campaigns.send_window_json`` is left in place, unread, so the downgrade has
each campaign's window back.

The downgrade drops both columns.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-03 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A copy of the statuses as they stood at this revision: a migration never imports
# application code, which may change after it.
_ACTIVATED: Final = ("active", "paused", "completed", "archived")

_campaigns = sa.table(
    "campaigns",
    sa.column("id", sa.Integer()),
    sa.column("status", sa.String()),
    sa.column("approved_at", sa.DateTime()),
    sa.column("created_at", sa.DateTime()),
    sa.column("starts_at", sa.DateTime()),
)


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("starts_at", sa.DateTime(), nullable=True))
    op.add_column("campaign_steps", sa.Column("send_time", sa.String(length=5), nullable=True))
    op.get_bind().execute(
        sa.update(_campaigns)
        .where(_campaigns.c.status.in_(_ACTIVATED))
        .values(starts_at=sa.func.coalesce(_campaigns.c.approved_at, _campaigns.c.created_at))
    )


def downgrade() -> None:
    op.drop_column("campaign_steps", "send_time")
    op.drop_column("campaigns", "starts_at")
