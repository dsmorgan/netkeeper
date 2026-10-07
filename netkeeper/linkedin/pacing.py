"""Human-like pacing for the LinkedIn extractor: spec 9.5, all of it pure.

Every function here is deterministic in its ``rng`` (a caller-supplied
:class:`random.Random`) and, where a current time matters, in a ``now_utc``
the caller passes in — nothing reads the wall clock or the module-level
:mod:`random` singleton, and nothing opens a browser, a database, or a
network connection. That is what spec 9.10 and ADR 0005 require of anything
under :mod:`netkeeper.linkedin`, and it is also what makes a pacing decision
replayable: give :func:`plan_enrichment` the same seed and the same visit
count twice, in the same process or two different ones, and it returns the
identical plan both times.

``scroll_like_a_person`` in spec 9.5 is written as ``scroll_like_a_person(page)``
— it drives a real tab. The function of that name here does not take a page
and touches nothing: it returns the :class:`ScrollPlan` a browser-facing
caller (P2-07's enrichment job, P2-13's smoke suite) replays against one,
one ``page.mouse.wheel`` call per :class:`ScrollStep`. Keeping the *decision*
of how to scroll pure and separate from the *act* of scrolling is exactly the
extractor boundary spec 9.10 draws for the rest of the module: job specs and
plain data in and out, no session, and — the reason this module in
particular has to be pure — a plan that can be asserted on in a test without
a real minute ever passing.
"""

from __future__ import annotations

import functools
import math
import random
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from types import MappingProxyType
from typing import Final
from zoneinfo import ZoneInfo

import regex

# --- human delay --------------------------------------------------------

# Appendix C: "Delay between profiles ... Median matches a person reading a
# profile; sigma gives a long tail without absurd waits."
DEFAULT_DELAY_MEDIAN_S: Final = 25.0
DEFAULT_DELAY_SIGMA: Final = 0.6
# Appendix C: "Distraction pause ... People get interrupted."
DEFAULT_TAIL_P: Final = 0.08
DEFAULT_TAIL_RANGE_S: Final = (120.0, 480.0)


def human_delay(
    rng: random.Random,
    *,
    median: float = DEFAULT_DELAY_MEDIAN_S,
    sigma: float = DEFAULT_DELAY_SIGMA,
    tail_p: float = DEFAULT_TAIL_P,
    tail_range: tuple[float, float] = DEFAULT_TAIL_RANGE_S,
) -> float:
    """Seconds to wait before the next human-like action. Spec 9.5's ``human_delay``.

    Lognormal around ``median`` (a lognormal's median is exactly its scale
    parameter, so ``median`` names what it sets); ``sigma`` is the spread —
    Appendix C picks 0.6 so the tail is long without absurd waits. On top of
    that, a ``tail_p`` chance of an extra pause drawn uniformly from
    ``tail_range`` — "people get interrupted" (Appendix C's distraction
    pause). ``rng`` makes every call deterministic for a given seed: never
    reach for the module-level :mod:`random`, whose global state another
    test or another call elsewhere could move out from under this one. Give
    a run its own :class:`random.Random` and it replays identically from the
    same seed, in this process or another.
    """
    if median <= 0:
        raise ValueError("median must be positive")
    delay = rng.lognormvariate(math.log(median), sigma)
    if rng.random() < tail_p:
        delay += rng.uniform(*tail_range)
    return delay


# --- scroll plan ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScrollStep:
    """One wheel event: how far to scroll (negative means back up) and the pause after it."""

    delta_px: int
    pause_s: float


@dataclass(frozen=True, slots=True)
class ScrollPlan:
    """One pass down a profile page: a sequence of wheel events, then a final dwell.

    This is the *plan* spec 9.5's ``scroll_like_a_person(page)`` would replay
    against a real tab — one ``page.mouse.wheel`` per :class:`ScrollStep`, in
    order, sleeping ``pause_s`` after each, then sleeping ``dwell_s``.
    Building that plan is pure and lives here; driving an actual page with it
    is P2-07/P2-13's job, kept out of this module by the same boundary spec
    9.10 draws for the rest of the extractor.
    """

    steps: tuple[ScrollStep, ...]
    dwell_s: float

    @property
    def total_delta_px(self) -> int:
        """Net scroll depth.

        Can be less than the sum of magnitudes when a step scrolls back up,
        and is sometimes short by design: spec 9.5 says total scroll depth
        "is random and sometimes short".
        """
        return sum(step.delta_px for step in self.steps)


@dataclass(frozen=True, slots=True)
class ScrollProfile:
    """Parameters for :func:`scroll_like_a_person`.

    Spec 9.5 only describes the shape — "a sequence of mouse.wheel deltas
    with variable magnitude, brief pauses, an occasional scroll back up, and
    a final dwell" — Appendix C has no numbers for it the way it does for
    delay and burst, so these are chosen to fit that description at reading
    speed: a handful of wheel events, sub-two-second pauses between them, and
    a short reading dwell at the end. Every field is overridable the same as
    any other pacing knob.
    """

    steps_range: tuple[int, int] = (3, 9)
    delta_range_px: tuple[int, int] = (120, 900)
    pause_range_s: tuple[float, float] = (0.3, 1.6)
    back_up_p: float = 0.15
    back_up_delta_range_px: tuple[int, int] = (80, 300)
    dwell_median_s: float = 3.0
    dwell_sigma: float = 0.5


DEFAULT_SCROLL_PROFILE: Final = ScrollProfile()


