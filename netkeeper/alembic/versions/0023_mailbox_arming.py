"""Mailboxes are armed, per mailbox, before ``serve`` hands them anything (#277).

``serve`` gives a mailbox's campaign steps to Gmail only while a person has
armed it. ``armed_at`` is when (NULL: disarmed), ``send_armed_at`` when the
separate step to let ``send`` steps go out was taken (NULL: drafts only),
``armed_by`` who took the latest step, and ``message_id_verified_at`` when a
search by Message-ID first found a campaign draft made there, which arming for
send requires. Every existing mailbox starts disarmed and unverified: nothing
armed one before this revision.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-27 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COLUMNS: Final = ("armed_at", "send_armed_at", "armed_by", "message_id_verified_at")


def upgrade() -> None:
    op.add_column("mailboxes", sa.Column("armed_at", sa.DateTime(), nullable=True))
    op.add_column("mailboxes", sa.Column("send_armed_at", sa.DateTime(), nullable=True))
    op.add_column("mailboxes", sa.Column("armed_by", sa.String(length=100), nullable=True))
    op.add_column("mailboxes", sa.Column("message_id_verified_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("mailboxes", column)
