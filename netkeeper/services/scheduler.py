"""APScheduler-driven job scheduling for the LinkedIn extractor (spec 9.4, 9.5, 9.7, 9.9).

**Why this lives here, not under ``linkedin/``.** The scheduler persists next-fire
state so a restart resumes the schedule instead of restarting it (spec: "next-fire
persistence"). Persistence needs a session and a ``User``, and nothing under
``netkeeper/linkedin/`` may open one (ADR 0005, spec 9.10) -- the same seam
``netkeeper.services.heat`` and ``netkeeper.services.budgets`` already draw. What
*is* pure -- active hours, the warm-up ramp, the weekend multiplier -- already
lives in :mod:`netkeeper.linkedin.pacing` (P2-04) and is used here, not
reimplemented: :func:`netkeeper.linkedin.pacing.is_active_at` and
:func:`next_window_start` are the only active-hours logic in this module.
Likewise the heat skip threshold (spec 9.7: "above heat_skip_threshold the
scheduler skips browser jobs entirely") is enforced here by calling
:mod:`netkeeper.services.heat`, never reimplemented.

**The account.** Spec 8.4's ``linkedin_account`` table -- one row per user in v1,
carrying the account's timezone and active hours -- does not exist yet; it
arrives with P2-06, which depends on *this* item. Every function here therefore
takes ``account_id: int`` as a plain caller-supplied identifier, the same
precedent :mod:`netkeeper.services.heat` and :mod:`netkeeper.services.budgets`
already set (PR #148) for the same reason. The account's timezone is read from
``User.timezone`` (already a real column) until then; active hours come from
:mod:`netkeeper.config`'s ``LinkedInSettings`` (a config default, not yet
per-account) unless a caller overrides them.

**The injected-job seam.** Spec 9.4 names four job kinds -- connections full
sync, connections incremental sync, enrichment, inbox poll -- and this item
schedules all four *by name*, but what each one actually does is P2-06's and
P2-07's job, neither of which exists yet. So :data:`JobRegistry` maps a
:class:`JobKind` to a plain ``async def handler(ctx: JobContext) -> None``;
:func:`default_registry` gives every kind a trivial no-op, which is what every
test in this module uses. When P2-06 and P2-07 land they build a
:data:`JobRegistry` of their own real handlers and pass it in -- nothing about
scheduling, persistence, catch-up, or the interleave gap changes.

**Two clocks, on purpose.** :func:`establish_schedule` (and
:func:`sync_account_schedule`, its per-account fan-out) is the *cold* path:
call it once at process start, once when settings that affect timing change,
and once when a downtime window ends. It is the only place that compares a
stored due time to "now" and decides whether that gap means "restored
unchanged", "downtime -- catch up once, 5 to 20 minutes out", or "the timing
parameters themselves changed -- reschedule fresh". :func:`poll_and_fire` (and
its per-account, per-poll fan-out :func:`poll_once`) is the *hot* path: call it
every heartbeat. It never re-derives anything -- it reads the due time
:func:`establish_schedule` already settled on, fires if it has arrived, and
advances it by exactly one interval. Collapsing these two into one function
that runs on every heartbeat was the first draft of this module and it does
not work: a due time that simply arrives during continuous, uninterrupted
operation is indistinguishable, from the stored state alone, from a due time
that lapsed during three days of downtime. A poller that re-derives on every
tick reads the ordinary case as the downtime case and jitters every single
fire 5 to 20 minutes into the future, which quietly breaks the cadence this
item exists to get right. Keeping catch-up detection exclusive to the cold
path is what keeps the hot path honest.

**Writer sessions.** Every function that persists (:func:`establish_schedule`,
:func:`record_fired`, and the ``_store_*`` helpers) needs
``session_scope(factory, write=True)`` -- each reads the current state before
writing the new one, and CLAUDE.md is explicit that scheduler jobs are
writers. :func:`poll_and_fire` releases the writer session (and the SQLite
write lock with it) *before* awaiting the injected handler: a real enrichment
run is minutes of bursts and human-like delays (spec 9.5), and holding a write
transaction open for that long would starve every other writer in the process.

**The interleave gap.** Spec 9.5's last bullet: "never run enrichment and a
message send in the same minute; the scheduler interleaves job kinds with a
gap." :func:`stagger_due_times` is the general mechanism -- it takes any set of
computed due times for one account and nudges apart any that land within
:data:`MIN_JOB_KIND_GAP` of each other. It is applied at both places a
collision can arise: :func:`sync_account_schedule` (multiple kinds catching up
after a shared downtime, each drawing an independent 5-20 minute jitter, can
land in the same minute by chance) and :func:`poll_once` (two kinds with
different cadences can drift into alignment over a long uptime). Because it is
keyed by :class:`JobKind`, not by name, message-send (P2-08) gets the same
protection automatically the day it is added to that enum -- nothing here
changes.
"""

