"""History from the old mailing tool: its campaigns and their recipients (#65).

``history_campaigns`` holds one row per campaign the old tool's workbook reports,
unique per user by name and start date, with the workbook's aggregate counts and
the digest of the file it was last imported from, and when the Gmail scan last
searched for replies by the campaign's subject.

``history_recipients`` holds one row per person the workbook names for a campaign,
unique per user, campaign and address (stored trimmed and lowercased, so the key
is the key on ``lower(email)``). It records the contact the address matched
(``SET NULL`` when that contact is deleted), which of the workbook's lists named
the person (or that the Gmail scan found them by the campaign's subject), the
timeline entries the import and the Gmail scan wrote (``SET
NULL`` when one is deleted), and what the scan found.

No existing data changes. Both tables start empty.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-03 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAMPAIGNS: Final = "history_campaigns"
RECIPIENTS: Final = "history_recipients"


def upgrade() -> None:
    op.create_table(
        CAMPAIGNS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("last_batch_on", sa.Date(), nullable=True),
        sa.Column("recipients_count", sa.Integer(), nullable=True),
        sa.Column("opens_count", sa.Integer(), nullable=True),
        sa.Column("bounces_count", sa.Integer(), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("subject_scanned_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f(f"fk_{CAMPAIGNS}_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{CAMPAIGNS}")),
        sa.UniqueConstraint(
            "user_id", "name", "started_on", name=op.f(f"uq_{CAMPAIGNS}_user_id_name_started_on")
        ),
    )
    op.create_index(op.f(f"ix_{CAMPAIGNS}_user_id"), CAMPAIGNS, ["user_id"], unique=False)

    op.create_table(
        RECIPIENTS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("history_campaign_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("opened", sa.Boolean(), nullable=False),
        sa.Column("clicked", sa.Boolean(), nullable=False),
        sa.Column("bounce_listed", sa.Boolean(), nullable=False),
        sa.Column("found_by_subject", sa.Boolean(), nullable=False),
        sa.Column("email_out_interaction_id", sa.Integer(), nullable=True),
        sa.Column("scanned_at", sa.DateTime(), nullable=True),
        sa.Column("reply_kind", sa.String(length=16), nullable=True),
        sa.Column("replied_at", sa.DateTime(), nullable=True),
        sa.Column("reply_gmail_id", sa.String(length=64), nullable=True),
        sa.Column("email_in_interaction_id", sa.Integer(), nullable=True),
        sa.Column("bounced_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "reply_kind IN ('unsubscribe', 'reply', 'bounce', 'auto')",
            name=op.f(f"ck_{RECIPIENTS}_history_reply_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["history_campaign_id"],
            [f"{CAMPAIGNS}.id"],
            name=op.f(f"fk_{RECIPIENTS}_history_campaign_id_{CAMPAIGNS}"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f(f"fk_{RECIPIENTS}_contact_id_contacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["email_out_interaction_id"],
            ["interactions.id"],
            name=op.f(f"fk_{RECIPIENTS}_email_out_interaction_id_interactions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["email_in_interaction_id"],
            ["interactions.id"],
            name=op.f(f"fk_{RECIPIENTS}_email_in_interaction_id_interactions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f(f"fk_{RECIPIENTS}_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{RECIPIENTS}")),
        sa.UniqueConstraint(
            "user_id",
            "history_campaign_id",
            "email",
            name=op.f(f"uq_{RECIPIENTS}_user_id_history_campaign_id_email"),
        ),
    )
    for column in (
        "user_id",
        "history_campaign_id",
        "contact_id",
        "email_out_interaction_id",
        "email_in_interaction_id",
    ):
        op.create_index(op.f(f"ix_{RECIPIENTS}_{column}"), RECIPIENTS, [column], unique=False)


def downgrade() -> None:
    op.drop_table(RECIPIENTS)
    op.drop_table(CAMPAIGNS)
