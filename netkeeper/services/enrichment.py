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
  the week's ceiling, is the run's visit budget, lowered to the run's own
  ``max_visits`` when a person gave one (never raised by it). A plan is cut at
  it, so a pin takes a place within the budget, never one on top of it.
* **Between visits.** Before every profile the gate checks the run's cancel
  flag (``sync_runs.cancel_requested_at``), the active window (spec 9.5: a run
  that outlasts the window stops at its edge, between profiles), and spends
  one ``profile_visits`` unit through
  :func:`netkeeper.services.budgets.consume`, each in its own short writer
  session, never partway through a profile. The wait between two profiles is
  sliced, and the cancel flag is read between slices (spec 9.9).
* **Each harvest** is mapped in its own writer session as it arrives, in the
  same transaction as the stored plan's record that the contact is done, so a
  resumed plan skips exactly what was written.
* **Why a visit was unreadable** (#405). Every visit that counts toward the
  unreadable limits -- unreadable, or under another id -- is recorded on the run
  as its visit number, the contact's id, and a fixed reason code
  (:class:`~netkeeper.linkedin.enrich.UnreadableCause`), nothing from the page:
  in ``progress_json.unreadable_visits`` as the run goes, so a run that ends by
  exception keeps them, and in ``counts_json.unreadable_visits`` when it stops.
* **The stopping response.** ``Throttled`` or ``Checkpoint`` raises heat;
  ``Checkpoint`` or ``LoggedOut`` sets the session flag (spec 9.7). The run is
  recorded ``completed`` when every target was visited and ``aborted``
  otherwise, with its counts; a refusal or an exception is recorded ``failed``
  on the way out (:func:`netkeeper.services.runs.recording`).
* **Resume** (:func:`resume_enrichment`, spec 9.9) records a new run whose plan
  is what the old run left, in its order, and runs it against today's budget.
  It never re-plans.

The caller holds the account's browser activity lock (spec 9.9) for the length
of the run and passes the source that reads through that browser run. Nothing
here attaches to a browser: ``netkeeper.worker`` does, for the scheduler, the
runs API, and ``netkeeper linkedin enrich``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import LinkedInSettings
from netkeeper.crm import apply as mapping
from netkeeper.db import off_loop, session_scope
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
    UnreadableVisit,
    run_enrichment,
)
from netkeeper.models import JsonValue, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.services import budgets, enrich_plan, run_contacts, runs
from netkeeper.services import heat as heat_service
from netkeeper.services.budgets import ActionClass, BudgetExceeded
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.pacing import profiles
from netkeeper.services.runs import (
    HeatSkipped,
    SessionFlagged,
    refuse_if_flagged_or_hot,
    stop_reason_of,
)
from netkeeper.services.runs import recording as runs_recording
from netkeeper.services.visit_budget import TodaysVisits as TodaysVisits
from netkeeper.services.visit_budget import todays_visits as todays_visits

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
class EnrichRunReport:
    """What one run did: its run row, the job's result, the mapping's counts.

    ``skipped`` counts planned contacts that could no longer be visited when the
    run started (merged away, archived, disconnected: a resume never re-plans,
    so they are simply left out). ``visit_budget`` is what the run was allowed:
    today's remaining visits, lowered to the run's ``max_visits`` when it has one.
    """

    account_id: int
    run_id: int
    visits: TodaysVisits
    result: EnrichResult
    harvests: mapping.HarvestCounts
    visit_budget: int = 0
    skipped: int = 0
    heat_raised: bool = False
    session_flagged: bool = False

    def counts(self) -> dict[str, JsonValue]:
        """The run's ``counts_json``: numbers and reason words only."""
        return {
            "planned": self.result.planned,
            "visits": self.result.visits,
            "completed": len(self.result.completed),
            "not_found": self.result.not_found,
            "unreadable": self.result.unreadable,
            "mismatched": self.result.mismatched,
            "lost": len(self.result.lost),
            "copied": len(self.result.copied),
            "skipped": self.skipped,
            "visit_budget": self.visit_budget,
            "harvests": dataclasses.asdict(self.harvests),
            "outcome": None if self.result.outcome is None else self.result.outcome.value,
            "heat_raised": self.heat_raised,
            "session_flagged": self.session_flagged,
            "unreadable_visits": visit_records(self.result.unreadable_visits),
        }


