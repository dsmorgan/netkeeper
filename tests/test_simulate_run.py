"""netkeeper.services.simulate_run: the CP3 demo, `netkeeper simulate`.

Mutation standard (CLAUDE.md, and this issue's own instructions): every test
here has to fail if the behavior it claims to check is removed. Where the
behavior can't be removed by editing this module directly (the throttle
injection, the heat skip gate), the twin is a second call with the lever
forced the other way -- `run_simulation`'s own `heat_gate` parameter for the
skip gate, `throttles=0` for the injection -- the same pattern
`tests/test_simulate.py` already uses for the scheduler's own heat skip.

Two traps this project keeps hitting, both guarded here:

* **fixtures too uniform for the failure to arise.** The warm-up and weekend
  tests use ``throttles=0`` runs so heat noise can't blur the numbers being
  checked, and the weekend test picks the day index a 14-day run starting on
  :data:`netkeeper.services.simulate_run.REFERENCE_START_DATE` (a Monday)
  actually lands a Saturday on, rather than asserting on a day that happens to
  be a weekday. The heat-skip scenario (``days=3, throttles=20, seed=7``) was
  picked by running it and checking a real job kind actually got skipped, not
  by construction alone.
* **constants pinned only against themselves.** Every threshold this module
  invents (:data:`~netkeeper.services.simulate_run.CONSECUTIVE_THROTTLE_ABORT`,
  :data:`~netkeeper.services.simulate_run.REFERENCE_START_DATE`) is asserted
  against a literal below, never against the module's own name for it.
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path
from random import Random

import pytest

from netkeeper.config import Settings
from netkeeper.linkedin.pacing import apply_weekend_multiplier
from netkeeper.services import simulate_run as sr
from netkeeper.services.posture import HeatPosture, TodaysBudget
from netkeeper.services.scheduler import HEAT_SKIP_DISABLED

DEFAULTS = Settings()

# A scenario picked by running it: enough throttles, few enough days, that
# some day is guaranteed (pigeonhole: 20 throttles over 3 days puts at least
# ceil(20/3) = 7 on one of them) to push heat past the default skip_threshold
# of 2.5 for at least one poll -- confirmed empirically to actually skip a
# job kind, not merely to be plausible on paper (see the module docstring's
# "fixtures too uniform" trap).
HOT_DAYS = 3
HOT_THROTTLES = 20
HOT_SEED = 7


# --- constants pinned literally, not against themselves ----------------------


def test_the_reference_date_is_a_monday() -> None:
    """Spec 9.5's weekend damping only has something to prove itself against if
    the run actually crosses a Saturday and Sunday -- pinned so nobody can swap
    this for an arbitrary date and quietly break every weekend-covering test."""
    assert sr.REFERENCE_START_DATE.weekday() == 0
    assert sr.REFERENCE_START_DATE.isoformat() == "2026-01-05"


def test_a_run_of_the_default_length_crosses_two_weekends() -> None:
    saturdays_and_sundays = [
        sr.REFERENCE_START_DATE.weekday()
        for offset in range(sr.DEFAULT_DAYS)
        if (sr.REFERENCE_START_DATE.weekday() + offset) % 7 in (5, 6)
    ]
    assert len(saturdays_and_sundays) >= 2


def test_two_consecutive_throttled_units_abort_the_fire() -> None:
    """Spec 9.7's classify table, for ``Throttled``: "two consecutive throttled
    units abort the run." Pinned against the literal 2, not against the
    module's own name for it (CLAUDE.md)."""
    assert sr.CONSECUTIVE_THROTTLE_ABORT == 2


# --- throttle injection: the heat column must move ----------------------------


async def test_throttles_raise_heat_visibly() -> None:
    report = await sr.run_simulation(days=14, throttles=2, seed=1, settings=DEFAULTS)

    assert report.throttles_landed == 2
    assert any(day.heat.score > 0 for day in report.days)


async def test_removing_the_throttle_injection_leaves_the_heat_column_flat() -> None:
    """The mutation twin of the test above: the same run with ``throttles=0`` --
    what "removing the throttle injection" looks like from the outside -- must
    show a heat column that never leaves 0.0."""
    report = await sr.run_simulation(days=14, throttles=0, seed=1, settings=DEFAULTS)

    assert report.throttles_landed == 0
    assert all(day.heat.score == 0.0 for day in report.days)


