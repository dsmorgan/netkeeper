"""The user's own job history (spec 8.1's extension for P1-26; #84).

``user_positions`` is the source :mod:`netkeeper.crm.triage` reads for the
you-and-them overlap on the triage card: the LinkedIn "you both worked at X"
signal spec 10.2 originally asked for, which the address-book "shared
companies" count (P1-09, #84) could not compute because nothing recorded the
user's own career. Most rows arrive from the LinkedIn archive's
``Positions.csv`` (:mod:`netkeeper.linkedin.archive`, imported through
:mod:`netkeeper.crm.positions`); every row is also readable and editable by
hand, because the archive is not the only way someone has a job history.

The shape mirrors :class:`~netkeeper.models.contacts.ContactPosition`
deliberately: title, company, an optional company URN, a date span, and
whether it is current. ``source`` and ``observed_at`` are the same per-row
provenance the contact child tables carry (:class:`ContactChild`) rather than
the full per-field ranking ``contacts`` itself has, because a position is
edited whole, never field by field, and re-import only ever refreshes a row
whose observation is at least as new as the one already recorded
(:mod:`netkeeper.crm.positions`).

Not a :class:`ContactChild`: this table has no ``contact_id`` at all, only
``user_id`` (:class:`UserOwned`) -- there is exactly one of these tables, and
it belongs to the person running netkeeper, not to anyone they know.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from netkeeper.models.base import Base, TimestampMixin, UserOwned, UTCDateTime, string_enum, utcnow
from netkeeper.models.contacts import ContactSource


class UserPosition(UserOwned, TimestampMixin, Base):
    __tablename__ = "user_positions"
    __table_args__ = (Index("ix_user_positions_user_id_company", "user_id", "company"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    title: Mapped[str | None] = mapped_column(String(300))
    company: Mapped[str | None] = mapped_column(String(300))
    company_urn: Mapped[str | None] = mapped_column(String(200))
    started_on: Mapped[date | None] = mapped_column(Date)
    ended_on: Mapped[date | None] = mapped_column(Date)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[ContactSource] = mapped_column(
        string_enum(ContactSource, "contact_source"), nullable=False, default=ContactSource.MANUAL
    )
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
