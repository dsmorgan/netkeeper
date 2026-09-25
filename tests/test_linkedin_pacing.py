"""netkeeper.linkedin.pacing: human-like pacing for the extractor, spec 9.5, item P2-04.

Two things this module has to get right, and that this file exists to prove:

* **Determinism per seed.** Every function here takes its own
  :class:`random.Random` rather than reaching for the module-level
  :mod:`random`. The tests below run the same seed twice — in this process
  and, for the composed plan, in two independent subprocesses — and assert
  byte-identical output; a different seed almost never matches.
* **Active hours with midnight wrap.** ``start < now < end`` is false for
  every hour of a 22:00-06:00 window; :func:`is_active_hour` is tested at
  both boundaries and on either side, for an ordinary window and a wrapping
  one.

Every statistical assertion below states, in a comment, the range it bounds
and why: wide enough that a correct implementation never flakes, narrow
enough that a meaningful change to the distribution's parameters (checked by
hand against every bound in this file while writing it) fails the test.
"""

from __future__ import annotations

import random
import statistics
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from netkeeper.linkedin import pacing

# 2026 US transitions in America/New_York: spring forward 2026-03-08 02:00 EST -> 03:00 EDT,
# fall back 2026-11-01 02:00 EDT -> 01:00 EST. Used only by the next_window_start DST tests.
_NY = "America/New_York"


# --- the boundary -----------------------------------------------------------
# Checked for every module under linkedin/, each in its own subprocess, by
# tests/test_browser_safety.py::test_no_extractor_module_drags_the_database_in.
# The list it reads lives in tests/boundary.py; this module used to carry a copy.


# --- Appendix C / config.example.toml: the defaults themselves --------------


def test_module_defaults_match_appendix_c_and_config_example_toml() -> None:
    """Every default this module ships pinned against its spec value, exactly.

    Every other test in this file passes its own arguments (deliberately, to
    prove the function honors them — see the ``uses_its_..._argument`` tests
    throughout), which means none of them ever exercises the *default*
    values these constants provide. A fat-fingered default — Appendix C's
    warm-up start silently becoming 5 instead of 20, say — would sail
    through the rest of the suite. This is the one place that would catch
    it: a plain equality check against the value spec Appendix C (and, for
    active hours, Appendix B's ``config.example.toml``) actually states.
    """
    assert pacing.DEFAULT_DELAY_MEDIAN_S == 25.0
    assert pacing.DEFAULT_DELAY_SIGMA == 0.6
    assert pacing.DEFAULT_TAIL_P == 0.08
    assert pacing.DEFAULT_TAIL_RANGE_S == (120.0, 480.0)
    assert pacing.DEFAULT_BURST_SIZE_RANGE == (8, 15)
    assert pacing.DEFAULT_BURST_BREAK_RANGE_S == (300.0, 1200.0)
    assert pacing.DEFAULT_WARMUP_START == 20
    assert pacing.DEFAULT_WARMUP_STEP == 10
    assert pacing.DEFAULT_WEEKEND_MULTIPLIER == 0.5
    # config.example.toml's [linkedin] active_hours entry: 08:30 through 21:30.
    assert time(8, 30) == pacing.DEFAULT_ACTIVE_START
    assert time(21, 30) == pacing.DEFAULT_ACTIVE_END


# --- human_delay --------------------------------------------------------


def test_human_delay_same_seed_is_identical() -> None:
    """Same seed, same sequence — the core determinism guarantee (spec P2-04 done-when)."""
    seq1 = [pacing.human_delay(random.Random(42)) for _ in range(50)]
    seq2 = [pacing.human_delay(random.Random(42)) for _ in range(50)]
    assert seq1 == seq2


def test_human_delay_different_seed_differs() -> None:
    seq1 = [pacing.human_delay(random.Random(1)) for _ in range(50)]
    seq2 = [pacing.human_delay(random.Random(2)) for _ in range(50)]
    assert seq1 != seq2


def test_human_delay_ignores_module_level_random_state() -> None:
    """Global :mod:`random` state moving between calls must not change the result.

    If ``human_delay`` ever reached for the module-level generator instead of
    ``rng``, spinning the global state between these two calls would make
    them diverge; ``rng``'s own state is untouched by that, so they stay
    identical.
    """
    random.seed(0)
    first = [pacing.human_delay(random.Random(7)) for _ in range(10)]
    for _ in range(1000):  # perturb the module-level generator's state
        random.random()
    random.seed(999)
    second = [pacing.human_delay(random.Random(7)) for _ in range(10)]
    assert first == second


def test_human_delay_rejects_nonpositive_median() -> None:
    with pytest.raises(ValueError, match="median"):
        pacing.human_delay(random.Random(1), median=0)


def test_human_delay_always_positive() -> None:
    rng = random.Random(3)
    assert all(pacing.human_delay(rng) > 0 for _ in range(1000))