from __future__ import annotations

import enum
import logging
import random
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import Any, Final

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import HeatSettings
from netkeeper.db import is_writer, session_scope
from netkeeper.linkedin import pacing
from netkeeper.models import JsonValue, User
from netkeeper.models.base import utcnow
from netkeeper.services import heat as heat_service
from netkeeper.services.settings_kv import get_setting, set_setting

log = logging.getLogger(__name__)


class JobKind(enum.StrEnum):
    """The four job kinds spec 9.4 names. Values match the eventual ``sync_run.kind``
    column (spec 8.4) minus ``message_send``, which belongs to a later item."""

    CONNECTIONS_FULL = "connections_full"
    CONNECTIONS_INCREMENTAL = "connections_incremental"
    ENRICH = "enrich"
    INBOX = "inbox"


@dataclass(frozen=True, slots=True)
class JobContext:
    """What an injected job handler is handed. Plain data in, spec 9.10's own rule
    for the extractor boundary applied one level up: the scheduler hands a handler
    a spec, not a session."""

    user_id: int
    account_id: int
    kind: JobKind
    due: datetime
    catch_up: bool


JobHandler = Callable[[JobContext], Awaitable[None]]
JobRegistry = Mapping[JobKind, JobHandler]


async def noop_handler(ctx: JobContext) -> None:
    """Does nothing. The default handler for every job kind until P2-06 and P2-07
    register real ones; every test in this module runs against this."""
    log.debug(
        "scheduler: no-op handler for %s (user %d, account %d)",
        ctx.kind.value,
        ctx.user_id,
        ctx.account_id,
    )


def default_registry() -> JobRegistry:
    """A registry where every :class:`JobKind` maps to :func:`noop_handler`."""
    return dict.fromkeys(JobKind, noop_handler)


@dataclass(frozen=True, slots=True)
class JobSchedule:
    """How often ``kind`` runs, and whether it waits for active hours (spec 9.5)."""

    kind: JobKind
    interval: timedelta
    respect_active_hours: bool = True


# Spec 9.4: incremental sync "runs daily"; full sync runs "on first setup and
# weekly". Enrichment and inbox poll are given no exact cadence ("[inbox poll]
# runs every few hours while any LinkedIn step is active") -- these two are
# reasonable defaults, not spec numbers, and are trivially overridable per call.
# A future item can make them per-account settings without changing anything
# else in this module.
DEFAULT_SCHEDULES: Final[dict[JobKind, JobSchedule]] = {
    JobKind.CONNECTIONS_INCREMENTAL: JobSchedule(
        JobKind.CONNECTIONS_INCREMENTAL, timedelta(days=1)
    ),
    JobKind.CONNECTIONS_FULL: JobSchedule(JobKind.CONNECTIONS_FULL, timedelta(days=7)),
    JobKind.ENRICH: JobSchedule(JobKind.ENRICH, timedelta(hours=3)),
    JobKind.INBOX: JobSchedule(JobKind.INBOX, timedelta(hours=3)),
}

# A fire missed while the process was down happens once, 5 to 20 minutes after
# the process notices -- never immediately (that lands mid-deploy) and never
# once per missed interval (a long outage must not queue a burst of runs).
CATCHUP_MIN_MINUTES: Final = 5.0
CATCHUP_MAX_MINUTES: Final = 20.0

