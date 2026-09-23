"""netkeeper.services.simulate_run: the CP3 demo, `netkeeper simulate`.

Mutation standard (CLAUDE.md, and this issue's own instructions): every test
here has to fail if the behavior it claims to check is removed. Where the
behavior can't be removed by editing this module directly (the throttle
injection, the heat skip gate), the twin is a second call with the lever
forced the other way -- `run_simulation`'s own `heat_gate` parameter for the
skip gate, `throttles=0` for the injection -- the same pattern
`tests/test_simulate.py` already uses for the scheduler's own heat skip.
Where a value asserted must actually have been produced by the mechanism
under test rather than merely being consistent with it (the heat-shrunk cap
taking effect *within* the fire that raised it, the two-consecutive-throttle
abort, the weekly ceiling's own overshoot-by-one), the test pins the literal
number a real run produces, confirmed by running it, not a range wide enough
to pass regardless.

Two traps this project keeps hitting, both guarded here:

* **fixtures too uniform for the failure to arise.** The warm-up and weekend
  tests use ``throttles=0`` runs so heat noise can't blur the numbers being
  checked, and the weekend test picks the day index a 14-day run starting on
  :data:`netkeeper.services.simulate_run.REFERENCE_START_DATE` (a Monday)
  actually lands a Saturday on. The heat-skip scenario (``days=3,
  throttles=20, seed=7``) and the consecutive-abort scenario were both picked
  by running them and checking the exact behavior claimed actually happened,
  not by construction alone.
* **constants pinned only against themselves.** Every threshold this module
  invents (:data:`~netkeeper.services.simulate_run.CONSECUTIVE_THROTTLE_ABORT`,
  :data:`~netkeeper.services.simulate_run.REFERENCE_START_DATE`) is asserted
  against a literal below, never against the module's own name for it.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from random import Random
from zoneinfo import ZoneInfo

import pytest

from netkeeper import migrations
from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.models import User, UserKind
from netkeeper.services import scheduler
from netkeeper.services import simulate_run as sr
from netkeeper.services.posture import SINGLE_ACCOUNT_ID, HeatPosture, TodaysBudget
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
    """Seeds 0 and 1 were picked by running them: both land all 2 requested
    throttles, so the comparison is about *where*, not *how many* -- whether a
    throttle lands at all can legitimately depend on how much an earlier
    throttle's own heat suppresses later spend (seed 3, for this same
    scenario, lands only 1 of 2), which is a real property of the simulation
    and not what this test is checking."""
    first = await sr.run_simulation(days=14, throttles=2, seed=0, settings=DEFAULTS)
    second = await sr.run_simulation(days=14, throttles=2, seed=1, settings=DEFAULTS)

    landed_first = tuple(day.throttles_landed for day in first.days)
    landed_second = tuple(day.throttles_landed for day in second.days)
    assert landed_first != landed_second
    assert sum(landed_first) == sum(landed_second) == 2


def test_pick_spend_ordinals_is_seeded_and_varies() -> None:
    """The pure placement function directly: same seed repeats, different seeds
    (usually) differ -- the property :func:`_pick_spend_ordinals`'s docstring
    claims. ``total_spend=100`` stands in for what a baseline pass would
    discover; this function does not care where the number came from."""
    assert sr._pick_spend_ordinals(Random(1), 2, 100) == sr._pick_spend_ordinals(Random(1), 2, 100)
    assert sr._pick_spend_ordinals(Random(1), 2, 100) != sr._pick_spend_ordinals(Random(2), 2, 100)


def test_pick_spend_ordinals_never_exceeds_the_true_total() -> None:
    """Every ordinal drawn is a position that actually exists in the sequence
    ``total_spend`` describes -- the whole reason ordinal placement was chosen
    over calendar-day placement (a day can have nothing to throttle at all)."""
    ordinals = sr._pick_spend_ordinals(Random(0), 5, 3)
    assert ordinals == {0, 1, 2}  # more requested than exist: every one is used, none invented


async def test_the_same_seed_twice_is_byte_identical() -> None:
    first = sr.render(await sr.run_simulation(days=14, throttles=2, seed=1, settings=DEFAULTS))
    second = sr.render(await sr.run_simulation(days=14, throttles=2, seed=1, settings=DEFAULTS))

    assert first == second


# --- the heat-shrunk cap must take effect within the fire that raised it -----


async def test_the_cap_shrinks_within_the_same_fire_that_raised_heat() -> None:
    """The main defect a review of this module caught: a throttle used to raise
    heat but the loop kept spending against the cap it had derived *before*
    the raise, for the rest of that same fire. A single simulated day (so
    every throttle lands on day 0, deterministically) with one throttle
    injected must spend measurably less than the identical day with none --
    confirmed by running both: 16 of a 20 cap with the throttle, all 20
    without."""
    with_throttle = await sr.run_simulation(days=1, throttles=1, seed=0, settings=DEFAULTS)
    without = await sr.run_simulation(days=1, throttles=0, seed=0, settings=DEFAULTS)

    day_with, day_without = with_throttle.days[0], without.days[0]
    assert day_with.throttles_landed == 1
    assert day_with.budget.spent < day_without.budget.spent
    assert day_with.budget.spent == 16
    assert day_without.budget.spent == 20
    # the cap itself dipped well below the day's undamped ramp of 20 at some
    # point while spending was still happening, not only in an end-of-day
    # reading taken after heat had already decayed back down
    assert day_with.min_heat_cap < day_with.budget.ramp
    assert day_with.min_heat_cap == 10  # shrink(20, cooldown_multiplier=2.0)


async def test_heat_cap_is_the_live_tracked_cap_not_an_end_of_day_rereading() -> None:
    """The defect a second review caught coming back with no test failing: a
    version that kept ``MIN-CAP`` live but reverted ``HEAT-CAP``
    (``budget.after_heat``) to an end-of-day re-derivation still passed every
    other test in this module. Pins the exact value a real run of the CP3
    demo's own invocation produces on the two days a throttle lands, so that
    specific regression fails here instead of nowhere.

    Confirmed by reverting ``_day_snapshot`` to always take the ``else``
    (end-of-day) branch for ``after_heat``: day 9 then reads 51, not 48 (heat
    has decayed further by the end of the day than it had when the handler
    itself last stopped spending), and day 10 reads 50, not 47.
    """
    report = await sr.run_simulation(days=14, throttles=2, seed=0, settings=DEFAULTS)

    day9, day10 = report.days[9], report.days[10]
    assert day9.throttles_landed == 1
    assert day9.budget.after_heat == 48
    assert day9.budget.spent == 48  # spending actually stopped at the live cap, not at 51
    assert day10.throttles_landed == 1
    assert day10.budget.after_heat == 47
    assert day10.budget.spent == 47


async def test_spending_stops_exactly_at_the_derived_cap_with_no_overshoot() -> None:
    """The handler's own stopping condition is "spend while under the cap", not
    "spend while at or under it": ``consume`` already provides one unit of
    overshoot at its own (much higher) hard-max layer, and a second overshoot
    stacked on top of the derived, heat/warm-up/weekend-shrunk cap would make
    that cap a number the day's own total silently exceeds."""
    report = await sr.run_simulation(days=1, throttles=0, seed=0, settings=DEFAULTS)

    day = report.days[0]
    assert day.budget.spent == day.budget.after_heat == 20