def scroll_like_a_person(
    rng: random.Random,
    *,
    steps_range: tuple[int, int] = DEFAULT_SCROLL_PROFILE.steps_range,
    delta_range_px: tuple[int, int] = DEFAULT_SCROLL_PROFILE.delta_range_px,
    pause_range_s: tuple[float, float] = DEFAULT_SCROLL_PROFILE.pause_range_s,
    back_up_p: float = DEFAULT_SCROLL_PROFILE.back_up_p,
    back_up_delta_range_px: tuple[int, int] = DEFAULT_SCROLL_PROFILE.back_up_delta_range_px,
    dwell_median_s: float = DEFAULT_SCROLL_PROFILE.dwell_median_s,
    dwell_sigma: float = DEFAULT_SCROLL_PROFILE.dwell_sigma,
) -> ScrollPlan:
    """A scroll plan: variable-magnitude wheel deltas, brief pauses, occasional scroll-back,
    then a dwell. Spec 9.5's ``scroll_like_a_person``, as a plan rather than a page driver
    (see the module docstring).

    The dwell reuses :func:`human_delay` with its own tail disabled
    (``tail_p=0``) — a reading dwell is the same fat-tailed-around-a-median
    shape as any other human pause, just shorter, so there is no reason to
    invent a second distribution for it.
    """
    step_count = rng.randint(*steps_range)
    steps: list[ScrollStep] = []
    for _ in range(step_count):
        if rng.random() < back_up_p:
            delta = -rng.randint(*back_up_delta_range_px)
        else:
            delta = rng.randint(*delta_range_px)
        pause = rng.uniform(*pause_range_s)
        steps.append(ScrollStep(delta_px=delta, pause_s=pause))
    dwell = human_delay(
        rng, median=dwell_median_s, sigma=dwell_sigma, tail_p=0.0, tail_range=(0.0, 0.0)
    )
    return ScrollPlan(steps=tuple(steps), dwell_s=dwell)


# --- resting the pointer over content ---------------------------------------

# #192: Playwright's `mouse.wheel` fires at the virtual pointer's position, which
# starts at (0, 0) and never moves until something moves it. On a page with a fixed
# header at (0, 0) -- not over the scrolling content -- a wheel replay that never
# moved the pointer first scrolls nothing. `BrowserRun.scroll` moves it to rest over
# the content once per tab before its first wheel event; the *decision* of how it
# gets there is pure and lives here, the same way `scroll_like_a_person` keeps a
# wheel replay's own decision separate from driving a real tab with it (module
# docstring above). Only the walk's shape is decided here -- small jittered hops,
# paced, ending precisely on a target -- never the target itself, which depends on
# the tab's viewport and is `BrowserRun`'s to know (see its `_rest_pointer_over_content`).
DEFAULT_REST_STEPS_RANGE: Final = (2, 4)
DEFAULT_REST_JITTER_PX: Final = 40
DEFAULT_REST_PAUSE_RANGE_S: Final = (0.05, 0.2)


@dataclass(frozen=True, slots=True)
class RestStep:
    """One hop on the way to resting the pointer: an (x, y) offset from the target
    point, and the pause after it. The last step of a :class:`RestPlan` always has
    ``dx == dy == 0`` -- a hand's final resting point is precise; the wobble is only
    on the way there.
    """

    dx: int
    dy: int
    pause_s: float


@dataclass(frozen=True, slots=True)
class RestPlan:
    """The pointer's walk to a resting point before a scroll replay (#192): a
    handful of jittered waypoints around the target, then a stop exactly on it.

    This is the *plan* :meth:`~netkeeper.linkedin.browser.BrowserRun._rest_pointer_over_content`
    replays against a real tab -- one ``page.mouse.move`` per :class:`RestStep`, at
    the target point plus its offset, sleeping ``pause_s`` after each. Building it
    is pure and lives here, for the same reason :class:`ScrollPlan` does: it can be
    asserted on, and reproduced from a seed, without a real tab or a real minute.
    """

    steps: tuple[RestStep, ...]


def rest_pointer_like_a_person(
    rng: random.Random,
    *,
    steps_range: tuple[int, int] = DEFAULT_REST_STEPS_RANGE,
    jitter_px: int = DEFAULT_REST_JITTER_PX,
    pause_range_s: tuple[float, float] = DEFAULT_REST_PAUSE_RANGE_S,
) -> RestPlan:
    """A pointer-rest walk: a few jittered hops, paced, ending exactly on the target.

    #192's "a few intermediate ``mouse.move`` steps, the way a hand comes to rest."
    Every hop but the last offsets the target by up to ``jitter_px`` in either axis;
    the last is ``(0, 0)`` -- squarely on it.

    ``steps_range`` must have a low end of at least 1, the same guard
    :func:`plan_burst_sizes` has for the same reason: a low end of 0 would not
    merely produce an odd walk sometimes, it would silently produce an *empty*
    one -- no ``mouse.move`` at all, and the pointer never leaves wherever it
    was, which is exactly the failure #192 exists to fix.
    """
    low, high = steps_range
    if low < 1 or high < low:
        raise ValueError(f"steps_range must have 1 <= low <= high, got {steps_range!r}")
    step_count = rng.randint(*steps_range)
    steps: list[RestStep] = []
    for index in range(step_count):
        if index == step_count - 1:
            dx, dy = 0, 0
        else:
            dx = rng.randint(-jitter_px, jitter_px)
            dy = rng.randint(-jitter_px, jitter_px)
        pause = rng.uniform(*pause_range_s)
        steps.append(RestStep(dx=dx, dy=dy, pause_s=pause))
    return RestPlan(steps=tuple(steps))


def depth_after(plan: ScrollPlan) -> int:
    """How far down the page ``plan`` leaves it: the steps summed, never above the top.

    A page cannot scroll above its top, so a scroll back up past it is clamped there
    at each step rather than carried as a debt the next step down would pay off.
    """
    depth = 0
    for step in plan.steps:
        depth = max(0, depth + step.delta_px)
    return depth


#: The wheel steps and pauses of a scroll back to the top, and the short look after it.
BACK_TO_TOP_DELTA_RANGE_PX: Final = (300, 900)
BACK_TO_TOP_PAUSE_RANGE_S: Final = (0.2, 0.9)
BACK_TO_TOP_DWELL_MEDIAN_S: Final = 1.0
BACK_TO_TOP_DWELL_SIGMA: Final = 0.4


def scroll_back_to_top(rng: random.Random, depth_px: int) -> ScrollPlan:
    """A person scrolling back up to the top of a page ``depth_px`` down, then a look.

    Upward wheel steps until they have covered ``depth_px``, and one more: a person
    overshoots, and the page stops at its top. A page that was never scrolled needs no
    steps, only the look. Enrichment replays this before the Contact info click (#190),
    so the link in the top card is on screen when it is clicked, the way a person
    would find it, instead of the page jumping to it.
    """
    steps: list[ScrollStep] = []
    covered = 0
    while depth_px > 0 and covered <= depth_px:
        delta = rng.randint(*BACK_TO_TOP_DELTA_RANGE_PX)
        covered += delta
        steps.append(ScrollStep(delta_px=-delta, pause_s=rng.uniform(*BACK_TO_TOP_PAUSE_RANGE_S)))
    dwell = human_delay(
        rng,
        median=BACK_TO_TOP_DWELL_MEDIAN_S,
        sigma=BACK_TO_TOP_DWELL_SIGMA,
        tail_p=0.0,
        tail_range=(0.0, 0.0),
    )
    return ScrollPlan(steps=tuple(steps), dwell_s=dwell)


