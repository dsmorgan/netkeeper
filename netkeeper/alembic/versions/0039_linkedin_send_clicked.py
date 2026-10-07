"""An auto-sent LinkedIn message's Send click (P4-04, #384, ADR 0008).

``messages`` gains ``send_clicked_at``: when auto-send's one click on **Send** was
sent for this message. The message stays ``prefilled`` until the inbox poll confirms
the send, as for a prefill the person sent.

``uq_messages_one_open_prefill`` (0036) holds one open LinkedIn prefill per user. An
auto-sent message is not one: nothing waits on the person for it. The index is rebuilt
to leave out a message whose Send click was sent, so the next auto-send can be claimed
while the poll has not confirmed the last one yet. Every other row it held, it still
holds.

No existing data changes. Every existing message has no Send click.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-07 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: Final = "messages"
OPEN_INDEX: Final = "uq_messages_one_open_prefill"
OLD_WHERE: Final = (
    "channel = 'linkedin' AND direction = 'out' AND status IN ('scheduled', 'prefilled')"
)
NEW_WHERE: Final = (
    "channel = 'linkedin' AND direction = 'out' AND status IN ('scheduled', 'prefilled')"
    " AND send_clicked_at IS NULL"
)


def _open_index(where: str) -> None:
    op.create_index(
        OPEN_INDEX,
        TABLE,
        ["user_id"],
        unique=True,
        sqlite_where=sa.text(where),
        postgresql_where=sa.text(where),
    )


def upgrade() -> None:
    op.drop_index(OPEN_INDEX, table_name=TABLE)
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column("send_clicked_at", sa.DateTime(), nullable=True))
    _open_index(NEW_WHERE)


def downgrade() -> None:
    # A user with more than one open row once auto-sent rows count again cannot go back
    # under the old index; the downgrade fails loudly rather than drop a message.
    op.drop_index(OPEN_INDEX, table_name=TABLE)
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column("send_clicked_at")
    _open_index(OLD_WHERE)
