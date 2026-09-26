"""Lists carry a builtin mark (spec 10.4; #133).

``builtin`` records that the app created the row: the "Validated" smart list
it seeds for every user. It is provenance, not protection, so it survives a
rename or an edit and the list can still be deleted.

Every existing list is added unmarked, then the one the app seeded is found
and marked. A list is that one only when all of these hold, so a list the
person made, or the seeded one after they changed it past recognition, stays
unmarked rather than being claimed by a guess:

* its user's ``lists.validated_seeded`` key is set,
* it is a smart list named ``Validated``,
* its filter is ``met = "met"`` over live contacts, as seeded.

The filter is compared in Python, because PostgreSQL's ``json`` type has no
equality operator. The name, key, and filter are restated here rather than
imported: a migration is frozen history and must not track the application's
modules.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-26 00:00:00 UTC
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEEDED_KEY = "lists.validated_seeded"
_NAME = "Validated"
_WHERE = {"op": "eq", "field": "met", "value": "met"}

_lists = sa.table(
    "lists",
    sa.column("id", sa.Integer()),
    sa.column("user_id", sa.Integer()),
    sa.column("name", sa.String()),
    sa.column("kind", sa.String()),
    sa.column("filter_json", sa.JSON()),
    sa.column("builtin", sa.Boolean()),
)
_settings = sa.table(
    "settings_kv",
    sa.column("user_id", sa.Integer()),
    sa.column("key", sa.String()),
    sa.column("value", sa.JSON()),
)


def _is_seeded_filter(stored: Any) -> bool:
    if isinstance(stored, str):  # a driver that hands JSON back undecoded
        stored = json.loads(stored)
    if not isinstance(stored, dict) or stored.get("where") != _WHERE:
        return False
    # Absent in a filter stored before include_archived existed; false either way.
    return set(stored) <= {"where", "include_archived"} and not stored.get("include_archived")


def upgrade() -> None:
    # A NOT NULL column on a table that may already hold rows needs a database
    # default to fill them; the model declares the same one (see 0012).
    op.add_column(
        "lists", sa.Column("builtin", sa.Boolean(), nullable=False, server_default=sa.false())
    )

    connection = op.get_bind()
    seeded_users = {
        user_id
        for user_id, value in connection.execute(
            sa.select(_settings.c.user_id, _settings.c.value).where(_settings.c.key == _SEEDED_KEY)
        )
        if value is True
    }
    candidates = connection.execute(
        sa.select(_lists.c.id, _lists.c.user_id, _lists.c.filter_json).where(
            _lists.c.name == _NAME, _lists.c.kind == "smart"
        )
    ).all()
    for list_id, user_id, stored in candidates:
        if user_id in seeded_users and _is_seeded_filter(stored):
            connection.execute(sa.update(_lists).where(_lists.c.id == list_id).values(builtin=True))


def downgrade() -> None:
    op.drop_column("lists", "builtin")
