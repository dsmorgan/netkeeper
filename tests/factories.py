"""Row factories for tests: a user, a contact with children, a campaign, flushed and ready.

Names come from counters, so the rows a test makes are the same on every run
(the counters reset before each test, see ``conftest.py``). Only the models are
imported here, so any test module can use these without pulling in the app.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from sqlalchemy.orm import Session

from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Contact,
    ContactEmail,
    ContactPhone,
    ContactPosition,
    ContactSource,
    Enrollment,
    EnrollmentStatus,
    Message,
    MessageDirection,
    MessageStatus,
    StepMode,
    Template,
    TemplateChannel,
    User,
    UserKind,
    UserPosition,
)
from netkeeper.models.base import utcnow

_users: Iterator[int] = itertools.count(1)
_contacts: Iterator[int] = itertools.count(1)
_campaigns: Iterator[int] = itertools.count(1)


def reset_counters() -> None:
    """Start the name counters over, so each test sees ``First1 Last1`` first."""
    global _users, _contacts, _campaigns
    _users = itertools.count(1)
    _contacts = itertools.count(1)
    _campaigns = itertools.count(1)


def make_user(session: Session, kind: UserKind = UserKind.LOCAL, **overrides: Any) -> User:
    """A flushed user. ``overrides`` are ``User`` columns."""
    n = next(_users)
    fields: dict[str, Any] = {
        "kind": kind,
        "display_name": f"User {n}",
        "email": f"user{n}@example.test",
        "timezone": "UTC",
    }
    fields.update(overrides)
    user = User(**fields)
    session.add(user)
    session.flush()
    return user


def make_contact(
    session: Session,
    user: User,
    *,
    emails: Sequence[str] = (),
    phones: Sequence[str] = (),
    positions: Sequence[Mapping[str, Any]] = (),
    **overrides: Any,
) -> Contact:
    """A flushed contact of ``user`` with LinkedIn identity and the given children.

    ``emails`` are addresses (the first is primary), ``phones`` raw numbers (the
    first is primary; a leading ``+`` also fills ``number_e164``), ``positions``
    keyword arguments for :class:`ContactPosition` (the first is current unless
    it says otherwise). ``overrides`` are ``Contact`` columns; pass ``li_urn=None``
    or ``li_public_id=None`` for a contact with no LinkedIn identity.
    """
    n = next(_contacts)
    fields: dict[str, Any] = {
        "li_urn": f"urn:li:fsd_profile/TEST{n:06d}",
        "li_public_id": f"first{n}-last{n}",
        "first_name": f"First{n}",
        "last_name": f"Last{n}",
        "headline": f"Title{n} at Company{n}",
        "current_title": f"Title{n}",
        "current_company": f"Company{n}",
        "source": ContactSource.MANUAL,
    }
    fields.update(overrides)
    contact = Contact(user_id=user.id, **fields)
    for index, email in enumerate(emails):
        contact.emails.append(ContactEmail(user_id=user.id, email=email, is_primary=index == 0))
    for index, raw in enumerate(phones):
        e164 = raw if raw.startswith("+") else None
        contact.phones.append(
            ContactPhone(user_id=user.id, raw=raw, number_e164=e164, is_primary=index == 0)
        )
    for index, position in enumerate(positions):
        values: dict[str, Any] = {"is_current": index == 0, **position}
        contact.positions.append(ContactPosition(user_id=user.id, **values))
    session.add(contact)
    session.flush()
    return contact


def make_user_position(session: Session, user: User, **overrides: Any) -> UserPosition:
    """A flushed :class:`UserPosition` of ``user``. ``overrides`` are its columns."""
    fields: dict[str, Any] = {
        "title": "Staff Engineer",
        "company": "Fixture Works",
        "is_current": False,
        "source": ContactSource.MANUAL,
        "observed_at": utcnow(),
    }
    fields.update(overrides)
    position = UserPosition(user_id=user.id, **fields)
    session.add(position)
    session.flush()
    return position


def make_campaign(
    session: Session,
    user: User,
    *,
    channels: Sequence[TemplateChannel] = (TemplateChannel.EMAIL,),
    **overrides: Any,
) -> Campaign:
    """A flushed campaign of ``user`` with one step per channel in ``channels``.

    Each step gets its own template, a draft email or a prefilled LinkedIn
    message, seven days after the one before. ``overrides`` are ``Campaign``
    columns; the recency guard defaults to 30 days, as the config does.
    """
    n = next(_campaigns)
    fields: dict[str, Any] = {
        "name": f"Campaign {n}",
        "status": CampaignStatus.ACTIVE,
        "contacted_within_days_guard": 30,
    }
    fields.update(overrides)
    campaign = Campaign(user_id=user.id, **fields)
    for position, channel in enumerate(channels, start=1):
        template = Template(
            user_id=user.id,
            name=f"Campaign {n} step {position}",
            channel=channel,
            subject="Hello" if channel is TemplateChannel.EMAIL else None,
            body="Hi {{ first_name }}",
            lint_json=[],
        )
        campaign.steps.append(
            CampaignStep(
                user_id=user.id,
                position=position,
                channel=channel,
                template=template,
                delay_days=0 if position == 1 else 7,
                mode=StepMode.DRAFT if channel is TemplateChannel.EMAIL else StepMode.PREFILL,
            )
        )
    session.add(campaign)
    session.flush()
    return campaign


def make_enrollment(
    session: Session, campaign: Campaign, contact: Contact, **overrides: Any
) -> Enrollment:
    """A flushed enrollment of ``contact`` in ``campaign``, active unless overridden."""
    fields: dict[str, Any] = {"status": EnrollmentStatus.ACTIVE, "user_id": campaign.user_id}
    fields.update(overrides)
    enrollment = Enrollment(campaign_id=campaign.id, contact_id=contact.id, **fields)
    session.add(enrollment)
    session.flush()
    return enrollment


def make_message(
    session: Session, enrollment: Enrollment, *, position: int = 1, **overrides: Any
) -> Message:
    """A flushed outbound message from step ``position`` of ``enrollment``, sent now."""
    step = next(s for s in enrollment.campaign.steps if s.position == position)
    fields: dict[str, Any] = {
        "channel": step.channel,
        "direction": MessageDirection.OUT,
        "status": MessageStatus.SENT,
        "sent_at": utcnow(),
    }
    fields.update(overrides)
    message = Message(
        user_id=enrollment.user_id,
        enrollment_id=enrollment.id,
        step_id=step.id,
        contact_id=enrollment.contact_id,
        **fields,
    )
    session.add(message)
    session.flush()
    return message
