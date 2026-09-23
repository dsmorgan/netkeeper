"""Every protection the LinkedIn extractor has, in one report (P2-11, CP3).

Spec section 9 spreads the extractor's safety across five modules: attach-only
browsing (9.1, ADR 0002), human-like pacing and active hours and the warm-up
ramp (9.5), per-day and per-week budgets with hard maxima (9.6), heat with its
skip threshold (9.7), and the session flag a checkpoint or a login wall raises
(9.7). Each is tested on its own. None of them answers the question somebody
actually asks before pointing this at their own LinkedIn account, which is
"what is protecting me, and is any of it off right now?"

This module answers exactly that, and it is built around one property:

    **A protection that is off, bypassed, or configured past spec 9.6's hard
    maximum always produces a warning.**

A report that can say "all clear" while something is disabled is worse than no
report, because it converts an unchecked assumption into a reassurance. That
property is structural here, not a convention: :class:`Protection` refuses to
exist with a status other than :attr:`Status.ON` and no warning to go with it,
and ``tests/test_posture.py`` turns each protection off in turn and asserts
that that one warns.

**Why this lives under ``services/`` and not under ``linkedin/``.** Budget
counters and heat are rows in ``settings_kv``, so reading them needs a session
and a ``User``; the extractor boundary (ADR 0005, spec 9.10) forbids both on
the ``linkedin/`` side of the line. The same seam already puts
``services.budgets`` and ``services.heat`` here. This module reads through
those two and through ``services.linkedin_session``, and calls the pure math in
``linkedin.pacing`` and ``linkedin.heat`` directly rather than restating any of
it -- a posture report that recomputed the warm-up ramp its own way could agree
with itself while disagreeing with what a run will actually do.

**What it does not import, on purpose.** Nothing here reaches
``netkeeper.linkedin.browser`` or ``netkeeper.linkedin.preflight``: a module
under ``services/`` that imports the provider is how browser work ends up
inside a request handler, and ``tests/test_browser_safety.py`` fails the build
for it. What a browser probe found therefore arrives as a
:class:`SessionProbe`, a plain value the caller builds (``netkeeper posture``
builds one from ``netkeeper.linkedin.preflight.preflight``). The probe carries
cookie *names* and never a cookie value, the same rule preflight itself keeps
(spec 9.1, CLAUDE.md): a posture report must not print what preflight was
careful not to read.

**Read-only.** Every call this makes -- :func:`netkeeper.services.budgets.status`,
:func:`netkeeper.services.heat.read`, :func:`netkeeper.services.linkedin_session.session_flag`
-- is a read, so :func:`posture` needs no writer session and takes no write
lock. Nothing about summarizing the protections should be able to change one.
``now`` is a parameter rather than a clock read, so the whole report is
testable at any instant: the weekend multiplier on a Saturday, a warm-up ramp
on day 0, an active window from outside it.
"""

from __future__ import annotations

import enum
import math
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from netkeeper.config import HeatSettings, LinkedInSettings, PacingSettings, Settings
from netkeeper.linkedin import activity_lock
from netkeeper.linkedin import heat as heat_math
from netkeeper.linkedin.pacing import (
    apply_weekend_multiplier,
    is_active_at,
    local_time_of,
    next_window_start,
    warmup_budget,
)
from netkeeper.models import User
from netkeeper.services import heat as heat_rows
from netkeeper.services.budgets import (
    HARD_MAX_PER_DAY,
    HARD_MAX_PER_WEEK,
    ActionClass,
    BudgetSnapshot,
    configured_default,
)
from netkeeper.services.budgets import status as budget_status
from netkeeper.services.linkedin_session import session_flag
from netkeeper.services.scheduler import (
    CATCHUP_MAX_MINUTES,
    CATCHUP_MIN_MINUTES,
    DEFAULT_SCHEDULES,
    HEAT_SKIP_DISABLED,
    MIN_JOB_KIND_GAP,
    HeatGate,
    HeatSkip,
    stored_due,
)

# --- the thresholds this module judges by -----------------------------------
# Spec 9.6's hard maxima and Appendix C's defaults live in the modules that
# enforce them; these are the extra lines *this* module draws, for "configured
# so far out that the protection stops protecting". Each is deliberately
# conservative, each is justified below, and each is pinned to its literal
# value by a test -- a ceiling that only ever gets compared against itself
# would let a bad merge move it and stay green (CLAUDE.md).

#: The only browser mode there is (ADR 0002). Spelled out here rather than
#: imported from ``linkedin.browser``, which ``services/`` may not import; the
#: caller passes its provider's ``mode`` and this compares the two, so a
#: provider that grew a second mode is caught by the comparison failing.
ATTACH_ONLY: Final = "attach"

#: The account id every caller used before ``linkedin_accounts`` existed (P2-06),
#: and the id the first user's account row gets. ``netkeeper posture`` now reads
#: the user's row (``services.linkedin_accounts.account_id_for``) and falls back
#: to this only for a user with no row yet; ``netkeeper simulate`` keeps it for
#: its scratch database. ``linkedin.activity_lock.SINGLE_ACCOUNT_KEY`` is still
#: the lock's key until the browser side keys it by the row too.
SINGLE_ACCOUNT_ID: Final = 1

#: Hosts a CDP url may point at. Chrome's debug port is only ever on the
#: machine's own loopback (spec 9.1); a url naming anything else is either a
#: typo or a browser on another machine, and neither is the device LinkedIn is
#: meant to see.
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})

#: How many consecutive blocks may be needed to trip the skip threshold before
#: the threshold stops being a brake. Spec 9.7 aborts a *run* after two
#: consecutive throttled units; a threshold that needs more than five blocks at
#: the configured ``per_block`` would let a bad day keep going all day. The
#: Appendix C defaults (``per_block`` 1.0, ``skip_threshold`` 2.5) need three.
MAX_BLOCKS_BEFORE_SKIP: Final = 5

#: Below this, heat decays away faster than the cooldown it is meant to impose.
#: Appendix C's default half-life is 6 hours, chosen so "one throttle stretches
#: the rest of the day"; under an hour, a throttle is forgotten before the next
#: burst starts.
MIN_HALF_LIFE_HOURS: Final = 1.0

#: An active window longer than this has stopped being a window. Appendix B's
#: default is 08:30 to 21:30, which is 13 hours; 16 leaves room for someone who
#: genuinely keeps long hours while still catching a window dialed open to 22.
MAX_ACTIVE_WINDOW_HOURS: Final = 16.0

#: Spec 9.5 damps weekend budgets by multiplying them. At 1.0 the damping does
#: nothing; above it, the "damping" raises weekend budgets above weekday ones.
WEEKEND_DAMPING_CEILING: Final = 1.0

#: The shortest median gap between profile views this report will call
#: human-like. Appendix C picks 25 seconds because that is how long a person
#: takes to read a profile; five is already a skim nobody performs sixty times
#: in a row, and below it the lognormal's own spread puts a large share of
#: waits under a second.
MIN_DELAY_MEDIAN_S: Final = 5.0

#: The longest a netkeeper run can plausibly hold the browser. Spec 9.6's hard
#: maximum of profile visits at Appendix C's pacing -- 25-second medians, bursts
#: of 8-15 with 5-20 minute breaks -- is about two hours of wall clock; four
#: leaves room for a slow day. A lock held longer is a stuck run, and a stuck run
#: silently answers ``busy`` to every job and every preflight.
MAX_PLAUSIBLE_HOLD_HOURS: Final = 4.0

