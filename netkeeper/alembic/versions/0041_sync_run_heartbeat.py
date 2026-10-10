"""A run's heartbeat (#467).

Two ``NETKEEPER_DATA`` directories can share one database through
``NETKEEPER_DATABASE_URL``. Each keeps its browser locks in its own directory, so
one could not tell whether the other's ``running`` run was still alive, and it
could mark that run ``failed`` and start a second run on the same Chrome.
``sync_runs`` gains ``heartbeat_at``: when the process running the run last said
it is still running it. A live runner refreshes it, and a run counts as left
behind only once its heartbeat is old.

No existing data changes. A run from before this migration has no heartbeat, so
it is judged by its start time, as before.

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-10 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: Final = "sync_runs"
COLUMN: Final = "heartbeat_at"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
