"""``linkedin_accounts``: the LinkedIn account a user's extractor runs as (spec 8.4, ADR 0005).

Budgets, heat, and the scheduler's next-fire state are ``settings_kv`` rows
keyed by this row's id (``linkedin.budget.<id>.…``, ``linkedin.heat.<id>``,
``scheduler.job.<id>.…``). Until P2-06 the id was a caller-supplied integer and
every caller passed 1 (``netkeeper.services.posture.SINGLE_ACCOUNT_ID``);
migration 0011 creates one row per existing user and renames any of that user's
keys onto the new row's id, so no counter written before the table existed is
orphaned.

v1 has exactly one account per user, labelled ``default``
(:func:`netkeeper.services.linkedin_accounts.ensure_account`).

Only the columns something reads are here. Spec 8.4 also lists ``cdp_url``,
``timezone``, ``active_hours_json``, ``session_status``, and
``session_flag_at``; today those come from ``config.toml``
(``[linkedin]``), ``users.timezone``, and ``settings_kv``
(``linkedin.session_flag``), and a column nobody reads or writes would be a
second source of truth that silently disagrees with the first. Each arrives
with the change that moves its value here.

``scheduled_runs_armed_at`` (P2-10, migration 0013) is when a person armed
this account's scheduled runs, or ``NULL`` while they are disarmed. Disarmed is
the default for every account, new or migrated: ``netkeeper serve`` runs its
scheduler either way, but no scheduled LinkedIn job fires until a person arms
it, by hand, through ``netkeeper linkedin schedule arm`` or the API. It is a
column rather than a ``settings_kv`` key on purpose: ``settings_kv`` values are
seeded from ``config.toml``, and a value copied around in a config file must
never be the thing that turns on scheduled LinkedIn traffic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from netkeeper.models.base import Base, TimestampMixin, UserOwned, UTCDateTime

DEFAULT_ACCOUNT_LABEL: Final = "default"

#: The longest LinkedIn conversation or message URN stored (migration 0035).
LI_URN_MAX_LENGTH: Final = 300


class LinkedInAccount(UserOwned, TimestampMixin, Base):
    __tablename__ = "linkedin_accounts"
    __table_args__ = (UniqueConstraint("user_id", "label"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    label: Mapped[str] = mapped_column(String(100), nullable=False, default=DEFAULT_ACCOUNT_LABEL)
    scheduled_runs_armed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class LiConversation(UserOwned, TimestampMixin, Base):
    """One LinkedIn one-to-one conversation with a known contact, as the inbox poll saw it.

    Written by :mod:`netkeeper.crm.inbox_apply` (P4-08, migration 0035), one row per
    conversation URN per user. ``contact_id`` is the contact whose ``li_urn`` is the
    conversation's other participant; a merge moves the row to the survivor. The
    poll never creates a contact, so a conversation with nobody known has no row.
    ``last_inbound_at`` and ``last_outbound_at`` are the newest message each way the
    poll has seen, ``polled_at`` when a poll last read the conversation. No message
    text is stored here: the timeline's interactions carry the snippet.
    """

    __tablename__ = "li_conversations"
    __table_args__ = (
        UniqueConstraint("user_id", "conversation_urn"),
        Index("ix_li_conversations_user_id_contact_id", "user_id", "contact_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    conversation_urn: Mapped[str] = mapped_column(String(LI_URN_MAX_LENGTH), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_inbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_outbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    polled_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
