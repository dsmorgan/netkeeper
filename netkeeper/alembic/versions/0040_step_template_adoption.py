"""A campaign step records adopting a newer version of its template (#397).

Editing a template an active or paused campaign uses makes a new version, and the
step keeps the version it was activated with. A person can now have the step use
the newest version instead, after a confirm that shows the change and approves
it. ``campaign_steps`` gains what that leaves behind:

- ``template_adopted_at``: when the person adopted the version the step now uses.
- ``template_adopted_by``: the id of the user who adopted it. It has no foreign
  key, because ``user_id`` must stay the table's only key to ``users``.
- ``template_adopted_from_version``: the version number the step used before.

No existing data changes. No step has adopted a version yet, so none of these is
set.

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-07 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: Final = "campaign_steps"
COLUMNS: Final = (
    "template_adopted_at",
    "template_adopted_by",
    "template_adopted_from_version",
)


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("template_adopted_at", sa.DateTime(), nullable=True))
    op.add_column(TABLE, sa.Column("template_adopted_by", sa.Integer(), nullable=True))
    op.add_column(TABLE, sa.Column("template_adopted_from_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        for column in reversed(COLUMNS):
            batch.drop_column(column)
