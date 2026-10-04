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
:mod:`netkeeper.services.heat`, never reimplemented. Spec 9.7 makes that skip
unconditional, so it is on by default: every entry point defaults
``heat_settings`` to :data:`DEFAULT_HEAT_SETTINGS` (config's
``[linkedin.heat]`` defaults), production passes its own loaded
``settings.linkedin.heat``, and turning the skip off takes the explicit,
named :data:`HEAT_SKIP_DISABLED` -- never a forgotten argument.

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
and once when a downtime window ends. It is the only place that decides
between "restored unchanged", "downtime -- catch up once", "the timing
parameters themselves changed -- reschedule fresh", and "never scheduled"
(one interval out, except for a kind spec 9.4 runs "on first setup", which is
due after the catch-up jitter). :func:`poll_and_fire`
(and its per-account, per-poll fan-out :func:`poll_once`) is the *hot* path:
call it every heartbeat. For a due time that has simply arrived it re-derives
nothing -- it reads the due time the cold path settled on, fires, and advances
it by exactly one interval. Collapsing these two into one function that
re-derives on every tick was the first draft of this module and it does not
work: it reads the ordinary case as the downtime case and jitters every
single fire 5 to 20 minutes into the future, which quietly breaks the cadence
this item exists to get right.

**The lapse the cold path cannot see.** Downtime does not always restart the
process. A sleeping laptop keeps it alive, so :func:`build_scheduler` and
:func:`sync_account_schedule` never run again -- the heartbeat just resumes
into a backlog. Advancing by one interval per poll replays one fire per
missed interval: 24 runs after a three-day sleep at the default three-hour
cadence, one per heartbeat minute, which is precisely the request burst
budgets (9.6), pacing (9.5), and heat (9.7) all exist to prevent. It is
reachable with no sleep at all, too: :func:`build_scheduler` arms one job
with ``max_instances=1`` and :func:`poll_and_fire` awaits the handler
*inside* the heartbeat, so a real enrichment run -- spec 9.5's bursts of 8 to
15 profiles plus 5 to 20 minute breaks -- that outlasts its own interval
comes back to the same backlog. So the hot path applies exactly one rule of
its own, and it is the cold path's rule at the cold path's threshold: a due
time lapsed by a *whole interval or more* is downtime, whoever noticed it,
and goes back through :func:`compute_due`'s catch-up branch -- one fire, 5 to
20 minutes out, flagged ``is_catchup``, with the independent jitter that also
keeps several kinds waking together from landing on the same minute. A due
time lapsed by less than one interval is ordinary polling latency and fires
straight away, as before. The distinction the two clocks exist to protect is
untouched: "it is simply time" and "we were asleep" are still told apart by
how far the due time lapsed, never by re-deriving a due time that has not.

**Writer sessions.** Every function that persists (:func:`establish_schedule`,
:func:`record_fired`, and the ``_store_*`` helpers) needs
``session_scope(factory, write=True)`` -- each reads the current state before
writing the new one, and CLAUDE.md is explicit that scheduler jobs are
writers. :func:`poll_and_fire` releases the writer session (and the SQLite
write lock with it) *before* awaiting the injected handler: a real enrichment
run is minutes of bursts and human-like delays (spec 9.5), and holding a write
transaction open for that long would starve every other writer in the process.
The heartbeat's sessions run off the event loop (:func:`netkeeper.db.off_loop`,
#259): each whole session in one call, so a request holding the write lock is
waited for on the database thread instead of freezing the loop it needs to commit.

