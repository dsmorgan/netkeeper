"""Today's profile-visit allowance, step by step (spec 9.5, 9.6, 9.7).

The enrichment runner's visit budget (:mod:`netkeeper.services.enrichment`),
kept apart from it so a read -- the runs API's budget panel -- can show the
same chain without importing the enrichment job. Read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from netkeeper.config import LinkedInSettings
from netkeeper.linkedin import heat as heat_math
from netkeeper.linkedin import pacing
from netkeeper.models import User
from netkeeper.services import budgets
from netkeeper.services.budgets import HARD_MAX_PER_DAY, ActionClass


@dataclass(frozen=True, slots=True)
class TodaysVisits:
    """Today's profile-visit allowance, step by step (spec 9.5 then 9.7), and what is left.

    ``ramp`` is the warm-up's figure for the day, ``after_weekend`` that damped,
    ``after_heat`` that shrunk; ``spent_today`` and ``week_left`` come from the
    counters. ``remaining`` is the run's visit budget.
    """

    ramp: int
    after_weekend: int
    after_heat: int
    spent_today: int
    week_left: int | None

    @property
    def remaining(self) -> int:
        left = max(self.after_heat - self.spent_today, 0)
        return left if self.week_left is None else min(left, self.week_left)


def todays_visits(
    session: Session,
    user: User,
    account_id: int,
    *,
    now: datetime,
    settings: LinkedInSettings,
    multiplier: float,
) -> TodaysVisits:
    """Today's ramped, weekend-damped, heat-shrunk profile visits (spec 9.5, 9.7). Read-only.

    Day 0 of the ramp is the day the user row was created (first start), in the
    account's zone, as ``netkeeper posture`` counts it. The cap is the
    configured daily budget clamped to spec 9.6's hard maximum.
    """
    zone = ZoneInfo(settings.timezone)
    local_now = pacing.local_time_of(now, zone)
    cap = min(settings.budget.profile_visits_per_day, HARD_MAX_PER_DAY[ActionClass.PROFILE_VISITS])
    ramp = pacing.warmup_budget(
        _days_since_install(user, local_now.date(), zone),
        cap,
        start=settings.budget.warmup_start,
        step=settings.budget.warmup_step,
    )
    after_weekend = pacing.apply_weekend_multiplier(
        ramp, local_now.date(), multiplier=settings.weekend_multiplier
    )
    after_heat = (
        heat_math.shrink(after_weekend, max(multiplier, heat_math.COOLDOWN_FLOOR))
        if after_weekend >= 1
        else after_weekend
    )
    status = budgets.status(
        session, user, account_id, ActionClass.PROFILE_VISITS, now=now, settings=settings.budget
    )
    return TodaysVisits(
        ramp=ramp,
        after_weekend=after_weekend,
        after_heat=after_heat,
        spent_today=status.day.count,
        week_left=None if status.week is None else status.week.remaining,
    )


def _days_since_install(user: User, today: date, zone: ZoneInfo) -> int:
    created = user.created_at
    if created.tzinfo is None or created.utcoffset() is None:
        created = created.replace(tzinfo=UTC)
    return max((today - created.astimezone(zone).date()).days, 0)