#: The shortest between-burst break that is still a break. Appendix C's range
#: is 5 to 20 minutes ("sessions, not streams"); a break measured in seconds
#: leaves one unbroken stream of requests, which is the shape bursts exist to
#: avoid.
MIN_BURST_BREAK_S: Final = 60.0


class Status(enum.StrEnum):
    """Whether one protection is in force.

    ``ON`` may still carry warnings -- a budget clamped down from a configured
    value above spec 9.6's hard max is working exactly as designed and is still
    worth saying out loud, and a heat score over its skip threshold is the
    protection *firing*, not failing. ``OFF`` and ``UNKNOWN`` may never be
    silent; :class:`Protection` enforces that.
    """

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Protection:
    """One protection: what it is, whether it is in force, and anything wrong with it."""

    name: str
    status: Status
    value: str
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status is not Status.ON and not self.warnings:
            raise ValueError(
                f"protection {self.name!r} is {self.status.value} with no warning; a"
                " report that can read as all-clear while a protection is off is worse"
                " than no report"
            )


@dataclass(frozen=True, slots=True)
class SessionProbe:
    """What a browser probe found, reduced to what a report may say about it.

    The caller builds this from
    :func:`netkeeper.linkedin.preflight.preflight` -- this module may not
    import that one (see the module docstring), and the narrow shape is the
    point rather than a workaround: there is no field here a cookie value, a
    token, or a message body could be put in, so no future edit to the renderer
    can leak one. ``logged_in`` is ``None`` when the cookie jar could not be
    read at all, which is different from reading it and finding no session.
    """

    attached: bool
    logged_in: bool | None
    browser_version: str = ""
    cookie_names: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HeatPosture:
    """Heat as spec 9.7 says the Settings page shows it: level, last raised, when runs resume.

    ``readable`` is false when the configured half-life is not a positive
    number, which is the one case where the decay math has no answer at all
    (:func:`netkeeper.linkedin.heat.decayed_score` raises rather than divide by
    it). The score, the multiplier, and the skip verdict are then all placeholders
    -- a posture report is the last thing that should crash on a bad config,
    since a bad config is exactly what it exists to tell you about.
    """

    score: float
    threshold: float
    multiplier: float
    tripped: bool
    last_raised_at: datetime | None
    resumes_at: datetime | None
    readable: bool = True
    #: When the score was last manually cleared (spec 9.7's "a manual clear
    #: exists for the case where the block was something else"). A clear writes
    #: a state like a raise does, so reading the stored timestamp without
    #: looking at the score reports a clear as the last raise -- which reads as
    #: "0.00, last raised five minutes ago" and sends someone hunting a
    #: throttle that did not happen.
    cleared_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SchedulerPosture:
    """What the scheduler (spec 9.4, 9.5, 9.7) is set to do, and when it next will.

    The cadences, the catch-up window, and the interleave gap are facts about
    the scheduler rather than knobs a config file can turn off, so they are
    reported here rather than as :class:`Protection` rows -- a "protection"
    nothing can disable is a row that can never warn, and rows that can never
    warn are how a report drifts into decoration. The two things that *can* go
    wrong -- the heat skip gate being disabled, and a schedule that is not
    established -- are protections, above.
    """

    heat_skip: bool
    catchup_minutes: tuple[float, float]
    job_kind_gap_minutes: float
    #: ``(kind, interval hours, next due or None)``, in :data:`DEFAULT_SCHEDULES` order.
    jobs: tuple[tuple[str, float, datetime | None], ...]

    @property
    def scheduled(self) -> tuple[str, ...]:
        return tuple(kind for kind, _, due in self.jobs if due is not None)

    @property
    def unscheduled(self) -> tuple[str, ...]:
        return tuple(kind for kind, _, due in self.jobs if due is None)


@dataclass(frozen=True, slots=True)
class TodaysBudget:
    """Today's profile-visit budget, and each protection's bite out of it, in order.

    ``ramp`` is the warm-up ramp's number for today (spec 9.5), ``after_weekend``
    is that damped on a Saturday or Sunday, and ``after_heat`` is that shrunk by
    the current cooldown multiplier (spec 9.7). ``spent`` is what the counter
    already records for today. Showing the chain rather than only the final
    number is what makes "60 is now 25" explicable instead of alarming.
    """

    ramp: int
    after_weekend: int
    after_heat: int
    spent: int

    @property
    def remaining(self) -> int:
        return max(self.after_heat - self.spent, 0)


@dataclass(frozen=True, slots=True)
class PostureReport:
    """Every protection, the numbers behind the ones that carry numbers, and the gaps.

    ``gaps`` are the things this report cannot see rather than things that are
    wrong -- a known limit of the tool, stated so nobody reads a clean report as
    covering more than it does. They deliberately do not make :attr:`ok` false:
    a warning is something to act on, a gap is something to know.
    """

    checked_at: datetime
    timezone: str
    local_time: datetime
    protections: tuple[Protection, ...]
    heat: HeatPosture
    scheduler: SchedulerPosture
    today: TodaysBudget
    gaps: tuple[str, ...] = ()

    @property
    def warnings(self) -> tuple[str, ...]:
        """Every protection's warnings, each prefixed with the protection it came from."""
        return tuple(
            f"{protection.name}: {warning}"
            for protection in self.protections
            for warning in protection.warnings
        )

    @property
    def disabled(self) -> tuple[Protection, ...]:
        """The protections that are not in force, whether off or unknown."""
        return tuple(
            protection for protection in self.protections if protection.status is not Status.ON
        )

    @property
    def ok(self) -> bool:
        """True only when nothing warned. Any protection that is off makes this false."""
        return not self.warnings


