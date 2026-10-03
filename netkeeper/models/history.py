"""History from the old mailing tool: its campaigns and who they reached (#65).

``history_campaigns`` holds one row per campaign the old tool ran, read from its
workbook by ``netkeeper history import``. ``history_recipients`` holds one row per
person the workbook names for that campaign, the contact the address matched, and
what ``netkeeper history scan-gmail`` later found in the mailbox: a reply, an
unsubscribe request, an automatic answer, or a bounce.

Nothing here sends anything, and nothing reads these rows to decide a send
directly. The import and the scan act through what the campaign guards already
read: an ``email_out`` interaction (the recency guard), the do-not-send list, a
contact's ``do_not_contact`` and ``needs_review_at``. These rows are the record of
why, and what makes a re-run idempotent.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from netkeeper.models.base import Base, TimestampMixin, UserOwned, UTCDateTime, string_enum
from netkeeper.models.contacts import normalize_email

HISTORY_CAMPAIGN_NAME_MAX_LENGTH = 500
SHA256_HEX_LENGTH = 64
GMAIL_ID_MAX_LENGTH = 64


class HistoryReplyKind(enum.StrEnum):
    """What the Gmail scan found from or about a recipient, strongest first.

    A recipient row keeps the strongest kind found (:data:`REPLY_KIND_RANK`).
    """

    UNSUBSCRIBE = "unsubscribe"
    """A reply that asks to unsubscribe (``campaign_replies.UNSUBSCRIBE_PHRASES``)."""
    REPLY = "reply"
    """Any other message a person sent from the address in the campaign's window."""
    BOUNCE = "bounce"
    """A mail system's notice that delivery to the address failed."""
    AUTO = "auto"
    """An automatic answer, such as an out-of-office message."""


REPLY_KIND_RANK: dict[HistoryReplyKind, int] = {
    HistoryReplyKind.AUTO: 0,
    HistoryReplyKind.BOUNCE: 1,
    HistoryReplyKind.REPLY: 2,
    HistoryReplyKind.UNSUBSCRIBE: 3,
}
"""Which kind a recipient row keeps when the scan finds more than one: the higher."""


class HistoryCampaign(UserOwned, TimestampMixin, Base):
    """One campaign the old tool ran, as its workbook reports it.

    Keyed by name and start date per user, so importing a later export of the same
    workbook updates the row instead of adding another. ``source_sha256`` is the
    digest of the file the row was last imported from. The three counts are the
    workbook's own aggregate cells; ``recipients_count`` can be larger than the
    number of people the workbook lists. ``subject_scanned_at`` is when the Gmail scan
    last searched for replies by the campaign's subject (a scan without ``--rescan``
    searches each campaign once).
    """

    __tablename__ = "history_campaigns"
    __table_args__ = (UniqueConstraint("user_id", "name", "started_on"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    name: Mapped[str] = mapped_column(String(HISTORY_CAMPAIGN_NAME_MAX_LENGTH), nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    started_on: Mapped[date] = mapped_column(Date, nullable=False)
    last_batch_on: Mapped[date | None] = mapped_column(Date)
    recipients_count: Mapped[int | None] = mapped_column(Integer)
    opens_count: Mapped[int | None] = mapped_column(Integer)
    bounces_count: Mapped[int | None] = mapped_column(Integer)
    source_sha256: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH), nullable=False)
    subject_scanned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    recipients: Mapped[list[HistoryRecipient]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )


class HistoryRecipient(UserOwned, TimestampMixin, Base):
    """One person the old tool's workbook names for one campaign.

    ``email`` is normalized as ``contact_emails.email`` is (trimmed, lowercased), so
    the unique key on it is the key on ``lower(email)``. ``contact_id`` is the
    contact the address matched when it was imported, or ``None``; it may name a
    contact merged away since, so readers resolve the survivor.

    ``opened``, ``clicked`` and ``bounce_listed`` say which of the workbook's lists
    named the person. ``found_by_subject`` marks a row the workbook did not list: the
    Gmail scan found a reply with the campaign's subject from this address, so the
    campaign reached them. ``email_out_interaction_id`` is the timeline entry the import
    wrote, so a re-import writes no second one.

    The rest is the Gmail scan's: ``scanned_at`` (a scan without ``--rescan`` skips
    a scanned row), ``reply_kind`` (the strongest found), ``replied_at`` (the first
    message a person sent from the address in the window), ``reply_gmail_id`` (the
    Gmail message recorded for it), ``email_in_interaction_id`` (its timeline
    entry) and ``bounced_at`` (the first hard-bounce notice).
    """

    __tablename__ = "history_recipients"
    __table_args__ = (UniqueConstraint("user_id", "history_campaign_id", "email"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    history_campaign_id: Mapped[int] = mapped_column(
        ForeignKey("history_campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    opened: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    clicked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bounce_listed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    found_by_subject: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_out_interaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL"), index=True
    )
    scanned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reply_kind: Mapped[HistoryReplyKind | None] = mapped_column(
        string_enum(HistoryReplyKind, "history_reply_kind")
    )
    replied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reply_gmail_id: Mapped[str | None] = mapped_column(String(GMAIL_ID_MAX_LENGTH))
    email_in_interaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL"), index=True
    )
    bounced_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    campaign: Mapped[HistoryCampaign] = relationship(back_populates="recipients")

    @validates("email")
    def _normalize_email(self, key: str, value: str) -> str:
        return normalize_email(value)


HISTORY_TABLES: tuple[type[UserOwned], ...] = (HistoryCampaign, HistoryRecipient)
"""Both history tables, for tests that walk them."""
