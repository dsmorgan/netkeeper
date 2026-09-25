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

import math
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

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
