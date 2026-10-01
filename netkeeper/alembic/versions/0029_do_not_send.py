"""The do-not-send list: addresses no campaign sends to, kept by address (#238, Part B).

``do_not_send_addresses`` holds one row per user and normalized address, with the
reason it is there (``manual``, ``invalid``, ``bounced`` or ``opted_out``) and the
contact it was found on (``SET NULL`` when that contact is deleted: the entry is
about the address, not the contact).

``bounced`` is true when the address bounced, whatever its strongest reason is,
so a person removing an opt-out can be told that a bounce goes with it.

Existing data is carried over: every address a contact holds as ``bounced`` or
``invalid`` is listed with that reason, and every address of a contact that opted
out is listed as ``opted_out``. A contact opted out when it has an ``opted_out``
enrollment or an inbound message that asked to unsubscribe
(``messages.asks_unsubscribe``), which also covers a reply to an enrollment that
had already completed. When an address has more than one reason, it keeps the
strongest: ``opted_out``, then ``bounced``, then ``invalid``. Its contact is the
lowest contact id among the rows with that reason.

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-30 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: Final = "do_not_send_addresses"

# A copy of DO_NOT_SEND_RANK as it stood at this revision: a migration never imports
# application code, which may change after it.
_RANK: Final = {"manual": 0, "invalid": 1, "bounced": 2, "opted_out": 3}


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("bounced", sa.Boolean(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "reason IN ('manual', 'invalid', 'bounced', 'opted_out')",
            name=op.f(f"ck_{TABLE}_do_not_send_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f(f"fk_{TABLE}_contact_id_contacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f(f"fk_{TABLE}_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
        sa.UniqueConstraint("user_id", "email", name=op.f(f"uq_{TABLE}_user_id_email")),
    )
    for column in ("user_id", "contact_id"):
        op.create_index(op.f(f"ix_{TABLE}_{column}"), TABLE, [column], unique=False)
    _backfill(op.get_bind())


def _backfill(connection: sa.Connection) -> None:
    found: list[tuple[int, str, str, int]] = [
        (row.user_id, row.email, row.status, row.contact_id)
        for row in connection.execute(
            sa.text(
                "SELECT user_id, email, status, contact_id FROM contact_emails"
                " WHERE status IN ('bounced', 'invalid')"
            )
        )
    ]
    found.extend(
        (row.user_id, row.email, "opted_out", row.contact_id)
        for row in connection.execute(
            sa.text(
                "SELECT DISTINCT e.user_id, e.email, e.contact_id FROM contact_emails e"
                " WHERE EXISTS (SELECT 1 FROM enrollments n"
                " WHERE n.contact_id = e.contact_id AND n.user_id = e.user_id"
                " AND n.status = 'opted_out')"
                " OR EXISTS (SELECT 1 FROM messages m"
                " WHERE m.contact_id = e.contact_id AND m.user_id = e.user_id"
                " AND m.direction = 'in' AND m.asks_unsubscribe = :yes)"
            ),
            {"yes": True},
        )
    )
    best: dict[tuple[int, str], tuple[str, int]] = {}
    bounced: set[tuple[int, str]] = set()
    for user_id, email, reason, contact_id in found:
        key = (user_id, email.strip().lower())
        if reason == "bounced":
            bounced.add(key)
        held = best.get(key)
        if held is None or (_RANK[reason], -contact_id) > (_RANK[held[0]], -held[1]):
            best[key] = (reason, contact_id)
    now = datetime.now(UTC).replace(tzinfo=None)  # stored naive UTC, as UTCDateTime does
    insert = sa.text(
        "INSERT INTO do_not_send_addresses"
        " (user_id, email, reason, bounced, contact_id, created_at, updated_at)"
        " VALUES (:user_id, :email, :reason, :bounced, :contact_id, :t, :t)"
    ).bindparams(sa.bindparam("t", type_=sa.DateTime()))  # SQLAlchemy renders it, not the driver
    for (user_id, email), (reason, contact_id) in sorted(best.items()):
        connection.execute(
            insert,
            {
                "user_id": user_id,
                "email": email,
                "reason": reason,
                "bounced": (user_id, email) in bounced,
                "contact_id": contact_id,
                "t": now,
            },
        )


def downgrade() -> None:
    op.drop_table(TABLE)