def test_human_delay_median_is_bounded_around_the_configured_value() -> None:
    """Sample median of 5000 draws at the default ``median=25`` lands near 25.

    Only ``tail_p`` (8%) of draws get the extra distraction pause, which
    pushes their *rank* up but not the 50th percentile itself, so the sample
    median should sit close to the base lognormal's median regardless of the
    tail. Measured for this exact seed: ~26.2. [20, 32] is generous around
    that but would already reject a ``median`` that silently drifted to, say,
    40 or 15 — the mutation this guards against.
    """
    rng = random.Random(12345)
    samples = [pacing.human_delay(rng) for _ in range(5000)]
    assert 20 <= statistics.median(samples) <= 32


def test_human_delay_tail_fraction_is_bounded() -> None:
    """About ``tail_p`` of draws should carry the distraction pause.

    Counted as "delay > 150s": the base lognormal (median 25, sigma 0.6)
    clears 150 on its own only about 0.45% of the time, so this is a close
    proxy for "the tail fired" without instrumenting the function. Expected
    count at n=5000, p=0.08 is 400, stdev ~19.2; [300, 500] is about a
    5-sigma band, wide enough never to flake on this seed (measured: 406) but
    tight enough that halving or doubling ``tail_p`` (checked by hand: 215
    and 814 respectively, both well outside the band) fails it.
    """
    rng = random.Random(12345)
    samples = [pacing.human_delay(rng) for _ in range(5000)]
    over_150 = sum(1 for s in samples if s > 150)
    assert 300 <= over_150 <= 500


def test_human_delay_uses_its_median_and_sigma_arguments() -> None:
    """A non-default ``median``/``sigma`` actually changes the distribution.

    Low sigma (0.1) and the tail disabled keep the sample tight around
    ``median=100``; a function that ignored these arguments (hard-coded to
    the 25s default) would fail both assertions outright.
    """
    rng = random.Random(12345)
    samples = [
        pacing.human_delay(rng, median=100.0, sigma=0.1, tail_p=0.0, tail_range=(0.0, 0.0))
        for _ in range(5000)
    ]
    assert 90 <= statistics.median(samples) <= 110
    assert max(samples) < 200  # sigma=0.1 around 100 never gets close to 200 without the tail


def test_human_delay_tail_p_zero_never_adds_the_tail() -> None:
    rng = random.Random(12345)
    samples = [pacing.human_delay(rng, tail_p=0.0) for _ in range(5000)]
    # Without the tail, the lognormal(median=25, sigma=0.6) essentially never reaches 150
    # (~0.45% per draw); over 5000 draws a handful may, but nothing like the ~400 the
    # default tail_p=0.08 produces (test above). A generous ceiling that still separates
    # the two regimes cleanly:
    assert sum(1 for s in samples if s > 150) < 50


# --- scroll_like_a_person -------------------------------------------------


def test_scroll_like_a_person_same_seed_is_identical() -> None:
    plan1 = pacing.scroll_like_a_person(random.Random(11))
    plan2 = pacing.scroll_like_a_person(random.Random(11))
    assert plan1 == plan2


def test_scroll_like_a_person_different_seed_differs() -> None:
    plan1 = pacing.scroll_like_a_person(random.Random(1))
    plan2 = pacing.scroll_like_a_person(random.Random(2))
    assert plan1 != plan2


def test_scroll_like_a_person_step_count_within_configured_range() -> None:
    rng = random.Random(4)
    for _ in range(500):
        plan = pacing.scroll_like_a_person(rng, steps_range=(3, 9))
        assert 3 <= len(plan.steps) <= 9


def test_scroll_like_a_person_deltas_within_configured_magnitude() -> None:
    rng = random.Random(5)
    for _ in range(500):
        plan = pacing.scroll_like_a_person(
            rng, delta_range_px=(120, 900), back_up_delta_range_px=(80, 300)
        )
        for step in plan.steps:
            if step.delta_px < 0:
                assert -300 <= step.delta_px <= -80
            else:
                assert 120 <= step.delta_px <= 900


def test_scroll_like_a_person_back_up_fraction_is_bounded() -> None:
    """About ``back_up_p`` of wheel events scroll back up (negative delta).

    Over ~12000 steps at the default ``back_up_p=0.15`` this seed measures
    ~14.8%; [0.10, 0.20] safely brackets that while still catching a
    ``back_up_p`` that silently became 0 (checked: 0.0%) or 0.3 (checked:
    ~29.8%, also outside the band).
    """
    rng = random.Random(2024)
    total = 0
    back_up = 0
    for _ in range(2000):
        plan = pacing.scroll_like_a_person(rng)
        for step in plan.steps:
            total += 1
            if step.delta_px < 0:
                back_up += 1
    fraction = back_up / total
    assert 0.10 <= fraction <= 0.20


def test_scroll_like_a_person_back_up_p_zero_never_scrolls_up() -> None:
    rng = random.Random(6)
    for _ in range(200):
        plan = pacing.scroll_like_a_person(rng, back_up_p=0.0)
        assert all(step.delta_px >= 0 for step in plan.steps)


def test_scroll_like_a_person_dwell_is_bounded_around_the_configured_median() -> None:
    rng = random.Random(8)
    dwells = [pacing.scroll_like_a_person(rng, dwell_median_s=3.0).dwell_s for _ in range(2000)]
    # Same reasoning as human_delay's median bound, at the scroll module's own default.
    assert 2.0 <= statistics.median(dwells) <= 4.5
    assert all(d > 0 for d in dwells)