async def _run_one_direct_fire(target_ordinals: frozenset[int]) -> sr._RunState:
    """One direct call to the ``ENRICH`` handler, isolated from the scheduler's
    multi-fire cadence and from throttle placement: a fresh scratch database
    and user, one fire at a fixed instant inside the default active window,
    with ``target_ordinals`` as the units to throttle."""
    zone = ZoneInfo(DEFAULTS.linkedin.timezone)
    start = datetime.combine(sr.REFERENCE_START_DATE, time(0, 0), tzinfo=zone).astimezone(UTC)
    cap = min(DEFAULTS.linkedin.budget.profile_visits_per_day, 100)

    with sr._scratch_database() as factory:
        with session_scope(factory, write=True) as session:
            user = User(kind=UserKind.LOCAL, timezone=DEFAULTS.linkedin.timezone, created_at=start)
            session.add(user)
            session.flush()

        state = sr._RunState(target_ordinals=target_ordinals)
        handler = sr._make_enrich_handler(
            factory,
            user,
            SINGLE_ACCOUNT_ID,
            budget_settings=DEFAULTS.linkedin.budget,
            heat_settings=DEFAULTS.linkedin.heat,
            weekend_multiplier=DEFAULTS.linkedin.weekend_multiplier,
            zone=zone,
            cap=cap,
            start_date=sr.REFERENCE_START_DATE,
            state=state,
        )
        due = start + timedelta(hours=9)  # inside the default active window
        ctx = scheduler.JobContext(
            user_id=user.id,
            account_id=SINGLE_ACCOUNT_ID,
            kind=scheduler.JobKind.ENRICH,
            due=due,
            catch_up=False,
        )
        await handler(ctx)
    return state


