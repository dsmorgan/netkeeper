"""Enrollments count the sends that certainly sent nothing (P3-07 follow-up, #280).

A send that sent nothing for a reason that may pass (``not_sent``: a rate
limit, an outage before the write, a thread that could not be read) gives its
claim back and is tried again later. Until now nothing counted those tries, so a
step whose thread could never be read was tried every 15 minutes for good, and
left no trace. ``not_sent_count`` and ``not_sent_since`` count the consecutive
tries and when the first was; each retry waits longer, and after enough of them
over long enough the step is failed for a person. ``not_sent_error`` is the
latest try's reason. Every existing enrollment starts with none: nothing it did
before this migration was counted.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-27 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COLUMNS: Final = ("not_sent_count", "not_sent_since", "not_sent_error")


def upgrade() -> None:
    op.add_column(
        "enrollments",
        sa.Column("not_sent_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("enrollments", sa.Column("not_sent_since", sa.DateTime(), nullable=True))
    op.add_column("enrollments", sa.Column("not_sent_error", sa.String(length=500), nullable=True))


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("enrollments", column)