# Spec 9.5: "never run enrichment and a message send in the same minute; the
# scheduler interleaves job kinds with a gap." Two minutes is comfortably more
# than the one full minute a collision needs to be resolved (see
# `stagger_due_times`), with room for polling jitter around the boundary.
MIN_JOB_KIND_GAP: Final = timedelta(minutes=2)


# --- active hours: delegates to netkeeper.linkedin.pacing, never reimplements it ---


def _parse_hhmm(value: str) -> time:
    hour, _, minute = value.partition(":")
    return time(int(hour), int(minute))


def _snap_to_active_hours(
    due: datetime,
    tz: str,
    *,
    start: time,
    end: time,
    respect_active_hours: bool,
) -> datetime:
    """``due``, or the next active-hours window start if ``due`` falls outside it.

    Spec 9.5: "Ticks outside [active hours] park a one-shot job for the window
    start." Applied uniformly to every freshly computed due time -- first
    schedule, catch-up, and settings-change reschedule alike -- so the
    persisted due time is always already the time the job will actually run,
    with nothing left to check again at fire time.
    """
    if not respect_active_hours:
        return due
    if pacing.is_active_at(due, tz, start=start, end=end):
        return due
    return pacing.next_window_start(due, tz, start=start)


# --- the pure decision: restored, catching up, or fresh ---------------------


def compute_due(
    prior_due: datetime | None,
    *,
    now: datetime,
    interval: timedelta,
    rng: random.Random,
) -> tuple[datetime, bool, str]:
    """``(due, is_catchup, reason)`` for a job with no still-valid schedule.

    ``prior_due`` is the previous due time *only* when it was computed under
    the same timing parameters that apply now (see :func:`establish_schedule`);
    pass ``None`` for a job that has never been scheduled, or whose timing
    parameters just changed -- both get a fresh ``now + interval``, never the
    catch-up jitter, because neither one is downtime.

    Mirrors igtracker's proven ``_first_fire`` (``services/scheduler.py``,
    cited in ``docs/architecture.md`` section 6 as this project's reason for
    picking APScheduler): no stored due time is a fresh schedule; a stored due
    time still in the future is restored unchanged; a stored due time at or
    before ``now`` was missed while the process was down and gets exactly one
    catch-up fire, 5 to 20 minutes out.
    """
    if prior_due is None:
        return now + interval, False, "no stored schedule yet"
    if prior_due > now:
        return prior_due, False, "restored"
    catchup = now + timedelta(minutes=rng.uniform(CATCHUP_MIN_MINUTES, CATCHUP_MAX_MINUTES))
    return catchup, True, "catching up after downtime"


# --- persistence (settings_kv; spec 8.4) ------------------------------------

_KEY_PREFIX: Final = "scheduler.job"


