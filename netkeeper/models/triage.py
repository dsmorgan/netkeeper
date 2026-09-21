"""The triage decision log, which is what makes undo exact (spec 10.2).

A ``triage_decisions`` row is one thing triage did to one contact: a met
decision (``m``, ``n``, ``s``), a preferred-name edit (``p``), or one contact's
share of a bulk suggestion. It records the fields the decision changed, both as
they were (``before_state``) and as it left them (``after_state``), so undo puts
back exactly what was there rather than guessing that "before" meant untriaged.

``after_state`` is not bookkeeping: undo compares it with the contact as it is
now and refuses when they differ, so an edit that arrived from somewhere else
between the decision and the undo is never silently overwritten.

Rows are never deleted. ``undone_at`` marks one as spent, so the next undo
reaches past it to the one before, and a contact's whole triage history stays
readable. ``batch_id`` ties the rows of one bulk apply together: the suggestion
was one click, so it is one undo.

The values in the two JSON maps are the column's value as text (see
``netkeeper.crm.triage``): ``met`` as its enum value, ``triaged_at`` as an ISO
datetime in UTC or ``null``, ``preferred_name`` as itself.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from netkeeper.models.base import Base, TimestampMixin, UserOwned, UTCDateTime, string_enum
from netkeeper.models.contacts import Contact

# Long enough for the longest member value below; the CHECK carries the rest.
_KIND_LENGTH = 20
BATCH_ID_LENGTH = 32


class TriageDecisionKind(enum.StrEnum):
    """What a decision was. The kind steers nothing in undo; it is for the history."""

    DECIDE = "decide"
    """A met decision from the queue: ``met`` and ``triaged_at``."""

    PREFERRED_NAME = "preferred_name"
    """A preferred-name edit made during triage."""

    BULK_MET = "bulk_met"
    """One contact's share of an applied bulk suggestion; ``batch_id`` names the batch."""


class TriageDecision(UserOwned, TimestampMixin, Base):
    """One undoable triage action on one contact (spec 10.2)."""

    __tablename__ = "triage_decisions"
    __table_args__ = (
        # The undo stack: the newest row of this user that is not spent yet.
        Index("ix_triage_decisions_user_id_undone_at_id", "user_id", "undone_at", "id"),
        # Undoing a bulk apply gathers its rows by batch.
        Index("ix_triage_decisions_user_id_batch_id", "user_id", "batch_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[TriageDecisionKind] = mapped_column(
        string_enum(TriageDecisionKind, "triage_decision_kind", length=_KIND_LENGTH), nullable=False
    )
    # Field name to value-as-text, for the fields this decision touched.
    before_state: Mapped[dict[str, str | None]] = mapped_column(
        JSON(), nullable=False, default=dict
    )
    after_state: Mapped[dict[str, str | None]] = mapped_column(JSON(), nullable=False, default=dict)
    # Set on every row of one bulk apply, so undo takes the batch as a unit.
    batch_id: Mapped[str | None] = mapped_column(String(BATCH_ID_LENGTH))
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    # When undo spent this row. NULL is "still on the stack".
    undone_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    contact: Mapped[Contact] = relationship()
