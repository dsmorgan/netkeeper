"""SQLite-only probe (temporary; reverted before merge).

Proves the P1-17 guarantee: a construct that SQLite accepts and PostgreSQL rejects
fails the PostgreSQL params of tests/test_migrations.py on CI. ``strftime`` is a
SQLite function; PostgreSQL rejects the DEFAULT expression at CREATE TABLE time.
The table is dropped again in the same upgrade so the SQLite params stay green
and the failure is visibly PostgreSQL-only.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-20 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portability_probe",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "created_epoch",
            sa.Integer(),
            server_default=sa.text("(strftime('%s','now'))"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portability_probe")),
    )
    op.drop_table("portability_probe")


def downgrade() -> None:
    pass