def posture(
    session: Session,
    user: User,
    account_id: int,
    *,
    now: datetime,
    settings: Settings,
    browser_mode: str = ATTACH_ONLY,
    probe: SessionProbe | None = None,
    heat_gate: HeatGate | None = None,
    lock: activity_lock.LockState | None = None,
) -> PostureReport:
    """Summarize every protection the extractor has, as of ``now``.

    ``browser_mode`` is the provider's own ``mode`` attribute, passed in rather
    than imported (see the module docstring). ``probe`` is what a browser probe
    found, or ``None`` when none was run -- which is reported as an unknown
    rather than quietly assumed to be fine.

    ``heat_gate`` is the gate the scheduler is actually being run with
    (:mod:`netkeeper.services.scheduler`'s ``heat_settings`` argument). Spec
    9.7's skip is unconditional, so the scheduler makes turning it off take the
    named :data:`~netkeeper.services.scheduler.HEAT_SKIP_DISABLED`; a caller
    that has done so gets a warning here, which is the whole reason this
    parameter exists rather than the report reading the config and assuming.
    ``None`` means "whatever ``settings`` says", which is what a scheduler
    started from this config would use.

    ``lock`` is the account's activity lock as the caller inspected it; ``None``
    inspects :data:`~netkeeper.linkedin.activity_lock.SINGLE_ACCOUNT_KEY` here. The
    inspection peeks with a shared file lock it drops at once, and creates nothing.

    Read-only: a plain ``session_scope(factory)`` is enough and a writer is not
    needed.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    linkedin = settings.linkedin
    zone, zone_warning = _zone_of(linkedin.timezone)
    local_now = local_time_of(now, zone)

    gate = settings.linkedin.heat if heat_gate is None else heat_gate
    effective_heat = settings.linkedin.heat if isinstance(gate, HeatSkip) else gate
    heat_posture = _heat_posture(session, user, account_id, now=now, heat_settings=effective_heat)
    scheduler = _scheduler_posture(session, user, account_id, gate=gate)
    budgets = {
        action: budget_status(session, user, account_id, action, now=now, settings=linkedin.budget)
        for action in ActionClass
    }
    today = _todays_budget(
        user,
        settings=settings,
        local_now=local_now,
        multiplier=heat_posture.multiplier,
        spent=budgets[ActionClass.PROFILE_VISITS].day.count,
    )

    protections: list[Protection] = [
        _browser_mode(browser_mode, linkedin.cdp_url),
        _activity_lock(lock, now=now),
        _linkedin_session(probe),
        _session_flag(session, user),
        _active_hours(linkedin, zone, zone_warning, now=now, local_now=local_now),
        _account_timezone(user, linkedin.timezone),
        _weekend_damping(linkedin.weekend_multiplier, local_now),
        _pacing(linkedin.pacing, _profile_visit_cap(settings)),
        _warmup_ramp(settings, today),
        _auto_send(settings),
        *(_budget(action, budgets[action], settings) for action in ActionClass),
        _heat(heat_posture, effective_heat),
        _heat_skip_gate(gate),
        _scheduled_jobs(scheduler),
    ]
    return PostureReport(
        checked_at=now,
        timezone=linkedin.timezone,
        local_time=local_now,
        protections=tuple(protections),
        heat=heat_posture,
        scheduler=scheduler,
        today=today,
        gaps=GAPS,
    )


_CONSUME: Final = "netkeeper.services.budgets.consume"
_ACTION_CLASS: Final = "netkeeper.services.budgets.ActionClass"

#: What this report cannot see yet. Each is a real limit of the tool as it
#: stands, not a warning about the configuration, and each names what would
#: close it. They are listed rather than silently omitted because a reader who
#: is deciding whether to trust this tool is entitled to know where the report
#: stops.
#: Which function has to be *called* for each protection to actually bite, and
#: which protections depend on it. A posture report reads configuration and
#: counters; it cannot see whether anything calls the enforcement. So the fact
#: that, say, nothing yet calls ``budgets.consume`` is recorded here rather
#: than left for a reader to discover after their first live run.
#:
#: The keys are fully qualified function names, which is what
#: ``tests/test_posture.py`` resolves every reference in the package to,
#: through its imports: ``budgets.consume(...)``, ``consume(...)``,
#: ``spend(...)`` after ``import consume as spend``, and ``consume`` handed
#: over as a callback all count, and an unrelated ``queue.consume()`` does
#: not (#162). That test is what keeps
#: :data:`UNENFORCED_TODAY` honest: when P2-06's enrichment job lands and
#: calls ``consume``, the scan sees it and the test fails until this list is
#: shortened. The list is therefore derived from the code, on a schedule of
#: "every test run", rather than being prose that quietly rots.
#:
#: Heat's key is the persisting ``services.heat.raise_heat``, not the pure
#: ``linkedin.heat.raise_heat`` it delegates to: the pure one returns a new
#: state and stores nothing, so a run that called only it would forget the
#: throttle the moment it returned.
#:
#: The budget's four keys are one function and four action classes: a key
#: written ``function[reference]`` is enforced by a file that uses both, so
#: ``budgets.consume`` called with ``ActionClass.CONNECTION_PAGES`` (the
#: connections sync, P2-06) wires the ``connection_pages`` budget and none of the
#: other three. Keyed on ``consume`` alone, that one caller read as all four
#: budgets enforced while nothing spent a profile visit.
#:
#: Human-like pacing's key is ``pacing.plan_enrichment``, the entry point that
#: produces the inter-visit plan (delays, bursts, breaks), not the helpers it
#: calls. ``human_delay`` is also reached by ``scroll_like_a_person`` for the
#: reading dwell alone, so a job that only scrolls pages would have read as
#: paced while nothing spaced its visits apart.
ENFORCED_BY: Final[dict[str, tuple[str, ...]]] = {
    f"{_CONSUME}[{_ACTION_CLASS}.CONNECTION_PAGES]": ("budget connection_pages",),
    f"{_CONSUME}[{_ACTION_CLASS}.PROFILE_VISITS]": ("budget profile_visits",),
    f"{_CONSUME}[{_ACTION_CLASS}.INBOX_POLLS]": ("budget inbox_polls",),
    f"{_CONSUME}[{_ACTION_CLASS}.LI_MESSAGES_AUTO]": ("budget li_messages_auto",),
    "netkeeper.linkedin.pacing.warmup_budget": ("warm-up ramp",),
    "netkeeper.linkedin.pacing.apply_weekend_multiplier": ("weekend damping",),
    "netkeeper.linkedin.pacing.plan_enrichment": ("human-like pacing",),
    "netkeeper.services.heat.raise_heat": ("heat",),
    "netkeeper.services.linkedin_session.flag_session": ("session flag",),
    "netkeeper.linkedin.pacing.is_active_at": ("active hours",),
    "netkeeper.services.heat.should_skip": ("heat skip gate",),
}

#: The subset of :data:`ENFORCED_BY` whose function nothing in the package
#: calls yet, outside this module and the rehearsal. Kept in sync by
#: ``test_the_unenforced_list_is_what_the_package_actually_shows``, which is
#: the whole point: a hand-maintained list of "not wired up yet" is wrong the
#: week after it is written.
UNENFORCED_TODAY: Final[tuple[str, ...]] = (
    f"{_CONSUME}[{_ACTION_CLASS}.PROFILE_VISITS]",
    f"{_CONSUME}[{_ACTION_CLASS}.INBOX_POLLS]",
    f"{_CONSUME}[{_ACTION_CLASS}.LI_MESSAGES_AUTO]",
    "netkeeper.linkedin.pacing.warmup_budget",
    "netkeeper.linkedin.pacing.apply_weekend_multiplier",
    "netkeeper.linkedin.pacing.plan_enrichment",
)


def _unenforced_protections() -> tuple[str, ...]:
    """The protection names that no caller enforces yet, deduplicated, in report order."""
    names: list[str] = []
    for function in UNENFORCED_TODAY:
        for name in ENFORCED_BY[function]:
            if name not in names:
                names.append(name)
    return tuple(names)


_UNENFORCED_TEXT: Final = ", ".join(_unenforced_protections())


GAPS: Final[tuple[str, ...]] = (
    "**this report reads configuration and counters, never callers.** It can"
    " tell you a limit is set and how much of it is spent; it cannot tell you"
    " that the code which will do the work remembers to ask. The protections"
    f" listed next have no enforcing caller in the package yet: {_UNENFORCED_TEXT}."
    " Until the job that must call it exists, each of those is a setting rather"
    " than a brake, and this report says the same thing on the day it is wired"
    " as on the day it is not.",
    "the activity lock binds netkeeper processes that share this data directory on"
    " this machine: it is a file lock under the data directory. A netkeeper started"
    " with a different NETKEEPER_DATA, a netkeeper on another machine, or any other"
    " tool attached to the same Chrome over CDP does not take it and is not seen.",
    "the activity lock is a file, `locks/browser-<account>.lock` under the data"
    " directory, and a file lock guards only the file that is still there. Deleting"
    " `locks/` or the file while netkeeper holds the browser leaves the holder"
    " locking a file nobody else can open, so the next process claims a fresh one"
    " and attaches as a second CDP client. Nothing in netkeeper deletes it; only a"
    " manual `rm` can cause this, so leave `locks/` alone while `netkeeper serve` runs.",
    "nothing wires the scheduler into `netkeeper serve` yet (that is P2-10), so"
    " on a normal install no schedule is established and no job fires on its"
    " own. The schedule in this report is whatever a caller has established;"
    " until serve starts one, runs are the ones you start by hand.",
    "this reports the *stored* due time for each job kind, not whether the"
    " process that would fire it is running. A schedule established by a"
    " `netkeeper serve` that has since exited still reads as scheduled.",
    "the heat skip gate reported is the one this report was handed. Run from"
    " the command line that is the gate a scheduler started from this config"
    " would use; it cannot see a gate some other running process was passed."
    " Serving this report from inside the process that owns the scheduler"
    " (P2-10) closes that.",
    "whether a LinkedIn session is not merely present but still accepted is"
    " something only a real job learns, from the response classification in"
    " spec 9.7. This reports the cookie jar and the last flag raised, which is"
    " everything that can be known without sending a request.",
)


# --- the individual protections ---------------------------------------------


def _browser_mode(mode: str, cdp_url: str) -> Protection:
    """Attach-only, and the debug port it attaches to (spec 9.1, ADR 0002)."""
    warnings: list[str] = []
    status = Status.ON
    if mode != ATTACH_ONLY:
        status = Status.OFF
        warnings.append(
            f"the browser provider reports mode {mode!r}, not {ATTACH_ONLY!r}. A browser"
            " netkeeper starts is a second device on your LinkedIn account, which is the"
            " restriction trigger ADR 0002 exists to avoid"
        )
    host = _host_of(cdp_url)
    if host not in LOOPBACK_HOSTS:
        warnings.append(
            f"linkedin.cdp_url points at {host or 'no host'}, not this machine's loopback."
            " Chrome's debug port is only ever reachable on its own loopback address"
        )
    return Protection(
        name="attach-only browser",
        status=status,
        value=f"{mode} over CDP to {cdp_url}",
        warnings=tuple(warnings),
    )


def _activity_lock(state: activity_lock.LockState | None, *, now: datetime) -> Protection:
    """One browser client per account, across processes (spec 9.9): free, or held by whom.

    Held is normally the lock doing its job -- a run is in progress and anything
    else that tries to attach answers ``busy`` -- so it stays ``on``. It warns when
    the holder looks wrong, because a stuck or unknown holder blocks every job and
    every preflight without saying so: no readable note, a pid that is not running,
    a holder that is not a netkeeper command, or a hold longer than
    :data:`MAX_PLAUSIBLE_HOLD_HOURS`. It is ``unknown`` when the lock cannot be
    inspected at all.
    """
    name = "one browser client"
    if state is None:
        try:
            state = activity_lock.inspect(activity_lock.SINGLE_ACCOUNT_KEY)
        except OSError as exc:
            return Protection(
                name=name,
                status=Status.UNKNOWN,
                value="lock unreadable",
                warnings=(
                    f"could not inspect the activity lock ({exc}), so whether another"
                    " netkeeper process holds the browser is unknown",
                ),
            )
    if not state.held:
        return Protection(
            name=name,
            status=Status.ON,
            value=f"free: no netkeeper process holds the browser for {state.account!r}",
        )
    holder = state.holder
    who = holder.describe() if holder is not None else "a holder that left no note"
    return Protection(
        name=name,
        status=Status.ON,
        value=f"held by {who}; anything else that tries to attach answers busy",
        warnings=_suspicious_holder(holder, state.path, now=now),
    )


def _suspicious_holder(
    holder: activity_lock.Holder | None, path: Path, *, now: datetime
) -> tuple[str, ...]:
    """Why a held lock looks stuck or foreign, if it does. Empty for an ordinary run."""
    until_released = (
        " Until it is released, every job and every preflight answers busy; stop the"
        " holding process to release it"
    )
    if holder is None:
        return (
            f"the activity lock ({path}) is held, but its holder left no readable note,"
            f" so nothing can say what holds it.{until_released}",
        )
    warnings: list[str] = []
    if not activity_lock.pid_alive(holder.pid):
        warnings.append(
            f"the activity lock is held, but pid {holder.pid} from its note is not"
            " running: something netkeeper cannot name holds it (a child process that"
            f" inherited it, or a process in another pid namespace).{until_released}"
        )
    if not holder.command.startswith("netkeeper"):
        warnings.append(
            f"the activity lock is held by {holder.command or 'an unnamed command'!r},"
            f" which is not a netkeeper command.{until_released}"
        )
    if holder.since is not None:
        since = holder.since
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        held_for = now - since
        if held_for > timedelta(hours=MAX_PLAUSIBLE_HOLD_HOURS):
            hours = held_for.total_seconds() / 3600
            warnings.append(
                f"the activity lock has been held for {hours:.1f} hours, longer than the"
                f" {MAX_PLAUSIBLE_HOLD_HOURS:g} any run should take: the run holding it"
                f" is probably stuck.{until_released}"
            )
    return tuple(warnings)


def _linkedin_session(probe: SessionProbe | None) -> Protection:
    """Whether a LinkedIn session is actually present, from a probe of the cookie jar.

    Cookie names only; a value cannot reach here because :class:`SessionProbe`
    has nowhere to put one.
    """
    if probe is None:
        return Protection(
            name="linkedin session",
            status=Status.UNKNOWN,
            value="not probed",
            warnings=(
                "no browser probe was run, so whether this Chrome profile still holds a"
                " LinkedIn session is unknown. `netkeeper preflight` answers it, and"
                " `netkeeper posture --probe` folds the answer into this report",
            ),
        )
    if not probe.attached:
        return Protection(
            name="linkedin session",
            status=Status.UNKNOWN,
            value="not attached",
            warnings=probe.problems
            or ("could not attach to Chrome, so the LinkedIn session could not be read",),
        )
    if probe.logged_in is None:
        return Protection(
            name="linkedin session",
            status=Status.UNKNOWN,
            value="cookie jar unreadable",
            warnings=("the browser's cookie jar could not be read, so the login state is unknown",),
        )
    if not probe.logged_in:
        return Protection(
            name="linkedin session",
            status=Status.OFF,
            value="no session in this profile",
            warnings=(
                "this Chrome profile holds no live LinkedIn session, so no job can run."
                " Log in once in the window `netkeeper browser launch` describes",
            ),
        )
    names = ", ".join(probe.cookie_names) or "no named cookies"
    return Protection(
        name="linkedin session",
        status=Status.ON,
        value=f"logged in ({names})",
    )


def _session_flag(session: Session, user: User) -> Protection:
    """The flag a ``Checkpoint`` or ``LoggedOut`` classification raises (spec 9.7)."""
    flag = session_flag(session, user)
    if flag is None:
        return Protection(
            name="session flag",
            status=Status.ON,
            value="clear (checkpoint and logged-out raise it)",
        )
    return Protection(
        name="session flag",
        status=Status.ON,
        value=f"{flag.outcome.value} at {flag.url or '/'}",
        warnings=(
            f"the session was flagged {flag.outcome.value} on"
            f" {flag.flagged_at:%Y-%m-%d %H:%M UTC}. Runs stop while a flag is up; a"
            " checkpoint is never retried. Clear it only once you have opened LinkedIn"
            " in the netkeeper profile and seen the account is healthy",
        ),
    )


def _active_hours(
    linkedin: LinkedInSettings,
    zone: ZoneInfo,
    zone_warning: str | None,
    *,
    now: datetime,
    local_now: datetime,
) -> Protection:
    """The active window, and where ``now`` falls in it (spec 9.5)."""
    active_hours = linkedin.active_hours
    timezone_name = linkedin.timezone
    warnings: list[str] = []
    if zone_warning is not None:
        warnings.append(zone_warning)
    window = _parse_window(active_hours)
    if window is None:
        return Protection(
            name="active hours",
            status=Status.UNKNOWN,
            value=f"unreadable: {active_hours[0]!r} to {active_hours[1]!r}",
            warnings=(
                *warnings,
                "linkedin.active_hours is not two HH:MM times, so no window can be"
                " enforced from it",
            ),
        )
    start, end = window
    span_hours = _window_hours(start, end)
    status = Status.ON
    if start == end:
        status = Status.OFF
        warnings.append(
            "the window starts and ends at the same time, which means active all 24"
            " hours: no tick is ever parked for a window start"
        )
    elif span_hours > MAX_ACTIVE_WINDOW_HOURS:
        status = Status.OFF
        warnings.append(
            f"the window is {span_hours:.1f} hours long, past the {MAX_ACTIVE_WINDOW_HOURS:.0f}"
            " this report treats as still being a window. Appendix B's default is 08:30"
            " to 21:30, 13 hours"
        )
    elif start > end:
        warnings.append(
            f"the window runs overnight ({start:%H:%M} to {end:%H:%M}), so netkeeper is"
            " active at hours your own browsing is not. Spec 9.1 leans on your organic"
            " activity as cover traffic, and a sidecar that is busiest while the account"
            " is otherwise asleep has none. If those really are your hours, this is fine"
        )
    inside = is_active_at(now, zone, start=start, end=end)
    where = "inside" if inside else "outside"
    opens = next_window_start(now, zone, start=start).astimezone(zone)
    value = (
        f"{start:%H:%M}-{end:%H:%M} {timezone_name}; now {local_now:%H:%M} ({where})"
        if inside
        else f"{start:%H:%M}-{end:%H:%M} {timezone_name}; outside, opens {opens:%a %H:%M}"
    )
    return Protection(name="active hours", status=status, value=value, warnings=tuple(warnings))


def _account_timezone(user: User, configured: str) -> Protection:
    """Budget days and active hours have to roll over at the same midnight.

    ``services.budgets`` keys its counters by ``user.timezone``; active hours
    read ``linkedin.timezone``. They are two fields, so they can disagree, and
    when they do the daily budget resets at a different midnight than the one
    the window opens against -- a hole big enough to run a second day's visits
    through and invisible in either module's own tests.
    """
    if user.timezone == configured:
        return Protection(
            name="one local midnight", status=Status.ON, value=f"budgets and hours in {configured}"
        )
    return Protection(
        name="one local midnight",
        status=Status.OFF,
        value=f"budgets in {user.timezone}, hours in {configured}",
        warnings=(
            f"budget counters roll over at midnight in {user.timezone} while active hours"
            f" are read in {configured}. Set linkedin.timezone and the user's timezone to"
            " the same zone, or a day's budget resets at an hour the window is open",
        ),
    )


def _weekend_damping(multiplier: float, local_now: datetime) -> Protection:
    """Spec 9.5: "multiply budgets by 0.5 on Saturday and Sunday by default"."""
    weekday = local_now.strftime("%A")
    applied = local_now.date().weekday() in (5, 6)
    value = f"x{multiplier:g} on Sat/Sun; today is {weekday}"
    if applied:
        value += " (applied)"
    if multiplier < 0:
        return Protection(
            name="weekend damping",
            status=Status.OFF,
            value=value,
            warnings=(
                f"linkedin.weekend_multiplier is {multiplier:g}, which is not a multiplier"
                " any budget can be scaled by",
            ),
        )
    if multiplier >= WEEKEND_DAMPING_CEILING:
        raised = (
            " and raises them above a weekday's" if multiplier > WEEKEND_DAMPING_CEILING else ""
        )
        return Protection(
            name="weekend damping",
            status=Status.OFF,
            value=value,
            warnings=(
                f"linkedin.weekend_multiplier is {multiplier:g}, so weekend budgets are not"
                f" damped at all{raised}. Appendix B's default is 0.5",
            ),
        )
    return Protection(name="weekend damping", status=Status.ON, value=value)


def _warmup_ramp(settings: Settings, today: TodaysBudget) -> Protection:
    """Spec 9.5: a fresh install starts at 20 profile visits a day and grows by 10."""
    budget = settings.linkedin.budget
    cap = _profile_visit_cap(settings)
    value = f"start {budget.warmup_start}, +{budget.warmup_step}/day, cap {cap}"
    if budget.warmup_start >= cap:
        return Protection(
            name="warm-up ramp",
            status=Status.OFF,
            value=value,
            warnings=(
                f"the ramp starts at {budget.warmup_start}, at or above the {cap}-visit"
                " cap, so a fresh install gets its full daily budget on day one. Spec 9.5"
                " ramps from 20 by 10 a day precisely because a new device does not",
            ),
        )
    return Protection(
        name="warm-up ramp",
        status=Status.ON,
        value=f"{value}; today {today.ramp}",
    )


def _pacing(pacing: PacingSettings, cap: int) -> Protection:
    """Spec 9.5's human-like behavior: the wait between profiles, and the bursts.

    Four of Appendix C's six rows live in ``[linkedin.pacing]`` -- the delay
    between profiles, the distraction pause, the burst size, and the break
    between bursts -- and every one of them is a real, loadable config key.
    Reporting the weekend multiplier while saying nothing about the gap between
    two profile views has the coverage backwards: the gap *is* what spec 9.5 is
    about, and a config that zeroes it turns the sidecar into exactly the
    request pattern the whole design exists to avoid.

    ``cap`` is the per-day profile-visit limit in force, which is what makes
    the burst-size check a derived fact rather than an invented ceiling: a
    burst larger than a day's whole budget can never reach its own end, so the
    break between bursts never happens.
    """
    warnings: list[str] = []
    status = Status.ON
    if pacing.profile_delay_median_s <= 0:
        status = Status.OFF
        warnings.append(
            f"linkedin.pacing.profile_delay_median_s is {pacing.profile_delay_median_s:g}:"
            " there is no wait between profile views at all. Worse than the pacing being"
            " off, pacing.human_delay refuses a median of zero or less, so this does not"
            " run fast -- it raises part-way through a run, after this report has been"
            " read. Appendix C's default is 25"
        )
    elif pacing.profile_delay_median_s < MIN_DELAY_MEDIAN_S:
        status = Status.OFF
        warnings.append(
            f"linkedin.pacing.profile_delay_median_s is {pacing.profile_delay_median_s:g}s,"
            f" under the {MIN_DELAY_MEDIAN_S:g}s this report treats as human. Appendix C"
            " picks 25 because that is how long a person takes to read a profile"
        )
    if pacing.profile_delay_sigma <= 0:
        status = Status.OFF
        warnings.append(
            f"linkedin.pacing.profile_delay_sigma is {pacing.profile_delay_sigma:g}, so"
            " every wait is exactly the median. A constant interval between requests is"
            " the most machine-like signature there is; the spread is what makes the"
            " timing look human at all"
        )
    if pacing.distraction_p <= 0:
        status = Status.OFF
        warnings.append(
            f"linkedin.pacing.distraction_p is {pacing.distraction_p:g}, so the run never"
            " pauses for a distraction. Appendix C gives it an 8% chance because people"
            " get interrupted, and a session that never is looks unattended"
        )
    elif pacing.distraction_range_s[1] <= 0:
        status = Status.OFF
        warnings.append(
            f"linkedin.pacing.distraction_range_s is {list(pacing.distraction_range_s)}, so"
            " a distraction pause adds nothing however often it is drawn"
        )
    low, high = pacing.burst_size
    if low < 1 or high < low:
        status = Status.OFF
        warnings.append(
            f"linkedin.pacing.burst_size is {list(pacing.burst_size)}, which is not a"
            " range pacing.plan_burst_sizes accepts: it raises rather than planning a"
            " run, so this fails part-way through rather than pacing badly"
        )
    elif low > cap:
        status = Status.OFF
        warnings.append(
            f"linkedin.pacing.burst_size starts at {low}, more than the {cap} profile"
            " visits a day allows, so a burst never reaches its own end and the break"
            " between bursts never happens. Spec 9.5's bursts are 8 to 15"
        )
    break_low, break_high = pacing.burst_break_s
    if break_high < break_low or break_low < 0:
        status = Status.OFF
        warnings.append(
            f"linkedin.pacing.burst_break_s is {list(pacing.burst_break_s)}, which is not a range"
        )
    elif break_high < MIN_BURST_BREAK_S:
        status = Status.OFF
        warnings.append(
            f"linkedin.pacing.burst_break_s tops out at {break_high:g}s, under the"
            f" {MIN_BURST_BREAK_S:g}s this report treats as a break. Appendix C's range is"
            " 5 to 20 minutes -- sessions, not streams"
        )
    return Protection(
        name="human-like pacing",
        status=status,
        value=(
            f"{pacing.profile_delay_median_s:g}s median (sigma {pacing.profile_delay_sigma:g}),"
            f" {pacing.distraction_p:.0%} distraction, bursts of {low}-{high}"
            f" then {break_low:g}-{break_high:g}s"
        ),
        warnings=tuple(warnings),
    )


def _auto_send(settings: Settings) -> Protection:
    """ADR 0004: LinkedIn messages are prefilled for a person to send, not sent."""
    if not settings.campaigns.linkedin_auto_send:
        return Protection(
            name="manual linkedin sends",
            status=Status.ON,
            value="auto-send off (ADR 0004)",
        )
    return Protection(
        name="manual linkedin sends",
        status=Status.OFF,
        value="auto-send ON",
        warnings=(
            "campaigns.linkedin_auto_send is true, so netkeeper sends LinkedIn messages"
            " itself rather than prefilling them for you to send. ADR 0004 defaults it"
            " off: an automated send is the action LinkedIn restricts hardest",
        ),
    )


def _budget(action: ActionClass, snapshot: BudgetSnapshot, settings: Settings) -> Protection:
    """One action class's day and week counters against their limits (spec 9.6)."""
    budget = settings.linkedin.budget
    warnings: list[str] = []
    parts = [f"{snapshot.day.count}/{snapshot.day.limit} today"]
    if snapshot.week is not None:
        parts.append(f"{snapshot.week.count}/{snapshot.week.limit} this week")
    hard_day = HARD_MAX_PER_DAY[action]
    asked_day = configured_default(action, budget, "day")
    if asked_day is not None and asked_day > hard_day:
        warnings.append(
            f"config asks for {asked_day} a day, above spec 9.6's hard max of {hard_day}."
            f" The clamp holds and {hard_day} is what is enforced, but the config file"
            " says something the tool will not do"
        )
    hard_week = HARD_MAX_PER_WEEK.get(action)
    asked_week = configured_default(action, budget, "week")
    if hard_week is not None and asked_week is not None and asked_week > hard_week:
        warnings.append(
            f"config asks for {asked_week} a week, above spec 9.6's hard max of"
            f" {hard_week}. The clamp holds and {hard_week} is what is enforced"
        )
    if snapshot.day.over:
        warnings.append(
            f"today's count is already past its limit ({snapshot.day.count} of"
            f" {snapshot.day.limit}); the next unit of work is refused"
        )
    if snapshot.week is not None and snapshot.week.over:
        warnings.append(
            f"this week's count is already past its limit ({snapshot.week.count} of"
            f" {snapshot.week.limit}); the next unit of work is refused"
        )
    ceiling = f"hard max {hard_day}/day"
    if hard_week is not None:
        ceiling += f", {hard_week}/week"
    return Protection(
        name=f"budget {action.value}",
        status=Status.ON,
        value=f"{', '.join(parts)} ({ceiling})",
        warnings=tuple(warnings),
    )