def visit_records(visits: tuple[UnreadableVisit, ...]) -> list[JsonValue]:
    """``unreadable_visits`` as the run stores it: visit number, contact id, reason code."""
    return [
        {"visit": visit.visit, "contact_id": visit.contact_ref, "reason": visit.cause.value}
        for visit in visits
    ]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _load_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"no user {user_id}")
    return user


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
    run_id: int
    settings: LinkedInSettings
    window: tuple[time, time]
    clock: Clock
    sleep: Sleep
    #: Set when the window closed: the sentence the log and the run's note use (#213).
    inactive_message: str | None = None

    def _cancelled_now(self) -> bool:
        with session_scope(self.factory) as session:
            user = _load_user(session, self.user_id)
            return runs.cancel_requested(session, user, self.run_id)

    async def _cancelled(self) -> bool:
        return await off_loop(self._cancelled_now)

    def _spend_visit(self, number: int, now: datetime) -> StopReason | None:
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

    async def before_visit(self, number: int) -> StopReason | None:
        if await self._cancelled():
            log.info("enrichment: cancelled before visit %d", number)
            return StopReason.CANCELLED
        now = self.clock()
        start, end = self.window
        if not pacing.is_active_at(now, self.settings.timezone, start=start, end=end):
            self.inactive_message = pacing.outside_window_message(
                now, self.settings.timezone, start=start, end=end
            )
            log.info("enrichment: stopped before visit %d: %s", number, self.inactive_message)
            return StopReason.INACTIVE
        return await off_loop(self._spend_visit, number, now)

    async def pause(self, seconds: float) -> bool:
        remaining = seconds
        while remaining > 0:
            step = min(CANCEL_SLICE_S, remaining)
            await self.sleep(step)
            remaining -= step
            if await self._cancelled():
                log.info("enrichment: cancelled during the wait between profiles")
                return False
        return True


@dataclass(frozen=True, slots=True)
class _Started:
    """What the opening transaction read off the run."""

    run_id: int
    account_id: int
    max_visits: int | None
    planned_already: bool


@dataclass(frozen=True, slots=True)
class _Planned:
    """What the planning transaction settled: the day's visits, the budget, the targets."""

    visits: TodaysVisits
    multiplier: float
    visit_budget: int
    chosen: list[tuple[int, str]]
    urns: dict[int, str]
    skipped: int


def _start(
    factory: sessionmaker[Session], user_id: int, run_id: int | None, clock: Clock
) -> _Started:
    with session_scope(factory, write=True) as session:
        user = _load_user(session, user_id)
        now = clock()
        if run_id is None:
            run_id = runs.create_run(
                session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=now
            ).id
        run = runs.get_run(session, user, run_id)
        if run.kind is not SyncRunKind.ENRICH:
            raise ValueError(f"run {run_id} is a {run.kind.value} run, not an enrichment run")
        return _Started(
            run_id=run_id,
            account_id=run.linkedin_account_id,
            max_visits=run.max_visits,
            planned_already=run.plan_json is not None,
        )


def _plan(
    factory: sessionmaker[Session],
    user_id: int,
    started: _Started,
    *,
    settings: LinkedInSettings,
    clock: Clock,
) -> _Planned:
    run_id, account_id, max_visits = started.run_id, started.account_id, started.max_visits
    with session_scope(factory, write=True) as session:
        user = _load_user(session, user_id)
        now = clock()
        refuse_if_flagged_or_hot(session, user, account_id, now=now, settings=settings)
        multiplier = heat_service.cooldown_multiplier(
            session, user, account_id, now=now, settings=settings.heat
        )
        visits = todays_visits(
            session, user, account_id, now=now, settings=settings, multiplier=multiplier
        )
        # A run's own cap only ever lowers the day's budget, never raises it.
        visit_budget = visits.remaining if max_visits is None else min(visits.remaining, max_visits)
        if not started.planned_already:
            chosen = enrich_plan.prioritize(
                session,
                user,
                account_id,
                now=now,
                limit=visit_budget,
                stale_days=settings.enrich_stale_days,
            )
            plan = enrich_plan.store_plan(
                session, user, run_id, [contact_id for contact_id, _ in chosen]
            )
        else:
            plan = enrich_plan.load_plan(session, user, run_id)
            chosen = enrich_plan.targets_for(session, user, plan)
        urns = enrich_plan.urns_for(session, user, [contact_id for contact_id, _ in chosen])
        chosen = [(contact_id, slug) for contact_id, slug in chosen if contact_id in urns]
        return _Planned(
            visits=visits,
            multiplier=multiplier,
            visit_budget=visit_budget,
            chosen=chosen,
            urns=urns,
            skipped=len(plan.remaining) - len(chosen),
        )


