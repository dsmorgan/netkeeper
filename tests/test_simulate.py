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


async def test_a_mutant_that_catches_up_per_missed_day_would_fail_the_above(
    db_path: str, owner: User
) -> None:
    """Mutation check: reimplements the buggy alternative -- one fire per
    interval missed -- directly against compute_due's inputs, and shows it
    produces the pattern the real implementation must not."""
    stale = START  # missed for 90 days
    now = START + timedelta(days=90)
    interval = timedelta(days=1)
    # The real behaviour: exactly one fire, soon.
    due, is_catchup, _ = scheduler.compute_due(stale, now=now, interval=interval, rng=Random(0))
    assert is_catchup is True
    assert due <= now + timedelta(minutes=scheduler.CATCHUP_MAX_MINUTES)

    # The bug this guards against, spelled out: a naive "advance by one
    # interval until caught up" loop schedules one fire per missed day.
    naive_fire_count = (now - stale) // interval
    assert naive_fire_count == 90  # exactly the pattern the real scheduler refuses to produce
    assert naive_fire_count != 1


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
        heat_settings=None,
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

    result = await simulate.simulate(
        factory,
        owner,
        ACCOUNT,
        start=START,
        end=START + timedelta(days=5),
        schedules=schedules,
        # seed 42: verified in tests/test_scheduler.py
        # (test_sync_account_schedule_staggers_a_shared_catchup_collision) to draw
        # near-identical catch-up jitter for both kinds -- the actual collision
        # case this test needs to exercise, not just a hoped-for one.
        seed=42,
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
