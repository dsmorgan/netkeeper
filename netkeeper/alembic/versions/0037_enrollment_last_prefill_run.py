"""The run behind an enrollment's latest LinkedIn prefill (#445).

``enrollments`` gains ``last_prefill_run_id``: the ``message_send`` run whose outcome
was recorded last for the enrollment (``SET NULL`` when that run is deleted). A
``not_typed`` prefill deletes its message, so without it nothing would say whether that
run clicked **Message** (a bubble may be open) or spent a ``li_prefills`` unit, and the
**Try again** action needs both.

An enrollment whose latest prefill ended ``not_typed`` loses its due time: before #445
it came back on a timer, and now it waits for a person to click **Try again**, so its
row says no next action, as the queue does. It has no run on file, so its retry asks
you to confirm no bubble is open. Nothing else changes. A downgrade drops the column
and leaves those enrollments parked.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-06 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: Final = "enrollments"
COLUMN: Final = "last_prefill_run_id"
RUN_FK: Final = "fk_enrollments_last_prefill_run_id_sync_runs"
#: ``linkedin_steps.NOT_TYPED_PREFIX`` as a pattern, written out: a migration never
#: imports the code it migrates for. ``_`` matches any one character, which only ever
#: widens it to ``not?typed:``, a reason nothing writes.
NOT_TYPED_LIKE: Final = "not_typed:%"


def upgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.Integer(), nullable=True))
        batch.create_foreign_key(RUN_FK, "sync_runs", [COLUMN], ["id"], ondelete="SET NULL")
    op.execute(
        sa.text(
            "UPDATE enrollments SET next_action_at = NULL"
            " WHERE not_sent_count > 0 AND not_sent_error LIKE :prefix"
        ).bindparams(prefix=NOT_TYPED_LIKE)
    )


def downgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(RUN_FK, type_="foreignkey")
        batch.drop_column(COLUMN)
