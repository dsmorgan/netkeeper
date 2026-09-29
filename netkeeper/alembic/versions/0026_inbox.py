"""The inbox lists what reply detection found, and remembers what a person handled
(P3-11b, #300).

``messages.handled_at`` is when a person marked an inbox item handled; NULL while it
is not. ``messages.asks_unsubscribe`` is whether an inbound message asked to
unsubscribe (spec 11.7's phrases), so the inbox can filter by it. ``bounced_at`` is
when detection found that an outbound message bounced.

Existing rows are filled in: an inbound message asks to unsubscribe when its subject
or snippet holds one of the phrases, matched as P3-08 matches them, and a message
already ``bounced`` takes its ``updated_at`` as the nearest record of when.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-29 00:00:00 UTC
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A copy of the reply poll's unsubscribe rule (spec 11.7) as it stood at this revision: a
# migration never imports application code, which may change after it.
_UNSUBSCRIBE: Final = re.compile(r"\b(?:unsubscribe|remove me|stop emailing)\b", re.IGNORECASE)


def upgrade() -> None:
    op.add_column("messages", sa.Column("handled_at", sa.DateTime(), nullable=True))
    op.add_column("messages", sa.Column("bounced_at", sa.DateTime(), nullable=True))
    op.add_column(
        "messages",
        sa.Column("asks_unsubscribe", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text("UPDATE messages SET bounced_at = updated_at WHERE status = 'bounced'")
    )
    rows = connection.execute(
        sa.text("SELECT id, subject, snippet FROM messages WHERE direction = 'in'")
    ).all()
    asking = [
        row.id for row in rows if _UNSUBSCRIBE.search(f"{row.subject or ''} {row.snippet or ''}")
    ]
    for message_id in asking:
        connection.execute(
            sa.text("UPDATE messages SET asks_unsubscribe = :yes WHERE id = :id"),
            {"yes": True, "id": message_id},
        )


def downgrade() -> None:
    op.drop_column("messages", "asks_unsubscribe")
    op.drop_column("messages", "bounced_at")
    op.drop_column("messages", "handled_at")