# --- bursts -----------------------------------------------------------------

# Appendix C: "Burst ... Sessions, not streams."
DEFAULT_BURST_SIZE_RANGE: Final = (8, 15)
DEFAULT_BURST_BREAK_RANGE_S: Final = (300.0, 1200.0)


def plan_burst_sizes(
    rng: random.Random,
    count: int,
    *,
    size_range: tuple[int, int] = DEFAULT_BURST_SIZE_RANGE,
) -> tuple[int, ...]:
    """Chop ``count`` visits into bursts of ``size_range`` items (spec 9.5: "8 to 15
    profiles, then a break").

    Every burst but the last draws a fresh size from ``size_range``; the last
    is whatever is left over, so it can be shorter than the range's floor
    but never longer than its ceiling. Sizes always sum to ``count``.
    ``count <= 0`` returns ``()``.

    ``size_range`` must have a low end of at least 1 and a high end no lower
    than the low one, or this raises :class:`ValueError`. A burst of zero
    visits is not a burst, and a low end of 0 does not merely produce an odd
    plan — it can draw 0 forever and never make progress, since
    ``remaining`` never shrinks; ``burst_size`` is a user-facing config knob
    (``config.example.toml``'s ``[linkedin.pacing] burst_size``), so a typo
    there must fail loudly here rather than hang the caller.
    """
    low, high = size_range
    if low < 1 or high < low:
        raise ValueError(f"size_range must have 1 <= low <= high, got {size_range!r}")
    if count <= 0:
        return ()
    sizes: list[int] = []
    remaining = count
    while remaining > 0:
        size = min(rng.randint(low, high), remaining)
        sizes.append(size)
        remaining -= size
    return tuple(sizes)


def burst_break(
    rng: random.Random,
    *,
    break_range_s: tuple[float, float] = DEFAULT_BURST_BREAK_RANGE_S,
) -> float:
    """Seconds to pause between bursts, uniform across ``break_range_s``.

    Appendix C: 5 to 20 minutes — "sessions, not streams".
    """
    return rng.uniform(*break_range_s)


# --- active hours -------------------------------------------------------

DEFAULT_ACTIVE_START: Final = time(8, 30)
DEFAULT_ACTIVE_END: Final = time(21, 30)


def is_active_hour(
    local_time: time,
    start: time = DEFAULT_ACTIVE_START,
    end: time = DEFAULT_ACTIVE_END,
) -> bool:
    """Whether ``local_time`` falls inside the active window ``[start, end)``.

    Half-open: a tick exactly on ``start`` is active, one exactly on ``end``
    is not — in both the ordinary case (``start < end``, e.g. 09:00-17:00)
    and the wrap case (``start > end``, e.g. 22:00-06:00, spanning
    midnight). ``start == end`` means active all 24 hours, matching the "all
    days" default in Appendix B rather than reading as an empty window.

    Naive-window arithmetic (``start <= local_time <= end``) is right for
    the ordinary case and wrong for every hour of a wrapping one — spec 9.5
    calls out 22:00-06:00 by name as the case that breaks it: at, say,
    02:00, ``start <= local_time`` (22:00 <= 02:00) is already false, so a
    naive check reports "inactive" in the middle of the night the window is
    supposed to cover.

    ``local_time`` must already be converted to the account's zone (see
    :func:`local_time_of`); this never reads a clock or a zone itself.
    """
    if start == end:
        return True
    if start < end:
        return start <= local_time < end
    return local_time >= start or local_time < end


def local_time_of(now_utc: datetime, tz: ZoneInfo | str) -> datetime:
    """``now_utc`` converted to ``tz``, as an aware local datetime — never a naive one.

    ``now_utc`` itself must be aware; a naive value is ambiguous about whose
    UTC it is, so this raises rather than guessing. Datetimes are stored
    naive UTC and returned timezone-aware throughout netkeeper (CLAUDE.md);
    the local value this returns is a further conversion of an
    already-aware datetime, kept aware end to end, and is meant to be read
    (:meth:`datetime.time`, :meth:`datetime.date`), not stored.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
    return now_utc.astimezone(zone)


def is_active_at(
    now_utc: datetime,
    tz: ZoneInfo | str,
    *,
    start: time = DEFAULT_ACTIVE_START,
    end: time = DEFAULT_ACTIVE_END,
) -> bool:
    """:func:`is_active_hour` for ``now_utc`` (aware) converted to the account's local zone."""
    return is_active_hour(local_time_of(now_utc, tz).time(), start, end)


def next_window_start(
    now_utc: datetime,
    tz: ZoneInfo | str,
    *,
    start: time = DEFAULT_ACTIVE_START,
) -> datetime:
    """The next aware UTC instant, strictly after ``now_utc``, at which the active window opens.

    Spec 9.5: "Ticks outside it park a one-shot job for the window start."
    Always a future instant — even while currently active, in which case it
    is tomorrow's start — so a caller never has to branch on
    :func:`is_active_at` first before calling this. Only ``start`` matters
    here: the window opens at ``start`` whether or not it wraps past
    midnight, so ``end`` plays no part in when it *opens*, only in
    :func:`is_active_hour`'s question of whether a given moment falls inside
    it.

    Built by converting to local time, finding the next local ``start``, and
    converting back to UTC; the local datetime along the way is aware
    throughout and is never returned or stored (CLAUDE.md: never store a
    naive local time) — only the UTC instant it corresponds to is.

    DST is handled correctly because ``.replace()`` keeps ``local_now``'s
    ``fold`` and ``.astimezone(UTC)`` resolves the result through it: adding
    a day of wall time (rather than 24 hours of UTC) keeps the window
    opening at the same local clock reading on a 23- or 25-hour day, and a
    ``start`` that falls in the spring-forward gap resolves forward to the
    first real instant at or after it. One deliberate consequence on a
    fall-back day: called from inside a repeated hour's *first* pass, with a
    ``start`` earlier in that hour than ``now``, this parks for tomorrow
    rather than for the *second* pass of the same local hour later today —
    ``candidate`` inherits ``now``'s fold, so it never considers the other
    one. That is the safer reading (one window opening per calendar day,
    not two 45 minutes apart), but it is a real choice, not an accident.
    """
    zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
    local_now = now_utc.astimezone(zone)
    candidate = local_now.replace(
        hour=start.hour, minute=start.minute, second=start.second, microsecond=start.microsecond
    )
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def outside_window_message(
    now_utc: datetime,
    tz: ZoneInfo | str,
    *,
    start: time = DEFAULT_ACTIVE_START,
    end: time = DEFAULT_ACTIVE_END,
    pinned_in: str | None = None,
) -> str:
    """The one sentence a run stopped or refused by active hours says (#213).

    For example: "outside active hours (08:30-21:30 America/New_York); the next
    window opens at 08:30 tomorrow. Change the active hours in Settings to
    adjust." The log line, the run's note, a refused manual run,
    and the API's answer all use this, so a person reads one wording wherever
    they meet it. ``tz``'s name is printed as given (a :class:`ZoneInfo`'s key).
    ``pinned_in`` is the config file that sets the hours, when one does: it wins over
    Settings (#343), so the sentence names the file instead.
    """
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    local_now = local_time_of(now_utc, zone)
    opens = next_window_start(now_utc, zone, start=start).astimezone(zone)
    days = (opens.date() - local_now.date()).days
    when = {0: "today", 1: "tomorrow"}.get(days, f"on {opens:%A}")
    return (
        f"outside active hours ({start:%H:%M}-{end:%H:%M} {zone.key}); the next window"
        f" opens at {opens:%H:%M} {when}. {active_hours_advice(pinned_in)}"
    )


