"""Static lists, smart lists, and saved table views (spec 10.1 and 10.4; item P1-08).

A ``lists`` row is either a static list, whose members are the ``list_members``
rows naming it, or a smart list, whose members are whatever
:func:`netkeeper.crm.filters.compile_filter` returns for the ``filter_json``
tree it carries, evaluated fresh on every read. A smart list's membership is
never materialized: ``list_members`` rows only ever belong to a static list,
and the CHECK constraint below keeps ``filter_json`` in step with ``kind`` so
the two can never say different things about what kind of list a row is.

A ``saved_views`` row is a named column set, sort, and optional filter for the
contacts table to restore (spec 10.1). It has no membership of its own and
never affects what a list contains.

Every table is user-owned (ADR 0005); :mod:`netkeeper.crm.lists` holds the
rules that decide what each kind may do and validates ``filter_json`` (a
:class:`netkeeper.crm.filters.FilterTree`) before it is ever stored.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from netkeeper.models.base import Base, TimestampMixin, UserOwned, UTCDateTime, string_enum, utcnow

LIST_NAME_MAX_LENGTH = 200
VIEW_NAME_MAX_LENGTH = 200


class ListKind(enum.StrEnum):
    """How a list's members are decided (spec 10.4)."""

    STATIC = "static"
    SMART = "smart"


class ContactList(UserOwned, TimestampMixin, Base):
    """A static or smart list of contacts (spec 10.4)."""

    __tablename__ = "lists"
    __table_args__ = (
        UniqueConstraint("user_id", "name"),
        # A static list stores no filter; a smart list always carries one. Kept as a
        # CHECK, not only in netkeeper.crm.lists, so the two can never disagree.
        CheckConstraint(
            "(kind = 'static' AND filter_json IS NULL)"
            " OR (kind = 'smart' AND filter_json IS NOT NULL)",
            name="list_kind_filter",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    name: Mapped[str] = mapped_column(String(LIST_NAME_MAX_LENGTH), nullable=False)
    kind: Mapped[ListKind] = mapped_column(string_enum(ListKind, "list_kind"), nullable=False)
    # A FilterTree as netkeeper.crm.filters dumps it (``model_dump(mode="json")``); NULL for a
    # static list. ``none_as_null``: JSON's default binds Python None as the JSON literal
    # ``null`` (a non-NULL column value), not SQL NULL, which would defeat the CHECK above.
    # Validated (parsed and compiled) by netkeeper.crm.lists before it is stored, so a broken
    # or not-yet-supported filter is refused at write time, not at read time.
    filter_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )

    members: Mapped[list[ListMember]] = relationship(
        back_populates="list",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: (ListMember.added_at, ListMember.id),
    )


class ListMember(UserOwned, Base):
    """One contact's explicit membership in a static list (spec 10.4).

    Only ever rows of a static list: a smart list's members are computed from
    its filter, never stored here. No ``updated_at``: a membership row is added
    or removed, never changed in place.
    """

    __tablename__ = "list_members"
    __table_args__ = (UniqueConstraint("user_id", "list_id", "contact_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    list_id: Mapped[int] = mapped_column(
        ForeignKey("lists.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    list: Mapped[ContactList] = relationship(back_populates="members")


class SavedView(UserOwned, TimestampMixin, Base):
    """A named column set, sort, and optional filter for the contacts table (spec 10.1)."""

    __tablename__ = "saved_views"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    name: Mapped[str] = mapped_column(String(VIEW_NAME_MAX_LENGTH), nullable=False)
    # Column identifiers as the frontend names them. netkeeper.crm.lists validates the shape
    # (non-empty, within length and count limits) but not against a fixed vocabulary: the
    # contacts table (P1-12) is not built yet and owns that list.
    columns: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # netkeeper.crm.filters.SortKey rows, dumped with model_dump(mode="json"); [] for unsorted.
    sort: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    # None for "no filter" (every contact). ``none_as_null``: see ContactList.filter_json.
    filter_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )


LIST_TABLES: tuple[type[UserOwned], ...] = (ContactList, ListMember, SavedView)
"""Every table this module adds, in creation order, for tests and tooling that iterate them."""
