"""Mailboxes count their authorizations (#256).

A health check reads a mailbox, asks Google about its token with no session
open, then records the answer only if the mailbox is still the one it asked
about. ``keychain_ref`` cannot tell: it never changes, re-authorization
included, so a check refreshing the old, dead token while a person
re-authorized marked the fresh grant ``reauth_required``. ``generation`` goes
up on every authorization, and the check compares it instead. Every existing
mailbox starts at 0; nothing is in flight across an upgrade.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-27 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mailboxes",
        sa.Column("generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("mailboxes", "generation")