def active_hours_advice(pinned_in: str | None) -> str:
    """Where to change the active hours: the config file that sets them, or Settings."""
    if pinned_in is not None:
        return f"Change `[linkedin] active_hours` in {pinned_in} to adjust."
    return "Change the active hours in Settings to adjust."


# --- warm-up ---------------------------------------------------------------

# Appendix C: "Warm-up ... New profile, new device: ramp."
DEFAULT_WARMUP_START: Final = 20
DEFAULT_WARMUP_STEP: Final = 10


def warmup_budget(
    days_since_install: int,
    cap: int,
    *,
    start: int = DEFAULT_WARMUP_START,
    step: int = DEFAULT_WARMUP_STEP,
) -> int:
    """The profile-visit budget for a day this many days after install.

    Spec 9.5: "a fresh install starts at 20 profile visits per day and grows
    by 10 per day up to the configured cap." Day 0 (install day) gets
    ``start``; each day after adds ``step``, clamped to ``cap`` — including
    when ``cap`` is below ``start``, which clamps immediately rather than
    raising, since a config change that lowers the cap below where warm-up
    already sits is a real case, not a bug to reject.
    ``days_since_install`` is clamped at 0 so a caller passing a clock that
    runs slightly backward (or a negative test value) never asks for less
    than day 0's budget.
    """
    day = max(days_since_install, 0)
    return min(start + step * day, cap)


# --- weekend multiplier ------------------------------------------------

# Appendix B/C default; spec 9.5: "multiply budgets by 0.5 on Saturday and Sunday by default."
DEFAULT_WEEKEND_MULTIPLIER: Final = 0.5
_WEEKEND_DAYS: Final = frozenset({5, 6})  # date.weekday(): Monday=0 ... Saturday=5, Sunday=6


def is_weekend(local_date: date) -> bool:
    """True for Saturday or Sunday, by the account's local calendar day.

    Local, never UTC's: someone active past 8pm local on a Friday near the
    UTC date boundary should not see Saturday's damping start a few hours
    early just because UTC has already turned over.
    """
    return local_date.weekday() in _WEEKEND_DAYS


def apply_weekend_multiplier(
    budget: int, local_date: date, *, multiplier: float = DEFAULT_WEEKEND_MULTIPLIER
) -> int:
    """``budget`` scaled by ``multiplier`` on Saturday and Sunday; unchanged otherwise.

    Floored, not rounded: this feeds a hard daily cap (spec 9.6), and a
    safety-relevant budget should never round up past what the multiplier
    asked for — 15 times 0.5 is 7.5 visits, and the honest budget for that
    is 7, not 8.
    """
    if not is_weekend(local_date):
        return budget
    return math.floor(budget * multiplier)


# --- composed plan -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class DelayProfile:
    """:func:`human_delay`'s parameters, bundled for :func:`plan_enrichment`."""

    median: float = DEFAULT_DELAY_MEDIAN_S
    sigma: float = DEFAULT_DELAY_SIGMA
    tail_p: float = DEFAULT_TAIL_P
    tail_range: tuple[float, float] = DEFAULT_TAIL_RANGE_S


@dataclass(frozen=True, slots=True)
class BurstProfile:
    """:func:`plan_burst_sizes` and :func:`burst_break`'s parameters, bundled for
    :func:`plan_enrichment`."""

    size_range: tuple[int, int] = DEFAULT_BURST_SIZE_RANGE
    break_range_s: tuple[float, float] = DEFAULT_BURST_BREAK_RANGE_S


DEFAULT_DELAY_PROFILE: Final = DelayProfile()
DEFAULT_BURST_PROFILE: Final = BurstProfile()


@dataclass(frozen=True, slots=True)
class VisitStep:
    """One profile visit: its scroll plan, and the wait after leaving it.

    ``delay_after_s`` is ``None`` on the plan's last step — nothing follows
    it to wait for. ``burst_break`` says whether that wait is a
    between-burst break (spec 9.5's 5-to-20-minute pause) rather than an
    ordinary :func:`human_delay`.
    """

    scroll: ScrollPlan
    delay_after_s: float | None
    burst_break: bool


@dataclass(frozen=True, slots=True)
class EnrichmentPlan:
    """A full deterministic pacing plan for one enrichment run of ``len(steps)`` profile
    visits — the part of spec 9.10's ``EnrichJobSpec`` pacing profile this module owns.

    ``burst_sizes`` sums to ``len(steps)``. Two calls with the same seed and
    the same ``visit_count`` produce an identical plan, in this process or
    another; two different seeds practically never do.
    """

    steps: tuple[VisitStep, ...]
    burst_sizes: tuple[int, ...]

    @property
    def total_delay_s(self) -> float:
        return sum(step.delay_after_s or 0.0 for step in self.steps)


