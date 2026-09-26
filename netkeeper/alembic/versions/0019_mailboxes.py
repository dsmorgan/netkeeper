"""Mailboxes, and the foreign key ``campaigns.mailbox_id`` has waited for (spec 8.5; item P3-01).

Nothing could have written a real mailbox id before this revision, so any value
already in ``campaigns.mailbox_id`` points nowhere and is cleared first; the
constraint could not be added over it on PostgreSQL. The key has no ``ON DELETE``
action: a mailbox is disconnected, never deleted, so a campaign always keeps the
account it sent from.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-26 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enums are strings plus a CHECK, identical on SQLite and PostgreSQL.
MAILBOX_PROVIDER = "provider IN ('gmail')"
MAILBOX_STATUS = "status IN ('ok', 'reauth_required', 'disabled')"


def upgrade() -> None:
    op.create_table(
        "mailboxes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("keychain_ref", sa.String(length=200), nullable=False),
        sa.Column("daily_cap", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_reason", sa.String(length=100), nullable=True),
        sa.Column("label_prefix", sa.String(length=100), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(MAILBOX_PROVIDER, name=op.f("ck_mailboxes_mailbox_provider")),
        sa.CheckConstraint(MAILBOX_STATUS, name=op.f("ck_mailboxes_mailbox_status")),
        sa.CheckConstraint("daily_cap >= 0", name=op.f("ck_mailboxes_mailbox_daily_cap")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_mailboxes_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mailboxes")),
        sa.UniqueConstraint("user_id", "email", name=op.f("uq_mailboxes_user_id_email")),
    )
    op.create_index(op.f("ix_mailboxes_user_id"), "mailboxes", ["user_id"], unique=False)

    op.execute(sa.text("UPDATE campaigns SET mailbox_id = NULL WHERE mailbox_id IS NOT NULL"))
    with op.batch_alter_table("campaigns") as batch:
        batch.create_foreign_key(
            "fk_campaigns_mailbox_id_mailboxes", "mailboxes", ["mailbox_id"], ["id"]
        )
    op.create_index(op.f("ix_campaigns_mailbox_id"), "campaigns", ["mailbox_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_campaigns_mailbox_id"), table_name="campaigns")
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_constraint("fk_campaigns_mailbox_id_mailboxes", type_="foreignkey")
    op.drop_index(op.f("ix_mailboxes_user_id"), table_name="mailboxes")
    op.drop_table("mailboxes")