def _heat_skip_gate(gate: HeatGate) -> Protection:
    """Spec 9.7's "above heat_skip_threshold the scheduler skips browser jobs entirely".

    The scheduler makes disabling this take a named argument rather than a
    falsy default, so a caller cannot turn it off by forgetting something. That
    also means the only way it is ever off is that somebody meant it -- and
    somebody meaning it is exactly what a posture report is for.
    """
    if gate is HEAT_SKIP_DISABLED:
        return Protection(
            name="heat skip gate",
            status=Status.OFF,
            value="DISABLED",
            warnings=(
                "the scheduler is running with HEAT_SKIP_DISABLED, so browser jobs run"
                " however warm the account is. Spec 9.7 makes the skip unconditional:"
                " above the threshold the scheduler skips browser jobs entirely, and"
                " with this off a throttled account keeps being asked for more",
            ),
        )
    return Protection(
        name="heat skip gate",
        status=Status.ON,
        value="on: browser jobs skipped above the threshold",
    )


def _scheduled_jobs(scheduler: SchedulerPosture) -> Protection:
    """Whether a schedule is established, because the scheduler-side protections need one.

    Active hours deferral, the catch-up rule, and the heat skip gate are all
    things the *scheduler* does. A report that called them in force while no
    job kind has a due time would be claiming protection from a mechanism that
    is not running, so a schedule that is missing or half-established warns.
    """
    total = len(scheduler.jobs)
    if not scheduler.unscheduled:
        soonest = min(due for _, _, due in scheduler.jobs if due is not None)
        return Protection(
            name="scheduled jobs",
            status=Status.ON,
            value=f"{total} kinds scheduled; next {soonest:%Y-%m-%d %H:%M UTC}",
        )
    missing = ", ".join(scheduler.unscheduled)
    if not scheduler.scheduled:
        return Protection(
            name="scheduled jobs",
            status=Status.UNKNOWN,
            value="nothing scheduled",
            warnings=(
                "no job kind has a due time for this account, so nothing fires on its"
                " own and the active-hours, catch-up, and heat-skip protections have"
                " nothing to act on. That is the expected state until `netkeeper serve`"
                " establishes a schedule",
            ),
        )
    return Protection(
        name="scheduled jobs",
        status=Status.UNKNOWN,
        value=f"{len(scheduler.scheduled)} of {total} kinds scheduled",
        warnings=(
            f"these job kinds have no due time: {missing}. A half-established"
            " schedule runs some kinds and silently never runs the others",
        ),
    )


