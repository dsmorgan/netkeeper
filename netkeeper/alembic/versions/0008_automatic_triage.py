"""The automatic first pass: who decided ``met``, what a tag means, why a batch ran.

Four things, all of them in service of triage starting from work already done
(spec 10.2):

* ``contacts.met_source`` records whether the person decided ``met`` or an
  offered batch did. Existing rows were all decided by hand or not at all, so
  ``manual`` is the right backfill.
* ``tags.met_signal`` is the user saying what one of their own tags means for
  triage: ``met``, ``not_met``, or NULL for nothing.
* ``triage_decisions.reason`` names the suggestion a batch came from, and the
  ``kind`` CHECK takes ``bulk_not_met`` as well, so a batch can mark people not
  met without the log calling it something it was not.
* ``import_runs`` carries what the auto-tag rules did to the contacts the run
  touched, which now happens inside the commit itself (#64).

Three of the four need ``batch_alter_table``: SQLite cannot add a CHECK
constraint to a table that exists, and cannot replace one either, so Alembic
rebuilds the table there and emits plain ``ALTER TABLE`` statements everywhere
else.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-21 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enums are strings plus a CHECK, identical on SQLite and PostgreSQL.
MET_SOURCE = "met_source IN ('manual', 'automatic')"
TAG_MET_SIGNAL = "met_signal IN ('met', 'not_met')"
DECISION_KIND_BEFORE = "kind IN ('decide', 'preferred_name', 'bulk_met')"
DECISION_KIND_AFTER = "kind IN ('decide', 'preferred_name', 'bulk_met', 'bulk_not_met')"

KIND_CONSTRAINT = "triage_decision_kind"
"""The bare name; ``batch_alter_table`` applies the ``ck_<table>_<name>`` convention."""


def upgrade() -> None:
    # A NOT NULL column on a table that may already hold rows needs a database
    # default to fill them; the model declares the same default in Python.
    with op.batch_alter_table("contacts") as batch:
        batch.add_column(
            sa.Column("met_source", sa.String(length=16), nullable=False, server_default="manual")
        )
        batch.create_check_constraint("contact_met_source", MET_SOURCE)
    # The review queue: the contacts a batch decided, waiting to be checked.
    op.create_index(
        "ix_contacts_user_id_met_source", "contacts", ["user_id", "met_source"], unique=False
    )

    with op.batch_alter_table("tags") as batch:
        batch.add_column(sa.Column("met_signal", sa.String(length=16), nullable=True))
        batch.create_check_constraint("tag_met_signal", TAG_MET_SIGNAL)

    op.add_column("triage_decisions", sa.Column("reason", sa.String(length=100), nullable=True))
    with op.batch_alter_table("triage_decisions") as batch:
        batch.drop_constraint(KIND_CONSTRAINT, type_="check")
        batch.create_check_constraint("triage_decision_kind", DECISION_KIND_AFTER)

    for column in ("tagged_contacts", "tags_added", "tags_removed"):
        op.add_column(
            "import_runs",
            sa.Column(column, sa.Integer(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    for column in ("tags_removed", "tags_added", "tagged_contacts"):
        op.drop_column("import_runs", column)

    # A row this version wrote holds a kind the old CHECK refuses. There is no
    # honest way to keep it: the decision it records cannot be undone by code
    # that does not know the kind, so those rows go and the older ones stay.
    op.execute("DELETE FROM triage_decisions WHERE kind = 'bulk_not_met'")
    with op.batch_alter_table("triage_decisions") as batch:
        batch.drop_constraint(KIND_CONSTRAINT, type_="check")
        batch.create_check_constraint("triage_decision_kind", DECISION_KIND_BEFORE)
    op.drop_column("triage_decisions", "reason")

    with op.batch_alter_table("tags") as batch:
        batch.drop_constraint("tag_met_signal", type_="check")
        batch.drop_column("met_signal")

    op.drop_index("ix_contacts_user_id_met_source", table_name="contacts")
    with op.batch_alter_table("contacts") as batch:
        batch.drop_constraint("contact_met_source", type_="check")
        batch.drop_column("met_source")