**Wired into ``netkeeper serve`` (P2-10), disarmed.** ``serve``'s lifespan
builds and starts this scheduler (:mod:`netkeeper.services.scheduled_runs`)
for :data:`SERVED_SCHEDULES` -- the connections syncs and enrichment; the
inbox poll has no page source yet (P4-01) -- with handlers that record a ``sync_runs`` row
and hand it to the browser worker. Before any of that, :func:`poll_and_fire`
asks the **arm gate**: while a person has not armed the account's scheduled
runs (every account starts disarmed) a due fire is skipped as ``"disarmed"``
exactly the way heat skips one, cadence and first-setup standing included. The
gate is on by default at every entry point (:data:`DEFAULT_ARM_GATE`); only
:data:`ARMING_NOT_REQUIRED`, which ``netkeeper simulate``'s scratch schedule
passes, turns it off. For the two connections kinds, :func:`poll_and_fire`
also asks :mod:`netkeeper.services.route_breaker` (#189 item 1): once two
connections runs in a row have ended ``route_changed``, a due fire is skipped
as ``"route_changed_breaker"`` the same way, with no off switch at all, and
once three of one kind in a row have ended ``answer_lost`` (#199), as ``"answer_lost_breaker"``. A
handler that could not reach the browser answers
:attr:`JobOutcome.RETRY_LATER`, and :func:`park_retry` parks one retry 20 to 50
minutes out (spec 9.9).

**Paused (#324).** A person can pause an armed schedule without disarming it
(``netkeeper linkedin schedule pause``, ``POST /linkedin/schedule/pause``): a due
fire is then skipped as ``"paused"``, the same way as ``"disarmed"``, and a run
already going is not stopped. Because a skipped fire still advances the cadence,
unpausing owes nothing: no missed fire is replayed, and each kind next fires at
its own stored due time, at most one interval away (a first-setup kind within
:data:`FIRST_SETUP_RETRY`). The pause is a ``settings_kv`` key, so it survives a
restart; a restart while paused still goes through the cold path's one catch-up,
which the gate then skips like any other fire.

**The interleave gap.** Spec 9.5's last bullet: "never run enrichment and a
message send in the same minute; the scheduler interleaves job kinds with a
gap." Two mechanisms, one constant (:data:`MIN_JOB_KIND_GAP`).
:func:`stagger_due_times` is the pure one: it nudges apart *computed* due
times that land within the gap, and :func:`sync_account_schedule` applies it
to the times it has just established (several kinds catching up after a
shared outage each draw an independent 5-20 minute jitter and can land in the
same minute by chance). :func:`poll_once` cannot use it, because by the time
it looks, colliding due times are in the *past*: nudging a past time two
minutes forward leaves it in the past, and it fires in the same poll anyway.
So the hot path enforces the gap against the fire it just performed instead
-- at most one kind fires per poll per account, and every other kind that was
due has its stored due time pushed a full gap past that fire, so it runs on a
later poll rather than in the same minute. Fires only ever happen at poll
instants, so a heartbeat of :data:`DEFAULT_HEARTBEAT_INTERVAL` (one minute)
or coarser makes "two kinds in one minute" structurally impossible for one
account. Because both mechanisms are keyed by :class:`JobKind`, not by name,
message-send (P2-08) gets the same protection the day it is added to that
enum -- nothing here changes.
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

from netkeeper.config import HeatSettings, LinkedInSettings
from netkeeper.db import is_writer, off_loop, session_scope
from netkeeper.linkedin import pacing
from netkeeper.models import JsonValue, User
from netkeeper.models.base import utcnow
from netkeeper.services import heat as heat_service
from netkeeper.services import route_breaker
from netkeeper.services.linkedin_accounts import schedule_paused, scheduled_runs_armed
from netkeeper.services.linkedin_session import session_flag
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


class JobOutcome(enum.Enum):
    """What a handler may tell the scheduler about the fire it just ran."""

    RETRY_LATER = "retry_later"
    """The browser was unreachable or busy (spec 9.9): park one retry
    :data:`RETRY_MIN_MINUTES` to :data:`RETRY_MAX_MINUTES` out."""

    NOT_DONE = "not_done"
    """The fire ran, but did not do its job (#200: a full sync that lost some of the
    page's answers, so it could not age anyone): offer it again
    :data:`NOT_DONE_RETRY` later instead of a whole interval out."""

    PAUSED_AFTER_GATE = "paused_after_gate"
    """The handler started nothing: the schedule was paused after the gate let the
    fire through (#324). The fire counts as skipped, not run, so a first-setup
    kind keeps its standing and is offered again :data:`FIRST_SETUP_RETRY` later,
    as a fire the gate skipped would be. Its value is the fire's skip reason."""

    DISARMED_AFTER_GATE = "disarmed_after_gate"
    """The same, for an account found disarmed after the gate let the fire through."""

    NOTHING_TO_WATCH = "nothing_to_watch"
    """The inbox poll's handler found no live enrollment with a LinkedIn contact
    (P4-08): it recorded no run and attached to nothing. The fire counts as skipped."""


#: The outcomes that say the handler started nothing: the fire was skipped, not run.
SKIPPED_AFTER_GATE: Final = frozenset(
    {JobOutcome.PAUSED_AFTER_GATE, JobOutcome.DISARMED_AFTER_GATE, JobOutcome.NOTHING_TO_WATCH}
)

#: A handler returns ``None`` when the fire ran (whatever the run made of it),
#: :attr:`JobOutcome.RETRY_LATER`, :attr:`JobOutcome.NOT_DONE`, or one of
#: :data:`SKIPPED_AFTER_GATE`.
JobHandler = Callable[[JobContext], Awaitable[JobOutcome | None]]
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
    """How often ``kind`` runs, and whether it waits for active hours (spec 9.5).

    ``run_on_first_setup`` makes a kind that has *never fired* for this
    account due right away (after the catch-up jitter) instead of one
    interval out -- on the first schedule, and again if its timing changes
    before that first fire (a timezone or active-hours edit during
    onboarding must not push it a whole interval away). Once it has fired, a
    timing change reschedules one interval out like any other kind. Not part of
    :class:`ScheduleFingerprint`, because it says nothing about when an
    established schedule fires.
    """

    kind: JobKind
    interval: timedelta
    respect_active_hours: bool = True
    run_on_first_setup: bool = False


# Spec 9.4: incremental sync "runs daily"; full sync runs "on first setup and
# weekly". Enrichment and inbox poll are given no exact cadence ("[inbox poll]
# runs every few hours while any LinkedIn step is active") -- these two are
# reasonable defaults, not spec numbers, and are trivially overridable per call.
# A future item can make them per-account settings without changing anything
# else in this module. The full sync is the only kind spec 9.4 says runs "on
# first setup", so it is the only one with ``run_on_first_setup``: every later
# incremental sync compares against the baseline it builds (#161).
DEFAULT_SCHEDULES: Final[dict[JobKind, JobSchedule]] = {
    JobKind.CONNECTIONS_INCREMENTAL: JobSchedule(
        JobKind.CONNECTIONS_INCREMENTAL, timedelta(days=1)
    ),
    JobKind.CONNECTIONS_FULL: JobSchedule(
        JobKind.CONNECTIONS_FULL, timedelta(days=7), run_on_first_setup=True
    ),
    JobKind.ENRICH: JobSchedule(JobKind.ENRICH, timedelta(hours=3)),
    JobKind.INBOX: JobSchedule(JobKind.INBOX, timedelta(hours=3)),
}

# A fire missed while the process was down happens once, 5 to 20 minutes after
# the process notices -- never immediately (that lands mid-deploy) and never
# once per missed interval (a long outage must not queue a burst of runs).
CATCHUP_MIN_MINUTES: Final = 5.0
CATCHUP_MAX_MINUTES: Final = 20.0

# A ``run_on_first_setup`` kind whose first fire is heat-skipped has not run,
# so it must not wait a whole interval (a week, for the full sync) for its next
# chance. It is offered again this long after the skip instead: long enough
# that a warm account is not polled into a skip every few minutes, short enough
# that the first full sync still lands the day heat clears (#161).
FIRST_SETUP_RETRY: Final = timedelta(hours=1)

# Spec 9.5: "never run enrichment and a message send in the same minute; the
# scheduler interleaves job kinds with a gap." Two minutes is comfortably more
# than the one full minute a collision needs to be resolved (see
# `stagger_due_times`), with room for polling jitter around the boundary.
MIN_JOB_KIND_GAP: Final = timedelta(minutes=2)

# Spec 9.9: when the browser is gone (``BrowserUnavailable``) -- or another
# netkeeper process holds it -- "the scheduler parks a retry 20 to 50 minutes
# out". Never sooner (Chrome is not coming back in seconds, and a tight loop of
# attach attempts is noise), never much later (the day's run should still land).
RETRY_MIN_MINUTES: Final = 20.0
RETRY_MAX_MINUTES: Final = 50.0

# #200: a fire that ran but was not done -- a weekly full sync that lost some of the
# page's answers, so it is incomplete and aged nobody -- is offered again this long
# after it ended, not a week later. A day, not minutes: a full sync is the longest
# read of the list there is, and running it twice in one day doubles what the
# account loads for no reason the next day does not also serve. Snapped into
# active hours like every due time.
NOT_DONE_RETRY: Final = timedelta(days=1)
#: How late a fire may run and still keep its cadence anchored to its due time
#: (#309 re-review, F1). A fire that ran later than this -- the machine slept with
#: ``serve`` alive, for up to just under an interval, so the hot path fired it on
#: waking rather than as a catch-up -- has its next fire a whole interval after it
#: actually ran, not ``due + interval``, which could be minutes away. Five minutes is
#: five heartbeats (:data:`DEFAULT_HEARTBEAT_INTERVAL`): an on-time fire is at most
#: about one heartbeat plus the poll's own work late, so it never drifts; anything
#: later is a stall, and the only effect of anchoring it is a later next fire. A fire
#: held up by a long run in the same heartbeat is anchored too, which only pushes
#: its next fire later by that run's length.
LATE_FIRE_SLACK: Final = timedelta(minutes=5)

#: Up to this much is added to a not-done re-offer, so it never lands at exactly the
#: same time of day as the fire that was not done (#201 review, M1). At most one
#: re-offer per interval: a re-offer that is not done either waits for the normal one.
NOT_DONE_JITTER: Final = timedelta(hours=3)

#: The kinds ``netkeeper serve`` schedules: the ones whose runner can do its job
#: (P2-06, P2-07). ``inbox`` is left out, not registered-but-inert: its runner and
#: handler exist (P4-08), but it has no page source until P4-01 (#380) wires one,
#: and a due time for a job that can only fail would read in ``netkeeper posture``
#: as a job that runs. P4-01 removes this exclusion in the PR that wires the source.
SERVED_SCHEDULES: Final[dict[JobKind, JobSchedule]] = {
    kind: schedule for kind, schedule in DEFAULT_SCHEDULES.items() if kind is not JobKind.INBOX
}


# --- the heat gate: on unless explicitly, namedly disabled (spec 9.7) --------


class HeatSkip(enum.Enum):
    """The one value that turns spec 9.7's heat skip off. A separate type, not
    ``None``, so disabling it is a deliberate, greppable argument rather than a
    default nobody noticed."""

    DISABLED = "disabled"


HEAT_SKIP_DISABLED: Final = HeatSkip.DISABLED

# Config's own ``[linkedin.heat]`` defaults. Production passes the loaded
# ``settings.linkedin.heat`` instead; this keeps a caller that passes nothing
# protected rather than unprotected, which is what spec 9.7 requires.
DEFAULT_HEAT_SETTINGS: Final = LinkedInSettings().heat

HeatGate = HeatSettings | HeatSkip


# --- the arm gate: nothing scheduled fires until a person arms it (P2-10) ----


class Arming(enum.Enum):
    """The one value that turns the arm gate off. For a scheduler that drives no
    real account -- ``netkeeper simulate``'s scratch database, and the
    scheduler's own tests -- never for ``netkeeper serve``."""

    NOT_REQUIRED = "not_required"


ARMING_NOT_REQUIRED: Final = Arming.NOT_REQUIRED

ArmCheck = Callable[[Session, User, int], bool]
ArmGate = ArmCheck | Arming

#: The production gate, and every entry point's default: whether a person has
#: armed the account's scheduled runs (``linkedin_accounts.scheduled_runs_armed_at``).
#: Every account starts disarmed, so a scheduler that nobody armed fires nothing.
DEFAULT_ARM_GATE: Final[ArmCheck] = scheduled_runs_armed


# --- the route-changed breaker gate: connections kinds only (#189 item 1) ----

#: The kinds :mod:`netkeeper.services.route_breaker` governs. Not ``enrich``:
#: spec 9.6 already caps enrichment's own unreadable-profile streak on a
#: different endpoint, and the two stay independent (see that module's
#: docstring). Unlike the heat and arm gates this one has no "disabled"
#: escape hatch -- it is unconditional, the same as the session flag below --
#: because nothing (``netkeeper simulate`` included) should be able to arm a
#: scheduler that skips this check.
_ROUTE_BREAKER_KINDS: Final = frozenset({JobKind.CONNECTIONS_FULL, JobKind.CONNECTIONS_INCREMENTAL})


# --- active hours: delegates to netkeeper.linkedin.pacing, never reimplements it ---


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
    first_setup: bool = False,
) -> tuple[datetime, bool, str]:
    """``(due, is_catchup, reason)`` for a job with no still-valid schedule.

    ``prior_due`` is the previous due time *only* when it was computed under
    the same timing parameters that apply now (see :func:`establish_schedule`);
    pass ``None`` for a job that has never been scheduled, or whose timing
    parameters just changed -- both get a fresh ``now + interval``, never the
    catch-up jitter, because neither one is downtime.

    ``first_setup`` (only meaningful with ``prior_due=None``) is the one
    exception: a kind that runs "on first setup" (spec 9.4's full sync) and
    has never been scheduled is due now, but through the same 5 to 20 minute
    jitter as a catch-up, so a fresh install -- which is also a process start,
    possibly mid-deploy -- never fires the moment it boots. It is not flagged
    ``is_catchup``, because nothing was missed.

    Mirrors igtracker's proven ``_first_fire`` (``services/scheduler.py``,
    cited in ``docs/architecture.md`` section 6 as this project's reason for
    picking APScheduler): no stored due time is a fresh schedule; a stored due
    time still in the future is restored unchanged; a stored due time at or
    before ``now`` was missed while the process was down and gets exactly one
    catch-up fire, 5 to 20 minutes out.
    """
    if prior_due is None:
        if first_setup:
            return now + _catchup_jitter(rng), False, "first setup"
        return now + interval, False, "no stored schedule yet"
    if prior_due > now:
        return prior_due, False, "restored"
    return now + _catchup_jitter(rng), True, "catching up after downtime"


def _catchup_jitter(rng: random.Random) -> timedelta:
    return timedelta(minutes=rng.uniform(CATCHUP_MIN_MINUTES, CATCHUP_MAX_MINUTES))


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
    that produced it, whether that due time is a pending catch-up fire, and
    whether the kind has ever fired (:func:`record_fired` sets it).

    ``resume_due`` is set only while ``due`` is a not-done re-offer
    (:func:`offer_again`, #200): the normal due time the re-offer stood in front of.
    The re-offer's fire goes back to it, and a re-offer is never re-offered."""

    due: datetime
    fingerprint: ScheduleFingerprint
    is_catchup: bool = False
    fired_once: bool = False
    resume_due: datetime | None = None


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
        # A row written before this flag existed counts as fired: the
        # conservative reading, which never adds a run nobody scheduled.
        fired_once=bool(raw.get("fired_once", True)),
        resume_due=(
            datetime.fromisoformat(str(raw["resume_due"]))
            if raw.get("resume_due") is not None
            else None
        ),
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
            "resume_due": None if state.resume_due is None else state.resume_due.isoformat(),
            "fired_once": state.fired_once,
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
    A re-offer's ``resume_due`` (:func:`offer_again`) survives that downtime path, so
    the catch-up fire is still the re-offer: it is never re-offered, and the cadence
    goes back to the normal due time afterwards (#196 item 9).
    """
    _require_writer(session, "scheduler.establish_schedule")
    fingerprint = ScheduleFingerprint.of(
        schedule, tz=tz, active_start=active_start, active_end=active_end
    )
    existing = _load_state(session, user, account_id, kind)
    # First setup is "has never fired", not "has no row": the row is written
    # the moment the schedule is set, and a timing change before the first
    # fire must keep that fire soon rather than push it an interval out (#161).
    fired_once = existing is not None and existing.fired_once
    first_setup = schedule.run_on_first_setup and not fired_once
    if existing is not None and existing.fingerprint == fingerprint:
        if existing.due > now:
            return ScheduleResult(
                due=existing.due, changed=False, is_catchup=False, reason="unchanged"
            )
        prior_due = existing.due  # same parameters, due already passed: downtime
    else:
        prior_due = None  # first time, or the timing parameters themselves changed
    due, is_catchup, reason = compute_due(
        prior_due, now=now, interval=schedule.interval, rng=rng, first_setup=first_setup
    )
    if existing is not None and prior_due is None:
        # compute_due cannot tell "never scheduled" from "retimed"; say which.
        reason = "timing changed before the first fire" if first_setup else "timing changed"
    due = _snap_to_active_hours(
        due,
        tz,
        start=active_start,
        end=active_end,
        respect_active_hours=schedule.respect_active_hours,
    )
    # A re-offer that lapsed in downtime is still a re-offer (#196 item 9): its
    # catch-up fire goes back to the normal due time it stood in front of, and is
    # never re-offered. A timing change starts the schedule over, re-offer and all.
    resume_due = existing.resume_due if existing is not None and prior_due is not None else None
    _store_state(
        session,
        user,
        account_id,
        kind,
        _JobState(
            due=due,
            fingerprint=fingerprint,
            is_catchup=is_catchup,
            fired_once=fired_once,
            resume_due=resume_due,
        ),
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


def seed_missing_kinds(
    session: Session,
    user: User,
    account_id: int,
    *,
    now: datetime,
    schedules: Mapping[JobKind, JobSchedule],
    rng: random.Random,
    tz: str,
    active_start: time = pacing.DEFAULT_ACTIVE_START,
    active_end: time = pacing.DEFAULT_ACTIVE_END,
) -> dict[JobKind, ScheduleResult]:
    """Give every kind in ``schedules`` that has no stored due time its first one (#327).

    The kind's normal first-due rule applies (:func:`establish_schedule` with no row):
    one interval out, or soon for a kind that runs on first setup. A kind that already
    has a due time is left exactly as it is. A new due time that lands within
    :data:`MIN_JOB_KIND_GAP` of any other is pushed later, never the other one.
    Returns the kinds it seeded. Arming calls it, so a kind added after the schedule
    was established gets a due time without waiting for a restart.
    """
    # Every kind's due time, not only those being seeded: the gap is between any two.
    stored = (stored_due(session, user, account_id, kind) for kind in JobKind)
    taken = [due for due in stored if due is not None]
    seeded: dict[JobKind, ScheduleResult] = {}
    for kind, schedule in schedules.items():
        if _load_state(session, user, account_id, kind) is not None:
            continue
        result = establish_schedule(
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
        due = result.due
        while any(abs(due - other) < MIN_JOB_KIND_GAP for other in taken):
            due += MIN_JOB_KIND_GAP
        if due != result.due:
            state = _load_state(session, user, account_id, kind)
            assert state is not None  # just written by establish_schedule above
            _store_state(session, user, account_id, kind, replace(state, due=due))
            result = replace(result, due=due)
        taken.append(due)
        seeded[kind] = result
    return seeded


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
    handler_ran: bool = True,
    now: datetime | None = None,
) -> datetime:
    """Advance ``kind``'s due time to the next cycle after a fire (executed or
    heat-skipped -- either way, this fire happened and the cadence moves on).

    Anchored to ``due`` -- the time this fire *was scheduled for* -- not
    ``now``, so the cadence never drifts with polling latency or how long the
    handler took to run. A fire that ran more than :data:`LATE_FIRE_SLACK` after
    its due time (a machine that slept with the process alive) is anchored to
    ``now`` instead, so the next fire is a whole interval after it ran, never
    minutes after it. The new due time is never a pending catch-up.

    ``handler_ran`` is False for a heat-skipped fire: the cadence moves on,
    but the kind has not yet *run*, so a ``run_on_first_setup`` kind keeps its
    first-setup standing (see :class:`JobSchedule`) -- and, since it still has
    not run, it is offered again :data:`FIRST_SETUP_RETRY` after the later of
    ``due`` and ``now`` rather than a whole interval out.
    """
    _require_writer(session, "scheduler.record_fired")
    state = _load_state(session, user, account_id, kind)
    if state is None:
        raise RuntimeError(
            f"record_fired: no established schedule for account {account_id}/{kind.value}"
        )
    ran = max(due, now or due)
    if not handler_ran and not state.fired_once and schedule.run_on_first_setup:
        next_due = ran + FIRST_SETUP_RETRY
    elif state.resume_due is not None and schedule.interval < NOT_DONE_RETRY:
        # The re-offer logic below needs an interval of at least NOT_DONE_RETRY
        # (#309 re-review, F2), and offer_again never parks a re-offer for a shorter
        # one. A state that carries one anyway is refused that logic: the next fire
        # is a whole interval after this one ran, and never before the due time the
        # re-offer stood in front of.
        log.warning(
            "scheduler: %s for account %d carries a re-offer its %s interval cannot"
            " have; ignoring it",
            kind.value,
            account_id,
            schedule.interval,
        )
        next_due = max(state.resume_due, ran + schedule.interval)
    elif state.resume_due is not None:
        # This fire was a not-done re-offer (#200): the cadence goes back to the
        # normal due time it stood in front of, not a week past the re-offer --
        # only while that is still a day or more away (#309 review). A re-offer
        # that fired late, after a restart's catch-up or a sleep, would otherwise
        # put the normal fire minutes after it; the next fire is instead a whole
        # interval after this one actually ran.
        if state.resume_due >= ran + NOT_DONE_RETRY:
            next_due = state.resume_due
        else:
            next_due = ran + schedule.interval
    elif ran - due > LATE_FIRE_SLACK:
        # A fire that ran late (#309 re-review, F1): a whole interval after it ran,
        # which is max(due + interval, now + interval), never sooner.
        next_due = ran + schedule.interval
    else:
        next_due = due + schedule.interval
    next_due = _snap_to_active_hours(
        next_due,
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
        replace(
            state,
            due=next_due,
            is_catchup=False,
            fired_once=state.fired_once or handler_ran,
            resume_due=None,
        ),
    )
    return next_due


def _record_skipped(
    session_factory: sessionmaker[Session],
    user: User,
    account_id: int,
    kind: JobKind,
    *,
    fired_before: bool,
    due: datetime,
    now: datetime,
    schedule: JobSchedule,
    tz: str,
    active_start: time,
    active_end: time,
) -> datetime:
    """Undo what claiming a fire assumed when its handler started nothing (#324).

    The claim advanced the due time as if the fire ran. A fire that did not run
    must not spend a first-setup kind's standing: it is put back, and the kind is
    offered again :data:`FIRST_SETUP_RETRY` after the later of ``due`` and ``now``,
    exactly as :func:`record_fired` treats a fire the gate skipped. Any other kind's
    cadence already moved on the same way a skip moves it, and is left alone.
    """
    with session_scope(session_factory, write=True) as session:
        state = _load_state(session, user, account_id, kind)
        if state is None:
            raise RuntimeError(
                f"_record_skipped: no established schedule for account {account_id}/{kind.value}"
            )
        if fired_before or not schedule.run_on_first_setup:
            return state.due
        retry = _snap_to_active_hours(
            max(due, now) + FIRST_SETUP_RETRY,
            tz,
            start=active_start,
            end=active_end,
            respect_active_hours=schedule.respect_active_hours,
        )
        _store_state(session, user, account_id, kind, replace(state, due=retry, fired_once=False))
    return retry


def _defer_as_catchup(
    session: Session,
    user: User,
    account_id: int,
    kind: JobKind,
    *,
    state: _JobState,
    now: datetime,
    schedule: JobSchedule,
    rng: random.Random,
    tz: str,
    active_start: time,
    active_end: time,
) -> datetime:
    """Reschedule a due time that lapsed by a whole interval as a single catch-up
    fire, 5 to 20 minutes out, and persist it. The hot path's half of the rule
    :func:`compute_due` already implements for the cold path -- same function,
    same bounds, same ``is_catchup`` flag -- so a lapse means one fire whether
    the process restarted or simply stopped being polled."""
    _require_writer(session, "scheduler._defer_as_catchup")
    due, _, _ = compute_due(state.due, now=now, interval=schedule.interval, rng=rng)
    due = _snap_to_active_hours(
        due,
        tz,
        start=active_start,
        end=active_end,
        respect_active_hours=schedule.respect_active_hours,
    )
    _store_state(session, user, account_id, kind, replace(state, due=due, is_catchup=True))
    return due


async def poll_and_fire(
    session_factory: sessionmaker[Session],
    user: User,
    account_id: int,
    kind: JobKind,
    *,
    now: datetime,
    schedule: JobSchedule,
    registry: JobRegistry,
    heat_settings: HeatGate = DEFAULT_HEAT_SETTINGS,
    armed: ArmGate = DEFAULT_ARM_GATE,
    tz: str,
    active_start: time = pacing.DEFAULT_ACTIVE_START,
    active_end: time = pacing.DEFAULT_ACTIVE_END,
    rng: random.Random | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FireResult | None:
    """One heartbeat's check for ``(account_id, kind)``: fire if due, else no-op.

    Returns ``None`` when the job is not due yet, when it has never been
    established (:func:`establish_schedule`/:func:`sync_account_schedule` must
    run first -- this function never derives a schedule from nothing), and
    when the due time lapsed by a whole interval or more: that is downtime the
    cold path never got to see (see the module docstring, "the lapse the cold
    path cannot see"), so it is rescheduled once through :func:`compute_due`'s
    catch-up branch -- 5 to 20 minutes out, exactly one fire -- instead of
    replaying one fire per missed interval. ``rng`` supplies that jitter and
    defaults to a fresh :class:`random.Random`; pass a seeded one to replay it.

    While the account's scheduled runs are disarmed (``armed``, P2-10; every
    account starts disarmed) the handler is not called either, and the fire
    is skipped as ``"disarmed"``; while the session flag is set (a checkpoint
    or a login wall, spec 9.7) it is skipped as ``"session_flagged"``. Above
    the configured heat threshold (spec 9.7) the browser job is skipped as
    ``"heat"``. For a connections kind (``CONNECTIONS_FULL``,
    ``CONNECTIONS_INCREMENTAL``) whose route-changed breaker is tripped
    (:mod:`netkeeper.services.route_breaker`, #189 item 1 -- two connections
    runs in a row ended ``route_changed``) it is skipped as
    ``"route_changed_breaker"``, and one whose answer-lost limit is tripped
    (#199 -- three runs of one connections kind in a row ended ``answer_lost``) as
    ``"answer_lost_breaker"``; those checks are unconditional, with no
    "disabled" escape hatch. While a person has paused the account's schedule
    (#324, ``linkedin_accounts.schedule_paused``; no escape hatch either) the
    fire is skipped as ``"paused"``, armed or not. Either way the cadence still
    advances, so the scheduler does not spin retrying the same fire on every
    heartbeat, and a skipped fire has not *run*: a ``run_on_first_setup`` kind
    keeps its first-setup standing and is offered again
    :data:`FIRST_SETUP_RETRY` later. The heat skip is on unless
    ``heat_settings`` is :data:`HEAT_SKIP_DISABLED`; the arm gate is on unless
    ``armed`` is :data:`ARMING_NOT_REQUIRED`.

    A handler that answers :attr:`JobOutcome.RETRY_LATER` (the browser was
    unreachable or busy) has one retry parked :data:`RETRY_MIN_MINUTES` to
    :data:`RETRY_MAX_MINUTES` after ``clock()`` (spec 9.9), unless the next
    due time is already sooner. One that answers :attr:`JobOutcome.NOT_DONE` (#200)
    is offered again :data:`NOT_DONE_RETRY` after ``clock()``, the same way.
    One that answers :attr:`JobOutcome.PAUSED_AFTER_GATE` or
    :attr:`JobOutcome.DISARMED_AFTER_GATE` (#324: it found the schedule paused, or
    the account disarmed, after this gate passed, and started nothing) has its fire
    counted as skipped, not run: the result is not ``fired``, its
    ``skipped_reason`` is the outcome's value, and a first-setup kind that had never
    run keeps that standing and is offered again :data:`FIRST_SETUP_RETRY` after
    the later of its due time and ``now``. ``clock`` defaults to returning ``now``.

    The writer session that reads and advances the due time is closed *before*
    the (possibly long-running) handler is awaited, so a real job never holds
    the SQLite write lock for its duration.
    """

    def claim() -> tuple[datetime, bool, bool, bool, str | None, datetime] | None:
        # The one writer session that reads and advances the due time, run whole
        # off the event loop (#259); it commits before the handler is awaited.
        with session_scope(session_factory, write=True) as session:
            state = _load_state(session, user, account_id, kind)
            if state is None or state.due > now:
                return None
            if now - state.due >= schedule.interval:
                deferred = _defer_as_catchup(
                    session,
                    user,
                    account_id,
                    kind,
                    state=state,
                    now=now,
                    schedule=schedule,
                    rng=rng if rng is not None else random.Random(),  # noqa: S311 -- jitter
                    tz=tz,
                    active_start=active_start,
                    active_end=active_end,
                )
                log.warning(
                    "scheduler: %s for account %d lapsed %s past its due time; "
                    "catching up once at %s instead of replaying every interval",
                    kind.value,
                    account_id,
                    now - state.due,
                    deferred.isoformat(),
                )
                return None
            due = state.due
            is_catchup = state.is_catchup
            was_reoffer = state.resume_due is not None
            fired_before = state.fired_once
            skipped_reason: str | None = None
            if not isinstance(armed, Arming) and not armed(session, user, account_id):
                skipped_reason = "disarmed"
            elif schedule_paused(session, user, account_id):
                # A person paused the schedule (#324): skipped like a disarmed fire,
                # so the cadence moves on and unpausing owes no backlog of runs.
                skipped_reason = "paused"
            elif session_flag(session, user) is not None:
                # A checkpoint or a login wall: the run would refuse anyway (spec 9.7),
                # so no run is recorded and nothing is attached (#175 review, F3).
                skipped_reason = "session_flagged"
            elif isinstance(heat_settings, HeatSettings) and heat_service.should_skip(
                session, user, account_id, now=now, settings=heat_settings
            ):
                skipped_reason = "heat"
            elif kind in _ROUTE_BREAKER_KINDS and route_breaker.tripped(session, user, account_id):
                # Two connections runs in a row ended route_changed (#189 item 1): a wall
                # served in place raises no heat and sets no flag, so this is what stops
                # a scheduled sync from loading it again at every interval.
                skipped_reason = "route_changed_breaker"
            elif kind in _ROUTE_BREAKER_KINDS and route_breaker.answer_lost_tripped(
                session, user, account_id
            ):
                # Three runs of one connections kind in a row ended answer_lost (#199):
                # the page's answers keep arriving unreadable, which moves neither heat
                # nor the route-changed breaker, so this stops scheduled runs spending
                # page views.
                skipped_reason = "answer_lost_breaker"
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
                handler_ran=skipped_reason is None,
                now=now,
            )
        return due, is_catchup, was_reoffer, fired_before, skipped_reason, next_due

    claimed = await off_loop(claim)
    if claimed is None:
        return None
    due, is_catchup, was_reoffer, fired_before, skipped_reason, next_due = claimed
    fired = skipped_reason is None
    if fired:
        handler = registry[kind]
        ctx = JobContext(
            user_id=user.id, account_id=account_id, kind=kind, due=due, catch_up=is_catchup
        )
        outcome = await handler(ctx)
        if outcome in SKIPPED_AFTER_GATE:
            # The handler started nothing (#324): the fire was skipped, not run.
            assert outcome is not None
            fired = False
            skipped_reason = outcome.value
            next_due = await off_loop(
                _record_skipped,
                session_factory,
                user,
                account_id,
                kind,
                fired_before=fired_before,
                due=due,
                now=now,
                schedule=schedule,
                tz=tz,
                active_start=active_start,
                active_end=active_end,
            )
        elif outcome is JobOutcome.NOT_DONE and was_reoffer:
            # One re-offer per interval (#201 review, M1): a re-offer that was not
            # done either goes back to the normal cadence, so a loss rate that never
            # clears -- or a wall that looks like unreadable bodies -- cannot turn the
            # weekly full sync into a daily one.
            log.info(
                "scheduler: %s for account %d was not done on its re-offer either;"
                " back to the normal interval",
                kind.value,
                account_id,
            )
        elif outcome is JobOutcome.NOT_DONE:
            next_due = await off_loop(
                offer_again,
                session_factory,
                user,
                account_id,
                kind,
                now=clock() if clock is not None else now,
                schedule=schedule,
                rng=rng if rng is not None else random.Random(),  # noqa: S311 -- jitter
                tz=tz,
                active_start=active_start,
                active_end=active_end,
            )
        elif outcome is JobOutcome.RETRY_LATER:
            next_due = await off_loop(
                park_retry,
                session_factory,
                user,
                account_id,
                kind,
                reoffer=was_reoffer,
                now=clock() if clock is not None else now,
                schedule=schedule,
                rng=rng if rng is not None else random.Random(),  # noqa: S311 -- jitter
                tz=tz,
                active_start=active_start,
                active_end=active_end,
            )
    elif skipped_reason in ("disarmed", "paused"):
        # Every poll of a disarmed or paused account lands here; it is the expected state.
        log.debug(
            "scheduler: %s for account %d not fired: %s", kind.value, account_id, skipped_reason
        )
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
    heat_settings: HeatGate = DEFAULT_HEAT_SETTINGS,
    armed: ArmGate = DEFAULT_ARM_GATE,
    active_start: time = pacing.DEFAULT_ACTIVE_START,
    active_end: time = pacing.DEFAULT_ACTIVE_END,
    rng: random.Random | None = None,
    clock: Callable[[], datetime] | None = None,
) -> list[FireResult]:
    """:func:`poll_and_fire` every ``(user, account_id)`` in ``accounts`` across
    every kind in ``schedules``. The body of the heartbeat job in
    :func:`build_scheduler`.

    Enforces spec 9.5's interleave gap against the fire it just performed, not
    against ``now``: once a kind has fired in this poll, every other kind that
    was also due has its stored due time pushed :data:`MIN_JOB_KIND_GAP` past
    that fire and runs on a later poll. Staggering the *due times* against each
    other cannot do this job here -- two times that are both already in the
    past stay in the past when nudged two minutes, and both fire in the same
    call (see the module docstring, "the interleave gap"). Nothing is skipped;
    at most one kind per account is delayed by the gap.
    """
    fired: list[FireResult] = []
    for user, account_id in accounts:
        tz = user.timezone
        due_now = await off_loop(_due_now, session_factory, user, account_id, schedules, now)
        last_fired_at: datetime | None = None
        for kind in sorted(due_now, key=lambda k: (due_now[k], k.value)):
            if last_fired_at is not None and now - last_fired_at < MIN_JOB_KIND_GAP:
                await off_loop(
                    _defer_past_the_gap,
                    session_factory,
                    user,
                    account_id,
                    kind,
                    after=last_fired_at,
                )
                continue
            result = await poll_and_fire(
                session_factory,
                user,
                account_id,
                kind,
                now=now,
                schedule=schedules[kind],
                registry=registry,
                heat_settings=heat_settings,
                armed=armed,
                tz=tz,
                active_start=active_start,
                active_end=active_end,
                rng=rng,
                clock=clock,
            )
            if result is None:
                continue
            fired.append(result)
            if result.fired:
                last_fired_at = now
    return fired


def _due_now(
    session_factory: sessionmaker[Session],
    user: User,
    account_id: int,
    schedules: Mapping[JobKind, JobSchedule],
    now: datetime,
) -> dict[JobKind, datetime]:
    """Every kind in ``schedules`` whose stored due time has arrived, in one read session."""
    with session_scope(session_factory) as session:
        return {
            kind: state.due
            for kind in schedules
            if (state := _load_state(session, user, account_id, kind)) is not None
            and state.due <= now
        }


def _defer_past_the_gap(
    session_factory: sessionmaker[Session],
    user: User,
    account_id: int,
    kind: JobKind,
    *,
    after: datetime,
) -> None:
    """Push ``kind``'s stored due time to a full :data:`MIN_JOB_KIND_GAP` past the
    fire at ``after``, so the next poll runs it instead of this one running it in
    the same minute. Leaves a due time that is already clear of the gap alone,
    and never moves one earlier."""
    target = after + MIN_JOB_KIND_GAP
    with session_scope(session_factory, write=True) as session:
        state = _load_state(session, user, account_id, kind)
        if state is None or state.due >= target:
            return
        _store_state(session, user, account_id, kind, replace(state, due=target))


def park_retry(
    session_factory: sessionmaker[Session],
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
    reoffer: bool = False,
) -> datetime:
    """Park one retry of ``kind`` :data:`RETRY_MIN_MINUTES` to :data:`RETRY_MAX_MINUTES`
    after ``now`` (spec 9.9: the browser went away), and return the due time now stored.

    A stored due time that is already sooner is kept: a retry never pushes a
    fire later. The parked time goes through active hours like every other due
    time, and is not a catch-up.

    ``reoffer`` is true when the fire that could not reach the browser was a
    re-offer (:func:`offer_again`). :func:`record_fired` has already cleared its
    ``resume_due``, so the retry puts it back, as the due time the fire just
    stored: the retry is still the re-offer, never re-offered, and its fire goes
    back to the normal cadence (#196 item 9).
    """
    retry = now + timedelta(minutes=rng.uniform(RETRY_MIN_MINUTES, RETRY_MAX_MINUTES))
    retry = _snap_to_active_hours(
        retry,
        tz,
        start=active_start,
        end=active_end,
        respect_active_hours=schedule.respect_active_hours,
    )
    with session_scope(session_factory, write=True) as session:
        state = _load_state(session, user, account_id, kind)
        if state is None:
            raise RuntimeError(
                f"park_retry: no established schedule for account {account_id}/{kind.value}"
            )
        if state.due <= retry:
            return state.due
        _store_state(
            session,
            user,
            account_id,
            kind,
            replace(
                state,
                due=retry,
                is_catchup=False,
                resume_due=state.due if reoffer else state.resume_due,
            ),
        )
    log.info(
        "scheduler: %s for account %d could not reach the browser; retrying at %s",
        kind.value,
        account_id,
        retry.isoformat(),
    )
    return retry


def offer_again(
    session_factory: sessionmaker[Session],
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
) -> datetime:
    """Offer ``kind`` again :data:`NOT_DONE_RETRY` after ``now``, plus up to
    :data:`NOT_DONE_JITTER` of jitter: its fire ran but was not done (#200). Returns
    the due time now stored.

    A stored due time that is already sooner is kept, as :func:`park_retry` keeps
    one: this never pushes a fire later. The time goes through active hours and is
    not a catch-up. The normal due time it stands in front of is kept as the state's
    ``resume_due``: the re-offer's fire goes back to it, and :func:`poll_and_fire`
    never re-offers a re-offer, so there is at most one per interval.
    """
    jitter = timedelta(seconds=rng.uniform(0.0, NOT_DONE_JITTER.total_seconds()))
    again = _snap_to_active_hours(
        now + NOT_DONE_RETRY + jitter,
        tz,
        start=active_start,
        end=active_end,
        respect_active_hours=schedule.respect_active_hours,
    )
    with session_scope(session_factory, write=True) as session:
        state = _load_state(session, user, account_id, kind)
        if state is None:
            raise RuntimeError(
                f"offer_again: no established schedule for account {account_id}/{kind.value}"
            )
        if state.due <= again:
            return state.due
        _store_state(
            session,
            user,
            account_id,
            kind,
            replace(state, due=again, is_catchup=False, resume_due=state.due),
        )
    log.info(
        "scheduler: %s for account %d ran but was not done; offering it again at %s",
        kind.value,
        account_id,
        again.isoformat(),
    )
    return again


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
    heat_settings: HeatGate = DEFAULT_HEAT_SETTINGS,
    armed: ArmGate = DEFAULT_ARM_GATE,
    rng: random.Random | None = None,
    clock: Callable[[], datetime] = utcnow,
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

    ``heat_settings`` defaults to :data:`DEFAULT_HEAT_SETTINGS`, so a caller
    that wires up the scheduler and passes nothing still gets spec 9.7's skip;
    production passes its loaded ``settings.linkedin.heat``, and only
    :data:`HEAT_SKIP_DISABLED` turns it off. ``armed`` defaults to
    :data:`DEFAULT_ARM_GATE` the same way: a scheduler built with no word
    about arming fires nothing on an account nobody armed.

    ``clock`` is read for every "now" -- the establishing pass and every
    heartbeat -- so a test can drive the real heartbeat across a due time.
    """
    registry = dict(registry) if registry is not None else default_registry()
    schedules = dict(schedules) if schedules is not None else dict(DEFAULT_SCHEDULES)
    jitter = rng if rng is not None else random.Random()  # noqa: S311 -- pacing jitter, not crypto
    scheduler = AsyncIOScheduler()

    for user, account_id in accounts():
        with session_scope(session_factory, write=True) as session:
            sync_account_schedule(
                session,
                user,
                account_id,
                now=clock(),
                schedules=schedules,
                rng=jitter,
                tz=user.timezone,
                active_start=active_start,
                active_end=active_end,
            )

    async def _heartbeat() -> None:
        # The accounts are read off the event loop too (#259); poll_once runs each
        # of its sessions there.
        await poll_once(
            session_factory,
            await off_loop(lambda: list(accounts())),
            now=clock(),
            registry=registry,
            schedules=schedules,
            heat_settings=heat_settings,
            armed=armed,
            active_start=active_start,
            active_end=active_end,
            rng=jitter,
            clock=clock,
        )

    scheduler.add_job(
        _heartbeat,
        IntervalTrigger(seconds=heartbeat_interval.total_seconds()),
        id=HEARTBEAT_JOB_ID,
        coalesce=True,
        max_instances=1,
    )
    return scheduler