async def test_a_different_seed_places_the_throttles_on_different_days() -> None:
    """Seeds 1 and 3 were picked by running them: both land all 2 requested
    throttles, so the comparison is about *where*, not *how many* -- whether a
    throttle lands at all can legitimately depend on budget being available
    that day (seed 2, for this same scenario, lands 0 of 2: both are drawn
    for day 13, and the weekly budget is already spent by then), which is a
    real property of the simulation and not what this test is checking."""
    first = await sr.run_simulation(days=14, throttles=2, seed=1, settings=DEFAULTS)
    second = await sr.run_simulation(days=14, throttles=2, seed=3, settings=DEFAULTS)

    landed_first = tuple(day.throttles_landed for day in first.days)
    landed_second = tuple(day.throttles_landed for day in second.days)
    assert landed_first != landed_second
    assert sum(landed_first) == sum(landed_second) == 2


def test_pick_days_is_seeded_and_varies() -> None:
    """The pure placement function directly: same seed repeats, different seeds
    (usually) differ -- the property :func:`_pick_days`'s docstring claims."""
    assert sr._pick_days(Random(1), 2, 14) == sr._pick_days(Random(1), 2, 14)
    assert sr._pick_days(Random(1), 2, 14) != sr._pick_days(Random(2), 2, 14)


async def test_the_same_seed_twice_is_byte_identical() -> None:
    first = sr.render(await sr.run_simulation(days=14, throttles=2, seed=1, settings=DEFAULTS))
    second = sr.render(await sr.run_simulation(days=14, throttles=2, seed=1, settings=DEFAULTS))

    assert first == second


# --- the heat skip gate: browser jobs must actually stop -----------------------


async def test_heat_skip_suppresses_fires_during_a_hot_period() -> None:
    report = await sr.run_simulation(
        days=HOT_DAYS, throttles=HOT_THROTTLES, seed=HOT_SEED, settings=DEFAULTS
    )

    skipped = [
        (day.index, job.kind, job.heat_skipped)
        for day in report.days
        for job in day.jobs
        if job.heat_skipped > 0
    ]
    assert skipped, "expected at least one heat-skipped fire in a scenario built to trip the gate"


async def test_disabling_the_heat_skip_lets_browser_jobs_run_through_the_hot_period() -> None:
    """The mutation twin: the identical scenario with the skip gate off must show
    zero heat-skipped fires, even though heat itself still rose (raise_heat is
    not gated by the skip gate -- only the scheduler firing a browser job is)."""
    hot = await sr.run_simulation(
        days=HOT_DAYS, throttles=HOT_THROTTLES, seed=HOT_SEED, settings=DEFAULTS
    )
    disabled = await sr.run_simulation(
        days=HOT_DAYS,
        throttles=HOT_THROTTLES,
        seed=HOT_SEED,
        settings=DEFAULTS,
        heat_gate=HEAT_SKIP_DISABLED,
    )

    assert any(job.heat_skipped > 0 for day in hot.days for job in day.jobs)
    assert all(job.heat_skipped == 0 for day in disabled.days for job in day.jobs)
    assert any(day.heat.score > 0 for day in disabled.days)  # heat still rose


# --- warm-up ramp: day 0 must not get the cap ---------------------------------


async def test_the_warmup_ramp_grows_by_ten_a_day_from_twenty() -> None:
    """Appendix C: "a fresh install starts at 20 profile visits per day and
    grows by 10 per day up to the configured cap." ``throttles=0`` so heat
    noise cannot be mistaken for a ramp effect."""
    report = await sr.run_simulation(days=3, throttles=0, seed=0, settings=DEFAULTS)

    assert report.days[0].budget.ramp == 20
    assert report.days[1].budget.ramp == 30
    assert report.days[2].budget.ramp == 40


async def test_breaking_the_ramp_to_return_the_cap_on_day_zero_would_fail_the_test_above() -> None:
    """Mutation check for the test above: the literal a broken "return cap from
    day 0" mutant would actually produce, so the assertion is proven to
    distinguish the correct value from the specific mutation it exists to
    catch."""
    cap = min(DEFAULTS.linkedin.budget.profile_visits_per_day, 100)
    assert cap != 20
    assert cap == 60


