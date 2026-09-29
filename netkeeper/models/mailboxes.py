"""The ``mailboxes`` table (spec 8.5, 11.5; item P3-01): a Gmail account campaigns send from.

A row is the account, never its credentials. The refresh token lives in the
Keychain under ``keychain_ref`` (:mod:`netkeeper.services.keychain`); nothing
here or in any other table holds a token (spec 15, 18).

``status`` is the mailbox's health as the email steps read it:

- ``ok``: the last check could refresh the token.
- ``reauth_required``: Google answered ``invalid_grant`` (the grant was revoked,
  or the consent screen is in Testing and the token's seven days are up),
  ``invalid_client`` or ``unauthorized_client``, or the Keychain has no token or
  client for it. Email steps pause until someone authorizes again.
- ``disabled``: a person disconnected it. Its token is gone from the Keychain.

Arming (#277). ``serve`` hands a mailbox's campaign steps to Gmail only while a
person has armed it, and every mailbox starts disarmed. ``armed_at`` is when it
was armed (NULL: disarmed; nothing is claimed on it and Gmail is never called for
it). Armed, it drafts every step, a ``send`` step included, until
``send_armed_at`` is set too: the separate step that lets ``send`` steps go out
with ``messages.send``. That step needs ``message_id_verified_at``: when a search
by Message-ID first found a draft netkeeper made on this mailbox, the live check
that Gmail keeps the Message-ID reconcile depends on. ``armed_by`` says who took
the latest arming step (``cli (<login>)`` or ``web (user <id>)``).

A mailbox is never deleted, because the campaigns that name it keep pointing at
it: ``campaigns.mailbox_id`` has no ``ON DELETE`` action. In v1 a user has one
live mailbox (spec 8.5); :mod:`netkeeper.services.mailboxes` enforces that.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Final

from sqlalchemy import BigInteger, CheckConstraint, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from netkeeper.models.base import Base, TimestampMixin, UserOwned, UTCDateTime, string_enum

MAILBOX_EMAIL_MAX_LENGTH: Final = 320
MAILBOX_LABEL_PREFIX_MAX_LENGTH: Final = 100
MAILBOX_KEYCHAIN_REF_MAX_LENGTH: Final = 200
MAILBOX_STATUS_REASON_MAX_LENGTH: Final = 100


class MailboxProvider(enum.StrEnum):
    """Who hosts the mailbox. Gmail only (ADR 0003)."""

    GMAIL = "gmail"


class MailboxStatus(enum.StrEnum):
    """A mailbox's health (spec 8.5, 11.5)."""

    OK = "ok"
    REAUTH_REQUIRED = "reauth_required"
    DISABLED = "disabled"


class MailboxArm(enum.StrEnum):
    """What an armed mailbox may do (#277). Not stored: read from the arming columns."""

    DRAFT = "draft"
    SEND = "send"


MAILBOX_ARMED_BY_MAX_LENGTH: Final = 100


class Mailbox(UserOwned, TimestampMixin, Base):
    """One Gmail account a user sends campaign mail from (spec 8.5)."""

    __tablename__ = "mailboxes"
    __table_args__ = (
        UniqueConstraint("user_id", "email"),
        CheckConstraint("daily_cap >= 0", name="mailbox_daily_cap"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    # Lower-cased, as Google reports it for the authorized account.
    email: Mapped[str] = mapped_column(String(MAILBOX_EMAIL_MAX_LENGTH), nullable=False)
    provider: Mapped[MailboxProvider] = mapped_column(
        string_enum(MailboxProvider, "mailbox_provider"),
        nullable=False,
        default=MailboxProvider.GMAIL,
    )
    # The Keychain entry's name under the user's key, never the secret itself.
    keychain_ref: Mapped[str] = mapped_column(
        String(MAILBOX_KEYCHAIN_REF_MAX_LENGTH), nullable=False
    )
    # Recipients per local day across every campaign (spec 11.4).
    daily_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[MailboxStatus] = mapped_column(
        string_enum(MailboxStatus, "mailbox_status"),
        nullable=False,
        default=MailboxStatus.OK,
    )
    # Why the status is what it is, as a short code (``invalid_grant``,
    # ``token_missing``, ``disconnected``); NULL while ``ok``. Never Google's own text.
    status_reason: Mapped[str | None] = mapped_column(String(MAILBOX_STATUS_REASON_MAX_LENGTH))
    # Campaign labels are ``<label_prefix>/<campaign name>`` (spec 11.5).
    label_prefix: Mapped[str] = mapped_column(
        String(MAILBOX_LABEL_PREFIX_MAX_LENGTH), nullable=False, default="netkeeper"
    )
    # When a token refresh last succeeded.
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # How many times it has been authorized; every authorization adds one. A
    # health check records its answer only if this has not moved while it asked
    # Google, since ``keychain_ref`` stays the same across a re-authorization.
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Arming (#277; see the module docstring). NULL ``armed_at`` is disarmed.
    armed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    send_armed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    armed_by: Mapped[str | None] = mapped_column(String(MAILBOX_ARMED_BY_MAX_LENGTH))
    message_id_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # The reply poll's place in Gmail's history (P3-08; 0025): the ``historyId`` the
    # next ``history.list`` starts from. NULL until the first poll sets a baseline.
    history_id: Mapped[int | None] = mapped_column(BigInteger)
    # When the reply poll last read everything up to then (P3-08; 0025). A follow-up that
    # starts a new conversation is held while this is too old (``campaign_sender``).
    replies_polled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    @property
    def arm(self) -> MailboxArm | None:
        """None while disarmed; ``send`` only once both arming steps are taken."""
        if self.armed_at is None:
            return None
        return MailboxArm.DRAFT if self.send_armed_at is None else MailboxArm.SEND
