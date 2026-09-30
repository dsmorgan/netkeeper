"""A test send on a mailbox armed for drafts only is a Gmail draft (#304).

``campaign_test_sends.gmail_draft_id`` is the draft's id when the test was drafted,
NULL when it was sent. ``rfc822_message_id`` is the Message-ID the test went in with,
which the drafts check searches for to verify the mailbox, and ``not_found_at`` is
when that search last found nothing. Existing rows were all sent, before the
Message-ID was kept, so all three stay NULL for them.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-30 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign_test_sends", sa.Column("gmail_draft_id", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "campaign_test_sends",
        sa.Column("rfc822_message_id", sa.String(length=200), nullable=True),
    )
    op.add_column("campaign_test_sends", sa.Column("not_found_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaign_test_sends", "not_found_at")
    op.drop_column("campaign_test_sends", "rfc822_message_id")
    op.drop_column("campaign_test_sends", "gmail_draft_id")