async def enrich_contacts(
    factory: sessionmaker[Session],
    user_id: int,
    source: ProfileSource,
    *,
    settings: LinkedInSettings,
    run_id: int | None = None,
    on_progress: ProgressSink | None = None,
    clock: Clock = _utcnow,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
) -> EnrichRunReport:
    """Run enrichment run ``run_id`` for ``user_id`` and record how it ended on the run.

    ``run_id`` is a ``running`` enrichment run (``services.runs.create_run``,
    or ``enrich_plan.start_resume`` for a resume); ``None`` records a new
    manual one first. A run with no plan yet is planned now (spec 9.6) and the
    plan stored on it; a resume's plan is already there and is run as it
    stands, never re-planned (spec 9.9).

    ``SessionFlagged`` while the session flag is set, and ``HeatSkipped`` when
    heat is at or above its skip threshold; either way nothing is planned,
    fetched, or spent, and the run is recorded ``failed`` with that reason.
    Every other stop is an :class:`EnrichRunReport`, recorded ``completed``
    (the whole plan visited) or ``aborted``. A run that ends by exception is
    recorded ``failed`` (``aborted``, "interrupted", when it was cancelled from
    outside) and the exception propagates.
    """
    window = _active_window(settings)
    # Every session below runs whole off the event loop (#259): open, work, commit
    # in one off_loop call, so no transaction spans an await. The boundaries are
    # the ones this function always had, one transaction per block.
    started = await off_loop(_start, factory, user_id, run_id, clock)
    run_id = started.run_id
    account_id = started.account_id

    async with runs_recording(factory, user_id, run_id, clock=clock):
        planned = await off_loop(_plan, factory, user_id, started, settings=settings, clock=clock)
        visits, multiplier, visit_budget = planned.visits, planned.multiplier, planned.visit_budget
        chosen, urns, skipped = planned.chosen, planned.urns, planned.skipped

        configured = profiles(settings.pacing)
        spec = EnrichJobSpec(
            targets=tuple(
                EnrichTarget(contact_id, slug, urns[contact_id]) for contact_id, slug in chosen
            ),
            visit_budget=visit_budget,
            pacing=PacingProfile(delay=configured.delay, burst=configured.burst),
            heat_multiplier=multiplier,
        )
        gate = _Gate(
            factory=factory,
            user_id=user_id,
            account_id=account_id,
            run_id=run_id,
            settings=settings,
            window=window,
            clock=clock,
            sleep=sleep,
        )
        counts = mapping.HarvestCounts()
        # #405: each visit counted toward the unreadable limits, as it is handed over.
        # A harvest is one visit, in order, so its number is how many came before it.
        handed_over = 0
        unreadable_seen: list[UnreadableVisit] = []

        def apply_harvest(harvest: ProfileHarvest) -> None:
            # One transaction: the harvest and the plan's record that its contact is
            # done commit together, so a resume skips exactly what was written.
            with session_scope(factory, write=True) as session:
                user = _load_user(session, user_id)
                outcome = mapping.apply_harvest(session, user, harvest, counts)
                enrich_plan.mark_completed(session, user, run_id, harvest.contact_ref)
                # The dashboard's last few contacts (#324), in the same transaction.
                run_contacts.record(
                    session, user, account_id, run_id, [(harvest.contact_ref, outcome.value)]
                )

        async def on_harvest(harvest: ProfileHarvest) -> None:
            nonlocal handed_over
            handed_over += 1
            if harvest.unreadable_cause is not None:
                unreadable_seen.append(
                    UnreadableVisit(handed_over, harvest.contact_ref, harvest.unreadable_cause)
                )
            await off_loop(apply_harvest, harvest)

        def record_progress(event: ProgressEvent) -> None:
            with session_scope(factory, write=True) as session:
                runs.record_progress(
                    session,
                    _load_user(session, user_id),
                    run_id,
                    {
                        "planned": event.planned,
                        "visited": event.visited,
                        "harvested": event.harvested,
                        "not_found": event.not_found,
                        "unreadable": event.unreadable,
                        "mismatched": event.mismatched,
                        "stopped": None if event.stopped is None else event.stopped.value,
                        "unreadable_visits": visit_records(tuple(unreadable_seen)),
                    },
                )

        async def progress(event: ProgressEvent) -> None:
            await off_loop(record_progress, event)
            if on_progress is not None:
                await on_progress(event)

        result = await run_enrichment(
            spec,
            source,
            gate,
            on_harvest=on_harvest,
            rng=rng if rng is not None else random.Random(),  # noqa: S311 -- pacing, not crypto
            on_progress=progress,
            clock=clock,
        )

        def finish(result: EnrichResult) -> EnrichRunReport:
            # One transaction, as before: heat, the session flag, and the run's ending.
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
                report = EnrichRunReport(
                    account_id=account_id,
                    run_id=run_id,
                    visits=visits,
                    result=result,
                    harvests=counts,
                    visit_budget=visit_budget,
                    skipped=skipped,
                    heat_raised=heat_raised,
                    session_flagged=flagged,
                )
                runs.finish_run(
                    session,
                    user,
                    run_id,
                    status=(
                        SyncRunStatus.COMPLETED
                        if result.reason is StopReason.END_OF_PLAN
                        else SyncRunStatus.ABORTED
                    ),
                    now=clock(),
                    stop_reason=stop_reason_of(result.reason.value, result.outcome),
                    counts=report.counts(),
                    notes=(*_lost_notes(result), *_inactive_notes(result, gate)),
                )
            return report

        report = await off_loop(finish, result)
    return report


