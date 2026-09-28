"""The review gate's records: previews, test sends, lint and guard acknowledgement (P3-09).

Spec 11.8: a campaign is activated only once its sampled and searched previews
are approved, each email step has a test send, its templates are lint clean,
and its guard summary is acknowledged. ``campaign_review_previews`` holds the
previews, ``campaign_test_sends`` the test sends, and four new ``campaigns``
columns the lint record and the acknowledgement. Each record carries the
fingerprint of what it was made for, so a later change undoes it. Nothing is
recorded before this revision, so every existing campaign starts unreviewed.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-27 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS: Final = (
    ("lint_checked_at", sa.DateTime()),
    ("lint_fingerprint", sa.String(length=64)),
    ("guards_acknowledged_at", sa.DateTime()),
    ("guards_fingerprint", sa.String(length=64)),
    ("guards_summary", sa.Text()),
)


def _owner(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["user_id"], ["users.id"], name=op.f(f"fk_{table}_user_id_users"), ondelete="CASCADE"
    )


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("campaigns", sa.Column(name, type_, nullable=True))

    op.create_table(
        "campaign_review_previews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("enrollment_id", sa.Integer(), nullable=False),
        sa.Column("sampled", sa.Boolean(), nullable=False),
        sa.Column("sample_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("viewed_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f("fk_campaign_review_previews_campaign_id_campaigns"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["enrollment_id"],
            ["enrollments.id"],
            name=op.f("fk_campaign_review_previews_enrollment_id_enrollments"),
            ondelete="CASCADE",
        ),
        _owner("campaign_review_previews"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_review_previews")),
        sa.UniqueConstraint(
            "user_id",
            "campaign_id",
            "enrollment_id",
            name=op.f("uq_campaign_review_previews_user_id_campaign_id_enrollment_id"),
        ),
    )
    for column in ("user_id", "campaign_id", "enrollment_id"):
        op.create_index(
            op.f(f"ix_campaign_review_previews_{column}"),
            "campaign_review_previews",
            [column],
            unique=False,
        )

    op.create_table(
        "campaign_test_sends",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("to_address", sa.String(length=320), nullable=False),
        sa.Column("gmail_message_id", sa.String(length=200), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f("fk_campaign_test_sends_campaign_id_campaigns"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["campaign_steps.id"],
            name=op.f("fk_campaign_test_sends_step_id_campaign_steps"),
            ondelete="CASCADE",
        ),
        _owner("campaign_test_sends"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_test_sends")),
    )
    for column in ("user_id", "campaign_id", "step_id"):
        op.create_index(
            op.f(f"ix_campaign_test_sends_{column}"), "campaign_test_sends", [column], unique=False
        )


def downgrade() -> None:
    op.drop_table("campaign_test_sends")
    op.drop_table("campaign_review_previews")
    for name, _ in reversed(_COLUMNS):
        op.drop_column("campaigns", name)
