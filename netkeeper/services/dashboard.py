"""What the dashboard reads that no other endpoint already answers (P3-12, spec 14.3).

The dashboard asks "what happens next, and is anything unhealthy?" Most of the
answer already has an endpoint: the LinkedIn schedule, budget, heat, session
flag and runs (``/linkedin/*``, P2-10), and the mailbox (``/mailboxes``,
#271). The page reads those as they are, so nothing here restates their math.
This module holds the three reads that had no home:

- **Changed jobs** (spec 9.8, "the single best reason to reconnect"): live
  contacts whose ``last_position_change`` falls in the last
  :data:`CHANGED_JOBS_DAYS` days. ``last_position_change`` means what the merge
  field means (:func:`netkeeper.campaigns.templates.contact_fields`): the
  latest start or end date on or before today among the contact's positions.
  Leaving a job counts (#232), and a start still in the future does not
  (#255). A contact who is archived, merged away, or do-not-contact is not a
  prompt to reach out.
- **Inbound this week**: interactions of kind ``email_in`` or ``li_in`` in the
  last seven days. This is not a reply count. Reply detection is P3-08 and is
  not built yet, so the dashboard says so, and shows this count as what it is.

The next campaign fires are :func:`netkeeper.services.campaign_engine.upcoming`,
which lives beside the tick so that "what the tick will fire" has one
definition.

Read-only, every one: a dashboard load never takes the write lock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, union_all
from sqlalchemy.orm import Session

from netkeeper.models import Contact, ContactPosition, Interaction, InteractionKind, User
from netkeeper.scoping import scoped

log = logging.getLogger(__name__)

#: Spec 9.8: 'The dashboard surfaces "changed jobs in the last 30 days"'.
CHANGED_JOBS_DAYS: Final = 30

#: "This week" on the dashboard is the last seven days, not the calendar week,
#: so Monday morning does not read as an empty week.
INBOUND_WINDOW: Final = timedelta(days=7)

INBOUND_KINDS: Final = (InteractionKind.EMAIL_IN, InteractionKind.LI_IN)


@dataclass(frozen=True, slots=True)
class ChangedJob:
    contact: Contact
    changed_on: date


def local_today(user: User, now: datetime) -> date:
    """``now`` as a date in the user's own timezone (UTC when it is unknown)."""
    return now.astimezone(_zone(user)).date()


def changed_jobs(
    session: Session, user: User, *, today: date, limit: int
) -> tuple[list[ChangedJob], int]:
    """Live contacts whose position changed in the last :data:`CHANGED_JOBS_DAYS` days.

    Newest change first, then by contact id; the total is the count before
    ``limit``. A change is a position's ``started_on`` or ``ended_on`` between
    ``today - CHANGED_JOBS_DAYS`` and ``today``, both inclusive; each contact is
    listed once, at its latest.
    """
    since = today - timedelta(days=CHANGED_JOBS_DAYS)
    days = union_all(
        *(
            select(ContactPosition.contact_id.label("contact_id"), column.label("day")).where(
                ContactPosition.user_id == user.id, column >= since, column <= today
            )
            for column in (ContactPosition.started_on, ContactPosition.ended_on)
        )
    ).subquery()
    latest = (
        select(days.c.contact_id, func.max(days.c.day).label("changed_on"))
        .group_by(days.c.contact_id)
        .subquery()
    )
    statement = (
        scoped(user, Contact)
        .join(latest, latest.c.contact_id == Contact.id)
        .where(
            Contact.archived_at.is_(None),
            Contact.merged_into_id.is_(None),
            Contact.do_not_contact.is_(False),
        )
    )
    total = session.scalar(statement.with_only_columns(func.count(Contact.id)).order_by(None))
    rows = session.execute(
        statement.add_columns(latest.c.changed_on)
        .order_by(latest.c.changed_on.desc(), Contact.id)
        .limit(limit)
    ).tuples()
    return [ChangedJob(contact=contact, changed_on=day) for contact, day in rows], total or 0


def inbound_since(session: Session, user: User, *, now: datetime) -> tuple[int, datetime]:
    """Inbound interactions (email or LinkedIn) in the last :data:`INBOUND_WINDOW`, and its start.

    An interaction dated after ``now`` (a clock skew, a hand-typed date) is left
    out: "this week" is up to now.
    """
    since = now - INBOUND_WINDOW
    count = session.scalar(
        scoped(user, Interaction)
        .with_only_columns(func.count(Interaction.id))
        .where(
            Interaction.kind.in_(INBOUND_KINDS),
            Interaction.at >= since,
            Interaction.at <= now,
        )
    )
    return count or 0, since


def _zone(user: User) -> tzinfo:
    try:
        return ZoneInfo(user.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning(
            "user %s has unknown timezone %r; the dashboard uses UTC", user.id, user.timezone
        )
        return UTC