async def resume_enrichment(
    factory: sessionmaker[Session],
    user_id: int,
    of_run_id: int,
    source: ProfileSource,
    *,
    settings: LinkedInSettings,
    max_visits: int | None = None,
    on_progress: ProgressSink | None = None,
    clock: Clock = _utcnow,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
) -> EnrichRunReport:
    """Record a resume of run ``of_run_id`` (spec 9.9) and run it within today's budget.

    Skips every contact the old run completed and never re-plans.
    :class:`~netkeeper.services.enrich_plan.PlanNotFound` for an unknown run,
    :class:`~netkeeper.services.enrich_plan.PlanFinished` for one with nothing
    left to resume, and the same refusals as :func:`enrich_contacts` after that.
    """

    def start_resume() -> int:
        with session_scope(factory, write=True) as session:
            user = _load_user(session, user_id)
            return enrich_plan.start_resume(
                session, user, of_run_id, now=clock(), max_visits=max_visits
            ).id

    run_id = await off_loop(start_resume)
    return await enrich_contacts(
        factory,
        user_id,
        source,
        settings=settings,
        run_id=run_id,
        on_progress=on_progress,
        clock=clock,
        sleep=sleep,
        rng=rng,
    )


def _lost_notes(result: EnrichResult) -> tuple[str, ...]:
    """#197: each unreadable visit whose answer's body was lost, in fixed words; and
    #207 review: each visit whose Contact info came from a streamed copy instead."""
    notes: list[str] = []
    if result.lost:
        notes.append(f"unreadable answers: {'; '.join(result.lost)}.")
    if result.copied:
        notes.append(f"read from streamed copies: {'; '.join(result.copied)}.")
    return tuple(notes)


def _inactive_notes(result: EnrichResult, gate: _Gate) -> tuple[str, ...]:
    """#213: a run the window stopped says so on the run, in the log's own words."""
    if result.reason is StopReason.INACTIVE and gate.inactive_message is not None:
        return (f"stopped {gate.inactive_message}",)
    return ()
