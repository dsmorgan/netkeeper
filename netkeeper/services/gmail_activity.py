"""What the Gmail page shows that no other endpoint answers (#449). Read-only.

The Gmail page reads the mailbox (``/mailboxes/status``), the reply poll
(``/poll-status``) and the posture report from the endpoints that already own them.
Two things had no home:

- **Today's sends**: each live mailbox's recipients so far today against its cap, as
  the campaign tick counts them (:func:`netkeeper.services.campaign_engine.mailbox_count`:
  any status, since a failed send may still have gone out) and caps them (the mailbox's
  ``daily_cap``, never over the hard maximum). The day is the user's local day.
- **Recent activity**: the newest email messages, sent or received, with the campaign
  and contact they belong to. There is no run table for Gmail (a send is a message, and
  a reply poll keeps only the mailbox's ``replies_polled_at``), so this is the history
  the app has.

Nothing here calls Gmail, reads the Keychain, or writes: every answer is a database read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from netkeeper.campaigns import schedule
from netkeeper.config import Settings
from netkeeper.models import (
    Campaign,
    CampaignStep,
    Contact,
    Enrollment,
    Mailbox,
    MailboxStatus,
    Message,
    MessageDirection,
    TemplateChannel,
    User,
)
from netkeeper.scoping import scoped
from netkeeper.services.campaign_engine import mailbox_count, slots_for
from netkeeper.services.mailboxes import MAILBOX_HARD_MAX_PER_DAY

log = logging.getLogger(__name__)

RECENT_LIMIT = 20


@dataclass(frozen=True, slots=True)
class MailboxSends:
    mailbox_id: int
    email: str
    sent_today: int
    daily_cap: int


@dataclass(frozen=True, slots=True)
class RecentMessage:
    id: int
    direction: MessageDirection
    status: str
    at: datetime
    campaign_id: int | None
    campaign_name: str | None
    step_position: int | None
    contact_id: int
    contact_name: str
    error: str | None


@dataclass(frozen=True, slots=True)
class GmailActivity:
    day_start: datetime
    day_end: datetime
    timezone: str
    mailboxes: tuple[MailboxSends, ...]
    recent: tuple[RecentMessage, ...]


def _day(settings: Settings, user: User, now: datetime) -> tuple[datetime, datetime, str]:
    """The user's local day around ``now``, as the tick counts it; UTC when the user's
    time zone cannot be read, so the page still answers."""
    try:
        start, end = slots_for(settings, user).day_bounds(now)
        return start, end, user.timezone
    except schedule.ScheduleError:
        log.warning("user %s has unknown timezone %r; counting UTC days", user.id, user.timezone)
        start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1), "UTC"


def _name(contact: Contact) -> str:
    first = contact.preferred_name or contact.first_name
    return " ".join(part for part in (first, contact.last_name) if part)


def gmail_activity(
    session: Session, user: User, *, now: datetime, settings: Settings, limit: int = RECENT_LIMIT
) -> GmailActivity:
    day_start, day_end, zone = _day(settings, user, now)
    live = session.scalars(
        scoped(user, Mailbox).where(Mailbox.status != MailboxStatus.DISABLED).order_by(Mailbox.id)
    ).all()
    sends = tuple(
        MailboxSends(
            mailbox_id=box.id,
            email=box.email,
            sent_today=mailbox_count(session, user, box.id, day_start, day_end),
            daily_cap=max(0, min(box.daily_cap, MAILBOX_HARD_MAX_PER_DAY)),
        )
        for box in live
    )

    when = func.coalesce(Message.sent_at, Message.scheduled_at, Message.created_at)
    rows = session.execute(
        scoped(user, Message)
        .with_only_columns(Message, when, Campaign, CampaignStep.position, Contact)
        .join(Enrollment, Enrollment.id == Message.enrollment_id)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .join(Contact, Contact.id == Message.contact_id)
        .outerjoin(CampaignStep, CampaignStep.id == Message.step_id)
        .where(
            Enrollment.user_id == user.id,
            Campaign.user_id == user.id,
            Contact.user_id == user.id,
            Message.channel == TemplateChannel.EMAIL,
        )
        .order_by(when.desc(), Message.id.desc())
        .limit(limit)
    ).all()
    recent = tuple(
        RecentMessage(
            id=message.id,
            direction=message.direction,
            status=message.status.value,
            at=at,
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            step_position=position,
            contact_id=contact.id,
            contact_name=_name(contact),
            error=message.error,
        )
        for message, at, campaign, position, contact in rows
    )
    return GmailActivity(
        day_start=day_start, day_end=day_end, timezone=zone, mailboxes=sends, recent=recent
    )
