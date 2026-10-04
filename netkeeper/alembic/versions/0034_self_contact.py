"""The self contact: the user's own details, held as a contact (#342, #320).

Adds ``contacts.is_self``, false for every existing contact, and a partial unique
index that allows one self contact per user.

No data step. The self contact is created at runtime, on the app's first start
on this revision (``crm/self_contact.py``), seeded from a ``[me]`` section an
older config still holds. A migration never reads the config or the data
directory.

The downgrade deletes each self contact, then drops the index and the column.
Kept, a self contact would become an ordinary contact the older code could list,
enroll or send to; its children go with it (``ON DELETE CASCADE``). Upgrading
again creates a fresh one on the next start.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-04 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "uq_contacts_user_id_self"

_contacts = sa.table("contacts", sa.column("is_self", sa.Boolean()))


def upgrade() -> None:
    op.add_column(
        "contacts", sa.Column("is_self", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_index(
        INDEX,
        "contacts",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("is_self"),
        postgresql_where=sa.text("is_self"),
    )


def downgrade() -> None:
    op.get_bind().execute(sa.delete(_contacts).where(_contacts.c.is_self.is_(sa.true())))
    op.drop_index(INDEX, table_name="contacts")
    op.drop_column("contacts", "is_self")
