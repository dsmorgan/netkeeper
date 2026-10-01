"""The do-not-send list: email addresses no campaign sends to (#238, Part B).

An address status (``bounced``, ``invalid``) lives on one contact's
``contact_emails`` row, and an opt-out on one contact. Either is lost when the
contact holding it changes: a merge drops the loser's copy of an address, and a
person can remove an address or a contact. The list keeps what was learned about
the address itself, keyed by the address, so no contact that holds it later is
sent to. Only a person removes an entry.
"""

from __future__ import annotations

import enum
from typing import Final

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, validates

from netkeeper.models.base import Base, TimestampMixin, UserOwned, string_enum
from netkeeper.models.contacts import normalize_email


class DoNotSendReason(enum.StrEnum):
    """Why an address is on the list."""

    MANUAL = "manual"
    """A person put it there."""
    INVALID = "invalid"
    """A person marked the address invalid on a contact."""
    BOUNCED = "bounced"
    """A message to it bounced, or a person marked it bounced on a contact."""
    OPTED_OUT = "opted_out"
    """Its owner asked to unsubscribe."""


DO_NOT_SEND_RANK: Final[dict[DoNotSendReason, int]] = {
    DoNotSendReason.MANUAL: 0,
    DoNotSendReason.INVALID: 1,
    DoNotSendReason.BOUNCED: 2,
    DoNotSendReason.OPTED_OUT: 3,
}
"""Which reason an entry keeps when a second one arrives: the higher. An opt-out ranks
highest because it blocks every channel, not only email."""


class DoNotSendAddress(UserOwned, TimestampMixin, Base):
    """One address of the user's that no campaign sends to.

    ``email`` is normalized as ``contact_emails.email`` is (trimmed and lowercased,
    nothing else: ``name+tag@`` and ``first.last@`` are addresses of their own, #238).
    ``contact_id`` is the contact the address was found on, for display; it is
    cleared, not cascaded, when that contact is deleted, because the entry is about
    the address.

    ``reason`` is the strongest reason the address is listed for
    (:data:`DO_NOT_SEND_RANK`). ``bounced`` records that a message to it bounced, or
    a person marked it bounced, whatever ``reason`` says: an opt-out outranks a
    bounce, and a person removing the opt-out is warned that the bounce goes with it.
    """

    __tablename__ = "do_not_send_addresses"
    __table_args__ = (UniqueConstraint("user_id", "email"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[DoNotSendReason] = mapped_column(
        string_enum(DoNotSendReason, "do_not_send_reason"), nullable=False
    )
    bounced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )

    @validates("email")
    def _normalize_email(self, key: str, value: str) -> str:
        return normalize_email(value)