async def test_two_consecutive_throttles_abort_one_fires_spend_loop() -> None:
    """Every one of the first five units is targeted -- more than the abort
    should ever let land in a single fire. The day's own budget (20) is
    nowhere close to exhausted, so only the two-consecutive-throttle abort
    explains stopping at exactly 2.

    Also pins the cap the second throttle itself produces: confirmed by
    running it, ``shrink(20, cooldown_multiplier=3.0) == 6`` -- lower than
    the 10 a single throttle alone produces
    (``test_the_cap_shrinks_within_the_same_fire_that_raised_heat``). The
    loop exits on the abort right after that second raise without ever
    spending another unit, so this cap governed nothing -- but it is the
    day's real lowest point, and a version that only records the cap when
    the loop goes on to spend again would leave ``min_cap`` one throttle
    behind at 10."""
    state = await _run_one_direct_fire(frozenset(range(5)))

    assert len(state.throttle_log) == sr.CONSECUTIVE_THROTTLE_ABORT
    assert state.units_spent_total == sr.CONSECUTIVE_THROTTLE_ABORT
    assert state.min_cap[0] == 6
    assert state.last_cap[0] == 6


async def test_removing_the_abort_would_let_more_than_two_land_in_one_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation check for the test above: with the abort's own threshold raised
    to something this scenario can never reach -- what "removing the abort"
    looks like here -- the identical direct-handler scenario lands 4 of the 5
    targeted units, not 2: confirmed by running it, the 4th throttle's own
    heat shrinks the cap down to exactly what has already been spent, so the
    5th unit is refused by the cap rather than by an abort that no longer
    exists. Either way, 4 is not 2, which is what proves the assertion above
    actually distinguishes "the abort stopped this fire" from "something else
    did"."""
    monkeypatch.setattr(sr, "CONSECUTIVE_THROTTLE_ABORT", 99)

    state = await _run_one_direct_fire(frozenset(range(5)))

    assert len(state.throttle_log) == 4
    assert len(state.throttle_log) != 2  # the real CONSECUTIVE_THROTTLE_ABORT, unpatched


# --- spec 9.6's other ceiling: the weekly limit, with real numbers -----------


async def test_the_weekly_ceiling_refuses_once_exceeded() -> None:
    """Spec 9.6's separate weekly limit on ``profile_visits`` (300 by default).
    ``throttles=0`` so heat noise cannot shift which day the week's counter
    actually crosses 300 -- confirmed by running it: the ISO week starting day
    7 (a Monday, so it aligns with the calendar week) reaches exactly 300 on
    day 11, gets the same one-unit overshoot ``consume`` gives every ceiling
    on day 12, and refuses every unit on day 13."""
    report = await sr.run_simulation(days=14, throttles=0, seed=0, settings=DEFAULTS)

    day11, day12, day13 = report.days[11], report.days[12], report.days[13]
    assert day11.week is not None and day11.week.count == 300 and day11.week.limit == 300
    assert day12.week is not None and day12.week.count == 301
    assert day12.budget.spent == 1  # the weekly ceiling's own overshoot-by-one
    assert day13.week is not None and day13.week.count == 301
    assert day13.budget.spent == 0  # refused entirely: the week is already over


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
    assert any(day.heat_skipped_today for day in report.days)


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
    assert all(not day.heat_skipped_today for day in disabled.days)
    assert any(day.heat.score > 0 for day in disabled.days)  # heat still rose


