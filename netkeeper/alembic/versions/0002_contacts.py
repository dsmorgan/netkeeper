"""Contacts and their child tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-20 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enums are strings plus a CHECK, identical on SQLite and PostgreSQL.
CONTACT_MET = "met IN ('unknown', 'met', 'not_met', 'skip')"
SOURCE = "source IN ('sync', 'archive', 'csv', 'manual')"
EMAIL_KIND = "kind IN ('personal', 'work', 'other')"
EMAIL_STATUS = "status IN ('ok', 'bounced', 'invalid')"
PHONE_KIND = "kind IN ('mobile', 'home', 'work', 'other')"
LINK_KIND = "kind IN ('website', 'twitter', 'github', 'other')"
INTERACTION_KIND = (
    "kind IN ('note', 'call', 'meeting', 'email_out', 'email_in', 'li_out', 'li_in', 'li_view')"
)

# Children of contacts, in creation order. Each has the same provenance and
# ownership columns around its own; see _child_columns and _child_constraints.
CHILDREN = (
    "contact_emails",
    "contact_phones",
    "contact_links",
    "contact_positions",
    "contact_snapshots",
    "contact_aliases",
    "interactions",
)


def _child_head() -> list[sa.Column[Any]]:
    return [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
    ]


def _child_tail() -> list[sa.Column[Any]]:
    return [
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]


def _child_constraints(table: str) -> list[sa.schema.SchemaItem]:
    return [
        sa.CheckConstraint(SOURCE, name=op.f(f"ck_{table}_contact_source")),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f(f"fk_{table}_contact_id_contacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f(f"fk_{table}_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    ]


def _child_indexes(table: str) -> None:
    op.create_index(op.f(f"ix_{table}_contact_id"), table, ["contact_id"], unique=False)
    op.create_index(op.f(f"ix_{table}_user_id"), table, ["user_id"], unique=False)


def upgrade() -> None:
    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("li_urn", sa.String(length=200), nullable=True),
        sa.Column("li_public_id", sa.String(length=200), nullable=True),
        sa.Column("li_url", sa.String(length=500), nullable=True),
        sa.Column("first_name", sa.String(length=200), nullable=False),
        sa.Column("last_name", sa.String(length=200), nullable=False),
        sa.Column("preferred_name", sa.String(length=200), nullable=False),
        sa.Column("headline", sa.String(length=500), nullable=True),
        sa.Column("current_title", sa.String(length=300), nullable=True),
        sa.Column("current_company", sa.String(length=300), nullable=True),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("connected_on", sa.Date(), nullable=True),
        sa.Column("degree", sa.Integer(), nullable=False),
        sa.Column("met", sa.String(length=16), nullable=False),
        sa.Column("triaged_at", sa.DateTime(), nullable=True),
        sa.Column("do_not_contact", sa.Boolean(), nullable=False),
        sa.Column("do_not_contact_reason", sa.String(length=500), nullable=True),
        sa.Column("li_missing_count", sa.Integer(), nullable=False),
        sa.Column("li_disconnected_at", sa.DateTime(), nullable=True),
        sa.Column("last_enriched_at", sa.DateTime(), nullable=True),
        sa.Column("enrich_priority", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("merged_into_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(CONTACT_MET, name=op.f("ck_contacts_contact_met")),
        sa.CheckConstraint(SOURCE, name=op.f("ck_contacts_contact_source")),
        sa.ForeignKeyConstraint(
            ["merged_into_id"],
            ["contacts.id"],
            name=op.f("fk_contacts_merged_into_id_contacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_contacts_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contacts")),
        sa.UniqueConstraint(
            "user_id", "li_public_id", name=op.f("uq_contacts_user_id_li_public_id")
        ),
        sa.UniqueConstraint("user_id", "li_urn", name=op.f("uq_contacts_user_id_li_urn")),
    )
    op.create_index(
        op.f("ix_contacts_merged_into_id"), "contacts", ["merged_into_id"], unique=False
    )
    op.create_index(op.f("ix_contacts_user_id"), "contacts", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_contacts_user_id_archived_at"),
        "contacts",
        ["user_id", "archived_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_contacts_user_id_current_company"),
        "contacts",
        ["user_id", "current_company"],
        unique=False,
    )
    op.create_index(
        op.f("ix_contacts_user_id_last_name_first_name"),
        "contacts",
        ["user_id", "last_name", "first_name"],
        unique=False,
    )
    op.create_index(op.f("ix_contacts_user_id_met"), "contacts", ["user_id", "met"], unique=False)

    op.create_table(
        "contact_emails",
        *_child_head(),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        *_child_tail(),
        sa.CheckConstraint(EMAIL_KIND, name=op.f("ck_contact_emails_email_kind")),
        sa.CheckConstraint(EMAIL_STATUS, name=op.f("ck_contact_emails_email_status")),
        *_child_constraints("contact_emails"),
        sa.UniqueConstraint(
            "user_id",
            "contact_id",
            "email",
            name=op.f("uq_contact_emails_user_id_contact_id_email"),
        ),
    )
    _child_indexes("contact_emails")
    op.create_index(
        op.f("ix_contact_emails_user_id_email"),
        "contact_emails",
        ["user_id", "email"],
        unique=False,
    )

    op.create_table(
        "contact_phones",
        *_child_head(),
        sa.Column("number_e164", sa.String(length=20), nullable=True),
        sa.Column("raw", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        *_child_tail(),
        sa.CheckConstraint(PHONE_KIND, name=op.f("ck_contact_phones_phone_kind")),
        *_child_constraints("contact_phones"),
    )
    _child_indexes("contact_phones")

    op.create_table(
        "contact_links",
        *_child_head(),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        *_child_tail(),
        sa.CheckConstraint(LINK_KIND, name=op.f("ck_contact_links_link_kind")),
        *_child_constraints("contact_links"),
    )
    _child_indexes("contact_links")

    op.create_table(
        "contact_positions",
        *_child_head(),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("company", sa.String(length=300), nullable=True),
        sa.Column("company_urn", sa.String(length=200), nullable=True),
        sa.Column("started_on", sa.Date(), nullable=True),
        sa.Column("ended_on", sa.Date(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        *_child_tail(),
        *_child_constraints("contact_positions"),
    )
    _child_indexes("contact_positions")

    op.create_table(
        "contact_snapshots",
        *_child_head(),
        sa.Column("headline", sa.String(length=500), nullable=True),
        sa.Column("current_title", sa.String(length=300), nullable=True),
        sa.Column("current_company", sa.String(length=300), nullable=True),
        sa.Column("location", sa.String(length=300), nullable=True),
        *_child_tail(),
        *_child_constraints("contact_snapshots"),
    )
    _child_indexes("contact_snapshots")
    op.create_index(
        op.f("ix_contact_snapshots_user_id_observed_at"),
        "contact_snapshots",
        ["user_id", "observed_at"],
        unique=False,
    )

    op.create_table(
        "contact_aliases",
        *_child_head(),
        sa.Column("li_public_id", sa.String(length=200), nullable=False),
        *_child_tail(),
        *_child_constraints("contact_aliases"),
        sa.UniqueConstraint(
            "user_id", "li_public_id", name=op.f("uq_contact_aliases_user_id_li_public_id")
        ),
    )
    _child_indexes("contact_aliases")

    op.create_table(
        "interactions",
        *_child_head(),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        # A foreign key to messages arrives with that table (P3-04).
        sa.Column("message_id", sa.Integer(), nullable=True),
        *_child_tail(),
        sa.CheckConstraint(INTERACTION_KIND, name=op.f("ck_interactions_interaction_kind")),
        *_child_constraints("interactions"),
    )
    _child_indexes("interactions")


def downgrade() -> None:
    # Children first: each references contacts. Dropping a table drops its indexes.
    for table in reversed(CHILDREN):
        op.drop_table(table)
    op.drop_table("contacts")