def test_scroll_plan_total_delta_px_sums_the_steps() -> None:
    plan = pacing.ScrollPlan(
        steps=(
            pacing.ScrollStep(delta_px=200, pause_s=0.5),
            pacing.ScrollStep(delta_px=-50, pause_s=0.4),
            pacing.ScrollStep(delta_px=300, pause_s=0.6),
        ),
        dwell_s=2.0,
    )
    assert plan.total_delta_px == 450


# --- rest_pointer_like_a_person (#192) --------------------------------------


def test_rest_pointer_like_a_person_same_seed_is_identical() -> None:
    plan1 = pacing.rest_pointer_like_a_person(random.Random(11))
    plan2 = pacing.rest_pointer_like_a_person(random.Random(11))
    assert plan1 == plan2


def test_rest_pointer_like_a_person_different_seed_differs() -> None:
    plan1 = pacing.rest_pointer_like_a_person(random.Random(1))
    plan2 = pacing.rest_pointer_like_a_person(random.Random(2))
    assert plan1 != plan2


def test_rest_pointer_like_a_person_step_count_within_configured_range() -> None:
    rng = random.Random(4)
    for _ in range(500):
        plan = pacing.rest_pointer_like_a_person(rng, steps_range=(2, 4))
        assert 2 <= len(plan.steps) <= 4


def test_rest_pointer_like_a_person_jitter_within_configured_magnitude() -> None:
    rng = random.Random(5)
    for _ in range(500):
        plan = pacing.rest_pointer_like_a_person(rng, jitter_px=40)
        for step in plan.steps:
            assert -40 <= step.dx <= 40
            assert -40 <= step.dy <= 40


def test_rest_pointer_like_a_person_last_step_always_lands_exactly_on_target() -> None:
    """A hand's final resting point is precise; the wobble is only on the way there
    (#192) -- this is what lets a caller compute the exact resting point without
    replaying the whole walk."""
    rng = random.Random(6)
    for _ in range(500):
        plan = pacing.rest_pointer_like_a_person(rng)
        assert (plan.steps[-1].dx, plan.steps[-1].dy) == (0, 0)


def test_rest_pointer_like_a_person_earlier_steps_are_not_all_on_target() -> None:
    """The last step is exact by design; an earlier one usually is not -- this is
    what tells a "the walk never jitters" regression from a legitimate rare draw of
    ``dx == dy == 0`` apart."""
    rng = random.Random(7)
    earlier_on_target = 0
    earlier_total = 0
    for _ in range(500):
        plan = pacing.rest_pointer_like_a_person(rng)
        for step in plan.steps[:-1]:
            earlier_total += 1
            if (step.dx, step.dy) == (0, 0):
                earlier_on_target += 1
    assert earlier_total > 0
    assert earlier_on_target < earlier_total


def test_rest_pointer_like_a_person_pauses_within_configured_range() -> None:
    rng = random.Random(9)
    for _ in range(500):
        plan = pacing.rest_pointer_like_a_person(rng, pause_range_s=(0.05, 0.2))
        for step in plan.steps:
            assert 0.05 <= step.pause_s <= 0.2


def test_rest_pointer_like_a_person_step_count_out_of_range_raises() -> None:
    """A user-facing config knob (or a config mistake) must fail loudly rather than
    silently produce an empty or wrong-shaped walk, the same as
    :func:`pacing.plan_burst_sizes`'s ``size_range`` guard."""
    with pytest.raises(ValueError):
        pacing.rest_pointer_like_a_person(random.Random(1), steps_range=(0, 3))


# --- bursts ----------------------------------------------------------------


@pytest.mark.parametrize("count", [0, -3, 1, 5, 8, 15, 16, 30, 100, 500])
def test_plan_burst_sizes_sums_to_count_and_respects_range(count: int) -> None:
    rng = random.Random(count)  # a different seed per case, still deterministic
    sizes = pacing.plan_burst_sizes(rng, count, size_range=(8, 15))
    if count <= 0:
        assert sizes == ()
        return
    assert sum(sizes) == count
    assert all(8 <= s <= 15 for s in sizes[:-1])
    assert 0 < sizes[-1] <= 15  # the last burst may be short, never long


def test_plan_burst_sizes_same_seed_is_identical() -> None:
    sizes1 = pacing.plan_burst_sizes(random.Random(9), 200)
    sizes2 = pacing.plan_burst_sizes(random.Random(9), 200)
    assert sizes1 == sizes2


def test_plan_burst_sizes_uses_its_size_range_argument() -> None:
    rng = random.Random(9)
    sizes = pacing.plan_burst_sizes(rng, 300, size_range=(2, 4))
    assert all(2 <= s <= 4 for s in sizes[:-1])
    assert 0 < sizes[-1] <= 4


