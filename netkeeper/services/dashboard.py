"""What the dashboard reads that no other endpoint already answers (P3-12, spec 14.3).

The dashboard asks "what happens next, and is anything unhealthy?" Most of the
answer already has an endpoint: the LinkedIn schedule, budget, heat, session
flag and runs (``/linkedin/*``, P2-10), and the mailbox (``/mailboxes``,
#271). The page reads those as they are, so nothing here restates their math.
This module holds the three reads that had no home:

- **Changed jobs** (spec 9.8, "the single best reason to reconnect"): live
  contacts with a ``contact_snapshot`` marked ``position_changed`` and written
  by the sync's own visits (source ``sync``) in the last
  :data:`CHANGED_JOBS_WINDOW`, dated by the snapshot's ``observed_at``: when
  netkeeper noticed the change, not the start date on the profile, which
  people often fill in late (#286). A new headline or location alone is not a
  job change, and neither is a title or company an import wrote: an import
  restates a file, it does not notice anything. The ``last_position_change``
  merge field (spec 11.1) still reads the positions' dates; a template says
  "congrats on the move" by the profile's calendar. A contact who is archived,
  merged away, or do-not-contact is not a prompt to reach out. A contact's
  first enrichment is never a change: only a later enrichment that finds a
  different position than an earlier one recorded is (#323). The CRM filter
  ``changed_jobs_within_days`` shares the selection
  (:mod:`netkeeper.crm.job_changes`).
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
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netkeeper.crm.job_changes import job_change
from netkeeper.models import (
    Contact,
    ContactSnapshot,
    Interaction,
    InteractionKind,
    User,
)
from netkeeper.scoping import scoped, scoped_contacts

log = logging.getLogger(__name__)

#: Spec 9.8: 'The dashboard surfaces "changed jobs in the last 30 days"'.
CHANGED_JOBS_DAYS: Final = 30
CHANGED_JOBS_WINDOW: Final = timedelta(days=CHANGED_JOBS_DAYS)

#: "This week" on the dashboard is the last seven days, not the calendar week,
#: so Monday morning does not read as an empty week.
INBOUND_WINDOW: Final = timedelta(days=7)

INBOUND_KINDS: Final = (InteractionKind.EMAIL_IN, InteractionKind.LI_IN)


@dataclass(frozen=True, slots=True)
class ChangedJob:
    contact: Contact
    noticed_at: datetime


def changed_jobs(
    session: Session, user: User, *, now: datetime, limit: int
) -> tuple[list[ChangedJob], int]:
    """Live contacts netkeeper saw change position in the last :data:`CHANGED_JOBS_WINDOW`.

    A change is what :func:`~netkeeper.crm.job_changes.job_change` selects (a
    ``contact_snapshot`` with ``position_changed`` set and source ``sync``),
    observed between ``now - CHANGED_JOBS_WINDOW`` and ``now``, both inclusive:
    the same rows the filter ``changed_jobs_within_days`` matches. Each contact
    is listed once, at its latest such snapshot; newest first, then by contact
    id. The total is the count before ``limit``.
    """
    latest = (
        select(
            ContactSnapshot.contact_id.label("contact_id"),
            func.max(ContactSnapshot.observed_at).label("noticed_at"),
        )
        .where(job_change(user, since=now - CHANGED_JOBS_WINDOW, until=now))
        .group_by(ContactSnapshot.contact_id)
        .subquery()
    )
    statement = (
        scoped_contacts(user)
        .join(latest, latest.c.contact_id == Contact.id)
        .where(
            Contact.archived_at.is_(None),
            Contact.merged_into_id.is_(None),
            Contact.do_not_contact.is_(False),
        )
    )
    total = session.scalar(statement.with_only_columns(func.count(Contact.id)).order_by(None))
    rows = session.execute(
        statement.add_columns(latest.c.noticed_at)
        .order_by(latest.c.noticed_at.desc(), Contact.id)
        .limit(limit)
    ).tuples()
    return [ChangedJob(contact=contact, noticed_at=at) for contact, at in rows], total or 0


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
