"""Reply and bounce detection keeps its place in Gmail's history, and each reply's snippet
(P3-08, #295).

``mailboxes.history_id`` is the Gmail ``historyId`` the reply poll reads from
next; NULL until the first poll sets a baseline, which every existing mailbox
starts without. ``mailboxes.replies_polled_at`` is when the poll last read
everything up to then; NULL until it has. ``messages.snippet`` is an inbound
message's snippet: the poll stores a reply's subject and snippet, never its
body (spec 11.7). Outbound messages have none.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-28 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mailboxes", sa.Column("history_id", sa.BigInteger(), nullable=True))
    op.add_column("mailboxes", sa.Column("replies_polled_at", sa.DateTime(), nullable=True))
    op.add_column("messages", sa.Column("snippet", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "snippet")
    op.drop_column("mailboxes", "replies_polled_at")
    op.drop_column("mailboxes", "history_id")
