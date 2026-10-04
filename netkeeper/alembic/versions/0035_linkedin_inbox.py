"""The LinkedIn inbox poll's rows: conversations, and a message's id on its interaction (#378).

``li_conversations`` holds one row per LinkedIn one-to-one conversation with a
known contact, unique per user by the conversation's URN, with the newest
activity and message each way the poll has seen and when it last read the
conversation. It goes with its contact (``CASCADE``) and its user.

``interactions.external_id`` is the LinkedIn message URN of an interaction the
poll recorded, unique per user, so a repeat poll writes nothing twice. Every
existing row gets ``NULL``, and SQLite and PostgreSQL both treat NULLs as
distinct, so no existing row collides.

No existing data changes. The downgrade drops both.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-03 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONVERSATIONS: Final = "li_conversations"
INTERACTIONS: Final = "interactions"
EXTERNAL_ID_UNIQUE: Final = "uq_interactions_user_id_external_id"


def upgrade() -> None:
    op.create_table(
        CONVERSATIONS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("conversation_urn", sa.String(length=300), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(), nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(), nullable=True),
        sa.Column("polled_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f(f"fk_{CONVERSATIONS}_contact_id_contacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f(f"fk_{CONVERSATIONS}_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{CONVERSATIONS}")),
        sa.UniqueConstraint(
            "user_id",
            "conversation_urn",
            name=op.f(f"uq_{CONVERSATIONS}_user_id_conversation_urn"),
        ),
    )
    op.create_index(op.f(f"ix_{CONVERSATIONS}_user_id"), CONVERSATIONS, ["user_id"], unique=False)
    op.create_index(
        f"ix_{CONVERSATIONS}_user_id_contact_id",
        CONVERSATIONS,
        ["user_id", "contact_id"],
        unique=False,
    )

    with op.batch_alter_table(INTERACTIONS) as batch:
        batch.add_column(sa.Column("external_id", sa.String(length=300), nullable=True))
        batch.create_unique_constraint(op.f(EXTERNAL_ID_UNIQUE), ["user_id", "external_id"])


def downgrade() -> None:
    with op.batch_alter_table(INTERACTIONS) as batch:
        batch.drop_constraint(op.f(EXTERNAL_ID_UNIQUE), type_="unique")
        batch.drop_column("external_id")
    op.drop_index(f"ix_{CONVERSATIONS}_user_id_contact_id", table_name=CONVERSATIONS)
    op.drop_index(op.f(f"ix_{CONVERSATIONS}_user_id"), table_name=CONVERSATIONS)
    op.drop_table(CONVERSATIONS)
