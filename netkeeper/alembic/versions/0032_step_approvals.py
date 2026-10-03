"""Approve a campaign once per step, not once per message (#339, spec 11.8).

``campaign_step_approvals`` holds one approval per step (``enrollment_id``
NULL), for the step fingerprint it was given for, and for a step whose template
uses ``{{ personal_line }}``, one approval per message (per enrollment). The
review gate reads it in place of the sampled and searched previews in
``campaign_review_previews``, which stays and is no longer read. Nothing is
recorded before this revision, so every campaign under review starts with no
step approved.

The downgrade drops the table. Code before this revision asks for the sample
again, so nothing it relies on is lost.

Revision ID: 0032
Revises: 0030
Create Date: 2026-10-03 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
# Provisional: repointed to "0031" when rebased after #338.
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "campaign_step_approvals"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.Integer(), nullable=False),
        sa.Column("enrollment_id", sa.Integer(), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f(f"fk_{TABLE}_campaign_id_campaigns"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["campaign_steps.id"],
            name=op.f(f"fk_{TABLE}_step_id_campaign_steps"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["enrollment_id"],
            ["enrollments.id"],
            name=op.f(f"fk_{TABLE}_enrollment_id_enrollments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f(f"fk_{TABLE}_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
        sa.UniqueConstraint(
            "user_id",
            "step_id",
            "enrollment_id",
            name=op.f(f"uq_{TABLE}_user_id_step_id_enrollment_id"),
        ),
    )
    for column in ("user_id", "campaign_id", "step_id", "enrollment_id"):
        op.create_index(op.f(f"ix_{TABLE}_{column}"), TABLE, [column], unique=False)
    # One whole-step approval per step: NULL is not equal to NULL in the constraint above.
    op.create_index(
        "uq_campaign_step_approvals_whole_step",
        TABLE,
        ["user_id", "step_id"],
        unique=True,
        sqlite_where=sa.text("enrollment_id IS NULL"),
        postgresql_where=sa.text("enrollment_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
