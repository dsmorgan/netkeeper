"""The inbox: what reply detection found, for a person to handle (spec 11.7; P3-11b).

An inbox item is a campaign message of one of three kinds:

- ``reply``: an inbound message (P3-08 stores its subject and snippet, never the body);
- ``unsubscribe``: an inbound message that asked to unsubscribe (``asks_unsubscribe``);
- ``bounce``: an outbound message detection marked ``bounced``. The notice itself is
  not stored, so the item carries what was sent: its subject, and no snippet.

An item's time is when it arrived: an inbound message's own date (``sent_at``, Gmail's
``internalDate``), a bounce's ``bounced_at``. Handling an item only sets
``handled_at``; nothing here touches Gmail, an enrollment, or the contact.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, and_, case, func, or_
from sqlalchemy.orm import Session

from netkeeper.db import is_writer
from netkeeper.models import (
    Campaign,
    Contact,
    Enrollment,
    EnrollmentStatus,
    Message,
    MessageDirection,
    MessageStatus,
    User,
)
from netkeeper.models.base import utcnow
from netkeeper.scoping import get_scoped, scoped

PAGE_MAX: Final = 200
"""The most items one page holds."""


class InboxKind(enum.StrEnum):
    REPLY = "reply"
    UNSUBSCRIBE = "unsubscribe"
    BOUNCE = "bounce"


class InboxItemNotFound(LookupError):
    """No message of the user's with that id is an inbox item."""


@dataclass(frozen=True, slots=True)
class InboxItem:
    message: Message
    kind: InboxKind
    received_at: datetime | None
    contact_name: str
    campaign_id: int
    campaign_name: str
    enrollment_status: EnrollmentStatus


@dataclass(frozen=True, slots=True)
class InboxPage:
    items: tuple[InboxItem, ...]
    total: int
    """Items matching the filters."""
    unhandled: int
    """Unhandled items of every kind, whatever the filters."""


_IS_REPLY: Final = Message.direction == MessageDirection.IN
_IS_BOUNCE: Final = and_(
    Message.direction == MessageDirection.OUT, Message.status == MessageStatus.BOUNCED
)
_ARRIVED: Final = case((_IS_REPLY, Message.sent_at), else_=Message.bounced_at)


def _kind_clause(kind: InboxKind) -> ColumnElement[bool]:
    if kind is InboxKind.BOUNCE:
        return _IS_BOUNCE
    return and_(_IS_REPLY, Message.asks_unsubscribe.is_(kind is InboxKind.UNSUBSCRIBE))


def kind_of(message: Message) -> InboxKind:
    if message.direction is MessageDirection.OUT:
        return InboxKind.BOUNCE
    return InboxKind.UNSUBSCRIBE if message.asks_unsubscribe else InboxKind.REPLY


def _contact_name(contact: Contact) -> str:
    return f"{contact.preferred_name or contact.first_name} {contact.last_name}".strip()


def list_inbox(
    session: Session,
    user: User,
    *,
    handled: bool | None = None,
    kind: InboxKind | None = None,
    enrollment_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> InboxPage:
    """The user's inbox items, newest first.

    ``handled`` keeps the handled (True) or the unhandled (False) ones, or both (None);
    ``kind`` keeps one kind; ``enrollment_id`` keeps one enrollment's."""
    limit = max(1, min(limit, PAGE_MAX))
    offset = max(0, offset)
    inbox = scoped(user, Message).where(or_(_IS_REPLY, _IS_BOUNCE))
    unhandled = (
        session.scalar(
            inbox.with_only_columns(func.count(Message.id)).where(Message.handled_at.is_(None))
        )
        or 0
    )
    stmt = inbox
    if handled is not None:
        stmt = stmt.where(
            Message.handled_at.is_not(None) if handled else Message.handled_at.is_(None)
        )
    if kind is not None:
        stmt = stmt.where(_kind_clause(kind))
    if enrollment_id is not None:
        stmt = stmt.where(Message.enrollment_id == enrollment_id)
    total = session.scalar(stmt.with_only_columns(func.count(Message.id))) or 0
    rows = session.execute(
        stmt.join(Enrollment, Enrollment.id == Message.enrollment_id)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .join(Contact, Contact.id == Message.contact_id)
        .where(Enrollment.user_id == user.id, Campaign.user_id == user.id)
        .where(Contact.user_id == user.id)
        .with_only_columns(Message, Enrollment, Campaign, Contact)
        # Explicit, so a NULL sorts last on Postgres too (its default puts it first).
        .order_by(_ARRIVED.desc().nulls_last(), Message.id.desc())
        .limit(limit)
        .offset(offset)
    ).tuples()
    return InboxPage(
        items=tuple(_item(m, e, c, contact) for m, e, c, contact in rows),
        total=total,
        unhandled=unhandled,
    )


def _item(
    message: Message, enrollment: Enrollment, campaign: Campaign, contact: Contact
) -> InboxItem:
    kind = kind_of(message)
    return InboxItem(
        message=message,
        kind=kind,
        received_at=message.bounced_at if kind is InboxKind.BOUNCE else message.sent_at,
        contact_name=_contact_name(contact),
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        enrollment_status=enrollment.status,
    )


def set_handled(session: Session, user: User, message_id: int, handled: bool) -> InboxItem:
    """Mark one item handled or not. Idempotent: marking a handled item handled again
    keeps when it was first handled. Needs a writer session."""
    if not is_writer(session):
        raise RuntimeError("set_handled needs a writer session")
    message = get_scoped(session, user, Message, message_id)
    if message is None or not (
        message.direction is MessageDirection.IN or message.status is MessageStatus.BOUNCED
    ):
        raise InboxItemNotFound(f"no inbox item {message_id}")
    if not handled:
        message.handled_at = None
    elif message.handled_at is None:
        message.handled_at = utcnow()
    session.flush()
    enrollment = get_scoped(session, user, Enrollment, message.enrollment_id)
    contact = get_scoped(session, user, Contact, message.contact_id)
    assert enrollment is not None and contact is not None  # plain foreign keys, same user
    campaign = get_scoped(session, user, Campaign, enrollment.campaign_id)
    assert campaign is not None
    return _item(message, enrollment, campaign, contact)
