"""Users and settings.

Revision ID: 0001
Revises:
Create Date: 2026-09-20 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        # A string plus CHECK rather than a native enum: identical on SQLite and PostgreSQL.
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('local', 'hosted')", name=op.f("ck_users_user_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_table(
        "settings_kv",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_settings_kv_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settings_kv")),
        sa.UniqueConstraint("user_id", "key", name=op.f("uq_settings_kv_user_id_key")),
    )
    op.create_index(op.f("ix_settings_kv_user_id"), "settings_kv", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_settings_kv_user_id"), table_name="settings_kv")
    op.drop_table("settings_kv")
    op.drop_table("users")
