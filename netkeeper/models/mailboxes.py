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

A mailbox is never deleted, because the campaigns that name it keep pointing at
it: ``campaigns.mailbox_id`` has no ``ON DELETE`` action. In v1 a user has one
live mailbox (spec 8.5); :mod:`netkeeper.services.mailboxes` enforces that.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Final

from sqlalchemy import CheckConstraint, Integer, String, UniqueConstraint, text
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