@pytest.mark.parametrize(
    "size_range",
    [
        (0, 0),  # would loop forever: rng.randint(0, 0) is always 0, remaining never shrinks
        (0, 4),  # a burst of 0 visits is not a burst; low end below 1 is never valid
        (-2, 4),
        (5, 3),  # high below low
    ],
)
def test_plan_burst_sizes_rejects_an_invalid_size_range(size_range: tuple[int, int]) -> None:
    """``burst_size`` is a user-facing config knob (``[linkedin.pacing]`` in
    ``config.example.toml``); a typo there must raise here, not hang or silently produce
    zero-length bursts. ``(0, 0)`` is the sharpest case: unvalidated, it is an infinite loop,
    not merely a bad plan — caught with a timeout in review, not caught by the suite at all.
    """
    with pytest.raises(ValueError, match="size_range"):
        pacing.plan_burst_sizes(random.Random(1), 10, size_range=size_range)


def test_plan_burst_sizes_validates_before_the_count_shortcut() -> None:
    """The validation applies even to a call that would otherwise return ``()`` immediately,
    so a bad ``size_range`` is never masked by also passing ``count <= 0``."""
    with pytest.raises(ValueError, match="size_range"):
        pacing.plan_burst_sizes(random.Random(1), 0, size_range=(0, 0))


def test_burst_break_within_configured_range_and_bounded_mean() -> None:
    """Uniform over ``break_range_s``: min/max are exact invariants; the mean is a loose
    sanity check (expected 750 at the default (300, 1200) range; measured ~742)."""
    rng = random.Random(999)
    breaks = [pacing.burst_break(rng) for _ in range(5000)]
    assert min(breaks) >= 300
    assert max(breaks) <= 1200
    assert 700 <= statistics.mean(breaks) <= 800


def test_burst_break_uses_its_break_range_argument() -> None:
    rng = random.Random(999)
    breaks = [pacing.burst_break(rng, break_range_s=(10.0, 20.0)) for _ in range(2000)]
    assert min(breaks) >= 10
    assert max(breaks) <= 20


# --- active hours: the midnight-wrap case -----------------------------------


@pytest.mark.parametrize(
    ("local_time", "start", "end", "expected"),
    [
        # Ordinary window, 09:00-17:00.
        (time(9, 0), time(9, 0), time(17, 0), True),  # start boundary: inclusive
        (time(16, 59, 59), time(9, 0), time(17, 0), True),  # just before end
        (time(17, 0), time(9, 0), time(17, 0), False),  # end boundary: exclusive
        (time(8, 59, 59), time(9, 0), time(17, 0), False),  # just before start
        (time(12, 0), time(9, 0), time(17, 0), True),  # interior
        (time(0, 0), time(9, 0), time(17, 0), False),  # far outside
        (time(23, 59, 59), time(9, 0), time(17, 0), False),  # far outside
        # Wrapping window, 22:00-06:00 (spans midnight) — the case spec 9.5 calls out.
        (time(22, 0), time(22, 0), time(6, 0), True),  # start boundary: inclusive
        (time(5, 59, 59), time(22, 0), time(6, 0), True),  # just before end
        (time(6, 0), time(22, 0), time(6, 0), False),  # end boundary: exclusive
        (time(21, 59, 59), time(22, 0), time(6, 0), False),  # just before start
        (time(0, 0), time(22, 0), time(6, 0), True),  # midnight itself, inside the wrap
        (time(3, 0), time(22, 0), time(6, 0), True),  # small hours, inside the wrap
        (time(12, 0), time(22, 0), time(6, 0), False),  # midday, outside the wrap
        (time(21, 0), time(22, 0), time(6, 0), False),  # evening, still outside
        # start == end: active all day (Appendix B's "all days" default), not empty.
        (time(0, 0), time(9, 0), time(9, 0), True),
        (time(23, 59), time(9, 0), time(9, 0), True),
    ],
)
def test_is_active_hour_boundaries(
    local_time: time, start: time, end: time, expected: bool
) -> None:
    assert pacing.is_active_hour(local_time, start, end) is expected


def test_is_active_hour_naive_window_arithmetic_would_be_wrong_for_every_wrap_hour() -> None:
    """The exact failure mode spec 9.5 warns about: ``start <= t <= end`` is false for
    every hour of a 22:00-06:00 window. Confirms the real function disagrees with that
    naive check across the whole wrapped range, not just at one sample point."""
    start, end = time(22, 0), time(6, 0)
    wrap_hours = [time(h, 0) for h in (22, 23, 0, 1, 2, 3, 4, 5)]
    for t in wrap_hours:
        naive_says_active = start <= t <= end
        assert naive_says_active is False
        assert pacing.is_active_hour(t, start, end) is True


# --- active hours: timezone conversion and the next window start -----------


def test_local_time_of_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="aware"):
        pacing.local_time_of(datetime(2026, 9, 22, 12, 0), "America/New_York")


def test_local_time_of_converts_and_stays_aware() -> None:
    now_utc = datetime(2026, 9, 22, 16, 0, tzinfo=UTC)  # 16:00 UTC
    local = pacing.local_time_of(now_utc, "America/New_York")  # UTC-4 in September (EDT)
    assert local.tzinfo is not None
    assert local.time() == time(12, 0)