# --- weekend damping: it needs an actual weekend to prove anything ------------


async def test_weekend_damping_halves_the_ramp_on_saturday_and_sunday() -> None:
    """``--days 14`` from the fixed Monday start (see the module docstring)
    guarantees days 5 and 6 are a real Saturday and Sunday."""
    report = await sr.run_simulation(days=14, throttles=0, seed=0, settings=DEFAULTS)

    saturday, sunday = report.days[5], report.days[6]
    assert saturday.date.strftime("%A") == "Saturday"
    assert sunday.date.strftime("%A") == "Sunday"
    assert saturday.is_weekend and sunday.is_weekend
    assert saturday.budget.ramp == 60  # the ramp is already capped by day 5
    assert saturday.budget.after_weekend == 30  # floor(60 * 0.5)
    assert sunday.budget.after_weekend == 30

    monday = report.days[0]
    assert not monday.is_weekend
    assert monday.budget.after_weekend == monday.budget.ramp  # undamped


async def test_a_weekend_multiplier_of_one_would_fail_the_test_above() -> None:
    """Mutation check: the literal a broken "no damping" mutant would produce,
    so the assertion above is proven to catch it rather than merely hoping to."""
    saturday = sr.REFERENCE_START_DATE + timedelta(days=5)
    undamped = apply_weekend_multiplier(60, saturday, multiplier=1.0)
    assert undamped == 60  # not 30 -- proves a multiplier of 1.0 would read differently


# --- never the real database ---------------------------------------------------


async def test_the_scratch_database_is_deleted_after_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[str] = []
    original_mkdtemp = tempfile.mkdtemp

    def spy(*, prefix: str | None = None) -> str:
        path = original_mkdtemp(prefix=prefix)
        created.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", spy)

    await sr.run_simulation(days=1, throttles=0, seed=0, settings=DEFAULTS)

    assert created, "the scratch directory was never created -- this test proves nothing"
    assert not Path(created[0]).exists()  # noqa: ASYNC240 -- a test assertion, not I/O to avoid blocking on


async def test_never_touches_the_configured_database_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A poisoned ``NETKEEPER_DATABASE_URL`` must be left completely alone: if
    ``run_simulation`` ever opened it, the file would exist afterward."""
    poison = tmp_path / "must-never-be-touched.sqlite3"
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", f"sqlite:///{poison}")

    await sr.run_simulation(days=2, throttles=1, seed=0, settings=DEFAULTS)

    assert not poison.exists()


async def test_never_touches_netkeeper_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same proof for ``NETKEEPER_DATA``: this module must never resolve
    ``paths.data_dir()`` at all, so nothing appears under it."""
    fake_data_dir = tmp_path / "netkeeper-data"
    monkeypatch.setenv("NETKEEPER_DATA", str(fake_data_dir))

    await sr.run_simulation(days=2, throttles=1, seed=0, settings=DEFAULTS)

    assert not fake_data_dir.exists()


# --- input validation -----------------------------------------------------------


async def test_zero_days_is_refused() -> None:
    with pytest.raises(sr.InvalidSimulation):
        await sr.run_simulation(days=0, throttles=0, seed=0, settings=DEFAULTS)


async def test_negative_throttles_is_refused() -> None:
    with pytest.raises(sr.InvalidSimulation):
        await sr.run_simulation(days=1, throttles=-1, seed=0, settings=DEFAULTS)


# --- the report reuses posture's own structures, not a second vocabulary ------


async def test_the_report_reuses_postures_todaysbudget_and_heatposture() -> None:
    report = await sr.run_simulation(days=1, throttles=0, seed=0, settings=DEFAULTS)

    assert isinstance(report.days[0].budget, TodaysBudget)
    assert isinstance(report.days[0].heat, HeatPosture)


# --- rendering: every scheduled job kind's fires show up ----------------------


async def test_render_shows_every_job_kind_and_the_week_ceiling() -> None:
    report = await sr.run_simulation(days=8, throttles=1, seed=1, settings=DEFAULTS)

    text = sr.render(report)

    for kind in ("connections_full", "connections_incremental", "enrich", "inbox"):
        assert kind in text
    assert "WARM-UP" in text and "WEEKEND" in text and "HEAT-CAP" in text and "WEEK" in text
    assert "throttle(s) requested" in text
    assert "scratch database" in text