def plan_enrichment(
    rng: random.Random,
    visit_count: int,
    *,
    delay: DelayProfile = DEFAULT_DELAY_PROFILE,
    burst: BurstProfile = DEFAULT_BURST_PROFILE,
    scroll: ScrollProfile = DEFAULT_SCROLL_PROFILE,
) -> EnrichmentPlan:
    """The full pacing plan for a run of ``visit_count`` profile visits.

    Bursts (spec 9.5) chop the run up first; then, per visit, in order:
    :func:`scroll_like_a_person` for the dwell on that profile, and the gap
    before the next one — :func:`burst_break` at a burst boundary,
    :func:`human_delay` otherwise, or nothing after the run's last visit.
    Every draw happens through ``rng``, in this one fixed order, so a given
    seed always retraces the same plan.
    """
    burst_sizes = plan_burst_sizes(rng, visit_count, size_range=burst.size_range)
    # The index (0-based) of the last visit of every burst but the final one:
    # that is where a burst break falls instead of an ordinary human_delay.
    boundary_indexes: set[int] = set()
    cursor = -1
    for size in burst_sizes[:-1]:
        cursor += size
        boundary_indexes.add(cursor)
    steps: list[VisitStep] = []
    for index in range(visit_count):
        scroll_plan = scroll_like_a_person(
            rng,
            steps_range=scroll.steps_range,
            delta_range_px=scroll.delta_range_px,
            pause_range_s=scroll.pause_range_s,
            back_up_p=scroll.back_up_p,
            back_up_delta_range_px=scroll.back_up_delta_range_px,
            dwell_median_s=scroll.dwell_median_s,
            dwell_sigma=scroll.dwell_sigma,
        )
        if index == visit_count - 1:
            steps.append(VisitStep(scroll=scroll_plan, delay_after_s=None, burst_break=False))
        elif index in boundary_indexes:
            gap = burst_break(rng, break_range_s=burst.break_range_s)
            steps.append(VisitStep(scroll=scroll_plan, delay_after_s=gap, burst_break=True))
        else:
            gap = human_delay(
                rng,
                median=delay.median,
                sigma=delay.sigma,
                tail_p=delay.tail_p,
                tail_range=delay.tail_range,
            )
            steps.append(VisitStep(scroll=scroll_plan, delay_after_s=gap, burst_break=False))
    return EnrichmentPlan(steps=tuple(steps), burst_sizes=burst_sizes)


# --- typing plan (P4-10, #376) -------------------------------------------------
#
# PerimeterX collects keystroke timing on LinkedIn's pages, so a uniform
# ``keyboard.type(delay=...)`` is detectable. :func:`typing_plan` decides, purely,
# how a message body is typed: one step per user-perceived character (an extended
# grapheme cluster), with a varied delay before each one. P4-03's prefill only
# replays the plan; nothing here touches a page.
#
# Three things the plan guarantees, because a wrong keystroke in a message box can
# send a message nobody meant to send:
#
# * No step ever carries a line break in its ``chunk``. A plain Enter sends the
#   message, so a newline (CR, LF, or CRLF: :data:`NEWLINE_CHARS`) is its own step
#   with ``newline=True`` and an empty chunk, for the replay to press as
#   Shift+Enter. Even that step is refused unless ``allow_newlines`` is set; its
#   default, :data:`SHIFT_ENTER_NEWLINES_ALLOWED`, is ``True`` since P4-06 (#374)
#   showed that Shift+Enter never sends. Every other line break (VT, FF, FS,
#   GS, RS, NEL, U+2028, U+2029) is refused whatever the flag says.
# * No step carries a code point :func:`is_untypable` refuses: a control character
#   (a tab moves focus), a line or paragraph separator, a lone surrogate, a
#   private-use or unassigned code point, or a format character other than the
#   joiners and tag characters emoji sequences need. The plan refuses the body.
# * No typos and no backspaces: a correction that misfires could leave wrong text,
#   or press a key netkeeper doesn't intend.
#
# The text is never logged, and no exception raised here quotes it.

#: Spec decision 2026-10-03: no plan whose delays add up to more than this many
#: seconds is ever typed.
MAX_TYPING_SECONDS: Final = 300.0
#: Spec decision 2026-10-03: a body longer than this many characters earns a
#: warning; exactly this many is clean. P4-11's (#377) lint raises it;
#: :func:`typing_length_warning` is the rule.
TYPING_WARN_CHARS: Final = 1000
#: The expected typing time (:func:`typing_expected_seconds`) above which P4-11's
#: lint flags a body. Set with a margin under :data:`MAX_TYPING_SECONDS`, so a body
#: the lint passes practically never draws a plan over the ceiling.
TYPING_LINT_SECONDS: Final = 240.0
#: Whether a newline may be typed (as Shift+Enter). P4-06's capture (#374,
#: ``docs/linkedin-messaging-shapes.md``) showed that Shift+Enter never sends a
#: LinkedIn message, whatever the "Press Enter to Send" setting, and ADR 0007 lets
#: P4-03 (#382) set this to ``True`` in the same change that adds the one Shift+Enter
#: press and its pins (``tests/test_browser_safety.py``). This is the single source of
#: truth for the newline flag: P4-11's (#377) lint imports it rather than keeping its
#: own, and the prefill never passes ``allow_newlines``. If a later capture shows
#: Shift+Enter sending, this goes back to ``False`` and multi-line bodies are refused.
SHIFT_ENTER_NEWLINES_ALLOWED: Final = True