def _heat(posture_of_heat: HeatPosture, heat_settings: HeatSettings) -> Protection:
    """Heat's level, its skip threshold, and whether it is holding runs back (spec 9.7)."""
    warnings: list[str] = []
    status = Status.ON
    if heat_settings.per_block <= heat_math.COLD_EPSILON:
        # At or under the cold cutoff, one block reads back as cold as soon as
        # any time passes, and the residue is zeroed before the next block is
        # added, so blocks never accumulate: as off as zero. `raise_heat` still
        # stores the raise, so the row may say "last raised"; the score as read
        # is what this warning is about.
        status = Status.OFF
        warnings.append(
            f"linkedin.heat.per_block is {heat_settings.per_block:g}, at or under the"
            f" {heat_math.COLD_EPSILON:g} below which the score reads as cold, so a"
            " throttle or a checkpoint never raises the score as read: it is back to"
            " 0.00 as soon as any time passes, even when a raise is recorded. Delays"
            " never stretch, and the skip threshold can never be reached"
        )
    if heat_settings.half_life_hours < MIN_HALF_LIFE_HOURS:
        status = Status.OFF
        warnings.append(
            f"linkedin.heat.half_life_hours is {heat_settings.half_life_hours:g}, under the"
            f" {MIN_HALF_LIFE_HOURS:g} this report treats as a real cooldown: a throttle is"
            " forgotten before the next burst starts. Appendix C's default is 6"
        )
    if heat_settings.skip_threshold <= 0:
        warnings.append(
            f"linkedin.heat.skip_threshold is {heat_settings.skip_threshold:g}, so every"
            " browser job is skipped whatever the score. Nothing will ever run"
        )
    elif heat_settings.per_block > 0:
        blocks = math.ceil(heat_settings.skip_threshold / heat_settings.per_block)
        if blocks > MAX_BLOCKS_BEFORE_SKIP:
            status = Status.OFF
            warnings.append(
                f"it takes {blocks} blocks at {heat_settings.per_block:g} each to reach a"
                f" skip threshold of {heat_settings.skip_threshold:g}, past the"
                f" {MAX_BLOCKS_BEFORE_SKIP} this report treats as a brake. Appendix C's"
                " defaults need 3"
            )
    if posture_of_heat.tripped:
        resumes = (
            f" until {posture_of_heat.resumes_at:%Y-%m-%d %H:%M UTC}"
            if posture_of_heat.resumes_at is not None
            else ""
        )
        warnings.append(
            f"the score is {posture_of_heat.score:.2f}, at or above the skip threshold of"
            f" {posture_of_heat.threshold:g}: browser jobs are skipped{resumes}"
        )
    if posture_of_heat.last_raised_at is not None:
        raised = f"last raised {posture_of_heat.last_raised_at:%Y-%m-%d %H:%M UTC}"
    elif posture_of_heat.cleared_at is not None:
        raised = f"cleared {posture_of_heat.cleared_at:%Y-%m-%d %H:%M UTC}"
    else:
        raised = "never raised"
    level = (
        "score unreadable"
        if not posture_of_heat.readable
        else (
            f"{posture_of_heat.score:.2f} of {posture_of_heat.threshold:g}"
            f" (x{posture_of_heat.multiplier:.2f})"
        )
    )
    return Protection(
        name="heat", status=status, value=f"{level}, {raised}", warnings=tuple(warnings)
    )


