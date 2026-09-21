"""Static lists, smart lists, and saved table views.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-21 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enums are strings plus a CHECK, identical on SQLite and PostgreSQL.
LIST_KIND = "kind IN ('static', 'smart')"
# A static list stores no filter; a smart list always carries one (see the ORM models).
LIST_KIND_FILTER = (
    "(kind = 'static' AND filter_json IS NULL) OR (kind = 'smart' AND filter_json IS NOT NULL)"
)


def _timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]


def _user_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["user_id"], ["users.id"], name=op.f(f"fk_{table}_user_id_users"), ondelete="CASCADE"
    )


def upgrade() -> None:
    op.create_table(
        "lists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        # A serialized filter tree (model_dump(mode="json")); NULL for a static list.
        sa.Column("filter_json", sa.JSON(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(LIST_KIND, name=op.f("ck_lists_list_kind")),
        sa.CheckConstraint(LIST_KIND_FILTER, name=op.f("ck_lists_list_kind_filter")),
        _user_fk("lists"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lists")),
        sa.UniqueConstraint("user_id", "name", name=op.f("uq_lists_user_id_name")),
    )
    op.create_index(op.f("ix_lists_user_id"), "lists", ["user_id"], unique=False)

    op.create_table(
        "list_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("list_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_list_members_contact_id_contacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["list_id"],
            ["lists.id"],
            name=op.f("fk_list_members_list_id_lists"),
            ondelete="CASCADE",
        ),
        _user_fk("list_members"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_list_members")),
        sa.UniqueConstraint(
            "user_id",
            "list_id",
            "contact_id",
            name=op.f("uq_list_members_user_id_list_id_contact_id"),
        ),
    )
    op.create_index(
        op.f("ix_list_members_contact_id"), "list_members", ["contact_id"], unique=False
    )
    op.create_index(op.f("ix_list_members_list_id"), "list_members", ["list_id"], unique=False)
    op.create_index(op.f("ix_list_members_user_id"), "list_members", ["user_id"], unique=False)

    op.create_table(
        "saved_views",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        # Column identifiers the frontend contacts table (P1-12) shows, in order.
        sa.Column("columns", sa.JSON(), nullable=False),
        # Serialized sort-key rows (model_dump(mode="json")); [] for none.
        sa.Column("sort", sa.JSON(), nullable=False),
        sa.Column("filter_json", sa.JSON(), nullable=True),
        *_timestamps(),
        _user_fk("saved_views"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_views")),
        sa.UniqueConstraint("user_id", "name", name=op.f("uq_saved_views_user_id_name")),
    )
    op.create_index(op.f("ix_saved_views_user_id"), "saved_views", ["user_id"], unique=False)


def downgrade() -> None:
    # Referencing tables first. Dropping a table drops its indexes.
    op.drop_table("saved_views")
    op.drop_table("list_members")
    op.drop_table("lists")