#: Every code point treated as a line break, the same set
#: ``netkeeper.campaigns.render`` splits a header on. For reference: only
#: :data:`NEWLINE_CHARS` may ever become a newline step; the rest are always refused.
LINE_BREAK_CHARS: Final = frozenset("\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
#: The only line breaks :data:`SHIFT_ENTER_NEWLINES_ALLOWED` governs: CR and LF,
#: with CRLF as one newline. With the flag on, each becomes a Shift+Enter step.
NEWLINE_CHARS: Final = frozenset("\r\n")

# The format characters (category Cf) an emoji sequence needs: zero-width joiner and
# non-joiner, and the tag characters of a subdivision flag.
_ALLOWED_FORMAT: Final = frozenset(
    {"\u200c", "\u200d"} | {chr(cp) for cp in range(0xE0020, 0xE0080)}
)
_UNTYPABLE_CATEGORIES: Final = frozenset({"Cc", "Zl", "Zp", "Cs", "Co", "Cn"})
# Invisible characters outside Cf: the combining grapheme joiner, the Hangul fillers
# (including the halfwidth one), and the Mongolian free variation selectors.
_INVISIBLE: Final = frozenset(
    {"\u034f", "\u115f", "\u1160", "\u3164", "\uffa0", "\u180b", "\u180c", "\u180d", "\u180f"}
)
# The only variation selectors a step may carry: text (U+FE0E) and emoji (U+FE0F)
# presentation. Every other one (U+FE00 to U+FE0D, U+E0100 to U+E01EF) is refused.
_ALLOWED_VARIATION_SELECTORS: Final = frozenset({"\ufe0e", "\ufe0f"})


def _tag_flag(code: str) -> str:
    """The subdivision flag for ``code``: U+1F3F4, ``code`` in tag characters, cancel tag."""
    return "\U0001f3f4" + "".join(chr(0xE0000 + ord(c)) for c in code) + "\U000e007f"


# The only tag sequences a step may carry: the three RGI subdivision flags, England,
# Scotland, and Wales. Any other tag sequence could carry a hidden payload.
SUBDIVISION_FLAGS: Final = frozenset(_tag_flag(code) for code in ("gbeng", "gbsct", "gbwls"))
_TAG_FIRST: Final = 0xE0020
_TAG_LAST: Final = 0xE007F
# The characters after which, followed by whitespace, the next character gets the
# sentence-end pause instead of the word-boundary pause.
_SENTENCE_ENDS: Final = frozenset(".!?")
_NEWLINE_RE: Final = regex.compile("(\r\n|\r|\n)")
_GRAPHEME_RE: Final = regex.compile(r"\X")


def is_untypable(char: str) -> bool:
    """Whether no typing step may carry this one code point.

    True for a control character (Cc, line breaks included), a line or paragraph
    separator (Zl, Zp), a lone surrogate (Cs), a private-use (Co) or unassigned (Cn)
    code point, and a format character (Cf) other than ZWJ, ZWNJ, and the tag
    characters U+E0020 to U+E007F. That refuses bidi controls, a zero-width space,
    and a byte-order mark. Also true for the invisible combining grapheme joiner
    (U+034F), the Hangul fillers (U+115F, U+1160, U+3164, U+FFA0), the Mongolian
    free variation selectors (U+180B to U+180D, U+180F), and every variation selector
    other than U+FE0E and U+FE0F. Unassigned means unassigned
    in the Unicode version of Python's :mod:`unicodedata`.

    A tag character passes here, but only :func:`is_untypable_cluster` decides
    whether its cluster is a real subdivision flag. P4-11's lint should check whole
    clusters with that function.
    """
    category = unicodedata.category(char)
    if category in _UNTYPABLE_CATEGORIES or char in _INVISIBLE:
        return True
    if _is_refused_variation_selector(char):
        return True
    return category == "Cf" and char not in _ALLOWED_FORMAT


def _is_variation_selector(char: str) -> bool:
    code = ord(char)
    return 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF


def _is_refused_variation_selector(char: str) -> bool:
    return _is_variation_selector(char) and char not in _ALLOWED_VARIATION_SELECTORS


def is_untypable_cluster(cluster: str) -> bool:
    """Whether no typing step may carry this grapheme cluster.

    True when any code point in it is :func:`is_untypable`, when it holds more than
    one variation selector, or when it holds a tag character (U+E0020 to U+E007F) and
    isn't exactly one of :data:`SUBDIVISION_FLAGS` (England, Scotland, or Wales).
    Any other tag sequence can carry invisible text. This is the rule
    :func:`typing_plan` and :class:`TypeStep` apply.
    """
    if any(is_untypable(char) for char in cluster):
        return True
    if sum(_is_variation_selector(char) for char in cluster) > 1:
        return True
    has_tag = any(_TAG_FIRST <= ord(char) <= _TAG_LAST for char in cluster)
    return has_tag and cluster not in SUBDIVISION_FLAGS


class TypingPlanError(ValueError):
    """A body :func:`typing_plan` refuses to plan. The message never quotes the body."""


class TypingTooLong(TypingPlanError):
    """The body's plan would take longer than :data:`MAX_TYPING_SECONDS` to type."""

    def __init__(self, duration_s: float, ceiling_s: float) -> None:
        super().__init__(
            f"typing this body would take {duration_s:.0f} s, over the {ceiling_s:.0f} s ceiling"
        )
        self.duration_s = duration_s
        self.ceiling_s = ceiling_s


class MultilineRefused(TypingPlanError):
    """The body has a newline (CR or LF) and newlines are not allowed (P4-06, #374)."""


class InvalidTypingProfile(TypingPlanError):
    """A :class:`TypingProfile` with a value no plan should use."""


class TypingPlanMismatch(TypingPlanError):
    """The steps a plan built don't type the body back exactly. A safety net: no body
    should reach it, and its message never quotes the body."""


class UnsupportedCharacter(TypingPlanError):
    """The body has a code point :func:`is_untypable` refuses, other than CR or LF.

    Includes every line break outside :data:`NEWLINE_CHARS`, with or without the
    newline flag."""


# The largest sigma a TypingProfile accepts. At 2, a lognormal's 99th percentile is
# about 100 times its median; anything wider is a typo, not a typist.
_MAX_SIGMA: Final = 2.0
# The longest thinking pause a TypingProfile accepts, in seconds.
_MAX_THINKING_S: Final = 60.0


@dataclass(frozen=True, slots=True)
class TypingProfile:
    """:func:`typing_plan`'s parameters. Spec decision 2026-10-03, P4-10 (#376).

    Every delay is a lognormal around its median (a lognormal's median is its scale
    parameter, the same as :func:`human_delay`). The word-boundary and sentence-end
    extras share ``extra_sigma``; the issue fixes only their medians.
    """

    char_median_s: float = 0.14
    char_sigma: float = 0.45
    word_extra_median_s: float = 0.12
    sentence_extra_median_s: float = 0.6
    extra_sigma: float = 0.45
    thinking_p: float = 0.02
    thinking_range_s: tuple[float, float] = (0.8, 2.5)
    floor_s: float = 0.04

    def __post_init__(self) -> None:
        positive = {
            "char_median_s": self.char_median_s,
            "word_extra_median_s": self.word_extra_median_s,
            "sentence_extra_median_s": self.sentence_extra_median_s,
            "floor_s": self.floor_s,
        }
        for name, value in positive.items():
            if not (math.isfinite(value) and value > 0):
                raise InvalidTypingProfile(f"{name} must be finite and positive")
        for name, value in {"char_sigma": self.char_sigma, "extra_sigma": self.extra_sigma}.items():
            if not (math.isfinite(value) and 0 <= value <= _MAX_SIGMA):
                raise InvalidTypingProfile(f"{name} must be between 0 and {_MAX_SIGMA}")
        if not (math.isfinite(self.thinking_p) and 0 <= self.thinking_p <= 1):
            raise InvalidTypingProfile("thinking_p must be between 0 and 1")
        low, high = self.thinking_range_s
        if not (math.isfinite(low) and 0 <= low <= high <= _MAX_THINKING_S):
            raise InvalidTypingProfile(
                f"thinking_range_s must have 0 <= low <= high <= {_MAX_THINKING_S:.0f}"
            )


DEFAULT_TYPING: Final = TypingProfile()


@dataclass(frozen=True, slots=True)
class TypeStep:
    """One keystroke of a plan: wait ``delay_before_s``, then type ``chunk``.

    ``chunk`` is exactly one extended grapheme cluster: a letter with its combining
    marks, or a whole emoji sequence (a ZWJ family, a skin tone, a flag). A step with
    ``newline=True`` has an empty chunk, and the replay presses Shift+Enter for it,
    never a bare Enter. The constructor refuses any other shape, a cluster
    :func:`is_untypable_cluster` refuses, and a delay that is negative or not finite.

    Where a cluster ends can depend on the text before it. With Unicode 17's data, a
    linker such as U+1CF5 joins the consonant after it only when a consonant comes
    before it too: U+0915 U+1CF5 U+0915 splits into U+0915 and the cluster U+1CF5
    U+0915, yet U+1CF5 U+0915 on its own splits in two. So ``chunk`` is checked
    where it was segmented: ``line`` is the line of the body it came from (the text
    between newlines) and ``offset`` is where in ``line`` it starts. With the default
    empty ``line``, the chunk is checked as a line of its own. Neither field is part
    of the step's repr or equality.

    ``line`` holds a whole line of the message body. :func:`dataclasses.asdict`,
    :func:`dataclasses.astuple`, and :mod:`pickle` all include it, so nothing may log,
    serialize, or persist a step; only ``repr`` and ``str`` leave it out.
    """

    chunk: str
    delay_before_s: float
    newline: bool
    line: str = field(default="", repr=False, compare=False)
    offset: int = field(default=0, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.delay_before_s) or self.delay_before_s < 0:
            raise ValueError("delay_before_s must be finite and not negative")
        if type(self.line) is not str or type(self.offset) is not int:
            raise ValueError("a step's line is a str and its offset an int")
        if self.newline:
            if self.chunk:
                raise ValueError("a newline step has an empty chunk")
            return
        if not _is_one_cluster_in(self.chunk, self.line or self.chunk, self.offset):
            raise ValueError("a typing step types exactly one grapheme cluster")
        if is_untypable_cluster(self.chunk):
            raise ValueError("a typing step never types a cluster is_untypable_cluster refuses")

    @property
    def needs_insert_text(self) -> bool:
        """Whether the replay inserts the chunk as text rather than pressing a key.

        Only a single printable ASCII character is safe to press with
        ``keyboard.type``; anything else (an accented letter, an emoji sequence) is
        inserted with ``insert_text``. A newline step is neither: it is Shift+Enter.
        """
        if self.newline:
            return False
        return not (len(self.chunk) == 1 and 0x20 <= ord(self.chunk) <= 0x7E)


def _is_one_cluster_in(chunk: str, line: str, offset: int) -> bool:
    """Whether ``chunk`` is non-empty and is, at ``offset``, one whole grapheme cluster
    of ``line`` as segmenting all of ``line`` finds it."""
    return (
        bool(chunk)
        and line.startswith(chunk, offset)
        and (_cluster_spans(line).get(offset) == offset + len(chunk))
    )


@functools.lru_cache(maxsize=4)
def _cluster_spans(line: str) -> Mapping[int, int]:
    """Where each grapheme cluster of ``line`` starts, mapped to where it ends.

    Cached, because a plan builds one step per cluster of the same line, and read-only,
    because every caller shares the cached mapping. :func:`typing_plan` clears the
    cache when it finishes, so no line of a message outlives its plan here."""
    return MappingProxyType({found.start(): found.end() for found in _GRAPHEME_RE.finditer(line)})


TypingPlan = tuple[TypeStep, ...]


def plan_duration(plan: TypingPlan) -> float:
    """Total seconds the plan's delays add up to."""
    return sum(step.delay_before_s for step in plan)


def typing_length_warning(text: str) -> bool:
    """Whether ``text`` is longer than :data:`TYPING_WARN_CHARS`, and so earns a warning.

    Counts code points, line breaks included.
    """
    return len(text) > TYPING_WARN_CHARS


def _located_units(text: str) -> list[tuple[str, str, int]]:
    """``text`` as typing units, each with the line it came from and its offset there.

    A unit is one grapheme cluster, or ``"\\n"`` for each newline (CR, LF, or CRLF as
    one), which has an empty line. Each line is segmented whole, so a cluster whose
    end depends on the text before it (see :class:`TypeStep`) comes out as it does in
    context. Any other line break stays a unit of its own, for :func:`typing_plan` to
    refuse. No validation here."""
    units: list[tuple[str, str, int]] = []
    for index, piece in enumerate(_NEWLINE_RE.split(text)):
        if index % 2:
            units.append(("\n", "", 0))
        elif piece:
            units.extend(
                (found.group(), piece, found.start()) for found in _GRAPHEME_RE.finditer(piece)
            )
    return units


def _units(text: str) -> list[str]:
    """``text`` as typing units: :func:`_located_units` without the locations."""
    return [unit for unit, _line, _offset in _located_units(text)]


def _extras(units: list[str]) -> list[str | None]:
    """For each unit, the extra pause before it: ``"sentence"``, ``"word"``, or ``None``.

    The first visible unit after a run of whitespace or line breaks gets the
    sentence-end extra when the last visible unit before that run ends a sentence,
    and the word-boundary extra otherwise.
    """
    extras: list[str | None] = []
    pending_boundary = False
    last_visible = ""
    for unit in units:
        is_space = unit == "\n" or unit.isspace()
        if pending_boundary and not is_space:
            extras.append("sentence" if last_visible in _SENTENCE_ENDS else "word")
        else:
            extras.append(None)
        if is_space:
            pending_boundary = bool(last_visible)
        else:
            pending_boundary = False
            last_visible = unit
    return extras


def typing_expected_seconds(text: str, profile: TypingProfile = DEFAULT_TYPING) -> float:
    """The expected seconds :func:`typing_plan` would plan for ``text``. Deterministic.

    The sum, over every unit, of the per-character delay's mean, the thinking pause's
    mean (``thinking_p`` times the middle of its range), and the mean of the word or
    sentence extra where one applies. A lognormal's mean is its median times
    ``exp(sigma ** 2 / 2)``. The floor is ignored; at the default profile it moves the
    mean by well under a millisecond a character. P4-11's lint compares this with
    :data:`TYPING_LINT_SECONDS`. It never raises, whatever the text holds.
    """
    char_mean = profile.char_median_s * math.exp(profile.char_sigma**2 / 2)
    spread = math.exp(profile.extra_sigma**2 / 2)
    extra_mean = {
        "word": profile.word_extra_median_s * spread,
        "sentence": profile.sentence_extra_median_s * spread,
    }
    low, high = profile.thinking_range_s
    thinking_mean = profile.thinking_p * (low + high) / 2
    units = _units(text)
    total = len(units) * (char_mean + thinking_mean)
    total += sum(extra_mean[extra] for extra in _extras(units) if extra is not None)
    return total


def typing_plan(
    text: str,
    rng: random.Random,
    profile: TypingProfile = DEFAULT_TYPING,
    *,
    allow_newlines: bool = SHIFT_ENTER_NEWLINES_ALLOWED,
    max_seconds: float = MAX_TYPING_SECONDS,
) -> TypingPlan:
    """The plan for typing ``text`` the way a person does. P4-10 (#376).

    One step per extended grapheme cluster, each after a lognormal delay around
    ``profile.char_median_s``. The first character after a run of whitespace gets an
    extra pause: the sentence-end extra when the last character before the
    whitespace ends a sentence (``.``, ``!``, or ``?``), the word-boundary extra
    otherwise. Any step has a ``profile.thinking_p`` chance of an added thinking
    pause, uniform across ``profile.thinking_range_s``. Every delay is at least
    ``profile.floor_s``. Draws come from ``rng`` in one fixed order, so the same
    seed and the same text always give the same plan.

    A newline (:data:`NEWLINE_CHARS`: CR, LF, or CRLF as one) is its own step with
    ``newline=True`` and an empty chunk, and only when ``allow_newlines`` is set;
    otherwise the body raises :class:`MultilineRefused`. Only ``\\r\\n``, in that
    order, is one newline: ``\\n\\r`` and ``\\r\\r`` are two newline steps each.
    Every other line break in :data:`LINE_BREAK_CHARS` (VT, FF, FS, GS, RS, NEL,
    U+2028, U+2029), and any other cluster :func:`is_untypable_cluster` refuses,
    raises :class:`UnsupportedCharacter`, whatever ``allow_newlines`` says.

    A plan whose delays add up to more than ``max_seconds``, or to a total that
    isn't finite, raises :class:`TypingTooLong`. ``max_seconds`` can lower the
    ceiling but never raise it past :data:`MAX_TYPING_SECONDS`. The ceiling counts only the planned
    delays, not the round trip of each key press, so real typing takes somewhat
    longer. Because the plan is random, a body near the ceiling can pass with one
    seed and fail with another: P4-03 must build the plan before it spends any
    budget or navigates, so a refusal costs nothing. A plan whose steps don't type
    the body back exactly (newlines as one newline step each) raises
    :class:`TypingPlanMismatch` rather than being returned; no body should reach it.
    No error message quotes the text.
    """
    return _typing_plan_unclamped(
        text,
        rng,
        profile,
        allow_newlines=allow_newlines,
        max_seconds=min(max_seconds, MAX_TYPING_SECONDS),
    )


def _typing_plan_unclamped(
    text: str,
    rng: random.Random,
    profile: TypingProfile,
    *,
    allow_newlines: bool,
    max_seconds: float,
) -> TypingPlan:
    """:func:`typing_plan` with a ceiling that may exceed :data:`MAX_TYPING_SECONDS`.

    Private, for statistical tests that need plans longer than any real message.
    Nothing outside the tests may call it.
    """
    located = _located_units(text)
    units = [unit for unit, _line, _offset in located]
    for position, unit in enumerate(units):
        if unit == "\n":
            if not allow_newlines:
                raise MultilineRefused(
                    "this body has a line break, and typing one (Shift+Enter) is not allowed"
                )
            continue
        for char in unit:
            if is_untypable(char):
                raise UnsupportedCharacter(
                    f"character {position} holds U+{ord(char):04X}, which no step types"
                )
        if is_untypable_cluster(unit):
            raise UnsupportedCharacter(
                f"character {position} is a tag or variation-selector sequence no step types"
            )

    try:
        plan = _steps(located, units, rng, profile)
    finally:
        _cluster_spans.cache_clear()
    typed = "".join("\n" if step.newline else step.chunk for step in plan)
    if typed != _NEWLINE_RE.sub("\n", text):
        raise TypingPlanMismatch("the plan's steps do not type this body back exactly")
    duration = plan_duration(plan)
    if not (math.isfinite(duration) and duration <= max_seconds):
        raise TypingTooLong(duration, max_seconds)
    return plan


def _steps(
    located: list[tuple[str, str, int]],
    units: list[str],
    rng: random.Random,
    profile: TypingProfile,
) -> TypingPlan:
    """One step per unit, drawing the delays from ``rng`` in :func:`typing_plan`'s order."""
    steps: list[TypeStep] = []
    extra_medians = {
        "word": profile.word_extra_median_s,
        "sentence": profile.sentence_extra_median_s,
    }
    for (unit, line, offset), extra in zip(located, _extras(units), strict=True):
        delay = rng.lognormvariate(math.log(profile.char_median_s), profile.char_sigma)
        if extra is not None:
            delay += rng.lognormvariate(math.log(extra_medians[extra]), profile.extra_sigma)
        if rng.random() < profile.thinking_p:
            delay += rng.uniform(*profile.thinking_range_s)
        delay = max(delay, profile.floor_s)
        if unit == "\n":
            steps.append(TypeStep(chunk="", delay_before_s=delay, newline=True))
        else:
            steps.append(
                TypeStep(chunk=unit, delay_before_s=delay, newline=False, line=line, offset=offset)
            )

    return tuple(steps)