# --- the numbers behind them -------------------------------------------------


def _heat_posture(
    session: Session,
    user: User,
    account_id: int,
    *,
    now: datetime,
    heat_settings: HeatSettings,
) -> HeatPosture:
    stored_state = heat_rows.state(session, user, account_id)
    if heat_settings.half_life_hours <= 0:
        # Nothing decays over a non-positive half-life, so there is no score to
        # report. :func:`_heat` warns about the setting itself; this only keeps
        # the report from raising on the way there.
        return HeatPosture(
            score=0.0,
            threshold=heat_settings.skip_threshold,
            multiplier=heat_math.COOLDOWN_FLOOR,
            tripped=False,
            last_raised_at=None if stored_state is None else stored_state.updated_at,
            resumes_at=None,
            readable=False,
        )
    score = heat_rows.read(session, user, account_id, now=now, settings=heat_settings)
    tripped = heat_rows.should_skip(session, user, account_id, now=now, settings=heat_settings)
    multiplier = heat_rows.cooldown_multiplier(
        session, user, account_id, now=now, settings=heat_settings
    )
    # A stored score of exactly 0.0 is what `heat.clear()` writes and nothing
    # else does: `raise_heat` adds a positive `per_block` to a non-negative
    # score, so a raised row is never exactly zero even when the score it was
    # raised from had decayed to a cold 0.0 (`COLD_EPSILON`). So the timestamp
    # means "cleared", not "raised".
    raised = stored_state is not None and stored_state.score > 0
    return HeatPosture(
        score=score,
        threshold=heat_settings.skip_threshold,
        multiplier=multiplier,
        tripped=tripped,
        last_raised_at=stored_state.updated_at if raised and stored_state else None,
        resumes_at=_resumes_at(score, now, settings=heat_settings) if tripped else None,
        cleared_at=None if stored_state is None or raised else stored_state.updated_at,
    )


