"""Campaigns, their steps, enrollments, and messages (spec 8.5, 11.2, 11.3; item P3-04).

Also gives ``interactions.message_id`` the foreign key it has waited for since
0002: ``messages`` now exists. Nothing could have written a real message id
before this revision, so any value already in the column points nowhere and is
cleared first; the constraint could not be added over it on PostgreSQL.

What each ``ON DELETE`` means is in the campaign models' module docstring: what was
sent is never deleted by deleting what it names, and what was never sent goes
with its owner.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-26 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enums are strings plus a CHECK, identical on SQLite and PostgreSQL.
CAMPAIGN_STATUS = "status IN ('draft', 'reviewing', 'active', 'paused', 'completed', 'archived')"
CHANNEL = "channel IN ('email', 'linkedin')"
STEP_MODE = "mode IN ('draft', 'send', 'prefill', 'auto_send')"
STEP_CONDITION = "condition IN ('always', 'no_reply')"
ENROLLMENT_STATUS = (
    "status IN ('pending', 'active', 'paused', 'replied', 'completed', 'bounced',"
    " 'opted_out', 'removed')"
)
MESSAGE_DIRECTION = "direction IN ('out', 'in')"
MESSAGE_STATUS = (
    "status IN ('scheduled', 'drafted', 'prefilled', 'sent', 'stale', 'discarded', 'bounced',"
    " 'failed', 'received')"
)


def _owner(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["user_id"], ["users.id"], name=op.f(f"fk_{table}_user_id_users"), ondelete="CASCADE"
    )


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_list_id", sa.Integer(), nullable=True),
        sa.Column("filter_json", sa.JSON(none_as_null=True), nullable=True),
        # No foreign key until the mailbox table exists (P3-07).
        sa.Column("mailbox_id", sa.Integer(), nullable=True),
        sa.Column("send_window_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("daily_cap", sa.Integer(), nullable=True),
        sa.Column("contacted_within_days_guard", sa.Integer(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("test_sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(CAMPAIGN_STATUS, name=op.f("ck_campaigns_campaign_status")),
        sa.CheckConstraint(
            "source_list_id IS NULL OR filter_json IS NULL",
            name=op.f("ck_campaigns_campaign_one_audience"),
        ),
        sa.CheckConstraint(
            "daily_cap IS NULL OR daily_cap >= 0", name=op.f("ck_campaigns_campaign_daily_cap")
        ),
        sa.CheckConstraint(
            "contacted_within_days_guard >= 0",
            name=op.f("ck_campaigns_campaign_contacted_within_days_guard"),
        ),
        sa.ForeignKeyConstraint(
            ["source_list_id"],
            ["lists.id"],
            name=op.f("fk_campaigns_source_list_id_lists"),
            ondelete="SET NULL",
        ),
        _owner("campaigns"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaigns")),
        sa.UniqueConstraint("user_id", "name", name=op.f("uq_campaigns_user_id_name")),
    )
    op.create_index(op.f("ix_campaigns_user_id"), "campaigns", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_campaigns_source_list_id"), "campaigns", ["source_list_id"], unique=False
    )

    op.create_table(
        "campaign_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("delay_days", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("condition", sa.String(length=16), nullable=False),
        sa.Column("same_thread", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(CHANNEL, name=op.f("ck_campaign_steps_step_channel")),
        sa.CheckConstraint(STEP_MODE, name=op.f("ck_campaign_steps_step_mode")),
        sa.CheckConstraint(STEP_CONDITION, name=op.f("ck_campaign_steps_step_condition")),
        sa.CheckConstraint("position >= 1", name=op.f("ck_campaign_steps_step_position")),
        sa.CheckConstraint("delay_days >= 0", name=op.f("ck_campaign_steps_step_delay_days")),
        sa.CheckConstraint(
            "(channel = 'email' AND mode IN ('draft', 'send'))"
            " OR (channel = 'linkedin' AND mode IN ('prefill', 'auto_send'))",
            name=op.f("ck_campaign_steps_step_channel_mode"),
        ),
        sa.CheckConstraint(
            "NOT same_thread OR channel = 'email'", name=op.f("ck_campaign_steps_step_same_thread")
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f("fk_campaign_steps_campaign_id_campaigns"),
            ondelete="CASCADE",
        ),
        # No ON DELETE: a template a campaign names cannot be deleted.
        sa.ForeignKeyConstraint(
            ["template_id"], ["templates.id"], name=op.f("fk_campaign_steps_template_id_templates")
        ),
        _owner("campaign_steps"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_steps")),
        sa.UniqueConstraint(
            "user_id",
            "campaign_id",
            "position",
            name=op.f("uq_campaign_steps_user_id_campaign_id_position"),
        ),
    )
    op.create_index(op.f("ix_campaign_steps_user_id"), "campaign_steps", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_campaign_steps_campaign_id"), "campaign_steps", ["campaign_id"], unique=False
    )
    op.create_index(
        op.f("ix_campaign_steps_template_id"), "campaign_steps", ["template_id"], unique=False
    )

    op.create_table(
        "enrollments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("current_step", sa.Integer(), nullable=True),
        sa.Column("next_action_at", sa.DateTime(), nullable=True),
        sa.Column("exit_reason", sa.String(length=100), nullable=True),
        sa.Column("replied_at", sa.DateTime(), nullable=True),
        sa.Column("channel_ids_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(ENROLLMENT_STATUS, name=op.f("ck_enrollments_enrollment_status")),
        sa.CheckConstraint(
            "current_step IS NULL OR current_step >= 1",
            name=op.f("ck_enrollments_enrollment_current_step"),
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f("fk_enrollments_campaign_id_campaigns"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_enrollments_contact_id_contacts"),
            ondelete="CASCADE",
        ),
        _owner("enrollments"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_enrollments")),
        sa.UniqueConstraint(
            "user_id",
            "campaign_id",
            "contact_id",
            name=op.f("uq_enrollments_user_id_campaign_id_contact_id"),
        ),
    )
    op.create_index(op.f("ix_enrollments_user_id"), "enrollments", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_enrollments_campaign_id"), "enrollments", ["campaign_id"], unique=False
    )
    op.create_index(op.f("ix_enrollments_contact_id"), "enrollments", ["contact_id"], unique=False)
    op.create_index(
        "ix_enrollments_user_id_status_next_action_at",
        "enrollments",
        ["user_id", "status", "next_action_at"],
        unique=False,
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("enrollment_id", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.Integer(), nullable=True),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.String(length=1000), nullable=True),
        sa.Column("body_rendered", sa.Text(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("gmail_message_id", sa.String(length=200), nullable=True),
        sa.Column("gmail_thread_id", sa.String(length=200), nullable=True),
        sa.Column("gmail_draft_id", sa.String(length=200), nullable=True),
        sa.Column("li_conversation_urn", sa.String(length=300), nullable=True),
        sa.Column("li_message_urn", sa.String(length=300), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(CHANNEL, name=op.f("ck_messages_message_channel")),
        sa.CheckConstraint(MESSAGE_DIRECTION, name=op.f("ck_messages_message_direction")),
        sa.CheckConstraint(MESSAGE_STATUS, name=op.f("ck_messages_message_status")),
        sa.CheckConstraint(
            "(direction = 'in') = (status = 'received')",
            name=op.f("ck_messages_message_direction_status"),
        ),
        # No ON DELETE on these three: a message is never deleted by deleting what it names.
        sa.ForeignKeyConstraint(
            ["enrollment_id"],
            ["enrollments.id"],
            name=op.f("fk_messages_enrollment_id_enrollments"),
        ),
        sa.ForeignKeyConstraint(
            ["step_id"], ["campaign_steps.id"], name=op.f("fk_messages_step_id_campaign_steps")
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"], ["contacts.id"], name=op.f("fk_messages_contact_id_contacts")
        ),
        _owner("messages"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index(op.f("ix_messages_user_id"), "messages", ["user_id"], unique=False)
    op.create_index(op.f("ix_messages_enrollment_id"), "messages", ["enrollment_id"], unique=False)
    op.create_index(op.f("ix_messages_step_id"), "messages", ["step_id"], unique=False)
    op.create_index(op.f("ix_messages_contact_id"), "messages", ["contact_id"], unique=False)
    op.create_index("ix_messages_user_id_status", "messages", ["user_id", "status"], unique=False)

    # Every existing value points at a table that did not exist; see the docstring.
    op.execute(sa.text("UPDATE interactions SET message_id = NULL WHERE message_id IS NOT NULL"))
    # batch mode: SQLite cannot add a foreign key to a table that exists, so Alembic
    # rebuilds it there and emits a plain ALTER TABLE everywhere else.
    with op.batch_alter_table("interactions") as batch:
        batch.create_foreign_key(
            "fk_interactions_message_id_messages",
            "messages",
            ["message_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        op.f("ix_interactions_message_id"), "interactions", ["message_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_interactions_message_id"), table_name="interactions")
    with op.batch_alter_table("interactions") as batch:
        batch.drop_constraint("fk_interactions_message_id_messages", type_="foreignkey")
    op.drop_table("messages")
    op.drop_table("enrollments")
    op.drop_table("campaign_steps")
    op.drop_table("campaigns")