def test_is_active_at_uses_local_not_utc_time() -> None:
    # 23:30 UTC is 19:30 EDT — inside the default 08:30-21:30 window locally, though
    # already past 21:30 if (wrongly) read as UTC.
    now_utc = datetime(2026, 9, 22, 23, 30, tzinfo=UTC)
    assert pacing.is_active_at(now_utc, "America/New_York") is True
    assert pacing.is_active_hour(now_utc.time()) is False  # the wrong-zone answer, for contrast


def test_next_window_start_is_always_strictly_future_and_aware_utc() -> None:
    tz = "America/New_York"
    for hour in range(24):
        now_utc = datetime(2026, 9, 22, hour, 0, tzinfo=UTC)
        nxt = pacing.next_window_start(now_utc, tz, start=time(22, 0))
        assert nxt.tzinfo is UTC
        assert nxt > now_utc


def test_next_window_start_before_todays_start_stays_same_day() -> None:
    # 12:00 UTC = 08:00 EDT, before the 22:00 local start: next start is today, 22:00 local.
    now_utc = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    nxt = pacing.next_window_start(now_utc, "America/New_York", start=time(22, 0))
    assert nxt == datetime(2026, 9, 23, 2, 0, tzinfo=UTC)  # 22:00 EDT == 02:00 UTC next day


def test_next_window_start_exactly_at_start_rolls_to_the_next_day() -> None:
    # 02:00 UTC on the 23rd is exactly 22:00 EDT on the 22nd — already at the boundary,
    # already active, so the *next* start is the following day's.
    now_utc = datetime(2026, 9, 23, 2, 0, tzinfo=UTC)
    nxt = pacing.next_window_start(now_utc, "America/New_York", start=time(22, 0))
    assert nxt == datetime(2026, 9, 24, 2, 0, tzinfo=UTC)


def test_next_window_start_after_start_rolls_to_the_next_day() -> None:
    # 04:00 UTC = 00:00 EDT, already past the 22:00 start: next start is tomorrow.
    now_utc = datetime(2026, 9, 23, 4, 0, tzinfo=UTC)
    nxt = pacing.next_window_start(now_utc, "America/New_York", start=time(22, 0))
    assert nxt == datetime(2026, 9, 24, 2, 0, tzinfo=UTC)


# --- next_window_start across a DST transition ------------------------

# Every next_window_start test above uses 2026-09-22/23, nowhere near a DST transition, so
# none of them would notice next_window_start silently adding 24 hours of UTC instead of a
# day of local wall time — the two agree except right around a transition. These sweep both
# 2026 US transitions in America/New_York (spring forward 2026-03-08, fall back 2026-11-01).


def test_next_window_start_keeps_the_local_wall_time_across_spring_forward() -> None:
    """The window opens at 08:30 *local*, not 24 hours after yesterday's opening.

    The day the clocks jump forward is 23 hours long, so adding 24 hours in
    UTC would park the job at 09:30 local. Adding a day of wall time and
    converting afterwards keeps it at 08:30.
    """
    now_utc = datetime(2026, 3, 7, 19, 0, tzinfo=UTC)  # 14:00 EST, the day before
    nxt = pacing.next_window_start(now_utc, _NY, start=time(8, 30))
    assert nxt == datetime(2026, 3, 8, 12, 30, tzinfo=UTC)  # 08:30 EDT, not 13:30Z
    assert nxt.astimezone(ZoneInfo(_NY)).time() == time(8, 30)


def test_next_window_start_keeps_the_local_wall_time_across_fall_back() -> None:
    """Mirror case: the fall-back day is 25 hours long, so a UTC +24h would fire at 07:30 local."""
    now_utc = datetime(2026, 10, 31, 18, 0, tzinfo=UTC)  # 14:00 EDT, the day before
    nxt = pacing.next_window_start(now_utc, _NY, start=time(8, 30))
    assert nxt == datetime(2026, 11, 1, 13, 30, tzinfo=UTC)  # 08:30 EST, not 12:30Z
    assert nxt.astimezone(ZoneInfo(_NY)).time() == time(8, 30)


def test_next_window_start_in_the_spring_forward_gap_lands_just_after_the_jump() -> None:
    """A start time that does not exist on the transition day resolves forward, not backward.

    02:30 never happens on 2026-03-08. The returned instant is the first
    real moment at or after it (03:30 EDT), which is inside the window,
    rather than an instant an hour *before* the window opens.
    """
    now_utc = datetime(2026, 3, 7, 14, 0, tzinfo=UTC)  # 09:00 EST, already past 02:30
    nxt = pacing.next_window_start(now_utc, _NY, start=time(2, 30))
    assert nxt > now_utc
    local = nxt.astimezone(ZoneInfo(_NY))
    assert local.date() == date(2026, 3, 8)
    assert local.time() >= time(2, 30)
    assert pacing.is_active_at(nxt, _NY, start=time(2, 30), end=time(21, 30)) is True


