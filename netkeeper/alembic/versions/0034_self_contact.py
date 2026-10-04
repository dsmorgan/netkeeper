"""The self contact: the user's own details, held as a contact (#342, #320).

Adds ``contacts.is_self``, false for every existing contact, and a partial unique
index that allows one self contact per user.

No data step. The self contact is created at runtime, on the app's first start
on this revision (``crm/self_contact.py``), seeded from a ``[me]`` section an
older config still holds. A migration never reads the config or the data
directory.

The downgrade deletes each self contact, then drops the index and the column.
Kept, a self contact would become an ordinary contact the older code could list,
enroll or send to. Migrations run with SQLite's foreign keys off, so ``ON DELETE``
never fires here: the downgrade reflects every foreign key that points at
``contacts.id`` and acts as the database would, scoped to the self contacts' ids.
A ``CASCADE`` key's rows are deleted (and, the same way, whatever points at
them), and a ``SET NULL`` key's column is cleared, before the contacts go. A key
with no action (``messages.contact_id``) refuses, as the database would; nothing
ever sends to the self contact, so no message names it. No row
is left naming an id SQLite could hand out again. Upgrading again creates a fresh
self contact on the next start.

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

_contacts = sa.table("contacts", sa.column("id", sa.Integer()), sa.column("is_self", sa.Boolean()))


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
    bind = op.get_bind()
    ids = list(bind.scalars(sa.select(_contacts.c.id).where(_contacts.c.is_self.is_(sa.true()))))
    if ids:
        metadata = sa.MetaData()
        metadata.reflect(bind)
        _delete_rows(bind, metadata, metadata.tables["contacts"], ids)
    op.drop_index(INDEX, table_name="contacts")
    op.drop_column("contacts", "is_self")


def _delete_rows(
    bind: sa.Connection, metadata: sa.MetaData, table: sa.Table, ids: list[int]
) -> None:
    """Delete ``table``'s rows ``ids`` as the database would with foreign keys on: every
    ``CASCADE`` row pointing at them first (recursively), every ``SET NULL`` column
    cleared, and a refusal for a key with no action that still has a row."""
    for other in metadata.sorted_tables:
        for fk in other.foreign_keys:
            if fk.column.table is not table or fk.column.name != "id":
                continue
            column = fk.parent
            rule = (fk.ondelete or "").upper()
            pointing: sa.ColumnElement[bool] = column.in_(ids)
            if other is table:
                # A row of the same table, such as a contact merged into one being deleted.
                pointing = sa.and_(pointing, other.c.id.not_in(ids))
            if rule == "CASCADE":
                if "id" in other.c:
                    child_ids = list(bind.scalars(sa.select(other.c.id).where(pointing)))
                    if child_ids:
                        _delete_rows(bind, metadata, other, child_ids)
                else:
                    bind.execute(sa.delete(other).where(pointing))
            elif rule == "SET NULL":
                bind.execute(sa.update(other).where(pointing).values({column.name: None}))
            elif bind.scalar(sa.select(sa.func.count()).select_from(other).where(pointing)):
                # No action: the database would refuse the delete, so this does too.
                raise RuntimeError(
                    f"cannot downgrade: {other.name}.{column.name} names a row being deleted"
                )
    bind.execute(sa.delete(table).where(table.c.id.in_(ids)))