@dataclass(frozen=True, slots=True)
class ScheduleFingerprint:
    """The parameters that determine *when* ``kind`` fires. Persisted next to the
    due time so a later call can tell a genuine timing change (any field here
    differs -- reschedule fresh) from an unrelated write (nothing here differs
    -- leave the stored due time exactly alone). Spec: "reschedule only on
    change."
    """

    interval_seconds: float
    timezone: str
    active_start: str
    active_end: str
    respect_active_hours: bool

    @classmethod
    def of(
        cls, schedule: JobSchedule, *, tz: str, active_start: time, active_end: time
    ) -> ScheduleFingerprint:
        return cls(
            interval_seconds=schedule.interval.total_seconds(),
            timezone=tz,
            active_start=active_start.isoformat(),
            active_end=active_end.isoformat(),
            respect_active_hours=schedule.respect_active_hours,
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "interval_seconds": self.interval_seconds,
            "timezone": self.timezone,
            "active_start": self.active_start,
            "active_end": self.active_end,
            "respect_active_hours": self.respect_active_hours,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> ScheduleFingerprint:
        return cls(
            interval_seconds=float(raw["interval_seconds"]),
            timezone=str(raw["timezone"]),
            active_start=str(raw["active_start"]),
            active_end=str(raw["active_end"]),
            respect_active_hours=bool(raw["respect_active_hours"]),
        )


@dataclass(frozen=True, slots=True)
class _JobState:
    """What is persisted per ``(account_id, kind)``: the due time, the fingerprint
    that produced it, and whether that due time is a pending catch-up fire."""

    due: datetime
    fingerprint: ScheduleFingerprint
    is_catchup: bool = False


def _key(account_id: int, kind: JobKind) -> str:
    return f"{_KEY_PREFIX}.{account_id}.{kind.value}"


def _load_state(session: Session, user: User, account_id: int, kind: JobKind) -> _JobState | None:
    raw = get_setting(session, user, _key(account_id, kind))
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError(
            f"scheduler state for account {account_id}/{kind.value} is not an object: {raw!r}"
        )
    fingerprint_raw = raw.get("fingerprint")
    if not isinstance(fingerprint_raw, dict):
        raise TypeError(
            f"scheduler state for account {account_id}/{kind.value} has no fingerprint: {raw!r}"
        )
    return _JobState(
        due=datetime.fromisoformat(str(raw["due"])),
        fingerprint=ScheduleFingerprint.from_json(fingerprint_raw),
        is_catchup=bool(raw.get("is_catchup", False)),
    )


def _store_state(
    session: Session, user: User, account_id: int, kind: JobKind, state: _JobState
) -> None:
    _require_writer(session, "scheduler._store_state")
    set_setting(
        session,
        user,
        _key(account_id, kind),
        {
            "due": state.due.isoformat(),
            "fingerprint": state.fingerprint.to_json(),
            "is_catchup": state.is_catchup,
        },
    )


def stored_due(session: Session, user: User, account_id: int, kind: JobKind) -> datetime | None:
    """The currently persisted due time for ``(account_id, kind)``, or ``None`` if
    it has never been scheduled. Read-only: no writer session needed."""
    state = _load_state(session, user, account_id, kind)
    return None if state is None else state.due


def _require_writer(session: Session, where: str) -> None:
    if not is_writer(session):
        raise RuntimeError(
            f"{where} needs a writer session; use session_scope(factory, write=True)"
        )


# --- the cold path: establish / restore / catch up / reschedule -------------


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    due: datetime
    changed: bool
    is_catchup: bool
    reason: str


def establish_schedule(
    session: Session,
    user: User,
    account_id: int,
    kind: JobKind,
    *,
    now: datetime,
    schedule: JobSchedule,
    rng: random.Random,
    tz: str,
    active_start: time = pacing.DEFAULT_ACTIVE_START,
    active_end: time = pacing.DEFAULT_ACTIVE_END,
) -> ScheduleResult:
    """Settle ``kind``'s due time for the current timing parameters.

    Call this at process start, whenever a settings write may have changed
    ``schedule``/``tz``/``active_start``/``active_end``, and when a downtime
    window ends (see :func:`sync_account_schedule` for the per-account version
    used in all three cases). Never call it from a per-heartbeat poll loop --
    see the module docstring for why that conflates "due because it's simply
    time" with "due because of downtime".

    Writes nothing, and reports ``changed=False``, when the stored fingerprint
    already matches and the stored due time is still in the future -- the
    "reschedule only on change" guarantee. A stored due time at or before
    ``now`` under an *unchanged* fingerprint is downtime, not a settings
    change, and goes through :func:`compute_due`'s catch-up branch instead of
    being treated as fresh.
    """
    _require_writer(session, "scheduler.establish_schedule")
    fingerprint = ScheduleFingerprint.of(
        schedule, tz=tz, active_start=active_start, active_end=active_end
    )
    existing = _load_state(session, user, account_id, kind)
    if existing is not None and existing.fingerprint == fingerprint:
        if existing.due > now:
            return ScheduleResult(
                due=existing.due, changed=False, is_catchup=False, reason="unchanged"
            )
        prior_due = existing.due  # same parameters, due already passed: downtime
    else:
        prior_due = None  # first time, or the timing parameters themselves changed
    due, is_catchup, reason = compute_due(prior_due, now=now, interval=schedule.interval, rng=rng)
    due = _snap_to_active_hours(
        due,
        tz,
        start=active_start,
        end=active_end,
        respect_active_hours=schedule.respect_active_hours,
    )
    _store_state(
        session,
        user,
        account_id,
        kind,
        _JobState(due=due, fingerprint=fingerprint, is_catchup=is_catchup),
    )
    return ScheduleResult(due=due, changed=True, is_catchup=is_catchup, reason=reason)


def stagger_due_times(
    dues: Mapping[JobKind, datetime], *, gap: timedelta = MIN_JOB_KIND_GAP
) -> dict[JobKind, datetime]:
    """Nudge forward any due time landing within ``gap`` of an earlier one.

    Spec 9.5: "never run enrichment and a message send in the same minute; the
    scheduler interleaves job kinds with a gap", generalized to any two kinds
    sharing an account. Pure, and order-stable: processes ``dues`` earliest
    first (ties broken by kind name) and only ever pushes a time *later*, so a
    kind already ``gap`` or more clear of everything else comes back
    untouched. ``gap >= 60`` seconds is what actually guarantees a different
    *minute* -- adding exactly one minute always advances the floor-minute by
    exactly one, whatever the phase -- and the two-minute default clears that
    with room for polling jitter.
    """
    ordered = sorted(dues.items(), key=lambda kv: (kv[1], kv[0].value))
    result: dict[JobKind, datetime] = {}
    last: datetime | None = None
    for kind, when in ordered:
        if last is not None and when - last < gap:
            when = last + gap
        result[kind] = when
        last = when
    return result


def sync_account_schedule(
    session: Session,
    user: User,
    account_id: int,
    *,
    now: datetime,
    schedules: Mapping[JobKind, JobSchedule] = DEFAULT_SCHEDULES,
    rng: random.Random,
    tz: str,
    active_start: time = pacing.DEFAULT_ACTIVE_START,
    active_end: time = pacing.DEFAULT_ACTIVE_END,
) -> dict[JobKind, ScheduleResult]:
    """:func:`establish_schedule` for every kind in ``schedules``, then
    :func:`stagger_due_times` across the results -- the per-account entry point
    for process start, a restart after downtime, and a settings change.

    Staggering can nudge a kind whose own :class:`ScheduleResult` reported
    ``changed=False`` (for example, two kinds independently catching up after
    a shared outage and landing in the same minute by chance): two job kinds
    firing in the same window is worse than a few minutes of drift on an
    otherwise-stable schedule, so that one narrow case is persisted too, and
    its result is corrected to ``changed=True`` to say so.
    """
    results = {
        kind: establish_schedule(
            session,
            user,
            account_id,
            kind,
            now=now,
            schedule=schedule,
            rng=rng,
            tz=tz,
            active_start=active_start,
            active_end=active_end,
        )
        for kind, schedule in schedules.items()
    }
    staggered = stagger_due_times({kind: r.due for kind, r in results.items()})
    for kind, when in staggered.items():
        if when != results[kind].due:
            state = _load_state(session, user, account_id, kind)
            assert state is not None  # just written by establish_schedule above
            _store_state(session, user, account_id, kind, replace(state, due=when))
            results[kind] = replace(results[kind], due=when, changed=True)
    return results


# --- the hot path: poll and fire, never re-derive ----------------------------


@dataclass(frozen=True, slots=True)
class FireResult:
    kind: JobKind
    due: datetime
    fired: bool
    skipped_reason: str | None
    is_catchup: bool
    next_due: datetime


def record_fired(
    session: Session,
    user: User,
    account_id: int,
    kind: JobKind,
    *,
    due: datetime,
    schedule: JobSchedule,
    tz: str,
    active_start: time = pacing.DEFAULT_ACTIVE_START,
    active_end: time = pacing.DEFAULT_ACTIVE_END,
) -> datetime:
    """Advance ``kind``'s due time to the next cycle after a fire (executed or
    heat-skipped -- either way, this fire happened and the cadence moves on).

    Anchored to ``due`` -- the time this fire *was scheduled for* -- not
    ``now``, so the cadence never drifts with polling latency or how long the
    handler took to run. The new due time is never a pending catch-up.
    """
    _require_writer(session, "scheduler.record_fired")
    state = _load_state(session, user, account_id, kind)
    if state is None:
        raise RuntimeError(
            f"record_fired: no established schedule for account {account_id}/{kind.value}"
        )
    next_due = due + schedule.interval
    next_due = _snap_to_active_hours(
        next_due,
        tz,
        start=active_start,
        end=active_end,
        respect_active_hours=schedule.respect_active_hours,
    )
    _store_state(session, user, account_id, kind, replace(state, due=next_due, is_catchup=False))
    return next_due


async def poll_and_fire(
    session_factory: sessionmaker[Session],
    user: User,
    account_id: int,
    kind: JobKind,
    *,
    now: datetime,
    schedule: JobSchedule,
    registry: JobRegistry,
    heat_settings: HeatSettings | None = None,
    tz: str,
    active_start: time = pacing.DEFAULT_ACTIVE_START,
    active_end: time = pacing.DEFAULT_ACTIVE_END,
) -> FireResult | None:
    """One heartbeat's check for ``(account_id, kind)``: fire if due, else no-op.

    Returns ``None`` when the job is not due yet, or when it has never been
    established (:func:`establish_schedule`/:func:`sync_account_schedule` must
    run first -- this function only ever reads the schedule, never derives
    it). Above the configured heat threshold (spec 9.7) the browser job is
    skipped -- the injected handler is not called -- but the cadence still
    advances, so the scheduler does not spin retrying the same fire on every
    subsequent heartbeat while heat is elevated.

    The writer session that reads and advances the due time is closed *before*
    the (possibly long-running) handler is awaited, so a real job never holds
    the SQLite write lock for its duration.
    """
    with session_scope(session_factory, write=True) as session:
        state = _load_state(session, user, account_id, kind)
        if state is None or state.due > now:
            return None
        due = state.due
        is_catchup = state.is_catchup
        skipped_reason: str | None = None
        if heat_settings is not None and heat_service.should_skip(
            session, user, account_id, now=now, settings=heat_settings
        ):
            skipped_reason = "heat"
        next_due = record_fired(
            session,
            user,
            account_id,
            kind,
            due=due,
            schedule=schedule,
            tz=tz,
            active_start=active_start,
            active_end=active_end,
        )
    fired = skipped_reason is None
    if fired:
        handler = registry[kind]
        ctx = JobContext(
            user_id=user.id, account_id=account_id, kind=kind, due=due, catch_up=is_catchup
        )
        await handler(ctx)
    else:
        log.warning(
            "scheduler: skipping %s for account %d (%s)", kind.value, account_id, skipped_reason
        )
    return FireResult(
        kind=kind,
        due=due,
        fired=fired,
        skipped_reason=skipped_reason,
        is_catchup=is_catchup,
        next_due=next_due,
    )


async def poll_once(
    session_factory: sessionmaker[Session],
    accounts: Iterable[tuple[User, int]],
    *,
    now: datetime,
    registry: JobRegistry,
    schedules: Mapping[JobKind, JobSchedule] = DEFAULT_SCHEDULES,
    heat_settings: HeatSettings | None = None,
    active_start: time = pacing.DEFAULT_ACTIVE_START,
    active_end: time = pacing.DEFAULT_ACTIVE_END,
) -> list[FireResult]:
    """:func:`poll_and_fire` every ``(user, account_id)`` in ``accounts`` across
    every kind in ``schedules``. The body of the heartbeat job in
    :func:`build_scheduler`.

    Before firing, re-applies :func:`stagger_due_times` across whichever kinds
    are due in *this same poll* -- schedules with different cadences can drift
    into alignment over a long uptime even though nothing collided when they
    were established. A kind nudged clear of a collision this way fires on a
    later poll instead of this one; it is not skipped, only delayed by the
    gap.
    """
    fired: list[FireResult] = []
    for user, account_id in accounts:
        tz = user.timezone
        with session_scope(session_factory) as session:
            due_now = {
                kind: state.due
                for kind in schedules
                if (state := _load_state(session, user, account_id, kind)) is not None
                and state.due <= now
            }
        if len(due_now) > 1:
            staggered = stagger_due_times(due_now)
            changed = {kind: when for kind, when in staggered.items() if when != due_now[kind]}
            if changed:
                with session_scope(session_factory, write=True) as session:
                    for kind, when in changed.items():
                        state = _load_state(session, user, account_id, kind)
                        assert state is not None  # read moments ago, above
                        _store_state(session, user, account_id, kind, replace(state, due=when))
            due_now = {kind: when for kind, when in staggered.items() if when <= now}
        for kind in due_now:
            result = await poll_and_fire(
                session_factory,
                user,
                account_id,
                kind,
                now=now,
                schedule=schedules[kind],
                registry=registry,
                heat_settings=heat_settings,
                tz=tz,
                active_start=active_start,
                active_end=active_end,
            )
            if result is not None:
                fired.append(result)
    return fired


# --- production wiring: build, never start -----------------------------------

HEARTBEAT_JOB_ID: Final = "netkeeper-scheduler-heartbeat"
DEFAULT_HEARTBEAT_INTERVAL: Final = timedelta(minutes=1)

AccountsProvider = Callable[[], Iterable[tuple[User, int]]]


def build_scheduler(
    session_factory: sessionmaker[Session],
    accounts: AccountsProvider,
    *,
    registry: JobRegistry | None = None,
    schedules: Mapping[JobKind, JobSchedule] | None = None,
    heartbeat_interval: timedelta = DEFAULT_HEARTBEAT_INTERVAL,
    active_start: time = pacing.DEFAULT_ACTIVE_START,
    active_end: time = pacing.DEFAULT_ACTIVE_END,
    heat_settings: HeatSettings | None = None,
    rng: random.Random | None = None,
) -> AsyncIOScheduler:
    """Build (never start) an :class:`AsyncIOScheduler` with one heartbeat job.

    Deliberately **one** APScheduler job, not one per ``(account, kind)`` the
    way igtracker parks per-fire ``DateTrigger`` one-shots: ``linkedin_account``
    doesn't exist yet (P2-06), so there is no table this module can enumerate
    accounts from ahead of time, and ``accounts`` is a caller-supplied callback
    for exactly that reason. Restart-safety, catch-up, and the interleave gap
    all live in the settings_kv-persisted state this module owns and tests
    directly (see the module docstring's "two clocks"), not in APScheduler's
    own in-memory trigger state -- so a single polling heartbeat is enough to
    drive all of it, and is far simpler to keep correct than N*4 live jobs
    kept in sync with an account table that isn't there yet.

    Establishes every account's schedule once, synchronously, before
    returning (the "process start" case in the module docstring). The caller
    starts the scheduler (``.start()``); this function never does, and no test
    in this module does either -- no job is armed by building one.
    """
    registry = dict(registry) if registry is not None else default_registry()
    schedules = dict(schedules) if schedules is not None else dict(DEFAULT_SCHEDULES)
    rng = rng if rng is not None else random.Random()  # noqa: S311 -- pacing jitter, not crypto
    scheduler = AsyncIOScheduler()

    for user, account_id in accounts():
        with session_scope(session_factory, write=True) as session:
            sync_account_schedule(
                session,
                user,
                account_id,
                now=utcnow(),
                schedules=schedules,
                rng=rng,
                tz=user.timezone,
                active_start=active_start,
                active_end=active_end,
            )

    async def _heartbeat() -> None:
        await poll_once(
            session_factory,
            accounts(),
            now=utcnow(),
            registry=registry,
            schedules=schedules,
            heat_settings=heat_settings,
            active_start=active_start,
            active_end=active_end,
        )

    scheduler.add_job(
        _heartbeat,
        IntervalTrigger(seconds=heartbeat_interval.total_seconds()),
        id=HEARTBEAT_JOB_ID,
        coalesce=True,
        max_instances=1,
    )
    return scheduler