def test_next_window_start_in_the_fall_back_repeated_hour_is_unambiguous_and_future() -> None:
    """01:30 happens twice on 2026-11-01; whichever is picked must still be in the future.

    From inside the *second* pass of the repeated hour (01:15 EST) the first
    01:30 is gone, so the next opening is the second one, 15 minutes out —
    not a naive "today's 01:30", which would already be in the past.
    """
    now_utc = datetime(2026, 11, 1, 6, 15, tzinfo=UTC)  # 01:15 EST, the repeat
    nxt = pacing.next_window_start(now_utc, _NY, start=time(1, 30))
    assert nxt > now_utc
    assert nxt == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


def test_next_window_start_from_the_repeated_hours_first_pass_parks_tomorrow() -> None:
    """The documented arguable-but-correct case: called from the *first* pass of the
    repeated hour with a ``start`` earlier in that hour, this skips the *second* pass of
    today's same local hour and parks for tomorrow instead — see :func:`next_window_start`'s
    docstring. "Now" is 01:45 EDT, the first pass (fold=0); ``start`` is 01:15, earlier in
    the hour. The second pass's 01:15 (fold=1, 06:15 UTC) is a real, later, same-day
    instant, but it is never considered here, because ``candidate`` inherits "now"'s fold
    rather than trying both.
    """
    now_utc = datetime(2026, 11, 1, 5, 45, tzinfo=UTC)  # 01:45 EDT, first pass
    nxt = pacing.next_window_start(now_utc, _NY, start=time(1, 15))
    assert nxt > now_utc
    # Not the same day's second-pass 01:15 (2026-11-01 06:15 UTC, still in the future) —
    # tomorrow's first-pass-equivalent 01:15 instead.
    assert nxt == datetime(2026, 11, 2, 6, 15, tzinfo=UTC)
    assert nxt.astimezone(ZoneInfo(_NY)) == datetime(2026, 11, 2, 1, 15, tzinfo=ZoneInfo(_NY))


@pytest.mark.parametrize("start", [time(8, 30), time(2, 30), time(1, 30), time(0, 0)])
@pytest.mark.parametrize(
    "first_day", [datetime(2026, 3, 6, tzinfo=UTC), datetime(2026, 10, 30, tzinfo=UTC)]
)
def test_next_window_start_is_strictly_future_every_quarter_hour_across_a_transition(
    start: time, first_day: datetime
) -> None:
    """The one invariant a parked one-shot job depends on, swept over both transitions.

    A returned instant that is not strictly in the future makes the
    scheduler re-fire immediately and spin. Checked every 15 minutes across
    4 days straddling each transition, for four different ``start`` times
    including one (01:30) that falls inside the fall-back repeated hour and
    one (02:30) that falls inside the spring-forward gap.
    """
    now = first_day
    while now < first_day + timedelta(days=4):
        assert pacing.next_window_start(now, _NY, start=start) > now
        now += timedelta(minutes=15)


# --- warm-up -----------------------------------------------------------


@pytest.mark.parametrize(
    ("days_since_install", "expected"),
    [
        (0, 20),
        (1, 30),
        (2, 40),
        (3, 50),
        (4, 60),  # exactly at the cap
        (5, 60),  # would be 70, clamped
        (100, 60),  # long past install, still clamped
        (-5, 20),  # a backward-running clock never asks for less than day 0
    ],
)
def test_warmup_budget_ramps_then_clamps(days_since_install: int, expected: int) -> None:
    assert pacing.warmup_budget(days_since_install, cap=60, start=20, step=10) == expected


def test_warmup_budget_clamps_immediately_when_cap_is_below_start() -> None:
    assert pacing.warmup_budget(0, cap=10, start=20, step=10) == 10
    assert pacing.warmup_budget(30, cap=10, start=20, step=10) == 10


def test_warmup_budget_uses_its_start_and_step_arguments() -> None:
    """A different start/step actually changes the ramp — catches a hard-coded 20/10."""
    assert pacing.warmup_budget(3, cap=50, start=5, step=2) == 11  # 5 + 2*3


# --- weekend multiplier ------------------------------------------------


@pytest.mark.parametrize(
    ("local_date", "expect_weekend"),
    [
        (date(2026, 9, 21), False),  # Monday
        (date(2026, 9, 22), False),  # Tuesday
        (date(2026, 9, 23), False),  # Wednesday
        (date(2026, 9, 24), False),  # Thursday
        (date(2026, 9, 25), False),  # Friday
        (date(2026, 9, 26), True),  # Saturday
        (date(2026, 9, 27), True),  # Sunday
    ],
)
def test_is_weekend_over_a_full_week(local_date: date, expect_weekend: bool) -> None:
    assert pacing.is_weekend(local_date) is expect_weekend


def test_apply_weekend_multiplier_leaves_weekdays_unchanged() -> None:
    assert pacing.apply_weekend_multiplier(60, date(2026, 9, 22)) == 60  # Tuesday


def test_apply_weekend_multiplier_halves_and_floors_on_the_weekend() -> None:
    saturday = date(2026, 9, 26)
    assert pacing.apply_weekend_multiplier(60, saturday) == 30
    # 15 * 0.5 = 7.5: floored to 7, never rounded up to 8 (a safety-relevant cap).
    assert pacing.apply_weekend_multiplier(15, saturday) == 7