def _scheduler_posture(
    session: Session, user: User, account_id: int, *, gate: HeatGate
) -> SchedulerPosture:
    """What the scheduler is set to do, read through its own public functions.

    ``stored_due`` is the scheduler's read-only accessor for the persisted due
    time; nothing here reaches into its ``settings_kv`` keys or recomputes a
    cadence, so a scheduler change cannot leave this report quietly describing
    a schedule nobody runs.
    """
    jobs = tuple(
        (
            kind.value,
            schedule.interval.total_seconds() / 3600,
            stored_due(session, user, account_id, kind),
        )
        for kind, schedule in DEFAULT_SCHEDULES.items()
    )
    return SchedulerPosture(
        heat_skip=gate is not HEAT_SKIP_DISABLED,
        catchup_minutes=(CATCHUP_MIN_MINUTES, CATCHUP_MAX_MINUTES),
        job_kind_gap_minutes=MIN_JOB_KIND_GAP.total_seconds() / 60,
        jobs=jobs,
    )


def _resumes_at(score: float, now: datetime, *, settings: HeatSettings) -> datetime | None:
    """When an exponentially decaying ``score`` falls back below the skip threshold.

    Spec 9.7: the Settings page shows "when runs resume". Derived from the
    decayed score rather than from the stored one, so it stays right however
    long ago the last block was: halving every ``half_life_hours`` means the
    score reaches ``threshold`` after ``half_life * log2(score / threshold)``
    hours. ``None`` when the threshold is not a positive number, where the
    question has no answer.
    """
    threshold = settings.skip_threshold
    half_life = settings.half_life_hours
    if threshold <= 0 or half_life <= 0 or score <= 0 or score < threshold:
        return None
    hours = half_life * math.log2(score / threshold)
    return now + timedelta(hours=hours)


