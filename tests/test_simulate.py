"""netkeeper.services.simulate: the virtual-clock scheduler replay.

This is where P2-09's "done when" lives: *simulate reproduces a daily
schedule across restarts without a skipped or doubled run.* Everything here
runs against a real (file-backed) SQLite database under tmp_path with no
sleeping and no real APScheduler loop -- a multi-week replay costs
milliseconds, and a "restart" is a second call against a fresh
``sessionmaker`` bound to the same file, which is what an actual process
restart differs by.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from random import Random

import factories
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import HeatSettings
from netkeeper.db import make_engine, make_session_factory, session_scope
from netkeeper.models import Base, User
from netkeeper.scoping import install_scope_guard
from netkeeper.services import heat, scheduler, simulate

START = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
ACCOUNT = 1
ALL_DAY = (
    time(0, 0),
    time(0, 0),
)  # always active: isolates schedule timing from active-hours snapping
DAILY = {
    scheduler.JobKind.CONNECTIONS_INCREMENTAL: scheduler.JobSchedule(
        scheduler.JobKind.CONNECTIONS_INCREMENTAL, timedelta(days=1)
    )
}
HEAT_SETTINGS = HeatSettings(per_block=1.0, half_life_hours=6, skip_threshold=2.5)
CATCHUP_BOUNDS = (scheduler.CATCHUP_MIN_MINUTES, scheduler.CATCHUP_MAX_MINUTES)
# The seed whose first two catch-up draws land in the same whole minute; the
# test that needs that collision pins it, and asserts it still holds.
COLLIDING_SEED = 2


def _make_factory(db_path: str) -> sessionmaker[Session]:
    """A brand new engine/sessionmaker bound to the same on-disk file -- the
    thing that actually distinguishes a 'restart' from reusing a live object."""
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    return factory


@pytest.fixture
def db_path(tmp_path: object) -> str:
    path = f"{tmp_path}/scheduler-sim.sqlite3"
    engine: Engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


@pytest.fixture
def owner(db_path: str) -> User:
    factory = _make_factory(db_path)
    with session_scope(factory, write=True) as session:
        return factories.make_user(session, timezone="UTC")


# --- the acceptance test: a daily schedule survives a restart mid-day -------


async def test_daily_schedule_survives_a_restart_with_no_skip_or_double(
    db_path: str, owner: User
) -> None:
    """The exact scenario in P2-09's "done when": stop mid-day, start again,
    and every daily fire happened exactly once."""
    continuous_factory = _make_factory(db_path + ".continuous")
    engine = make_engine(f"sqlite:///{db_path}.continuous")
    Base.metadata.create_all(engine)
    engine.dispose()
    with session_scope(continuous_factory, write=True) as session:
        continuous_owner = factories.make_user(session, timezone="UTC")
    end = START + timedelta(days=10)
    baseline = await simulate.simulate(
        continuous_factory,
        continuous_owner,
        ACCOUNT,
        start=START,
        end=end,
        schedules=DAILY,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )
    assert (
        baseline.count(scheduler.JobKind.CONNECTIONS_INCREMENTAL) == 9
    )  # days 1..9; day 10 is the end

    # Now the same ten days, but as two separate "processes": the first stops
    # partway through day 5, and a *new* sessionmaker on the same file resumes.
    mid = START + timedelta(days=5, hours=6)
    factory_a = _make_factory(db_path)
    part_one = await simulate.simulate(
        factory_a,
        owner,
        ACCOUNT,
        start=START,
        end=mid,
        schedules=DAILY,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )
    # "restart": a brand new sessionmaker/engine, nothing carried over in memory
    factory_b = _make_factory(db_path)
    with session_scope(factory_b) as session:
        owner_b = session.get(User, owner.id)
    assert owner_b is not None
    part_two = await simulate.simulate(
        factory_b,
        owner_b,
        ACCOUNT,
        start=mid,
        end=end,
        schedules=DAILY,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )

    combined_fires = sorted(f.at for f in part_one.fires + part_two.fires if f.fired)
    baseline_fires = sorted(f.at for f in baseline.fires if f.fired)
    assert combined_fires == baseline_fires  # identical fire times either way
    assert len(combined_fires) == len(set(combined_fires))  # no run doubled
    assert len(combined_fires) == 9  # no run skipped


async def test_removing_persistence_would_fail_the_restart_test(db_path: str, owner: User) -> None:
    """Mutation check for the acceptance test above: simulate the "no
    persistence" bug directly by starting the second half from a database that
    never saw the first half's writes (a fresh, empty database rather than the
    same file) -- the schedule for the second half then has to start fresh
    from its own `start`, landing on different fire times than the continuous
    baseline, which is exactly the failure next-fire persistence prevents."""
    mid = START + timedelta(days=5, hours=6)
    end = START + timedelta(days=10)
    factory_a = _make_factory(db_path)
    await simulate.simulate(
        factory_a,
        owner,
        ACCOUNT,
        start=START,
        end=mid,
        schedules=DAILY,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )

    # A fresh, unrelated database: what "no persistence" looks like -- the
    # second half has no memory of the first half's schedule at all.
    empty_path = f"{db_path}.no-persistence"
    empty_engine = make_engine(f"sqlite:///{empty_path}")
    Base.metadata.create_all(empty_engine)
    empty_engine.dispose()
    factory_fresh = _make_factory(empty_path)
    with session_scope(factory_fresh, write=True) as session:
        fresh_owner = factories.make_user(session, timezone="UTC")
    without_persistence = await simulate.simulate(
        factory_fresh,
        fresh_owner,
        ACCOUNT,
        start=mid,
        end=end,
        schedules=DAILY,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )
    # Fresh-from-mid always lands on mid + 1 day, mid + 2 days, ... -- a
    # different grid than the continuous-from-START schedule was on, proving
    # the two are not simply coincidentally identical.
    first_without = min(f.at for f in without_persistence.fires if f.fired)
    assert first_without == mid + timedelta(days=1)
    assert (first_without - START) % timedelta(days=1) != timedelta(0)


# --- catch-up after downtime: once, 5 to 20 minutes after restart -----------


async def test_downtime_catches_up_once_not_once_per_missed_day(db_path: str, owner: User) -> None:
    factory = _make_factory(db_path)
    down_start = START + timedelta(days=2, hours=1)  # partway through day 2
    down_end = down_start + timedelta(days=90)  # a long outage
    end = down_end + timedelta(days=3)

    result = await simulate.simulate(
        factory,
        owner,
        ACCOUNT,
        start=START,
        end=end,
        schedules=DAILY,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        downtime=(down_start, down_end),
    )

    catchups = result.catchups(scheduler.JobKind.CONNECTIONS_INCREMENTAL)
    assert len(catchups) == 1  # not 90 -- once, however long the outage
    fire = catchups[0]
    assert down_end + timedelta(minutes=scheduler.CATCHUP_MIN_MINUTES) <= fire.at
    assert fire.at <= down_end + timedelta(minutes=scheduler.CATCHUP_MAX_MINUTES)
    # nothing at all fired *during* the outage
    assert all(f.at < down_start or f.at >= down_end for f in result.fires)


# --- reschedule only on change, observed through simulate -------------------


async def test_a_settings_change_mid_run_does_not_double_or_skip_the_next_fire(
    db_path: str, owner: User
) -> None:
    """A settings write occurs between two fires but changes nothing about
    timing (simulated here by re-running establish with identical parameters
    partway through) -- the next fire must land exactly where it would have
    without the write."""
    factory = _make_factory(db_path)
    with session_scope(factory, write=True) as session:
        established = scheduler.establish_schedule(
            session,
            owner,
            ACCOUNT,
            scheduler.JobKind.CONNECTIONS_INCREMENTAL,
            now=START,
            schedule=DAILY[scheduler.JobKind.CONNECTIONS_INCREMENTAL],
            rng=Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )
    # A no-op settings write, some hours later, before the job is due.
    with session_scope(factory, write=True) as session:
        owner_now = session.get(User, owner.id)
        assert owner_now is not None
        noop = scheduler.establish_schedule(
            session,
            owner_now,
            ACCOUNT,
            scheduler.JobKind.CONNECTIONS_INCREMENTAL,
            now=START + timedelta(hours=5),
            schedule=DAILY[scheduler.JobKind.CONNECTIONS_INCREMENTAL],
            rng=Random(99),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )
    assert noop.changed is False
    assert noop.due == established.due

    result = await simulate.simulate(
        factory,
        owner,
        ACCOUNT,
        start=START + timedelta(hours=5, minutes=1),
        end=START + timedelta(days=3),
        schedules=DAILY,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )
    fires = sorted(f.at for f in result.fires if f.fired)
    assert fires == [established.due, established.due + timedelta(days=1)]


# --- heat skip, observed through simulate ------------------------------------

# A short interval so the first check falls well inside the 6h heat half-life
# -- a daily schedule would let heat decay from 3.0 to ~0.19 (four half-lives)
# before the first fire was ever checked, which would prove nothing about the
# skip itself.
SHORT = {
    scheduler.JobKind.ENRICH: scheduler.JobSchedule(scheduler.JobKind.ENRICH, timedelta(minutes=30))
}


async def test_heat_above_threshold_suppresses_fires_until_it_decays(
    db_path: str, owner: User
) -> None:
    factory = _make_factory(db_path)
    with session_scope(factory, write=True) as session:
        for _ in range(3):  # 3 * per_block(1.0) = 3.0 >= 2.5 threshold
            heat.raise_heat(session, owner, ACCOUNT, now=START, settings=HEAT_SETTINGS)

    result = await simulate.simulate(
        factory,
        owner,
        ACCOUNT,
        start=START,
        end=START + timedelta(hours=1),
        schedules=SHORT,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        heat_settings=HEAT_SETTINGS,
    )

    # The slot due at START+30min sees almost no decay against a 6h half-life,
    # so it must be skipped, not fired.
    assert len(result.fires) == 1
    assert result.fires[0].fired is False
    assert result.fires[0].skipped_reason == "heat"


async def test_removing_the_heat_check_would_fail_the_skip_test(db_path: str, owner: User) -> None:
    """Mutation check: the identical scenario with heat_settings=None (the gate
    disabled) must fire normally instead of skipping."""
    factory = _make_factory(db_path)
    with session_scope(factory, write=True) as session:
        for _ in range(3):
            heat.raise_heat(session, owner, ACCOUNT, now=START, settings=HEAT_SETTINGS)

    result = await simulate.simulate(
        factory,
        owner,
        ACCOUNT,
        start=START,
        end=START + timedelta(hours=1),
        schedules=SHORT,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        heat_settings=scheduler.HEAT_SKIP_DISABLED,
    )
    assert len(result.fires) == 1
    assert result.fires[0].fired is True
    assert result.fires[0].skipped_reason is None


# --- interleave gap, observed through simulate -------------------------------


async def test_two_job_kinds_never_fire_in_the_same_minute(db_path: str, owner: User) -> None:
    """Spec 9.5's last bullet, end to end: force enrich and inbox onto the same
    cadence so their catch-up jitter can collide, and confirm simulate never
    reports two fires in the same minute for one account."""
    schedules = {
        scheduler.JobKind.ENRICH: scheduler.JobSchedule(
            scheduler.JobKind.ENRICH, timedelta(hours=6)
        ),
        scheduler.JobKind.INBOX: scheduler.JobSchedule(scheduler.JobKind.INBOX, timedelta(hours=6)),
    }
    factory = _make_factory(db_path)
    # Both start stale (missed since before START), so both take the catch-up
    # jitter path on the very first establish -- the highest-risk collision case.
    with session_scope(factory, write=True) as session:
        for kind, sch in schedules.items():
            fp = scheduler.ScheduleFingerprint.of(
                sch, tz="UTC", active_start=ALL_DAY[0], active_end=ALL_DAY[1]
            )
            scheduler._store_state(
                session,
                owner,
                ACCOUNT,
                kind,
                scheduler._JobState(due=START - timedelta(days=1), fingerprint=fp),
            )

    # One `Random` draws two different values, so most seeds put the two kinds
    # far enough apart that this test passes without the interleave gap ever
    # being needed -- and 77 seconds apart is not enough either, because it can
    # straddle a minute boundary. Seed 2 draws 19.341 and 19.217 minutes: the
    # same whole minute. Pinned here so a future seed change cannot quietly
    # turn this into a test of nothing.
    probe = Random(COLLIDING_SEED)
    raw = [START + timedelta(minutes=probe.uniform(*CATCHUP_BOUNDS)) for _ in schedules]
    assert raw[0].replace(second=0, microsecond=0) == raw[1].replace(second=0, microsecond=0), (
        "seed no longer puts the two catch-up draws in the same minute"
    )

    result = await simulate.simulate(
        factory,
        owner,
        ACCOUNT,
        start=START,
        end=START + timedelta(days=5),
        schedules=schedules,
        seed=COLLIDING_SEED,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )

    fired = [f for f in result.fires if f.fired]
    assert len(fired) >= 2  # otherwise this test isn't exercising anything
    minutes_seen: dict[int, list[scheduler.JobKind]] = {}
    for f in fired:
        bucket = int(f.at.timestamp() // 60)
        minutes_seen.setdefault(bucket, []).append(f.kind)
    for kinds in minutes_seen.values():
        assert len(kinds) == 1, f"more than one job kind fired in the same minute: {kinds}"


# --- first setup: the full sync runs on day 0 (#161) --------------------------


def _full_syncs(result: simulate.SimResult) -> list[simulate.SimFire]:
    return [f for f in result.fires if f.kind == scheduler.JobKind.CONNECTIONS_FULL and f.fired]


async def test_a_fresh_install_runs_the_full_sync_on_day_zero(db_path: str, owner: User) -> None:
    """Spec 9.4: the full sync "runs on first setup and weekly". Every default kind,
    so the day-0 fire also has to clear the interleave gap against the others."""
    result = await simulate.simulate(
        _make_factory(db_path),
        owner,
        ACCOUNT,
        start=START,
        end=START + timedelta(days=1),
        seed=0,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )

    full = _full_syncs(result)
    assert len(full) == 1
    assert START + timedelta(minutes=CATCHUP_BOUNDS[0]) <= full[0].at
    assert full[0].at <= START + timedelta(minutes=CATCHUP_BOUNDS[1])
    assert full[0].is_catchup is False


async def test_a_fresh_install_after_hours_runs_the_full_sync_first_and_alone(
    db_path: str, owner: User
) -> None:
    """Installed at 22:00, outside 08:30-21:30: the full sync, enrich (22:00 + 3 h)
    and inbox all snap to 08:30, so they really contend. The full sync still
    runs that morning, and no two kinds share a minute."""
    installed = START + timedelta(hours=22)
    morning = START + timedelta(days=1, hours=8, minutes=30)

    result = await simulate.simulate(
        _make_factory(db_path),
        owner,
        ACCOUNT,
        start=installed,
        end=morning + timedelta(hours=1),
        seed=0,
        active_start=time(8, 30),
        active_end=time(21, 30),
    )

    fired = [f for f in result.fires if f.fired]
    assert [f.at for f in _full_syncs(result)] == [morning]
    contenders = [f for f in fired if f.at < morning + timedelta(minutes=10)]
    assert {f.kind for f in contenders} == {
        scheduler.JobKind.CONNECTIONS_FULL,
        scheduler.JobKind.ENRICH,
        scheduler.JobKind.INBOX,
    }
    minutes = [int(f.at.timestamp() // 60) for f in fired]
    assert len(minutes) == len(set(minutes))


async def test_a_first_full_sync_skipped_for_heat_runs_once_heat_decays(
    db_path: str, owner: User
) -> None:
    """Installed hot (3.0 against a 2.5 threshold, so skipping until about 1.6 h
    in): the full sync is skipped, retried hourly, and runs the same day --
    not a week later -- then settles into its weekly cadence."""
    factory = _make_factory(db_path)
    with session_scope(factory, write=True) as session:
        for _ in range(3):
            heat.raise_heat(session, owner, ACCOUNT, now=START, settings=HEAT_SETTINGS)

    result = await simulate.simulate(
        factory,
        owner,
        ACCOUNT,
        start=START,
        end=START + timedelta(days=8),
        seed=0,
        schedules={
            scheduler.JobKind.CONNECTIONS_FULL: scheduler.DEFAULT_SCHEDULES[
                scheduler.JobKind.CONNECTIONS_FULL
            ]
        },
        heat_settings=HEAT_SETTINGS,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )

    skipped = [f for f in result.fires if f.skipped_reason == "heat"]
    ran = _full_syncs(result)
    assert skipped, "heat never skipped the first fire; the test is not exercising anything"
    assert ran[0].at < START + timedelta(hours=4)
    assert [f.at for f in ran] == [ran[0].at, ran[0].at + timedelta(days=7)]


async def test_restarting_an_established_install_does_not_rerun_the_full_sync(
    db_path: str, owner: User
) -> None:
    """The hot path / cold path line: a restart two days in restores the weekly
    due time, so the next full sync is a week after the day-0 one, not now."""
    first = await simulate.simulate(
        _make_factory(db_path),
        owner,
        ACCOUNT,
        start=START,
        end=START + timedelta(days=2),
        seed=0,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )
    (day_zero,) = _full_syncs(first)

    restarted = await simulate.simulate(
        _make_factory(db_path),  # a new process against the same file
        owner,
        ACCOUNT,
        start=START + timedelta(days=2),
        end=START + timedelta(days=10),
        seed=1,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )

    assert [f.at for f in _full_syncs(restarted)] == [day_zero.at + timedelta(days=7)]


# --- the sleeping laptop: an outage that never restarts the process ----------


async def test_a_sleeping_laptop_resumes_without_replaying_the_backlog(
    db_path: str, owner: User
) -> None:
    """The motivating case for the hot path's catch-up branch, end to end. A
    closed laptop keeps the process alive, so nothing re-establishes anything
    on waking: the heartbeat simply finds a due time three days old. It must
    produce one catch-up fire, not one fire per missed day -- that burst is
    what budgets (9.6), pacing (9.5), and heat (9.7) exist to prevent."""
    factory = _make_factory(db_path)
    sleep_start = START + timedelta(days=2, hours=1)
    sleep_end = sleep_start + timedelta(days=3)  # 3 missed daily fires
    end = sleep_end + timedelta(days=3)

    result = await simulate.simulate(
        factory,
        owner,
        ACCOUNT,
        start=START,
        end=end,
        schedules=DAILY,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        sleep=(sleep_start, sleep_end),
    )

    catchups = result.catchups(scheduler.JobKind.CONNECTIONS_INCREMENTAL)
    assert len(catchups) == 1  # not 3
    woke = catchups[0].at
    assert sleep_end + timedelta(minutes=scheduler.CATCHUP_MIN_MINUTES) <= woke
    assert woke <= sleep_end + timedelta(minutes=scheduler.CATCHUP_MAX_MINUTES)
    assert all(f.at < sleep_start or f.at >= sleep_end for f in result.fires)

    # No two fires closer together than the cadence, anywhere in the replay:
    # a replayed backlog shows up as several fires at (or near) one instant.
    fires = sorted(f.at for f in result.fires if f.fired)
    assert len(fires) == len(set(fires))
    for earlier, later in pairwise(fires):
        assert later - earlier >= DAILY[scheduler.JobKind.CONNECTIONS_INCREMENTAL].interval

    # and the cadence resumes counted from the catch-up fire
    after_waking = [at for at in fires if at >= sleep_end]
    assert after_waking == [woke, woke + timedelta(days=1), woke + timedelta(days=2)]


# --- the stall guard: a broken schedule fails the test, never hangs it -------


async def test_a_schedule_that_stops_advancing_raises_instead_of_looping(
    db_path: str, owner: User
) -> None:
    """A mutation that leaves a due time where it was turns this event loop into
    an infinite one. Reproduce that directly -- a handler that rewinds its own
    due time after every fire -- and confirm the guard ends the run."""
    factory = _make_factory(db_path)
    stuck_at = START + timedelta(days=1)

    async def rewinding_handler(ctx: scheduler.JobContext) -> None:
        with session_scope(factory, write=True) as session:
            state = scheduler._load_state(session, owner, ACCOUNT, ctx.kind)
            assert state is not None
            scheduler._store_state(
                session,
                owner,
                ACCOUNT,
                ctx.kind,
                scheduler._JobState(due=stuck_at, fingerprint=state.fingerprint),
            )

    with pytest.raises(RuntimeError, match="stalled"):
        await simulate.simulate(
            factory,
            owner,
            ACCOUNT,
            start=START,
            end=START + timedelta(days=10),
            schedules=DAILY,
            registry={scheduler.JobKind.CONNECTIONS_INCREMENTAL: rewinding_handler},
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
            max_iterations=25,
        )
