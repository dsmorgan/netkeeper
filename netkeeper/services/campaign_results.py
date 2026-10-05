"""A campaign's results (#350): sends per day, and replies, bounces and opt-outs per step.

Read-only: nothing here writes, so any session will do, and no migration backs it.

What counts:

- **A send** is an outbound message that went out: status ``sent``, or ``bounced``
  (a bounce is found after the message was sent), with a ``sent_at``. A
  scheduled, drafted, prefilled, stale, discarded or failed message is not one.
- **Sends per day** count sends by the local date of ``sent_at``, in the user's
  time zone, from the first send's date to today (to the last send's, for a
  campaign that is over: ``completed`` or ``archived``), with a zero for each day
  in between that sent nothing. A campaign that has sent nothing has no days.
- **A reply** is the enrollment's ``replied_at``, its first reply. It counts
  against the last step sent to that enrollment at or before that time, so a
  reply that arrives while step 3 waits to send counts against step 2.
- **A bounce** is the enrollment's earliest bounced message (by ``bounced_at``),
  and counts against that message's own step: a bounce is a fact about the
  message, so step 1's bounce found after step 2 sent is still step 1's. This
  departs from #350's "by the time of the event", which suits replies only.
- **An opt-out** is the enrollment's earliest reply that asked to unsubscribe,
  and counts against the last step sent at or before it, as a reply does.
- Each enrollment counts at most once for each of the three. An event before the
  enrollment's first send answers nothing the campaign sent, so it counts nowhere.

The totals are the sums of the steps, and ``reply_rate`` is the replies over the
enrollments with at least one send (None while there are none).
"""

from __future__ import annotations

import bisect
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Final

from sqlalchemy.orm import Session

from netkeeper.campaigns import schedule
from netkeeper.models import (
    CampaignStatus,
    CampaignStep,
    Enrollment,
    Message,
    MessageDirection,
    MessageStatus,
    User,
)
from netkeeper.scoping import scoped
from netkeeper.services.campaigns import get_campaign

log = logging.getLogger(__name__)

WENT_OUT: Final = (MessageStatus.SENT, MessageStatus.BOUNCED)
"""The outbound statuses of a message that was sent."""

OVER: Final = frozenset({CampaignStatus.COMPLETED, CampaignStatus.ARCHIVED})
"""A campaign that sends nothing more: its days end at its last send, not today."""


@dataclass(frozen=True, slots=True)
class DaySends:
    day: date
    """A local calendar day in the user's time zone."""
    sent: int


@dataclass(frozen=True, slots=True)
class StepResults:
    step_id: int
    position: int
    sent: int
    replied: int
    bounced: int
    opted_out: int


@dataclass(frozen=True, slots=True)
class ResultTotals:
    sent: int
    contacted: int
    """Enrollments with at least one send: the reply rate's denominator."""
    replied: int
    reply_rate: float | None
    """``replied / contacted``, from 0 to 1; None while nobody has been sent anything."""
    bounced: int
    opted_out: int


@dataclass(frozen=True, slots=True)
class CampaignResults:
    campaign_id: int
    timezone: str
    """The time zone the days are in."""
    sends_per_day: tuple[DaySends, ...]
    steps: tuple[StepResults, ...]
    totals: ResultTotals


@dataclass(frozen=True, slots=True)
class _Send:
    at: datetime
    step_id: int


def _zone(user: User) -> tuple[tzinfo, str]:
    """The user's time zone, as the engine counts its days (``schedule.zone``), or UTC
    for one that cannot be read: a report should still answer."""
    try:
        return schedule.zone(user.timezone), user.timezone
    except schedule.ScheduleError:
        log.warning(
            "user %s has unknown timezone %r; results count UTC days", user.id, user.timezone
        )
        return UTC, "UTC"


def _attribute(sends: list[_Send], at: datetime) -> int | None:
    """The step of the last send at or before ``at``; None when nothing was sent by then."""
    index = bisect.bisect_right(sends, at, key=lambda s: s.at)
    return None if index == 0 else sends[index - 1].step_id