def test_apply_weekend_multiplier_uses_its_multiplier_argument() -> None:
    saturday = date(2026, 9, 26)
    assert pacing.apply_weekend_multiplier(100, saturday, multiplier=0.25) == 25


# --- plan_enrichment: the composed plan generator ---------------------


def test_plan_enrichment_same_seed_is_identical() -> None:
    plan1 = pacing.plan_enrichment(random.Random(42), 30)
    plan2 = pacing.plan_enrichment(random.Random(42), 30)
    assert plan1 == plan2


def test_plan_enrichment_different_seed_differs() -> None:
    plan1 = pacing.plan_enrichment(random.Random(1), 30)
    plan2 = pacing.plan_enrichment(random.Random(2), 30)
    assert plan1 != plan2


def test_plan_enrichment_forwards_all_three_profile_arguments() -> None:
    """``delay``, ``burst``, and ``scroll`` must actually reach the functions they parametrize.

    Every other ``plan_enrichment`` test in this file passes the *default*
    profiles, so none of them would notice ``plan_enrichment`` silently
    calling ``human_delay(rng)``, ``scroll_like_a_person(rng)``, and
    ``plan_burst_sizes(rng, visit_count)`` with no forwarding at all — the
    defaults would produce a plausible-looking plan either way. This test
    passes profiles far outside their defaults specifically so that using
    the real defaults instead would be unmistakable: spec 9.7's heat
    response ("while warm, human_delay medians stretch") is exactly the
    caller this guards — P2-05/P2-07 will express that stretch as
    ``DelayProfile(median=...)``, and a forwarding regression here would
    silently put every gap back to 25s at full heat.
    """
    delay = pacing.DelayProfile(median=1000.0, sigma=0.05, tail_p=0.0, tail_range=(0.0, 0.0))
    burst = pacing.BurstProfile(size_range=(2, 3), break_range_s=(5000.0, 6000.0))
    scroll = pacing.ScrollProfile(
        steps_range=(1, 1),
        delta_range_px=(500, 500),
        pause_range_s=(0.1, 0.1),
        back_up_p=0.0,
        back_up_delta_range_px=(1, 1),
        dwell_median_s=1.0,
        dwell_sigma=0.01,
    )
    plan = pacing.plan_enrichment(random.Random(5), 30, delay=delay, burst=burst, scroll=scroll)

    # burst: every burst but the last sized 2 or 3 (default range is 8-15).
    assert all(2 <= size <= 3 for size in plan.burst_sizes[:-1])

    # scroll: every visit's plan is exactly the one fixed step this profile allows
    # (default steps_range is 3-9 wheel events of variable magnitude).
    for step in plan.steps:
        assert len(step.scroll.steps) == 1
        assert step.scroll.steps[0].delta_px == 500
        assert step.scroll.steps[0].pause_s == pytest.approx(0.1)
        assert 0.5 <= step.scroll.dwell_s <= 2.0  # tight around dwell_median_s=1.0

    # delay: ordinary gaps land near median=1000 (default median is 25); burst-break
    # gaps land inside break_range_s=(5000, 6000) (default range is (300, 1200)).
    ordinary_gaps = [
        step.delay_after_s
        for step in plan.steps
        if step.delay_after_s is not None and not step.burst_break
    ]
    burst_gaps = [
        step.delay_after_s
        for step in plan.steps
        if step.burst_break and step.delay_after_s is not None
    ]
    assert ordinary_gaps and burst_gaps  # the run has at least one of each
    assert all(800 <= gap <= 1200 for gap in ordinary_gaps)
    assert all(5000 <= gap <= 6000 for gap in burst_gaps)


def test_plan_enrichment_burst_sizes_sum_to_visit_count() -> None:
    plan = pacing.plan_enrichment(random.Random(13), 47)
    assert sum(plan.burst_sizes) == 47
    assert len(plan.steps) == 47


def test_plan_enrichment_burst_breaks_fall_exactly_at_burst_boundaries() -> None:
    plan = pacing.plan_enrichment(random.Random(13), 47)
    breaks = [i for i, step in enumerate(plan.steps) if step.burst_break]
    # One break per burst boundary, i.e. one fewer than the number of bursts.
    assert len(breaks) == len(plan.burst_sizes) - 1
    # The break falls on the last visit of each burst but the final one.
    cursor = -1
    expected_boundaries = []
    for size in plan.burst_sizes[:-1]:
        cursor += size
        expected_boundaries.append(cursor)
    assert breaks == expected_boundaries


def test_plan_enrichment_last_step_has_no_trailing_delay() -> None:
    plan = pacing.plan_enrichment(random.Random(13), 47)
    assert plan.steps[-1].delay_after_s is None
    assert all(step.delay_after_s is not None for step in plan.steps[:-1])


def test_plan_enrichment_total_delay_s_sums_the_steps() -> None:
    plan = pacing.plan_enrichment(random.Random(13), 47)
    expected = sum(step.delay_after_s or 0.0 for step in plan.steps)
    assert plan.total_delay_s == expected
    assert plan.total_delay_s > 0


