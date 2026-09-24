"""Run an enrichment: plan, job, budget, heat, session flag, cancel, resume (spec 9.4-9.9).

The seam between :mod:`netkeeper.linkedin.enrich` (pure, no database),
:mod:`netkeeper.services.enrich_plan` (who to visit, and the stored plan), and
:mod:`netkeeper.crm.apply` (rows, no browser). Modelled on
:mod:`netkeeper.services.connections_sync`; this module owns neither half's
logic, only what the job may do and what happens after it stops:

* **Before anything.** No run starts while the session flag is set, or while
  heat is at or above the skip threshold (spec 9.7): nothing is fetched and
  nothing is spent (:class:`SessionFlagged`, :class:`HeatSkipped`).
* **Today's visits.** Profile visits are the budget that matters (spec 9.6),
  and this runner is the caller that enforces the parts of it that are not a
  counter: the warm-up ramp (spec 9.5: 20 a day on a fresh install, 10 more
  each day, up to the configured cap), weekend damping (half on Saturday and
  Sunday), and heat's shrink (spec 9.7), applied in that order, the order
  ``netkeeper posture`` reports and ``netkeeper simulate`` replays
  (:func:`todays_visits`). What is left of that after today's spend, and of
  the week's ceiling, is the run's visit budget. A plan is cut at it, so a pin
  takes a place within the budget, never one on top of it.
* **Between visits.** Before every profile the gate checks the plan's cancel
  flag, the active window (spec 9.5: a run that outlasts the window stops at
  its edge, between profiles), and spends one ``profile_visits`` unit through
  :func:`netkeeper.services.budgets.consume`, each in its own short writer
  session, never partway through a profile. The wait between two profiles is
  sliced, and the cancel flag is read between slices (spec 9.9).
* **Each harvest** is mapped in its own writer session as it arrives, in the
  same transaction as the stored plan's record that the contact is done, so a
  resumed plan skips exactly what was written.
* **The stopping response.** ``Throttled`` or ``Checkpoint`` raises heat;
  ``Checkpoint`` or ``LoggedOut`` sets the session flag (spec 9.7). The plan is
  marked ``completed`` when every target was visited and ``aborted`` otherwise,
  and a run that ends by exception is marked ``aborted`` on the way out.
* **Resume** (:func:`resume_enrichment`, spec 9.9) reopens a stored plan and
  runs what it has left, in its order, against today's budget. It never
  re-plans.

The caller holds the account's browser activity lock (spec 9.9) for the length
of the run and passes the source that reads through that browser run. Nothing
here attaches to a browser, and nothing yet calls this: the scheduler and the
runs API (P2-09, P2-10) wire it.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import LinkedInSettings
from netkeeper.crm import apply as mapping
from netkeeper.db import session_scope
from netkeeper.linkedin import heat as heat_math
from netkeeper.linkedin import pacing
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.enrich import (
    EnrichJobSpec,
    EnrichResult,
    EnrichTarget,
    PacingProfile,
    ProfileHarvest,
    ProfileSource,
    ProgressEvent,
    ProgressSink,
    StopReason,
    run_enrichment,
)
from netkeeper.models import User
from netkeeper.services import budgets, enrich_plan
from netkeeper.services import heat as heat_service
from netkeeper.services.budgets import HARD_MAX_PER_DAY, ActionClass, BudgetExceeded
from netkeeper.services.connections_sync import HeatSkipped, SessionFlagged
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import flag_session, session_flag
from netkeeper.services.pacing import profiles

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]

#: The longest single sleep between two profiles before the cancel flag is read
#: again (spec 9.9: "checked between profiles and inside sliced cooldowns"). A
#: cancel waits at most this long, even inside a twenty-minute burst break.
CANCEL_SLICE_S: Final = 5.0

_HEAT_OUTCOMES = frozenset({Outcome.THROTTLED, Outcome.CHECKPOINT})
_FLAG_OUTCOMES = frozenset({Outcome.CHECKPOINT, Outcome.LOGGED_OUT})

__all__ = [
    "CANCEL_SLICE_S",
    "EnrichRunReport",
    "HeatSkipped",
    "SessionFlagged",
    "TodaysVisits",
    "enrich_contacts",
    "resume_enrichment",
    "todays_visits",
]


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


@dataclass(frozen=True, slots=True)
class EnrichRunReport:
    """What one run did: the plan it ran, the job's result, the mapping's counts.

    ``skipped`` counts planned contacts that could no longer be visited when the
    run started (merged away, archived, disconnected: a resume never re-plans,
    so they are simply left out).
    """

    account_id: int
    plan_id: str
    visits: TodaysVisits
    result: EnrichResult
    harvests: mapping.HarvestCounts
    skipped: int = 0
    heat_raised: bool = False
    session_flagged: bool = False


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _load_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"no user {user_id}")
    return user


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


def _active_window(settings: LinkedInSettings) -> tuple[time, time]:
    """``[linkedin] active_hours`` as times. A window that does not parse fails the run
    before anything is spent, rather than letting it run unguarded."""
    start, end = settings.active_hours
    return time.fromisoformat(start), time.fromisoformat(end)


@dataclass(slots=True)
class _Gate:
    """Cancel, active hours, then one ``profile_visits`` unit, before each visit."""

    factory: sessionmaker[Session]
    user_id: int
    account_id: int
    plan_id: str
    settings: LinkedInSettings
    window: tuple[time, time]
    clock: Clock
    sleep: Sleep

    def _cancelled(self) -> bool:
        with session_scope(self.factory) as session:
            user = _load_user(session, self.user_id)
            return enrich_plan.cancel_requested(session, user, self.account_id, self.plan_id)

    async def before_visit(self, number: int) -> StopReason | None:
        if self._cancelled():
            log.info("enrichment: cancelled before visit %d", number)
            return StopReason.CANCELLED
        now = self.clock()
        start, end = self.window
        if not pacing.is_active_at(now, self.settings.timezone, start=start, end=end):
            log.info("enrichment: the active window closed before visit %d", number)
            return StopReason.INACTIVE
        with session_scope(self.factory, write=True) as session:
            user = _load_user(session, self.user_id)
            try:
                budgets.consume(
                    session,
                    user,
                    self.account_id,
                    ActionClass.PROFILE_VISITS,
                    now=now,
                    settings=self.settings.budget,
                )
            except BudgetExceeded as exc:
                log.info("enrichment: visit %d refused by the budget: %s", number, exc)
                return StopReason.BUDGET
        return None

    async def pause(self, seconds: float) -> bool:
        remaining = seconds
        while remaining > 0:
            step = min(CANCEL_SLICE_S, remaining)
            await self.sleep(step)
            remaining -= step
            if self._cancelled():
                log.info("enrichment: cancelled during the wait between profiles")
                return False
        return True


async def enrich_contacts(
    factory: sessionmaker[Session],
    user_id: int,
    source: ProfileSource,
    *,
    settings: LinkedInSettings,
    on_progress: ProgressSink | None = None,
    clock: Clock = _utcnow,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
) -> EnrichRunReport:
    """Plan today's enrichment for ``user_id`` (spec 9.6), store the plan, and run it.

    ``SessionFlagged`` while the session flag is set, and ``HeatSkipped`` when
    heat is at or above its skip threshold; either way nothing is planned,
    fetched, or spent. Every other stop is an :class:`EnrichRunReport`, whose
    ``plan_id`` a resume names.
    """
    return await _run(
        factory,
        user_id,
        source,
        plan_id=None,
        settings=settings,
        on_progress=on_progress,
        clock=clock,
        sleep=sleep,
        rng=rng,
    )


async def resume_enrichment(
    factory: sessionmaker[Session],
    user_id: int,
    plan_id: str,
    source: ProfileSource,
    *,
    settings: LinkedInSettings,
    on_progress: ProgressSink | None = None,
    clock: Clock = _utcnow,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
) -> EnrichRunReport:
    """Run what a stored plan has left, in its order, within today's budget (spec 9.9).

    Skips every contact the plan completed and never re-plans; a cancel
    request on the plan is cleared, since resuming is the person saying go on.
    :class:`~netkeeper.services.enrich_plan.PlanNotFound` for an unknown plan,
    :class:`~netkeeper.services.enrich_plan.PlanFinished` for a completed one,
    and the same refusals as :func:`enrich_contacts` before either is touched.
    """
    return await _run(
        factory,
        user_id,
        source,
        plan_id=plan_id,
        settings=settings,
        on_progress=on_progress,
        clock=clock,
        sleep=sleep,
        rng=rng,
    )


async def _run(
    factory: sessionmaker[Session],
    user_id: int,
    source: ProfileSource,
    *,
    plan_id: str | None,
    settings: LinkedInSettings,
    on_progress: ProgressSink | None,
    clock: Clock,
    sleep: Sleep,
    rng: random.Random | None,
) -> EnrichRunReport:
    window = _active_window(settings)
    with session_scope(factory, write=True) as session:
        user = _load_user(session, user_id)
        account_id = ensure_account(session, user).id
        now = clock()
        flagged_by = session_flag(session, user)
        if flagged_by is not None:
            raise SessionFlagged(
                f"the LinkedIn session is flagged ({flagged_by.outcome.value}); not enriching"
                " until it is cleared"
            )
        if heat_service.should_skip(session, user, account_id, now=now, settings=settings.heat):
            raise HeatSkipped(f"heat is at or above {settings.heat.skip_threshold}; not enriching")
        multiplier = heat_service.cooldown_multiplier(
            session, user, account_id, now=now, settings=settings.heat
        )
        visits = todays_visits(
            session, user, account_id, now=now, settings=settings, multiplier=multiplier
        )
        if plan_id is None:
            chosen = enrich_plan.prioritize(
                session,
                user,
                account_id,
                now=now,
                limit=visits.remaining,
                stale_days=settings.enrich_stale_days,
            )
            plan = enrich_plan.create_plan(
                session, user, account_id, [contact_id for contact_id, _ in chosen], now=now
            )
        else:
            plan = enrich_plan.reopen(session, user, account_id, plan_id)
            chosen = enrich_plan.targets_for(session, user, plan)
        skipped = len(plan.remaining) - len(chosen)

    configured = profiles(settings.pacing)
    spec = EnrichJobSpec(
        targets=tuple(EnrichTarget(contact_id, slug) for contact_id, slug in chosen),
        visit_budget=visits.remaining,
        pacing=PacingProfile(delay=configured.delay, burst=configured.burst),
        heat_multiplier=multiplier,
    )
    gate = _Gate(
        factory=factory,
        user_id=user_id,
        account_id=account_id,
        plan_id=plan.plan_id,
        settings=settings,
        window=window,
        clock=clock,
        sleep=sleep,
    )
    counts = mapping.HarvestCounts()

    async def on_harvest(harvest: ProfileHarvest) -> None:
        with session_scope(factory, write=True) as session:
            user = _load_user(session, user_id)
            mapping.apply_harvest(session, user, harvest, counts)
            enrich_plan.mark_completed(session, user, account_id, plan.plan_id, harvest.contact_ref)

    async def no_progress(event: ProgressEvent) -> None:
        return None

    finished = False
    try:
        result = await run_enrichment(
            spec,
            source,
            gate,
            on_harvest=on_harvest,
            rng=rng if rng is not None else random.Random(),  # noqa: S311 -- pacing, not crypto
            on_progress=on_progress if on_progress is not None else no_progress,
            clock=clock,
        )
        finished = True
    finally:
        if not finished:
            with session_scope(factory, write=True) as session:
                user = _load_user(session, user_id)
                enrich_plan.finish(
                    session, user, account_id, plan.plan_id, status="aborted", stopped="error"
                )

    heat_raised = flagged = False
    with session_scope(factory, write=True) as session:
        user = _load_user(session, user_id)
        if result.reason is StopReason.RESPONSE and result.outcome is not None:
            if result.outcome in _HEAT_OUTCOMES:
                heat_service.raise_heat(
                    session, user, account_id, now=clock(), settings=settings.heat
                )
                heat_raised = True
            if result.outcome in _FLAG_OUTCOMES:
                flag_session(session, user, result.outcome, url=result.final_url or "")
                flagged = True
        enrich_plan.finish(
            session,
            user,
            account_id,
            plan.plan_id,
            status="completed" if result.reason is StopReason.END_OF_PLAN else "aborted",
            stopped=result.reason.value,
        )
    return EnrichRunReport(
        account_id=account_id,
        plan_id=plan.plan_id,
        visits=visits,
        result=result,
        harvests=counts,
        skipped=skipped,
        heat_raised=heat_raised,
        session_flagged=flagged,
    )
