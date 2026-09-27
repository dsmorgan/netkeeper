"""Messages keep what the Gmail sender's reconcile has seen (P3-07, #273 review).

``thread_known_json`` is the Gmail message ids a draft's thread already held
when the draft was made. The drafts poll reads a draft gone from Gmail as sent
only for a sent message outside them and no older than the draft, so a note the
person sent in the thread before is never taken for the draft.

``reconcile_misses``, ``reconcile_first_miss_at`` and ``reconcile_last_miss_at``
count the searches by Message-ID that found nothing. A message whose send got no
answer is ruled "not in Gmail" only after several searches spread over time,
since Gmail's search can lag a send. Every existing message starts with none:
nothing it did before this migration counted a miss.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-27 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COLUMNS: Final = (
    "thread_known_json",
    "reconcile_misses",
    "reconcile_first_miss_at",
    "reconcile_last_miss_at",
)


def upgrade() -> None:
    op.add_column("messages", sa.Column("thread_known_json", sa.JSON(), nullable=True))
    op.add_column(
        "messages",
        sa.Column("reconcile_misses", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("messages", sa.Column("reconcile_first_miss_at", sa.DateTime(), nullable=True))
    op.add_column("messages", sa.Column("reconcile_last_miss_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("messages", column)
