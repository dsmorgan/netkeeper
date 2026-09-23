"""``netkeeper simulate``: the scenario `netkeeper/services/simulate.py` runs, and its report.

P2-11's third and last command (#156, #107): "what would two weeks of this
actually do to my account?" -- answered without doing any of it. Two things
`simulate.simulate()` does not do on its own, because they are not its job
(it drives the *scheduler*, against a caller-supplied :class:`JobRegistry`
of handlers that default to no-ops):

1. **A real job handler for ``JobKind.ENRICH``**, the job kind spec 9.4 spends
   a visit budget on. This module's handler is that job: before every unit it
   spends, it re-derives today's warm-up-ramped, weekend-damped,
   heat-shrunk profile-visit allowance
   (:func:`netkeeper.linkedin.pacing.warmup_budget`,
   :func:`netkeeper.linkedin.pacing.apply_weekend_multiplier`,
   :func:`netkeeper.linkedin.heat.shrink` -- the same three calls
   ``netkeeper.services.posture``'s report derives its numbers from, not a
   second copy of that arithmetic), then spends the unit through
   :func:`netkeeper.services.budgets.consume`, the real enforcement call,
   against a *scratch* database this module owns end to end. **Re-derives
   before every unit, not once per fire**: a throttle inside a fire raises
   heat immediately, and the very next unit in that same fire has to see the
   shrunk cap, or spec 9.7's "while warm... the per-run budget shrinks" is
   true of the report and false of the run it claims to describe.

2. **Injected throttles.** ``--throttles K`` (the CLI's, threaded through here
   as ``throttles``) marks ``K`` distinct positions in the *global, run-wide
   sequence of profile-visit units this scenario actually spends* -- not a
   calendar day, which can have no spend to throttle at all (a day the weekly
   ceiling has already exhausted, say, while an earlier day in the same run
   had untouched budget). :func:`run_simulation` runs the scenario *twice*:
   once with no throttles, purely to discover how many units it really
   spends (the weekly ceiling and weekend damping both cut that well below
   any per-day formula), and once for real, with :func:`_pick_spend_ordinals`
   drawing ``K`` ordinals from a seeded :class:`random.Random` over that
   genuine total. The handler keeps a running count of units actually
   consumed and checks it against that set on every one. When a targeted
   unit comes up, it calls :func:`netkeeper.services.heat.raise_heat` -- the
   real call, not a number this module invents -- instead of merely
   succeeding. Two throttled units in a row abort that fire's spend loop
   early (spec 9.7's classify table: "two consecutive throttled units abort
   the run"; :data:`CONSECUTIVE_THROTTLE_ABORT` pins the "two"). An ordinal
   can still go unreached in the second pass if an earlier throttle's heat
   suppresses spend the throttle-free baseline did not see --
   :attr:`SimulationReport.throttles_landed` says how many actually landed,
   and :func:`render` prints a warning line when it falls short of what was
   requested, rather than only ever claiming the header count.

That combination -- a handler that is a real caller of both ``consume`` and
``raise_heat`` -- is new: ``netkeeper.services.posture.UNENFORCED_TODAY``
lists both (and the warm-up ramp and weekend damping) as having no enforcing
caller anywhere in the package. This module is that caller now, which is
exactly why it is excluded, by name and with a comment saying why, from
``tests/test_posture.py``'s AST scan: a simulation spending a scratch
account's budget is not production enforcement, and letting the scan count it
would shrink ``UNENFORCED_TODAY`` over a gap that has not actually closed.

**Never the user's real database.** :func:`run_simulation` creates a SQLite
file under a throwaway temporary directory, migrates it to the production
schema (:func:`netkeeper.migrations.upgrade` -- the same schema `netkeeper db
upgrade` builds, not a `Base.metadata.create_all` approximation of it), runs
the whole scenario against it, and deletes the directory again before
returning (:func:`_scratch_database`). It is seeded from the *config*
``settings`` passed in -- budgets, pacing, heat, active hours, timezone --
never from a real account's ``settings_kv`` counters, because there is no
real account row this module ever opens. The scratch user's ``created_at``
is set to the simulation's own start instant, so day 0 reads as day 0 of the
warm-up ramp (spec 9.5) regardless of when the command is actually run.

**Why the run always starts on the same calendar date.** ``--seed`` is meant
to make a run reproducible byte for byte; if the start date were "today",
the very same seed would produce a different weekday split -- and therefore
different weekend damping -- depending on which day the command happened to
be run. :data:`REFERENCE_START_DATE` is a fixed Monday instead, so the only
inputs that ever change the output are ``--days``, ``--throttles``, and
``--seed`` themselves, and a fortnight (or longer) always crosses at least
one weekend for the damping to show.

**What every column actually means, because a report that shows a number
that never governed anything is worse than not showing it.** ``HEAT-CAP`` is
not recomputed after the fact from an end-of-day heat reading -- that number
can be *higher* than what really governed spending (heat decays back down
before the day ends) or simply never have been in force for a single unit.
It is the cap the handler itself last checked before it stopped spending
that day, tracked live in :class:`_RunState` as the day happens.
``MIN-CAP`` is the lowest that live-tracked cap ever reached that day, for a
day where heat moved the cap more than once. ``SKIP?`` is not read off an
end-of-day heat snapshot either (the score can fall back under the threshold
by the time the day closes, well after a real fire was actually skipped
mid-day) -- it is whether the scheduler's own ``SimResult`` recorded a
heat-skipped fire that day, at all. A day the handler never touched at all
(heat skipped every fire, or the day fell outside active hours) has no live
reading to fall back on, so ``HEAT-CAP``/``MIN-CAP`` there come from a
one-shot end-of-day derivation instead -- an approximation, not a recovered
truth: heat decays continuously through a day whether or not anything spent
against it, so that single end-of-day reading can itself be higher than the
cap was earlier the same day (most visibly on a day the skip gate held for
its whole span: the longer heat has had to decay by the time this reads it,
the more permissive a cap it reports for a day nothing was ever allowed to
spend against). No scenario the second review found reaches this path.

**Nothing here imports a browser.** No ``AttachBrowserProvider``, no CDP, no
network -- ``tests/test_browser_safety.py``'s allowlist is untouched by this
module on purpose. ``simulate.simulate()`` already drives a virtual clock
with no sleeping and no real APScheduler loop; this module adds a handler and
a report on top of it, not a second clock.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from random import Random
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from netkeeper import migrations
from netkeeper.config import BudgetSettings, HeatSettings, Settings
from netkeeper.db import make_engine, make_session_factory, session_scope
from netkeeper.linkedin import heat as heat_math
from netkeeper.linkedin import pacing
from netkeeper.models import User, UserKind
from netkeeper.scoping import install_scope_guard
from netkeeper.services import heat as heat_rows
from netkeeper.services import scheduler, simulate
from netkeeper.services.budgets import (
    HARD_MAX_PER_DAY,
    ActionClass,
    BudgetExceeded,
    PeriodBudget,
    consume,
)
from netkeeper.services.budgets import status as budget_status
from netkeeper.services.posture import SINGLE_ACCOUNT_ID, HeatPosture, TodaysBudget
from netkeeper.services.scheduler import HeatGate, HeatSkip

# spec 9.7's classify table, for the ``Throttled`` outcome's Action column:
# "at most 3 attempts; two consecutive throttled units abort the run." The
# handler below models one attempt per unit (no per-unit retry loop -- there
# is nothing to retry against in a simulation), so only the second half
# applies here. Pinned literally (CLAUDE.md: a module constant lifted from a
# spec table is not protected by any other test) in
# tests/test_simulate_run.py.
CONSECUTIVE_THROTTLE_ABORT: Final = 2

# A fixed Monday, chosen only so `date.weekday()` is 0 and a 14-day run
# crosses two weekends -- see the module docstring's "why the run always
# starts on the same calendar date." Not tied to spec or config; a plain
# implementation choice, so nothing pins it beyond
# ``test_the_reference_date_is_a_monday``.
REFERENCE_START_DATE: Final = date(2026, 1, 5)

DEFAULT_DAYS: Final = 14
DEFAULT_THROTTLES: Final = 0
DEFAULT_SEED: Final = 0


@dataclass(frozen=True, slots=True)
class JobFires:
    """One job kind's fires on one simulated day, from the scheduler's own ``SimResult``."""

    kind: str
    fired: int
    heat_skipped: int
    catchup: int


@dataclass(frozen=True, slots=True)
class DayReport:
    """One simulated day: the budget chain and heat, plus what the scheduler did.

    ``budget.after_heat`` is the cap that actually governed spending that
    day, not an end-of-day recomputation (see the module docstring).
    ``min_heat_cap`` is the lowest that cap fell to that day, which can be
    lower still when more than one throttle landed. ``week`` is spec 9.6's
    *other* profile-visit ceiling (``PeriodBudget``, reused from
    ``netkeeper.services.budgets``): the daily chain can say a day is
    allowed 29 more visits while the ISO week is already at its own limit
    and refuses every one of them. ``heat_skipped_today`` is read directly
    off that day's fires, not off an end-of-day heat reading that may have
    already decayed back under the threshold.
    """

    index: int
    date: date
    weekday: str
    is_weekend: bool
    budget: TodaysBudget
    min_heat_cap: int
    week: PeriodBudget | None
    heat: HeatPosture
    heat_skipped_today: bool
    throttles_landed: int
    jobs: tuple[JobFires, ...]


@dataclass(frozen=True, slots=True)
class SimulationReport:
    seed: int
    days_requested: int
    throttles_requested: int
    throttles_landed: int
    timezone: str
    start: datetime
    days: tuple[DayReport, ...]


class InvalidSimulation(ValueError):
    """``days`` or ``throttles`` was not something a scenario can be built from."""


async def run_simulation(
    *,
    days: int,
    throttles: int,
    seed: int,
    settings: Settings,
    heat_gate: HeatGate | None = None,
) -> SimulationReport:
    """Replay ``days`` simulated days against a scratch database, seeded from ``settings``.

    ``heat_gate`` is the gate the scheduler runs the replay under -- ``None``
    (the default) means "whatever ``settings.linkedin.heat`` says", the same
    convention ``netkeeper.services.posture.posture`` uses. Passing
    :data:`netkeeper.services.scheduler.HEAT_SKIP_DISABLED` here is how a test
    proves the skip gate is what suppresses fires during a hot period (it is
    not exposed as a CLI flag: production callers get spec 9.7's skip
    unconditionally, the same rule the scheduler itself enforces).

    Never opens the real database (see the module docstring); everything this
    needs comes from ``settings`` and the two integer parameters.
    """
    if days < 1:
        raise InvalidSimulation(f"days must be at least 1, got {days}")
    if throttles < 0:
        raise InvalidSimulation(f"throttles must not be negative, got {throttles}")

    if throttles == 0:
        return await _replay(
            days=days,
            throttles=0,
            target_ordinals=frozenset(),
            seed=seed,
            settings=settings,
            heat_gate=heat_gate,
        )

    # Two passes. `_pick_spend_ordinals` needs an upper bound on how many
    # profile-visit units this scenario can ever spend, and the honest one
    # is not a formula -- it is "how many a throttle-free run of the exact
    # same days, seed, and settings actually spends", because the weekly
    # ceiling (spec 9.6) and weekend damping both cut real spend well below
    # any per-day arithmetic that does not also account for a whole week's
    # worth of days sharing one ceiling. A looser analytic estimate was
    # tried first and routinely lost a throttle or two even on the exact
    # invocation #30 names (`--days 14 --throttles 2`, no seed): most of its
    # headroom was ordinals past the point the weekly ceiling had already
    # cut spending off. The baseline run is thrown away -- its own database
    # is a second, separate scratch database, deleted like any other -- and
    # costs about as much wall time as the real run.
    baseline = await _replay(
        days=days,
        throttles=0,
        target_ordinals=frozenset(),
        seed=seed,
        settings=settings,
        heat_gate=heat_gate,
    )
    total_spend = sum(day.budget.spent for day in baseline.days)
    rng = Random(seed)  # noqa: S311 -- deterministic replay, not crypto
    target_ordinals = _pick_spend_ordinals(rng, throttles, total_spend)
    return await _replay(
        days=days,
        throttles=throttles,
        target_ordinals=target_ordinals,
        seed=seed,
        settings=settings,
        heat_gate=heat_gate,
    )


async def _replay(
    *,
    days: int,
    throttles: int,
    target_ordinals: frozenset[int],
    seed: int,
    settings: Settings,
    heat_gate: HeatGate | None,
) -> SimulationReport:
    """One full scratch-database replay, with throttles injected at exactly
    ``target_ordinals``. :func:`run_simulation` is the public entry point;
    this is split out so it can be called once (the throttle-free case, or
    the baseline pass that discovers a real ``target_ordinals`` for the
    second call -- see its own comment).
    """
    linkedin = settings.linkedin
    zone = ZoneInfo(linkedin.timezone)
    active_start, active_end = _active_window(linkedin.active_hours)
    start = datetime.combine(REFERENCE_START_DATE, time(0, 0), tzinfo=zone).astimezone(UTC)
    cap = min(linkedin.budget.profile_visits_per_day, HARD_MAX_PER_DAY[ActionClass.PROFILE_VISITS])

    gate = linkedin.heat if heat_gate is None else heat_gate
    effective_heat = linkedin.heat if isinstance(gate, HeatSkip) else gate

    with _scratch_database() as factory, _quiet_scheduler_warnings():
        with session_scope(factory, write=True) as session:
            user = User(
                kind=UserKind.LOCAL,
                display_name="simulated account",
                timezone=linkedin.timezone,
                created_at=start,
            )
            session.add(user)
            session.flush()

        state = _RunState(target_ordinals=target_ordinals)
        registry = dict(scheduler.default_registry())
        registry[scheduler.JobKind.ENRICH] = _make_enrich_handler(
            factory,
            user,
            SINGLE_ACCOUNT_ID,
            budget_settings=linkedin.budget,
            heat_settings=effective_heat,
            weekend_multiplier=linkedin.weekend_multiplier,
            zone=zone,
            cap=cap,
            start_date=REFERENCE_START_DATE,
            state=state,
        )

        # One `simulate()` call *per simulated day*, chained against the same
        # scratch database -- exactly the "restart is not a special code
        # path" composition `tests/test_simulate.py` proves correct for the
        # scheduler's own persisted state. This is not merely a convenience:
        # heat is a single mutable row (`netkeeper.services.heat` overwrites
        # it on every `raise_heat`), so it has no history to read back after
        # the fact. Querying it retroactively once the whole run has
        # finished would see day 0's heat as it stood on the *last* day a
        # throttle landed, not as it stood on day 0 -- `decayed_score` never
        # projects backward past a state's own `updated_at` (its docstring:
        # "returns the stored score unchanged rather than projecting it
        # backward"). Snapshotting immediately after each day's own window
        # closes, before the next day's throttles can land, is what keeps
        # every day's heat honestly its own.
        day_reports: list[DayReport] = []
        for day_index in range(days):
            day_date = REFERENCE_START_DATE + timedelta(days=day_index)
            # Each boundary is built fresh from its own calendar date rather
            # than by adding a day's worth of timedelta to the previous
            # instant, so a DST transition inside the run cannot skew it.
            day_start = datetime.combine(day_date, time(0, 0), tzinfo=zone).astimezone(UTC)
            day_end = datetime.combine(
                day_date + timedelta(days=1), time(0, 0), tzinfo=zone
            ).astimezone(UTC)
            landed_before = len(state.throttle_log)
            day_result = await simulate.simulate(
                factory,
                user,
                SINGLE_ACCOUNT_ID,
                start=day_start,
                end=day_end,
                seed=seed,
                registry=registry,
                schedules=scheduler.DEFAULT_SCHEDULES,
                active_start=active_start,
                active_end=active_end,
                heat_settings=gate,
            )
            snapshot_now = day_end - timedelta(seconds=1)
            with session_scope(factory) as session:
                budget, min_heat_cap, week = _day_snapshot(
                    session,
                    user,
                    SINGLE_ACCOUNT_ID,
                    day_index=day_index,
                    local_date=day_date,
                    budget_settings=linkedin.budget,
                    heat_settings=effective_heat,
                    weekend_multiplier=linkedin.weekend_multiplier,
                    cap=cap,
                    now=snapshot_now,
                    state=state,
                )
                heat_posture = _heat_snapshot(
                    session, user, SINGLE_ACCOUNT_ID, now=snapshot_now, settings=effective_heat
                )
            day_reports.append(
                DayReport(
                    index=day_index,
                    date=day_date,
                    weekday=day_date.strftime("%A"),
                    is_weekend=pacing.is_weekend(day_date),
                    budget=budget,
                    min_heat_cap=min_heat_cap,
                    week=week,
                    heat=heat_posture,
                    heat_skipped_today=any(
                        not f.fired and f.skipped_reason == "heat" for f in day_result.fires
                    ),
                    throttles_landed=len(state.throttle_log) - landed_before,
                    jobs=_bucket_fires(day_result.fires),
                )
            )

    return SimulationReport(
        seed=seed,
        days_requested=days,
        throttles_requested=throttles,
        throttles_landed=len(state.throttle_log),
        timezone=linkedin.timezone,
        start=start,
        days=tuple(day_reports),
    )


# --- throttle placement: a position in the run-wide spend sequence, not a day ---


def _pick_spend_ordinals(rng: Random, count: int, total_spend: int) -> frozenset[int]:
    """``count`` distinct 0-based positions in the global sequence of profile-visit
    units this run spends, drawn from ``range(total_spend)``.

    ``total_spend`` is meant to be the *real* number of units a throttle-free
    run of this exact scenario spends (:func:`run_simulation`'s baseline
    pass discovers it by actually running one) -- every ordinal drawn is then
    guaranteed reachable, because it names a unit the scenario is already
    known to spend. Placing throttles by ordinal rather than by calendar day
    is what makes that guarantee possible at all: a day can have nothing left
    to spend (the weekly ceiling already hit, say) while an earlier day in
    the same run had plenty of untouched budget, so a day-based target can
    miss even when the run as a whole had room for it.

    Passed anything smaller than the real total -- an estimate, or a second
    run whose own throttles suppressed some spend the baseline did not see --
    an ordinal at or beyond it simply never comes up, which
    :func:`run_simulation` reports honestly as fewer throttles landed than
    were requested rather than silently drawing fewer to begin with.

    A different seed draws a different set; the same seed repeats. ``count``
    or ``total_spend`` non-positive returns ``frozenset()``.
    """
    if total_spend <= 0 or count <= 0:
        return frozenset()
    return frozenset(rng.sample(range(total_spend), k=min(count, total_spend)))


def _local_date(at: datetime, zone: ZoneInfo) -> date:
    return pacing.local_time_of(at, zone).date()


# --- the injected job handler: the first real caller of consume and raise_heat ---


@dataclass
class _RunState:
    """Mutable, closed over by the handler across every fire of the whole replay."""

    target_ordinals: frozenset[int]
    #: How many profile-visit units this run has successfully spent so far,
    #: across every day and every fire -- the counter :func:`_pick_spend_ordinals`'s
    #: ordinals are positions into.
    units_spent_total: int = 0
    throttle_log: list[datetime] = field(default_factory=list)
    #: Per simulated day: the derived cap (:func:`_derive_allowance`'s
    #: ``after_heat``) as it stood the last time the handler checked it that
    #: day, and the lowest it ever stood that day. Absent for a day the
    #: handler never ran on at all.
    last_cap: dict[int, int] = field(default_factory=dict)
    min_cap: dict[int, int] = field(default_factory=dict)


def _make_enrich_handler(
    factory: sessionmaker[Session],
    user: User,
    account_id: int,
    *,
    budget_settings: BudgetSettings,
    heat_settings: HeatSettings,
    weekend_multiplier: float,
    zone: ZoneInfo,
    cap: int,
    start_date: date,
    state: _RunState,
) -> scheduler.JobHandler:
    """A real ``JobKind.ENRICH`` handler: spends today's derived budget one unit at a
    time, re-deriving the cap before every single one, and turns targeted units
    into throttles.

    This is what makes ``consume`` and ``raise_heat`` real callers instead of
    numbers this module invents (see the module docstring). Every other job
    kind still gets ``noop_handler`` -- spec 9.6 budgets only ``profile_visits``
    against a warm-up ramp; the other three job kinds have nothing here to
    enforce.

    The cap is re-derived from a fresh ``cooldown_multiplier`` read *before
    every unit*, not once per fire: a throttle raises heat mid-loop, and the
    very next unit in that same fire has to spend against the shrunk cap for
    spec 9.7's "the per-run budget shrinks" to be true of what this handler
    actually does, not only of what a snapshot says afterward. ``today_count``
    is tracked locally rather than re-read from the database on every
    iteration -- it only has to be seeded once, from what earlier fires
    already spent today, and re-reading it every time would make the loop's
    own termination depend on ``consume`` actually persisting something,
    which is one of the properties a test has to be free to break without
    hanging the suite.
    """

    async def handler(ctx: scheduler.JobContext) -> None:
        if ctx.kind is not scheduler.JobKind.ENRICH:
            return
        local_date = _local_date(ctx.due, zone)
        day_index = (local_date - start_date).days
        with session_scope(factory, write=True) as session:
            today_count = budget_status(
                session,
                user,
                account_id,
                ActionClass.PROFILE_VISITS,
                now=ctx.due,
                settings=budget_settings,
            ).day.count
            consecutive = 0
            while True:
                multiplier = heat_rows.cooldown_multiplier(
                    session, user, account_id, now=ctx.due, settings=heat_settings
                )
                _, _, after_heat = _derive_allowance(
                    day_index,
                    local_date,
                    budget_settings=budget_settings,
                    weekend_multiplier=weekend_multiplier,
                    cap=cap,
                    multiplier=multiplier,
                )
                # Recorded before the abort check below, not after: the
                # second consecutive throttle raises heat and then this loop
                # exits without ever spending another unit, but the cap that
                # raise just produced is real -- it is the day's true lowest
                # point, even though no unit was spent against it. Recording
                # it only when the loop goes on to spend another unit would
                # leave MIN-CAP one throttle behind the day's actual low.
                state.last_cap[day_index] = after_heat
                state.min_cap[day_index] = min(state.min_cap.get(day_index, after_heat), after_heat)
                if consecutive >= CONSECUTIVE_THROTTLE_ABORT:
                    break
                if today_count >= after_heat:
                    break
                try:
                    consume(
                        session,
                        user,
                        account_id,
                        ActionClass.PROFILE_VISITS,
                        now=ctx.due,
                        settings=budget_settings,
                    )
                except BudgetExceeded:
                    break
                today_count += 1
                ordinal = state.units_spent_total
                state.units_spent_total += 1
                if ordinal in state.target_ordinals:
                    heat_rows.raise_heat(
                        session, user, account_id, now=ctx.due, settings=heat_settings
                    )
                    state.throttle_log.append(ctx.due)
                    consecutive += 1
                else:
                    consecutive = 0

    return handler


def _derive_allowance(
    day_index: int,
    local_date: date,
    *,
    budget_settings: BudgetSettings,
    weekend_multiplier: float,
    cap: int,
    multiplier: float,
) -> tuple[int, int, int]:
    """``(ramp, after_weekend, after_heat)`` -- the same chain, in the same order,
    ``netkeeper.services.posture``'s ``_todays_budget`` derives its numbers in.

    Shared by the handler (which needs it live, before every unit) and
    :func:`_day_snapshot` (the fallback for a day the handler never touched at
    all), so the two can never say something different about the same day.
    """
    ramp = pacing.warmup_budget(
        day_index, cap, start=budget_settings.warmup_start, step=budget_settings.warmup_step
    )
    after_weekend = pacing.apply_weekend_multiplier(ramp, local_date, multiplier=weekend_multiplier)
    after_heat = (
        heat_math.shrink(after_weekend, max(multiplier, heat_math.COOLDOWN_FLOOR))
        if after_weekend >= 1
        else after_weekend
    )
    return ramp, after_weekend, after_heat


# --- the per-day report: read-only, after the fact ---------------------------


def _day_snapshot(
    session: Session,
    user: User,
    account_id: int,
    *,
    day_index: int,
    local_date: date,
    budget_settings: BudgetSettings,
    heat_settings: HeatSettings,
    weekend_multiplier: float,
    cap: int,
    now: datetime,
    state: _RunState,
) -> tuple[TodaysBudget, int, PeriodBudget | None]:
    """``(budget, min_heat_cap, week)`` for ``day_index``, read back right after
    that day's window closes.

    ``budget.after_heat`` and ``min_heat_cap`` come from ``state``'s live
    tracking -- the cap as it actually governed the handler's spending that
    day -- whenever the handler ran at all. A day it never touched (every
    fire heat-skipped, or the day fell outside active hours) has no live
    reading to use, so this falls back to a one-shot derivation at ``now``
    instead -- an approximation, not a recovered truth. Heat decays
    continuously through the day regardless of whether anything spent
    against it, so this end-of-day reading can be *higher* (more permissive)
    than the cap really was earlier the same day, most visibly on a day the
    skip gate held throughout: the more of the day has passed by the time
    this reads it, the more heat has already decayed away. See the module
    docstring's "what every column actually means".
    """
    snapshot = budget_status(
        session, user, account_id, ActionClass.PROFILE_VISITS, now=now, settings=budget_settings
    )
    if day_index in state.last_cap:
        ramp = pacing.warmup_budget(
            day_index, cap, start=budget_settings.warmup_start, step=budget_settings.warmup_step
        )
        after_weekend = pacing.apply_weekend_multiplier(
            ramp, local_date, multiplier=weekend_multiplier
        )
        after_heat = state.last_cap[day_index]
        min_heat_cap = state.min_cap[day_index]
    else:
        multiplier = heat_rows.cooldown_multiplier(
            session, user, account_id, now=now, settings=heat_settings
        )
        ramp, after_weekend, after_heat = _derive_allowance(
            day_index,
            local_date,
            budget_settings=budget_settings,
            weekend_multiplier=weekend_multiplier,
            cap=cap,
            multiplier=multiplier,
        )
        min_heat_cap = after_heat
    budget = TodaysBudget(
        ramp=ramp, after_weekend=after_weekend, after_heat=after_heat, spent=snapshot.day.count
    )
    return budget, min_heat_cap, snapshot.week


def _heat_snapshot(
    session: Session, user: User, account_id: int, *, now: datetime, settings: HeatSettings
) -> HeatPosture:
    if settings.half_life_hours <= 0:
        return HeatPosture(
            score=0.0,
            threshold=settings.skip_threshold,
            multiplier=heat_math.COOLDOWN_FLOOR,
            tripped=False,
            last_raised_at=None,
            resumes_at=None,
            readable=False,
        )
    score = heat_rows.read(session, user, account_id, now=now, settings=settings)
    tripped = heat_rows.should_skip(session, user, account_id, now=now, settings=settings)
    multiplier = heat_rows.cooldown_multiplier(
        session, user, account_id, now=now, settings=settings
    )
    stored = heat_rows.state(session, user, account_id)
    raised = stored is not None and stored.score > 0
    return HeatPosture(
        score=score,
        threshold=settings.skip_threshold,
        multiplier=multiplier,
        tripped=tripped,
        last_raised_at=stored.updated_at if raised and stored is not None else None,
        resumes_at=None,
    )


def _bucket_fires(fires: Sequence[simulate.SimFire]) -> tuple[JobFires, ...]:
    """``fires`` from one day's own ``simulate()`` call, split by job kind.

    No date filtering needed: :func:`run_simulation` calls ``simulate()`` once
    per simulated day (see its own comment for why), so every fire here
    already belongs to the one day being reported.
    """
    return tuple(
        JobFires(
            kind=kind.value,
            fired=sum(1 for f in fires if f.kind is kind and f.fired),
            heat_skipped=sum(
                1 for f in fires if f.kind is kind and not f.fired and f.skipped_reason == "heat"
            ),
            catchup=sum(1 for f in fires if f.kind is kind and f.fired and f.is_catchup),
        )
        for kind in scheduler.JobKind
    )


def _active_window(active_hours: tuple[str, str]) -> tuple[time, time]:
    start, end = active_hours
    return time.fromisoformat(start), time.fromisoformat(end)


# --- quieting other modules' own logging for the duration of a replay --------


@contextmanager
def _quiet_scheduler_warnings() -> Iterator[None]:
    """``netkeeper.services.scheduler`` logs at WARNING on its own: a heat-skipped
    fire ("scheduler: skipping enrich for account 1 (heat)") and a catch-up
    jitter, both naming the scratch account's id. The skip is already in the
    day table this command prints (``heat_skipped_today``, and the fires
    table's own column) -- a copy of the same fact on stderr, worded exactly
    like a real skip on a real account, would be. Raised to ERROR for the
    whole replay and restored after, on every path, the same pattern
    :func:`_quiet_alembic` uses -- not disabled outright, so a caller who
    configured their own scheduler logging is not overridden permanently."""
    logger = logging.getLogger("netkeeper.services.scheduler")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous)


# --- the scratch database: created and deleted by this call, never the user's ---


@contextmanager
def _quiet_alembic() -> Iterator[None]:
    """Alembic narrates every revision it applies at INFO. This command's own
    output is the report, not migration chatter, so the ``alembic`` logger is
    raised to WARNING for the migration and restored after -- not disabled
    outright, so a caller who configured their own alembic logging, or wants
    to debug a migration, is not overridden permanently."""
    logger = logging.getLogger("alembic")
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous)


@contextmanager
def _scratch_database() -> Iterator[sessionmaker[Session]]:
    """A throwaway, file-backed SQLite database under its own temporary directory,
    migrated to the same schema ``netkeeper db upgrade`` builds.

    Never the path ``netkeeper.db.database_url()`` or ``netkeeper.paths.data_dir()``
    would resolve to -- this function builds its own URL from a directory
    ``tempfile.mkdtemp`` hands out and reads no environment variable to find it.
    The schema comes from :func:`netkeeper.migrations.upgrade`, not
    ``Base.metadata.create_all``: the two can drift (a server default, a check
    constraint, an index Alembic's own diff test polices against the migrated
    schema but a model-only build never sees), and a scratch database whose
    schema is not the production one is testing something else. The whole
    directory (the database file, and SQLite's WAL/SHM companions) is removed
    in the ``finally``, whether the replay raised or not.
    """
    tmp_dir = tempfile.mkdtemp(prefix="netkeeper-simulate-")
    try:
        engine = make_engine(f"sqlite:///{Path(tmp_dir) / 'scratch.sqlite3'}")
        try:
            with _quiet_alembic():
                migrations.upgrade(engine)
            factory = make_session_factory(engine)
            install_scope_guard(factory)
            yield factory
        finally:
            engine.dispose()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --- rendering: the CLI's whole output ----------------------------------------


def render(report: SimulationReport) -> str:
    """The report as ``netkeeper simulate`` prints it: a day table, a fires table,
    and a one-line summary -- the same table-and-summary shape
    ``netkeeper.services.posture.render`` uses, over this module's own numbers."""
    lines = [
        f"netkeeper simulate: {report.days_requested} simulated days from"
        f" {report.start.date().isoformat()}, seed {report.seed}, timezone {report.timezone}",
        f"{report.throttles_requested} throttle(s) requested, {report.throttles_landed} landed",
    ]
    if report.throttles_landed < report.throttles_requested:
        lines.append(
            f"WARNING: only {report.throttles_landed} of {report.throttles_requested}"
            " requested throttle(s) landed -- the run did not spend enough budget in this"
            " window to reach the rest"
        )
    lines.append(
        "ran against a scratch database this command created and deleted; nothing here"
        " touched your real data or linkedin.com"
    )
    lines.append("")
    lines.extend(_table(_DAY_HEADERS, [_day_row(day) for day in report.days]).splitlines())
    lines.append("")
    lines.append("fires by day and job kind (fired/heat-skipped/catch-up):")
    kinds = [kind.value for kind in scheduler.JobKind]
    lines.extend(_table(("DAY", *kinds), [_fire_row(day) for day in report.days]).splitlines())
    lines.append("")
    last = report.days[-1]
    lines.append(
        f"heat ended the run at {last.heat.score:.2f} of {last.heat.threshold:g}"
        f" (x{last.heat.multiplier:.2f})"
    )
    return "".join(f"{line}\n" for line in lines)


_DAY_HEADERS: Final = (
    "DAY",
    "DATE",
    "WEEKDAY",
    "WARM-UP",
    "WEEKEND",
    "HEAT-CAP",
    "MIN-CAP",
    "USED",
    "WEEK",
    "HEAT SCORE",
    "SKIP?",
    "THROTTLES",
)


def _day_row(day: DayReport) -> tuple[str, ...]:
    week = "-" if day.week is None else f"{day.week.count}/{day.week.limit}"
    return (
        str(day.index),
        day.date.isoformat(),
        day.weekday,
        str(day.budget.ramp),
        str(day.budget.after_weekend),
        str(day.budget.after_heat),
        str(day.min_heat_cap),
        str(day.budget.spent),
        week,
        f"{day.heat.score:.2f}",
        "yes" if day.heat_skipped_today else "no",
        str(day.throttles_landed),
    )


def _fire_row(day: DayReport) -> tuple[str, ...]:
    return (str(day.index), *(f"{job.fired}/{job.heat_skipped}/{job.catchup}" for job in day.jobs))


def _table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    """Left-aligned columns, two spaces between, one line per row -- the same
    layout ``netkeeper.services.posture._table`` and ``netkeeper.cli._format_table``
    already use; a third small copy rather than importing a leading-underscore name
    across a module boundary."""
    if not rows:
        return ""
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows, strict=True)]
    lines = []
    for row in (headers, *rows):
        cells = (cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)
