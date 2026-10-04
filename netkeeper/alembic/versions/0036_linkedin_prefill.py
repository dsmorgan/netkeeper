"""A LinkedIn step's prefill (P4-09, #379).

``messages`` gains what a prefill leaves behind:

- ``prefilled_at``: when the run typed the message into LinkedIn's composer. A
  ``prefilled`` message goes ``stale`` three days after it.
- ``sync_run_id``: the ``message_send`` run that typed it (``SET NULL`` when that
  run is deleted), indexed.
- An index on ``(user_id, li_conversation_urn)``, for the inbox poll that finds a
  prefilled message sent (P4-02).

No existing data changes. Every existing message has neither value.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-03 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: Final = "messages"
RUN_FK: Final = "fk_messages_sync_run_id_sync_runs"
RUN_INDEX: Final = "ix_messages_sync_run_id"
CONVERSATION_INDEX: Final = "ix_messages_user_id_li_conversation_urn"


def upgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column("prefilled_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("sync_run_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(RUN_FK, "sync_runs", ["sync_run_id"], ["id"], ondelete="SET NULL")
    op.create_index(RUN_INDEX, TABLE, ["sync_run_id"], unique=False)
    op.create_index(CONVERSATION_INDEX, TABLE, ["user_id", "li_conversation_urn"], unique=False)


def downgrade() -> None:
    op.drop_index(CONVERSATION_INDEX, table_name=TABLE)
    op.drop_index(RUN_INDEX, table_name=TABLE)
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(RUN_FK, type_="foreignkey")
        batch.drop_column("sync_run_id")
        batch.drop_column("prefilled_at")