def _todays_budget(
    user: User,
    *,
    settings: Settings,
    local_now: datetime,
    multiplier: float,
    spent: int,
) -> TodaysBudget:
    """The warm-up ramp, damped for the weekend, shrunk by heat -- in that order.

    The order is spec 9.5's then spec 9.7's, and it is the order a run applies
    them in: the ramp says what the day is worth, the weekend halves it, and
    heat divides what is left. Computed by calling those modules rather than by
    restating the arithmetic, so this cannot drift from what a run does.
    """
    budget = settings.linkedin.budget
    cap = _profile_visit_cap(settings)
    days = _days_since_install(user, local_now)
    ramp = warmup_budget(days, cap, start=budget.warmup_start, step=budget.warmup_step)
    after_weekend = apply_weekend_multiplier(
        ramp, local_now.date(), multiplier=settings.linkedin.weekend_multiplier
    )
    after_heat = (
        heat_math.shrink(after_weekend, max(multiplier, heat_math.COOLDOWN_FLOOR))
        if after_weekend >= 1
        else after_weekend
    )
    return TodaysBudget(ramp=ramp, after_weekend=after_weekend, after_heat=after_heat, spent=spent)


def _profile_visit_cap(settings: Settings) -> int:
    """The per-day profile-visit ceiling actually in force: config, clamped by spec 9.6."""
    return min(
        settings.linkedin.budget.profile_visits_per_day,
        HARD_MAX_PER_DAY[ActionClass.PROFILE_VISITS],
    )


def _days_since_install(user: User, local_now: datetime) -> int:
    """Local calendar days between the user row being created and today.

    The user row is created at first start, so its ``created_at`` is the
    install date. Counted in the account's own local days, not in 24-hour
    blocks: the ramp steps at local midnight along with the budget counters it
    feeds, and a run at 01:00 local on day 2 is on day 2's budget even though
    fewer than 48 hours have passed.
    """
    created = user.created_at
    if created.tzinfo is None or created.utcoffset() is None:
        created = created.replace(tzinfo=UTC)
    installed_local = created.astimezone(local_now.tzinfo)
    return max((local_now.date() - installed_local.date()).days, 0)


def _zone_of(name: str) -> tuple[ZoneInfo, str | None]:
    """``name`` as a zone, or UTC with the warning that says so."""
    try:
        return ZoneInfo(name), None
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC"), (
            f"linkedin.timezone is {name!r}, which is not a zone this machine knows."
            " Everything below is read in UTC instead, so the active window and the"
            " budget day are both in the wrong place"
        )


def _parse_window(active_hours: tuple[str, str]) -> tuple[time, time] | None:
    try:
        return time.fromisoformat(active_hours[0]), time.fromisoformat(active_hours[1])
    except (ValueError, IndexError):
        return None


def _window_hours(start: time, end: time) -> float:
    """The window's length in hours, wrapping past midnight when it has to."""
    minutes = ((end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)) % (24 * 60)
    return 24.0 if start == end else minutes / 60


def _host_of(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


# --- rendering ----------------------------------------------------------------
# Separate from the report on purpose: `netkeeper posture` prints this table,
# and P2-10's API serves the dataclasses above as JSON. Neither shape is
# derived from the other, and adding a field to the report never silently
# changes what the CLI prints.


def render(report: PostureReport) -> str:
    """The report as a person reads it: a table, the day's arithmetic, warnings, gaps, verdict."""
    lines = [
        f"netkeeper posture at {report.checked_at:%Y-%m-%d %H:%M UTC}"
        f" ({report.local_time:%H:%M} {report.timezone})",
        "",
    ]
    rows = [(p.name, p.status.value, p.value) for p in report.protections]
    lines.extend(_table(("PROTECTION", "STATE", "DETAIL"), rows).splitlines())
    lines.append("")
    today = report.today
    lines.append(
        f"today's profile-visit budget: {today.after_heat}"
        f" (warm-up {today.ramp} -> weekend {today.after_weekend} -> heat {today.after_heat}),"
        f" {today.spent} spent, {today.remaining} left"
    )
    lines.append("")
    lines.extend(_scheduler_lines(report.scheduler))
    if report.warnings:
        lines.append("")
        for warning in report.warnings:
            lines.extend(_wrapped(warning, first="warning: ", rest="         "))
    if report.gaps:
        lines.append("")
        lines.append("not covered by this report:")
        for gap in report.gaps:
            lines.extend(_wrapped(gap, first="  - ", rest="    "))
    lines.append("")
    lines.append(_verdict(report))
    return "".join(f"{line}\n" for line in lines)


def _scheduler_lines(scheduler: SchedulerPosture) -> list[str]:
    """The scheduler's own settings, and when each job kind next fires."""
    low, high = scheduler.catchup_minutes
    rows = [
        (
            kind,
            f"every {_hours(interval)}",
            "not scheduled" if due is None else f"{due:%Y-%m-%d %H:%M UTC}",
        )
        for kind, interval, due in scheduler.jobs
    ]
    lines = _table(("JOB", "CADENCE", "NEXT DUE"), rows).splitlines()
    lines.append(
        f"a run missed while the process was down happens once, {low:g} to {high:g} minutes"
        f" after it notices; job kinds are kept {scheduler.job_kind_gap_minutes:g} minutes apart"
    )
    return lines


def _hours(interval: float) -> str:
    return f"{interval / 24:g} d" if interval >= 24 else f"{interval:g} h"


def _verdict(report: PostureReport) -> str:
    """What this report is entitled to claim, which is narrower than "you are safe".

    The report reads configuration and counters. It cannot see whether the code
    that will do the work calls the enforcement -- and today, for several of
    the protections, nothing does (:data:`UNENFORCED_TODAY`, stated in
    :data:`GAPS`). So a clean report says *nothing is misconfigured*, which is
    true and worth a great deal, rather than *every protection is in force*,
    which would be the same sentence on the day a protection works and the day
    it was never wired.
    """
    total = len(report.protections)
    if report.ok:
        return f"nothing is misconfigured: {total} protections, none of them disabled"
    off = len(report.disabled)
    warned = _plural(len(report.warnings), "warning")
    if off:
        verb = "is" if off == 1 else "are"
        return f"NOT clear: {off} of {total} protections {verb} not in force, {warned} in all"
    verb = "needs" if len(report.warnings) == 1 else "need"
    return f"NOT clear: no protection is disabled, but {warned} {verb} reading"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _wrapped(text: str, *, first: str, rest: str, width: int = 88) -> list[str]:
    """``text`` wrapped to ``width``, with a label on the first line and a hanging indent.

    A report whose warnings run off the side of a terminal is a report nobody
    reads to the end, and the end is where the instruction usually is.
    """
    return textwrap.wrap(text, width=width, initial_indent=first, subsequent_indent=rest) or [
        first.rstrip()
    ]


def _table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    """Left-aligned columns, two spaces between, one line per row."""
    if not rows:
        return ""
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows, strict=True)]
    lines = []
    for row in (headers, *rows):
        cells = (cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        lines.append("  ".join(cells).rstrip())
    return "".join(f"{line}\n" for line in lines)