async def test_the_schedulers_own_heat_skip_warning_never_reaches_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``netkeeper.services.scheduler`` logs a WARNING on its own for every
    heat-skipped fire ("scheduler: skipping enrich for account 1 (heat)"),
    naming the scratch account's id exactly the way it would name a real
    one. The skip is still fully visible in the report -- this same scenario
    is what :func:`test_heat_skip_suppresses_fires_during_a_hot_period`
    checks -- so nothing is lost by keeping a copy of it off stderr during a
    replay. ``caplog`` itself tries to lower the logger back to WARNING to
    capture at that level; this asserts that ``run_simulation`` still wins
    for the whole time it runs."""
    with caplog.at_level(logging.WARNING, logger="netkeeper.services.scheduler"):
        report = await sr.run_simulation(
            days=HOT_DAYS, throttles=HOT_THROTTLES, seed=HOT_SEED, settings=DEFAULTS
        )

    assert any(job.heat_skipped > 0 for day in report.days for job in day.jobs)
    assert caplog.records == []


# --- warm-up ramp: day 0 must not get the cap ---------------------------------


async def test_the_warmup_ramp_grows_by_ten_a_day_from_twenty() -> None:
    """Appendix C: "a fresh install starts at 20 profile visits per day and
    grows by 10 per day up to the configured cap." ``throttles=0`` so heat
    noise cannot be mistaken for a ramp effect."""
    report = await sr.run_simulation(days=3, throttles=0, seed=0, settings=DEFAULTS)

    assert report.days[0].budget.ramp == 20
    assert report.days[1].budget.ramp == 30
    assert report.days[2].budget.ramp == 40


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

    await sr.run_simulation(days=2, throttles=0, seed=0, settings=DEFAULTS)

    assert not poison.exists()


async def test_never_touches_netkeeper_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same proof for ``NETKEEPER_DATA``: this module must never resolve
    ``paths.data_dir()`` at all, so nothing appears under it."""
    fake_data_dir = tmp_path / "netkeeper-data"
    monkeypatch.setenv("NETKEEPER_DATA", str(fake_data_dir))

    await sr.run_simulation(days=2, throttles=0, seed=0, settings=DEFAULTS)

    assert not fake_data_dir.exists()


async def test_the_scratch_schema_is_built_by_migrations_not_create_all() -> None:
    """``Base.metadata.create_all`` never writes ``alembic_version``; only
    ``netkeeper.migrations.upgrade`` does. Its presence, at the newest
    revision on disk, is direct evidence of which one built the scratch
    schema -- the production one, not a models-only approximation of it that
    can drift from what the migrations actually build."""
    with sr._scratch_database() as factory, session_scope(factory) as session:
        engine = session.get_bind()
        assert migrations.current_revision(engine) == migrations.head_revision()  # type: ignore[arg-type]


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


# --- rendering: every scheduled job kind's fires show up, and nothing lies ----


async def test_render_shows_every_job_kind_and_the_week_ceiling() -> None:
    report = await sr.run_simulation(days=8, throttles=1, seed=1, settings=DEFAULTS)

    text = sr.render(report)

    for kind in ("connections_full", "connections_incremental", "enrich", "inbox"):
        assert kind in text
    assert "WARM-UP" in text and "WEEKEND" in text and "HEAT-CAP" in text and "WEEK" in text
    assert "MIN-CAP" in text and "SKIP?" in text
    assert "throttle(s) requested" in text
    assert "scratch database" in text


async def test_render_warns_when_fewer_throttles_land_than_requested() -> None:
    """A scenario built to fall short (see ``HOT_*`` above) must say so loudly,
    not merely understate the header count."""
    report = await sr.run_simulation(
        days=HOT_DAYS, throttles=HOT_THROTTLES, seed=HOT_SEED, settings=DEFAULTS
    )

    assert report.throttles_landed < report.throttles_requested
    text = sr.render(report)
    assert "WARNING" in text
    assert f"{report.throttles_landed} of {report.throttles_requested}" in text


async def test_render_does_not_warn_when_every_throttle_lands() -> None:
    report = await sr.run_simulation(days=14, throttles=2, seed=0, settings=DEFAULTS)

    assert report.throttles_landed == report.throttles_requested == 2
    assert "WARNING" not in sr.render(report)
