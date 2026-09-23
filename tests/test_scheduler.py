"""netkeeper.services.scheduler: next-fire persistence, catch-up, reschedule-only-
on-change, the interleave gap, and heat skip (spec 9.4, 9.5, 9.7, 9.9).

End-to-end restart/downtime behavior through the ``simulate`` harness is in
tests/test_simulate.py; this file exercises the scheduler's own functions
directly so each behavior is pinned to the exact mechanism that provides it,
not to a coincidence of two code paths landing on the same number (the
"coincidence trap" this project's test standard calls out).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from random import Random

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import HeatSettings
from netkeeper.db import session_scope
from netkeeper.models import User
from netkeeper.services import heat, scheduler

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
ACCOUNT = 1
ALL_DAY = (time(0, 0), time(0, 0))  # start == end: active all 24 hours (pacing.is_active_hour)
SCHEDULE = scheduler.JobSchedule(scheduler.JobKind.CONNECTIONS_INCREMENTAL, timedelta(days=1))
HEAT_SETTINGS = HeatSettings(per_block=1.0, half_life_hours=6, skip_threshold=2.5)


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer, timezone="UTC")


def _establish(
    session: Session,
    user: User,
    *,
    now: datetime = NOW,
    schedule: scheduler.JobSchedule = SCHEDULE,
    rng: Random | None = None,
    active_start: time = ALL_DAY[0],
    active_end: time = ALL_DAY[1],
) -> scheduler.ScheduleResult:
    return scheduler.establish_schedule(
        session,
        user,
        ACCOUNT,
        schedule.kind,
        now=now,
        schedule=schedule,
        rng=rng or Random(0),
        tz="UTC",
        active_start=active_start,
        active_end=active_end,
    )


# --- compute_due: the pure restore / catch-up / fresh decision --------------


def test_no_stored_schedule_is_a_fresh_interval_out() -> None:
    due, is_catchup, reason = scheduler.compute_due(
        None, now=NOW, interval=timedelta(days=1), rng=Random(0)
    )
    assert due == NOW + timedelta(days=1)
    assert is_catchup is False
    assert "no stored schedule" in reason


def test_a_future_due_time_is_restored_unchanged() -> None:
    future = NOW + timedelta(hours=3)
    due, is_catchup, reason = scheduler.compute_due(
        future, now=NOW, interval=timedelta(days=1), rng=Random(0)
    )
    assert due == future
    assert is_catchup is False
    assert "restored" in reason


def test_a_past_due_time_catches_up_once_soon_not_immediately() -> None:
    stale = NOW - timedelta(days=2)
    due, is_catchup, reason = scheduler.compute_due(
        stale, now=NOW, interval=timedelta(days=1), rng=Random(0)
    )
    assert is_catchup is True
    assert "catching up" in reason
    assert NOW + timedelta(minutes=scheduler.CATCHUP_MIN_MINUTES) <= due
    assert due <= NOW + timedelta(minutes=scheduler.CATCHUP_MAX_MINUTES)


def test_a_long_outage_still_catches_up_only_once() -> None:
    """The bug this item exists to prevent: once, not once per missed interval.
    A 90-day outage on a daily job must not schedule 90 catch-up runs."""
    stale = NOW - timedelta(days=90)
    due, is_catchup, _ = scheduler.compute_due(
        stale, now=NOW, interval=timedelta(days=1), rng=Random(0)
    )
    assert is_catchup is True
    # A single near-*future* fire (not 90 daily fires counted forward from
    # `stale`, which would themselves all still be in the past relative to
    # `now`): both bounds matter, or "replay one interval and stop" -- still
    # in the past for a 90-day gap on a daily job -- would slip through.
    assert due > NOW
    assert NOW + timedelta(minutes=scheduler.CATCHUP_MIN_MINUTES) <= due
    assert due <= NOW + timedelta(minutes=scheduler.CATCHUP_MAX_MINUTES)


# --- establish_schedule: persistence, restart, reschedule-only-on-change ----


def test_first_establish_persists_and_is_read_back(writer: Session, user: User) -> None:
    result = _establish(writer, user)
    assert result.changed is True
    assert scheduler.stored_due(writer, user, ACCOUNT, SCHEDULE.kind) == result.due


def test_restart_restores_the_pending_due_time_unchanged(
    session_factory: sessionmaker[Session],
) -> None:
    """The whole point: a second 'process' reading the same account's state must
    see the identical due time the first one computed, not a freshly derived one."""
    with session_scope(session_factory, write=True) as setup:
        owner = factories.make_user(setup, timezone="UTC")
        first = _establish(setup, owner)
    with session_scope(session_factory, write=True) as restart:
        owner_again = restart.get(User, owner.id)
        assert owner_again is not None
        restored = scheduler.establish_schedule(
            restart,
            owner_again,
            ACCOUNT,
            SCHEDULE.kind,
            now=NOW + timedelta(hours=1),  # later "now", as a real restart would see
            schedule=SCHEDULE,
            rng=Random(1),  # a different rng too -- must not matter for a restore
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )
    assert restored.due == first.due
    assert restored.changed is False


def test_removing_persistence_would_fail_the_restart_test(writer: Session, user: User) -> None:
    """Mutation check, made explicit: if the due time were not read back from
    storage, a second establish call could only ever recompute `now + interval`
    fresh, which moves every time `now` moves. Prove that directly here, so the
    restart guarantee is pinned to storage and not to two calls coincidentally
    landing on the same number."""
    first = _establish(writer, user, now=NOW)
    fresh_from_now = scheduler.compute_due(
        None, now=NOW + timedelta(hours=1), interval=SCHEDULE.interval, rng=Random(0)
    )[0]
    assert fresh_from_now != first.due  # proves the two code paths diverge
    restored = _establish(writer, user, now=NOW + timedelta(hours=1))
    assert restored.due == first.due  # establish_schedule takes the storage path, not the fresh one


def test_a_settings_change_reschedules_fresh_from_now(writer: Session, user: User) -> None:
    """Changing the timing parameters (here: active hours) is a genuine change,
    not downtime -- it must not go through the catch-up jitter, and it must not
    reuse the old due time either."""
    first = _establish(writer, user, now=NOW)
    changed = _establish(
        writer, user, now=NOW + timedelta(hours=1), active_start=time(9, 0), active_end=time(17, 0)
    )
    assert changed.changed is True
    assert changed.is_catchup is False
    assert changed.due != first.due
    assert changed.due == NOW + timedelta(hours=1) + SCHEDULE.interval


def test_a_noop_settings_write_does_not_rebuild_the_job(writer: Session, user: User) -> None:
    """The exact scenario spec calls out: a settings write that doesn't alter
    timing must leave the due time alone. Calling establish_schedule again with
    identical parameters, at a later `now`, is what a save-without-changes
    handler actually does."""
    first = _establish(writer, user, now=NOW)
    again = _establish(writer, user, now=NOW + timedelta(hours=6))  # same schedule/tz/hours
    assert again.changed is False
    assert again.due == first.due


def test_mutating_reschedule_always_would_fail_the_noop_test(writer: Session, user: User) -> None:
    """Mutation check: if establish_schedule always rebuilt (ignored the stored
    fingerprint), the second call above -- at a later `now` -- would compute a
    new `now + interval` and diverge from the first due time. Confirmed here so
    the no-op guarantee doesn't happen to pass by the two `now` values landing
    on the same due time."""
    first = _establish(writer, user, now=NOW)
    always_rebuilt = scheduler.compute_due(
        None, now=NOW + timedelta(hours=6), interval=SCHEDULE.interval, rng=Random(0)
    )[0]
    assert always_rebuilt != first.due


def test_a_due_time_that_has_passed_under_the_same_settings_is_downtime_not_a_change(
    writer: Session, user: User
) -> None:
    """Distinguishes the two ways establish_schedule can see a past due time:
    unchanged settings + a lapsed due time is downtime (catch-up), not a
    reschedule-from-now."""
    _establish(writer, user, now=NOW)
    later = NOW + SCHEDULE.interval + timedelta(hours=2)  # well past the first due time
    result = _establish(writer, user, now=later)
    assert result.is_catchup is True
    assert later + timedelta(minutes=scheduler.CATCHUP_MIN_MINUTES) <= result.due
    assert result.due <= later + timedelta(minutes=scheduler.CATCHUP_MAX_MINUTES)


def test_establish_schedule_requires_a_writer_session(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as setup:
        owner = factories.make_user(setup, timezone="UTC")
    owner_again = session.get(User, owner.id)
    assert owner_again is not None
    with pytest.raises(RuntimeError, match="writer session"):
        scheduler.establish_schedule(
            session,
            owner_again,
            ACCOUNT,
            SCHEDULE.kind,
            now=NOW,
            schedule=SCHEDULE,
            rng=Random(0),
            tz="UTC",
        )


# --- active hours: delegates to pacing, never reimplements it ---------------


def test_a_due_time_outside_active_hours_is_snapped_to_the_window_start(
    writer: Session, user: User
) -> None:
    # 22:00 UTC is outside a 08:30-21:30 window
    late = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)
    result = scheduler.establish_schedule(
        writer,
        user,
        ACCOUNT,
        SCHEDULE.kind,
        now=late,
        schedule=SCHEDULE,
        rng=Random(0),
        tz="UTC",
        active_start=time(8, 30),
        active_end=time(21, 30),
    )
    assert result.due.time() == time(8, 30)
    assert result.due > late


def test_respect_active_hours_false_ignores_the_window(writer: Session, user: User) -> None:
    late = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)
    schedule = scheduler.JobSchedule(SCHEDULE.kind, SCHEDULE.interval, respect_active_hours=False)
    result = scheduler.establish_schedule(
        writer,
        user,
        ACCOUNT,
        SCHEDULE.kind,
        now=late,
        schedule=schedule,
        rng=Random(0),
        tz="UTC",
        active_start=time(8, 30),
        active_end=time(21, 30),
    )
    assert result.due == late + SCHEDULE.interval


# --- record_fired: advances from the scheduled time, not wall-clock now -----


def test_record_fired_advances_from_due_not_from_now(writer: Session, user: User) -> None:
    """Anchoring to `now` instead of `due` would drift the cadence by however
    late polling happened to run; anchoring to `due` keeps it exact."""
    established = _establish(writer, user, now=NOW)
    polled_late = established.due + timedelta(minutes=45)  # heartbeat caught it late
    next_due = scheduler.record_fired(
        writer,
        user,
        ACCOUNT,
        SCHEDULE.kind,
        due=established.due,
        schedule=SCHEDULE,
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )
    assert next_due == established.due + SCHEDULE.interval
    assert next_due != polled_late + SCHEDULE.interval


def test_record_fired_clears_the_catchup_flag(writer: Session, user: User) -> None:
    stale = NOW - timedelta(days=2)
    scheduler._store_state(
        writer,
        user,
        ACCOUNT,
        SCHEDULE.kind,
        scheduler._JobState(
            due=stale,
            fingerprint=scheduler.ScheduleFingerprint.of(
                SCHEDULE, tz="UTC", active_start=ALL_DAY[0], active_end=ALL_DAY[1]
            ),
            is_catchup=True,
        ),
    )
    scheduler.record_fired(
        writer,
        user,
        ACCOUNT,
        SCHEDULE.kind,
        due=stale,
        schedule=SCHEDULE,
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )
    state = scheduler._load_state(writer, user, ACCOUNT, SCHEDULE.kind)
    assert state is not None
    assert state.is_catchup is False


def test_record_fired_requires_an_established_schedule(writer: Session, user: User) -> None:
    with pytest.raises(RuntimeError, match="no established schedule"):
        scheduler.record_fired(
            writer,
            user,
            ACCOUNT,
            SCHEDULE.kind,
            due=NOW,
            schedule=SCHEDULE,
            tz="UTC",
        )


# --- the interleave gap: never two kinds in the same minute -----------------


def test_stagger_leaves_well_separated_times_alone() -> None:
    dues = {
        scheduler.JobKind.ENRICH: NOW,
        scheduler.JobKind.INBOX: NOW + timedelta(hours=1),
    }
    assert scheduler.stagger_due_times(dues) == dues


def test_stagger_pushes_a_collision_apart_by_the_gap() -> None:
    """The exact scenario spec 9.5 names: two job kinds landing in the same
    minute get interleaved with a gap instead."""
    dues = {
        scheduler.JobKind.ENRICH: NOW,
        scheduler.JobKind.INBOX: NOW + timedelta(seconds=10),  # same minute
    }
    staggered = scheduler.stagger_due_times(dues)
    assert staggered[scheduler.JobKind.ENRICH] == NOW
    gap = staggered[scheduler.JobKind.INBOX] - staggered[scheduler.JobKind.ENRICH]
    assert gap >= scheduler.MIN_JOB_KIND_GAP
    assert staggered[scheduler.JobKind.ENRICH].replace(second=0, microsecond=0) != staggered[
        scheduler.JobKind.INBOX
    ].replace(second=0, microsecond=0)


def test_removing_the_stagger_step_would_fail_the_gap_test() -> None:
    """Mutation check: an identity 'stagger' (returns its input unchanged) is
    exactly what a removed gap-enforcement would look like -- confirm it
    produces a same-minute collision, so the assertion above is provably
    checking the real mechanism."""
    dues = {
        scheduler.JobKind.ENRICH: NOW,
        scheduler.JobKind.INBOX: NOW + timedelta(seconds=10),
    }
    identity = dict(dues)
    gap = identity[scheduler.JobKind.INBOX] - identity[scheduler.JobKind.ENRICH]
    assert gap < scheduler.MIN_JOB_KIND_GAP


def test_stagger_three_way_collision_keeps_every_pair_apart() -> None:
    dues = {
        scheduler.JobKind.ENRICH: NOW,
        scheduler.JobKind.INBOX: NOW,
        scheduler.JobKind.CONNECTIONS_INCREMENTAL: NOW,
    }
    staggered = scheduler.stagger_due_times(dues)
    ordered = sorted(staggered.values())
    for earlier, later in pairwise(ordered):
        assert later - earlier >= scheduler.MIN_JOB_KIND_GAP


def test_sync_account_schedule_staggers_a_shared_catchup_collision(
    writer: Session, user: User
) -> None:
    """Two kinds independently catching up after a shared outage can draw
    overlapping jitter; sync_account_schedule must still keep them apart."""
    schedules = {
        scheduler.JobKind.ENRICH: scheduler.JobSchedule(
            scheduler.JobKind.ENRICH, timedelta(hours=3)
        ),
        scheduler.JobKind.INBOX: scheduler.JobSchedule(scheduler.JobKind.INBOX, timedelta(hours=3)),
    }
    # Force both to the identical stale due time so both take the catch-up path.
    fingerprint = {
        kind: scheduler.ScheduleFingerprint.of(
            schedule, tz="UTC", active_start=ALL_DAY[0], active_end=ALL_DAY[1]
        )
        for kind, schedule in schedules.items()
    }
    for kind in schedules:
        scheduler._store_state(
            writer,
            user,
            ACCOUNT,
            kind,
            scheduler._JobState(due=NOW - timedelta(days=1), fingerprint=fingerprint[kind]),
        )
    # A single rng draws identical jitter for both, since interval is identical
    # and both start from the same stale due time -- the worst case for collision.
    scheduler.sync_account_schedule(
        writer,
        user,
        ACCOUNT,
        now=NOW,
        schedules=schedules,
        rng=Random(42),
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )
    due_enrich = scheduler.stored_due(writer, user, ACCOUNT, scheduler.JobKind.ENRICH)
    due_inbox = scheduler.stored_due(writer, user, ACCOUNT, scheduler.JobKind.INBOX)
    assert due_enrich is not None and due_inbox is not None
    assert abs(due_enrich - due_inbox) >= scheduler.MIN_JOB_KIND_GAP


# --- heat skip: above the threshold, the handler never runs -----------------


async def test_heat_above_threshold_skips_the_handler(
    session_factory: sessionmaker[Session],
) -> None:
    calls: list[scheduler.JobContext] = []

    async def recording_handler(ctx: scheduler.JobContext) -> None:
        calls.append(ctx)

    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        scheduler.establish_schedule(
            session,
            owner,
            ACCOUNT,
            SCHEDULE.kind,
            now=NOW,
            schedule=SCHEDULE,
            rng=Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )
        for _ in range(3):  # 3 blocks * 1.0 per_block = 3.0 >= 2.5 threshold
            heat.raise_heat(
                session, owner, ACCOUNT, now=NOW + SCHEDULE.interval, settings=HEAT_SETTINGS
            )

    result = await scheduler.poll_and_fire(
        session_factory,
        owner,
        ACCOUNT,
        SCHEDULE.kind,
        now=NOW + SCHEDULE.interval,  # due has arrived
        schedule=SCHEDULE,
        registry={SCHEDULE.kind: recording_handler},
        heat_settings=HEAT_SETTINGS,
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )

    assert calls == []
    assert result is not None
    assert result.fired is False
    assert result.skipped_reason == "heat"
    # the cadence still advances -- a skip is not a stall
    assert result.next_due == NOW + SCHEDULE.interval * 2


async def test_removing_the_heat_check_would_fail_the_skip_test(
    session_factory: sessionmaker[Session],
) -> None:
    """Mutation check: without the heat_settings gate (heat_settings=None), the
    same hot account fires normally -- proving the skip above is caused by the
    heat check and not some other condition in this scenario."""
    calls: list[scheduler.JobContext] = []

    async def recording_handler(ctx: scheduler.JobContext) -> None:
        calls.append(ctx)

    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        scheduler.establish_schedule(
            session,
            owner,
            ACCOUNT,
            SCHEDULE.kind,
            now=NOW,
            schedule=SCHEDULE,
            rng=Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )
        for _ in range(3):
            heat.raise_heat(
                session, owner, ACCOUNT, now=NOW + SCHEDULE.interval, settings=HEAT_SETTINGS
            )

    result = await scheduler.poll_and_fire(
        session_factory,
        owner,
        ACCOUNT,
        SCHEDULE.kind,
        now=NOW + SCHEDULE.interval,
        schedule=SCHEDULE,
        registry={SCHEDULE.kind: recording_handler},
        heat_settings=None,  # gate disabled
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )

    assert len(calls) == 1
    assert result is not None
    assert result.fired is True


async def test_cold_account_is_not_skipped(session_factory: sessionmaker[Session]) -> None:
    calls: list[scheduler.JobContext] = []

    async def recording_handler(ctx: scheduler.JobContext) -> None:
        calls.append(ctx)

    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        scheduler.establish_schedule(
            session,
            owner,
            ACCOUNT,
            SCHEDULE.kind,
            now=NOW,
            schedule=SCHEDULE,
            rng=Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )

    result = await scheduler.poll_and_fire(
        session_factory,
        owner,
        ACCOUNT,
        SCHEDULE.kind,
        now=NOW + SCHEDULE.interval,
        schedule=SCHEDULE,
        registry={SCHEDULE.kind: recording_handler},
        heat_settings=HEAT_SETTINGS,
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )

    assert len(calls) == 1
    assert result is not None
    assert result.fired is True
    assert result.is_catchup is False


# --- poll_and_fire: not-yet-due and never-established are both no-ops -------


async def test_poll_and_fire_is_a_noop_before_the_due_time(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        established = scheduler.establish_schedule(
            session,
            owner,
            ACCOUNT,
            SCHEDULE.kind,
            now=NOW,
            schedule=SCHEDULE,
            rng=Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )

    result = await scheduler.poll_and_fire(
        session_factory,
        owner,
        ACCOUNT,
        SCHEDULE.kind,
        now=established.due - timedelta(minutes=1),
        schedule=SCHEDULE,
        registry=scheduler.default_registry(),
        tz="UTC",
    )
    assert result is None


async def test_poll_and_fire_is_a_noop_when_never_established(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        session.flush()

    result = await scheduler.poll_and_fire(
        session_factory,
        owner,
        ACCOUNT,
        SCHEDULE.kind,
        now=NOW,
        schedule=SCHEDULE,
        registry=scheduler.default_registry(),
        tz="UTC",
    )
    assert result is None


# --- default_registry / noop_handler -----------------------------------------


async def test_default_registry_covers_every_job_kind() -> None:
    registry = scheduler.default_registry()
    assert set(registry) == set(scheduler.JobKind)
    for handler in registry.values():
        assert handler is scheduler.noop_handler


async def test_noop_handler_does_nothing_observable() -> None:
    ctx = scheduler.JobContext(
        user_id=1, account_id=ACCOUNT, kind=scheduler.JobKind.INBOX, due=NOW, catch_up=False
    )
    await scheduler.noop_handler(ctx)  # must not raise; nothing else to observe


# --- build_scheduler: constructed, never started -----------------------------


def test_build_scheduler_establishes_every_account_without_starting(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")

    # Deliberately never call .start() or .shutdown() here: "no job may be
    # armed by this PR" means this test must not run a live scheduler loop
    # either, only prove the object was built and the schedule established.
    built = scheduler.build_scheduler(session_factory, lambda: [(owner, ACCOUNT)], rng=Random(0))
    assert built.running is False  # never started by this call
    job = built.get_job(scheduler.HEARTBEAT_JOB_ID)
    assert job is not None
    with session_scope(session_factory) as session:
        due = scheduler.stored_due(
            session, owner, ACCOUNT, scheduler.JobKind.CONNECTIONS_INCREMENTAL
        )
    assert due is not None  # establishment happened at build time


def test_build_scheduler_restart_keeps_the_same_due_times(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")

    scheduler.build_scheduler(session_factory, lambda: [(owner, ACCOUNT)], rng=Random(0))
    with session_scope(session_factory) as session:
        before = {
            kind: scheduler.stored_due(session, owner, ACCOUNT, kind) for kind in scheduler.JobKind
        }

    # A second, independent build() call is the "restart": a fresh scheduler
    # object and a fresh rng, same on-disk state.
    scheduler.build_scheduler(session_factory, lambda: [(owner, ACCOUNT)], rng=Random(1))
    with session_scope(session_factory) as session:
        after = {
            kind: scheduler.stored_due(session, owner, ACCOUNT, kind) for kind in scheduler.JobKind
        }

    assert after == before
