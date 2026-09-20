"""Tags, tag assignments, suppressions, and auto-tag rules.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-20 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enums are strings plus a CHECK, identical on SQLite and PostgreSQL.
TAG_KIND = "kind IN ('manual', 'auto', 'llm')"
TAG_SOURCE = "source IN ('manual', 'rule', 'llm')"
RULE_FIELD = "field IN ('title', 'headline', 'company')"


def _timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]


def _user_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["user_id"], ["users.id"], name=op.f(f"fk_{table}_user_id_users"), ondelete="CASCADE"
    )


def _tag_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tag_id"], ["tags.id"], name=op.f(f"fk_{table}_tag_id_tags"), ondelete="CASCADE"
    )


def _contact_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["contact_id"],
        ["contacts.id"],
        name=op.f(f"fk_{table}_contact_id_contacts"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        # ``name`` lowercased: the per-user unique is case-insensitive through it.
        sa.Column("name_key", sa.String(length=100), nullable=False),
        sa.Column("color", sa.String(length=7), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(TAG_KIND, name=op.f("ck_tags_tag_kind")),
        _user_fk("tags"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tags")),
        sa.UniqueConstraint("user_id", "name_key", name=op.f("uq_tags_user_id_name_key")),
    )
    op.create_index(op.f("ix_tags_user_id"), "tags", ["user_id"], unique=False)

    op.create_table(
        "autotag_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(length=16), nullable=False),
        sa.Column("pattern", sa.String(length=500), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(RULE_FIELD, name=op.f("ck_autotag_rules_rule_field")),
        _tag_fk("autotag_rules"),
        _user_fk("autotag_rules"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_autotag_rules")),
    )
    op.create_index(op.f("ix_autotag_rules_tag_id"), "autotag_rules", ["tag_id"], unique=False)
    op.create_index(op.f("ix_autotag_rules_user_id"), "autotag_rules", ["user_id"], unique=False)

    op.create_table(
        "contact_tags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        # The rule credited with a ``rule`` assignment; cleared when that rule goes.
        sa.Column("rule_id", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(TAG_SOURCE, name=op.f("ck_contact_tags_tag_source")),
        _contact_fk("contact_tags"),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["autotag_rules.id"],
            name=op.f("fk_contact_tags_rule_id_autotag_rules"),
            ondelete="SET NULL",
        ),
        _tag_fk("contact_tags"),
        _user_fk("contact_tags"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_tags")),
        sa.UniqueConstraint(
            "user_id",
            "contact_id",
            "tag_id",
            name=op.f("uq_contact_tags_user_id_contact_id_tag_id"),
        ),
    )
    op.create_index(
        op.f("ix_contact_tags_contact_id"), "contact_tags", ["contact_id"], unique=False
    )
    op.create_index(op.f("ix_contact_tags_rule_id"), "contact_tags", ["rule_id"], unique=False)
    op.create_index(op.f("ix_contact_tags_tag_id"), "contact_tags", ["tag_id"], unique=False)
    op.create_index(op.f("ix_contact_tags_user_id"), "contact_tags", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_contact_tags_user_id_tag_id"), "contact_tags", ["user_id", "tag_id"], unique=False
    )

    op.create_table(
        "contact_tag_suppressions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        *_timestamps(),
        _contact_fk("contact_tag_suppressions"),
        _tag_fk("contact_tag_suppressions"),
        _user_fk("contact_tag_suppressions"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_tag_suppressions")),
        sa.UniqueConstraint(
            "user_id",
            "contact_id",
            "tag_id",
            name=op.f("uq_contact_tag_suppressions_user_id_contact_id_tag_id"),
        ),
    )
    op.create_index(
        op.f("ix_contact_tag_suppressions_contact_id"),
        "contact_tag_suppressions",
        ["contact_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_contact_tag_suppressions_tag_id"),
        "contact_tag_suppressions",
        ["tag_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_contact_tag_suppressions_user_id"),
        "contact_tag_suppressions",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    # Referencing tables first. Dropping a table drops its indexes.
    op.drop_table("contact_tag_suppressions")
    op.drop_table("contact_tags")
    op.drop_table("autotag_rules")
    op.drop_table("tags")