@pytest.mark.parametrize("visit_count", [0, 1, 2])
def test_plan_enrichment_small_visit_counts(visit_count: int) -> None:
    plan = pacing.plan_enrichment(random.Random(1), visit_count)
    assert len(plan.steps) == visit_count
    assert sum(plan.burst_sizes) == visit_count
    if visit_count:
        assert plan.steps[-1].delay_after_s is None


def test_plan_enrichment_ordinary_and_burst_gaps_are_drawn_from_their_own_distributions() -> None:
    """The two kinds of gap stay distinct across a big run.

    Burst gaps are ``burst_break``'s uniform draw and so are bounded exactly
    by ``break_range_s`` (default (300, 1200)) — an invariant, not a
    statistical bound. Ordinary gaps are ``human_delay`` at its defaults, on
    which the distraction tail (spec 9.5: 8% chance, 2 to 8 minutes) can
    itself land anywhere from about 120s to nearly 500s above the base
    delay, so the two ranges legitimately overlap; what should not happen is
    the *typical* ordinary gap looking anything like a burst break. The
    median of 200 ordinary draws bounds that: [15, 45] comfortably brackets
    the ~25s default and would already catch ``delay`` silently not being
    forwarded from :class:`DelayProfile` into :func:`human_delay` (in which
    case this would instead track whatever the hard-coded fallback is).
    """
    burst = pacing.BurstProfile(size_range=(8, 15))
    plan = pacing.plan_enrichment(random.Random(77), 200, burst=burst)
    ordinary_gaps = [
        step.delay_after_s
        for step in plan.steps
        if step.delay_after_s is not None and not step.burst_break
    ]
    burst_gaps = [
        step.delay_after_s
        for step in plan.steps
        if step.burst_break and step.delay_after_s is not None
    ]
    assert ordinary_gaps  # the run has at least one non-boundary visit
    assert burst_gaps  # and at least one burst boundary
    assert 15 <= statistics.median(ordinary_gaps) <= 45
    assert min(burst_gaps) >= 300  # burst_break's own floor, exactly
    assert max(burst_gaps) <= 1200  # and ceiling


def test_plan_enrichment_same_seed_is_identical_across_processes() -> None:
    """P2-04's determinism requirement taken literally: the *same seed* on the *same
    ``visit_count`` produces the identical plan not just twice in one process (covered
    above) but in two independent Python processes, proving nothing in the plan generator
    depends on process-local state such as PYTHONHASHSEED-sensitive iteration order or
    accidental reliance on the module-level ``random`` singleton's default seeding.
    """

    def run(seed: int) -> str:
        script = (
            "import random\n"
            "from netkeeper.linkedin import pacing\n"
            f"plan = pacing.plan_enrichment(random.Random({seed}), 25)\n"
            "print(repr(plan))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent.parent,
        )
        return result.stdout

    first = run(42)
    second = run(42)
    third = run(43)
    assert first == second
    assert first != third


# --- #190: back to the top before the Contact info click ------------------------------------


def test_the_depth_a_plan_leaves_never_goes_above_the_top() -> None:
    plan = pacing.ScrollPlan(
        steps=(
            pacing.ScrollStep(300, 0.1),
            pacing.ScrollStep(-900, 0.1),  # past the top: the page stops there
            pacing.ScrollStep(200, 0.1),
        ),
        dwell_s=1.0,
    )
    assert plan.total_delta_px == -400
    assert pacing.depth_after(plan) == 200


@pytest.mark.parametrize("depth", [0, 1, 250, 900, 5_000])
def test_the_scroll_back_covers_the_depth_and_goes_only_up(depth: int) -> None:
    for seed in range(50):
        plan = pacing.scroll_back_to_top(random.Random(seed), depth)
        assert all(step.delta_px < 0 for step in plan.steps)
        covered = -sum(step.delta_px for step in plan.steps)
        assert (covered > depth) if depth else not plan.steps
        # At most one step past what was needed: a person overshoots, but not by a page.
        assert covered - depth <= pacing.BACK_TO_TOP_DELTA_RANGE_PX[1]
        assert all(
            pacing.BACK_TO_TOP_PAUSE_RANGE_S[0] <= s.pause_s <= pacing.BACK_TO_TOP_PAUSE_RANGE_S[1]
            for s in plan.steps
        )
        assert 0 < plan.dwell_s < 10


def test_the_scroll_back_constants_are_pinned() -> None:
    assert pacing.BACK_TO_TOP_DELTA_RANGE_PX == (300, 900)
    assert pacing.BACK_TO_TOP_PAUSE_RANGE_S == (0.2, 0.9)
    assert (pacing.BACK_TO_TOP_DWELL_MEDIAN_S, pacing.BACK_TO_TOP_DWELL_SIGMA) == (1.0, 0.4)


def test_the_scroll_back_replays_from_its_seed() -> None:
    assert pacing.scroll_back_to_top(random.Random(3), 1200) == pacing.scroll_back_to_top(
        random.Random(3), 1200
    )