def campaign_results(
    session: Session, user: User, campaign_id: int, *, now: datetime
) -> CampaignResults:
    """One campaign's results. Raises ``CampaignNotFound`` for a campaign that is not
    ``user``'s."""
    campaign = get_campaign(session, user, campaign_id)
    zone, zone_name = _zone(user)
    steps = list(
        session.scalars(
            scoped(user, CampaignStep)
            .where(CampaignStep.campaign_id == campaign.id)
            .order_by(CampaignStep.position)
        )
    )
    enrollment_ids = (
        scoped(user, Enrollment)
        .with_only_columns(Enrollment.id)
        .where(Enrollment.campaign_id == campaign.id)
    )

    sends: defaultdict[int, list[_Send]] = defaultdict(list)
    first_bounce: dict[int, tuple[datetime, int]] = {}
    for enrollment_id, step_id, status, sent_at, bounced_at in session.execute(
        scoped(user, Message)
        .with_only_columns(
            Message.enrollment_id,
            Message.step_id,
            Message.status,
            Message.sent_at,
            Message.bounced_at,
        )
        .where(
            Message.enrollment_id.in_(enrollment_ids.scalar_subquery()),
            Message.direction == MessageDirection.OUT,
            Message.status.in_(WENT_OUT),
            Message.sent_at.is_not(None),
            Message.step_id.is_not(None),
        )
        .order_by(Message.sent_at, Message.id)
    ):
        assert sent_at is not None and step_id is not None
        sends[enrollment_id].append(_Send(sent_at, step_id))
        if status is MessageStatus.BOUNCED:
            at = bounced_at or sent_at
            if enrollment_id not in first_bounce or at < first_bounce[enrollment_id][0]:
                first_bounce[enrollment_id] = (at, step_id)

    replied: Counter[int] = Counter()
    for enrollment_id, replied_at in session.execute(
        scoped(user, Enrollment)
        .with_only_columns(Enrollment.id, Enrollment.replied_at)
        .where(Enrollment.campaign_id == campaign.id, Enrollment.replied_at.is_not(None))
    ):
        assert replied_at is not None
        step_id = _attribute(sends.get(enrollment_id, []), replied_at)
        if step_id is not None:
            replied[step_id] += 1

    bounced: Counter[int] = Counter()
    for _at, own_step in first_bounce.values():
        bounced[own_step] += 1

    first_opt_out: dict[int, datetime] = {}
    for enrollment_id, received_at in session.execute(
        scoped(user, Message)
        .with_only_columns(Message.enrollment_id, Message.sent_at)
        .where(
            Message.enrollment_id.in_(enrollment_ids.scalar_subquery()),
            Message.direction == MessageDirection.IN,
            Message.asks_unsubscribe.is_(True),
            Message.sent_at.is_not(None),
        )
        .order_by(Message.sent_at, Message.id)
    ):
        assert received_at is not None
        first_opt_out.setdefault(enrollment_id, received_at)
    opted_out: Counter[int] = Counter()
    for enrollment_id, opt_out_at in first_opt_out.items():
        step_id = _attribute(sends.get(enrollment_id, []), opt_out_at)
        if step_id is not None:
            opted_out[step_id] += 1

    sent_by_step: Counter[int] = Counter()
    by_day: Counter[date] = Counter()
    for rows in sends.values():
        for send in rows:
            sent_by_step[send.step_id] += 1
            by_day[send.at.astimezone(zone).date()] += 1
    days: list[DaySends] = []
    if by_day:
        last = max(by_day)
        if campaign.status not in OVER:
            last = max(last, now.astimezone(zone).date())
        day = min(by_day)
        while day <= last:
            days.append(DaySends(day, by_day[day]))
            day += timedelta(days=1)

    step_rows = tuple(
        StepResults(
            step_id=s.id,
            position=s.position,
            sent=sent_by_step[s.id],
            replied=replied[s.id],
            bounced=bounced[s.id],
            opted_out=opted_out[s.id],
        )
        for s in steps
    )
    contacted = len(sends)
    total_replied = sum(r.replied for r in step_rows)
    totals = ResultTotals(
        sent=sum(r.sent for r in step_rows),
        contacted=contacted,
        replied=total_replied,
        reply_rate=None if contacted == 0 else total_replied / contacted,
        bounced=sum(r.bounced for r in step_rows),
        opted_out=sum(r.opted_out for r in step_rows),
    )
    return CampaignResults(campaign.id, zone_name, tuple(days), step_rows, totals)
