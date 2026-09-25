"""netkeeper.services.scheduler: next-fire persistence, catch-up, reschedule-only-
on-change, the interleave gap, and heat skip (spec 9.4, 9.5, 9.7, 9.9).

End-to-end restart/downtime behavior through the ``simulate`` harness is in
tests/test_simulate.py; this file exercises the scheduler's own functions
directly so each behavior is pinned to the exact mechanism that provides it,
not to a coincidence of two code paths landing on the same number (the
"coincidence trap" this project's test standard calls out).
"""

from __future__ import annotations

import re
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
from netkeeper.models.base import utcnow
from netkeeper.services import heat, route_breaker, scheduler
from netkeeper.services.scheduler import DEFAULT_SCHEDULES
from netkeeper.services.settings_kv import get_setting, set_setting

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
ACCOUNT = 1
ALL_DAY = (time(0, 0), time(0, 0))  # start == end: active all 24 hours (pacing.is_active_hour)
SCHEDULE = scheduler.JobSchedule(scheduler.JobKind.CONNECTIONS_INCREMENTAL, timedelta(days=1))
HEAT_SETTINGS = HeatSettings(per_block=1.0, half_life_hours=6, skip_threshold=2.5)
CATCHUP_BOUNDS = (scheduler.CATCHUP_MIN_MINUTES, scheduler.CATCHUP_MAX_MINUTES)
# The seed whose first two catch-up draws land inside MIN_JOB_KIND_GAP of each
# other; every test that needs a real collision pins it, and asserts it.
COLLIDING_SEED = 0


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
    # Anti-coincidence: an establish_schedule that ignored the stored
    # fingerprint and always rebuilt would land here instead, so the two
    # outcomes are provably different numbers before the real call is made.
    always_rebuilt = scheduler.compute_due(
        None, now=NOW + timedelta(hours=6), interval=SCHEDULE.interval, rng=Random(0)
    )[0]
    assert always_rebuilt != first.due

    again = _establish(writer, user, now=NOW + timedelta(hours=6))  # same schedule/tz/hours
    assert again.changed is False
    assert again.due == first.due
    assert again.due != always_rebuilt


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
    # Matched on the guard's own name, not just "writer session": every
    # persisting function reaches _store_state, whose guard would otherwise
    # cover for a missing one anywhere upstream of it.
    guard = re.escape("scheduler.establish_schedule needs a writer")
    with pytest.raises(RuntimeError, match=guard):
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
    # The input really does collide, so an identity "stagger" -- what removing
    # the gap enforcement looks like -- could not pass the assertions below.
    collision = dues[scheduler.JobKind.INBOX] - dues[scheduler.JobKind.ENRICH]
    assert collision < scheduler.MIN_JOB_KIND_GAP

    staggered = scheduler.stagger_due_times(dues)
    assert staggered[scheduler.JobKind.ENRICH] == NOW
    gap = staggered[scheduler.JobKind.INBOX] - staggered[scheduler.JobKind.ENRICH]
    assert gap >= scheduler.MIN_JOB_KIND_GAP
    assert staggered[scheduler.JobKind.ENRICH].replace(second=0, microsecond=0) != staggered[
        scheduler.JobKind.INBOX
    ].replace(second=0, microsecond=0)


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
    # One `Random` yields two *different* draws, so most seeds put the two
    # kinds harmlessly far apart and the test passes without the stagger ever
    # running. Seed 0 draws 17.666 and 16.369 minutes -- 77.8 seconds apart,
    # inside MIN_JOB_KIND_GAP -- so the collision this test is named for
    # actually happens. Pinned below, so a future seed change cannot silently
    # drift back into harmlessness.
    probe = Random(COLLIDING_SEED)
    raw = [
        NOW + timedelta(minutes=probe.uniform(*CATCHUP_BOUNDS)) for _ in schedules
    ]  # the two draws sync_account_schedule is about to make, in the same order
    assert abs(raw[0] - raw[1]) < scheduler.MIN_JOB_KIND_GAP, "seed no longer collides"

    scheduler.sync_account_schedule(
        writer,
        user,
        ACCOUNT,
        now=NOW,
        schedules=schedules,
        rng=Random(COLLIDING_SEED),
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
        armed=scheduler.ARMING_NOT_REQUIRED,
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
        heat_settings=scheduler.HEAT_SKIP_DISABLED,  # the only way to turn 9.7's skip off
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        armed=scheduler.ARMING_NOT_REQUIRED,
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
        armed=scheduler.ARMING_NOT_REQUIRED,
    )

    assert len(calls) == 1
    assert result is not None
    assert result.fired is True
    assert result.is_catchup is False


# --- route-changed breaker skip: connections kinds only (#189 item 1) -------


def _tripped(session_factory: sessionmaker[Session], owner: User) -> None:
    """Two consecutive route_changed connections runs: exactly :data:`route_breaker.THRESHOLD`."""
    with session_scope(session_factory, write=True) as session:
        for _ in range(route_breaker.THRESHOLD):
            route_breaker.record(
                session, owner, ACCOUNT, route_changed=True, succeeded=False, now=NOW
            )


async def test_the_route_changed_breaker_skips_a_connections_fire_when_tripped(
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
    _tripped(session_factory, owner)

    result = await scheduler.poll_and_fire(
        session_factory,
        owner,
        ACCOUNT,
        SCHEDULE.kind,
        now=NOW + SCHEDULE.interval,
        schedule=SCHEDULE,
        registry={SCHEDULE.kind: recording_handler},
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        armed=scheduler.ARMING_NOT_REQUIRED,
        heat_settings=scheduler.HEAT_SKIP_DISABLED,
    )

    assert calls == []
    assert result is not None
    assert result.fired is False
    assert result.skipped_reason == "route_changed_breaker"
    # the cadence still advances -- a skip is not a stall
    assert result.next_due == NOW + SCHEDULE.interval * 2


async def test_a_route_changed_streak_below_threshold_does_not_skip(
    session_factory: sessionmaker[Session],
) -> None:
    """Mutation check: one route_changed run (below THRESHOLD == 2) fires normally,
    proving the skip above is caused by crossing the threshold, not by any
    route_changed streak at all."""
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
        route_breaker.record(session, owner, ACCOUNT, route_changed=True, succeeded=False, now=NOW)

    result = await scheduler.poll_and_fire(
        session_factory,
        owner,
        ACCOUNT,
        SCHEDULE.kind,
        now=NOW + SCHEDULE.interval,
        schedule=SCHEDULE,
        registry={SCHEDULE.kind: recording_handler},
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        armed=scheduler.ARMING_NOT_REQUIRED,
        heat_settings=scheduler.HEAT_SKIP_DISABLED,
    )

    assert len(calls) == 1
    assert result is not None
    assert result.fired is True


async def test_the_route_changed_breaker_does_not_skip_enrichment(
    session_factory: sessionmaker[Session],
) -> None:
    """#189 item 1's decision: enrichment has its own, separate unreadable-profile
    cap (spec 9.6) and does not share this counter."""
    enrich = DEFAULT_SCHEDULES[scheduler.JobKind.ENRICH]
    calls: list[scheduler.JobContext] = []

    async def recording_handler(ctx: scheduler.JobContext) -> None:
        calls.append(ctx)

    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        scheduler.establish_schedule(
            session,
            owner,
            ACCOUNT,
            enrich.kind,
            now=NOW,
            schedule=enrich,
            rng=Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )
    _tripped(session_factory, owner)

    result = await scheduler.poll_and_fire(
        session_factory,
        owner,
        ACCOUNT,
        enrich.kind,
        now=NOW + enrich.interval,
        schedule=enrich,
        registry={enrich.kind: recording_handler},
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        armed=scheduler.ARMING_NOT_REQUIRED,
        heat_settings=scheduler.HEAT_SKIP_DISABLED,
    )

    assert len(calls) == 1
    assert result is not None
    assert result.fired is True


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
        armed=scheduler.ARMING_NOT_REQUIRED,
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
        armed=scheduler.ARMING_NOT_REQUIRED,
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


# --- the hot path never replays a backlog (finding 1) ------------------------

THREE_HOURLY = scheduler.JobSchedule(scheduler.JobKind.ENRICH, timedelta(hours=3))


async def test_waking_from_a_long_sleep_fires_once_not_once_per_missed_interval(
    session_factory: sessionmaker[Session],
) -> None:
    """A sleeping laptop keeps the process alive, so the cold path never runs
    again -- the heartbeat just resumes into a backlog. One fire per missed
    interval is the request burst budgets, pacing, and heat all exist to
    prevent (spec 9.5, 9.6, 9.7): at the default three-hour cadence a three-day
    sleep would replay 24 runs, one per heartbeat minute."""
    calls: list[scheduler.JobContext] = []

    async def recording_handler(ctx: scheduler.JobContext) -> None:
        calls.append(ctx)

    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        established = scheduler.establish_schedule(
            session,
            owner,
            ACCOUNT,
            THREE_HOURLY.kind,
            now=NOW,
            schedule=THREE_HOURLY,
            rng=Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )

    woke = established.due + timedelta(days=3)  # 24 missed three-hour intervals
    results: list[scheduler.FireResult] = []
    for minute in range(60):  # an hour of one-minute heartbeats after waking
        result = await scheduler.poll_and_fire(
            session_factory,
            owner,
            ACCOUNT,
            THREE_HOURLY.kind,
            now=woke + timedelta(minutes=minute),
            schedule=THREE_HOURLY,
            registry={THREE_HOURLY.kind: recording_handler},
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
            armed=scheduler.ARMING_NOT_REQUIRED,
        )
        if result is not None:
            results.append(result)

    assert len(calls) == 1  # not 24
    assert len(results) == 1
    fire = results[0]
    assert fire.fired is True
    assert fire.is_catchup is True  # the lapse was routed through the catch-up branch
    assert woke + timedelta(minutes=scheduler.CATCHUP_MIN_MINUTES) <= fire.due
    assert fire.due <= woke + timedelta(minutes=scheduler.CATCHUP_MAX_MINUTES)
    # and the cadence resumes counted from the catch-up fire, not from the backlog
    assert fire.next_due == fire.due + THREE_HOURLY.interval


# --- poll_once: one kind per poll (finding 2) --------------------------------


async def test_poll_once_never_fires_two_kinds_in_the_same_poll(
    session_factory: sessionmaker[Session],
) -> None:
    """Spec 9.5's last bullet. Two kinds due 30 seconds apart, polled well after
    both of them: staggering times that are already in the past leaves them in
    the past, so filtering on `when <= now` lets both through.

    The two kinds are deliberately declared in the opposite order to their due
    times, so "whichever the schedules mapping happens to list first" and "the
    one that has been waiting longest" are different answers."""
    schedules = {
        scheduler.JobKind.INBOX: scheduler.JobSchedule(scheduler.JobKind.INBOX, timedelta(hours=3)),
        scheduler.JobKind.ENRICH: scheduler.JobSchedule(
            scheduler.JobKind.ENRICH, timedelta(hours=3)
        ),
    }
    calls: list[scheduler.JobKind] = []

    async def recording_handler(ctx: scheduler.JobContext) -> None:
        calls.append(ctx.kind)

    registry = dict.fromkeys(schedules, recording_handler)
    dues = {
        scheduler.JobKind.ENRICH: NOW,
        scheduler.JobKind.INBOX: NOW + timedelta(seconds=30),  # same minute
    }
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        for kind, schedule in schedules.items():
            scheduler._store_state(
                session,
                owner,
                ACCOUNT,
                kind,
                scheduler._JobState(
                    due=dues[kind],
                    fingerprint=scheduler.ScheduleFingerprint.of(
                        schedule, tz="UTC", active_start=ALL_DAY[0], active_end=ALL_DAY[1]
                    ),
                ),
            )

    polled_at = NOW + timedelta(minutes=30)  # both well past due, neither a full interval late
    fired = await scheduler.poll_once(
        session_factory,
        [(owner, ACCOUNT)],
        now=polled_at,
        registry=registry,
        schedules=schedules,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        armed=scheduler.ARMING_NOT_REQUIRED,
    )

    assert len(calls) == 1, f"both kinds ran in one poll: {calls}"
    assert len(fired) == 1
    # the one that had been waiting longest goes first, whatever order the
    # schedules mapping lists them in
    assert calls == [scheduler.JobKind.ENRICH]
    # the kind that did not run is pushed a full gap past the one that did,
    # so it runs on a later poll rather than in the same minute
    deferred = next(kind for kind in schedules if kind != calls[0])
    with session_scope(session_factory) as session:
        due = scheduler.stored_due(session, owner, ACCOUNT, deferred)
    assert due is not None
    assert due >= polled_at + scheduler.MIN_JOB_KIND_GAP

    later = await scheduler.poll_once(
        session_factory,
        [(owner, ACCOUNT)],
        now=due,
        registry=registry,
        schedules=schedules,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        armed=scheduler.ARMING_NOT_REQUIRED,
    )
    assert len(later) == 1
    assert calls == [calls[0], deferred]


# --- the safety-relevant constants, pinned to the spec they come from --------


def test_catchup_window_is_the_five_to_twenty_minutes_the_item_specifies() -> None:
    """Issue #105: "catch-up after downtime (once, 5 to 20 minutes after start)".
    Asserted as literals: every other test in this file refers to the constants,
    so without this one they could be changed to any numbers at all and the
    suite would stay green."""
    assert scheduler.CATCHUP_MIN_MINUTES == 5.0
    assert scheduler.CATCHUP_MAX_MINUTES == 20.0


def test_the_interleave_gap_is_two_minutes_and_clears_a_whole_minute() -> None:
    """Spec 9.5: "never run enrichment and a message send in the same minute."
    One minute is the floor that guarantees a different minute; two is the
    chosen value, with room for polling jitter around the boundary."""
    assert timedelta(minutes=2) == scheduler.MIN_JOB_KIND_GAP
    assert timedelta(minutes=1) <= scheduler.MIN_JOB_KIND_GAP


def test_the_heartbeat_is_a_minute_which_is_what_makes_the_gap_structural() -> None:
    """At most one kind fires per poll per account, so a heartbeat of a minute
    or coarser makes two kinds in one minute impossible for an account. A
    finer heartbeat would quietly give that guarantee up."""
    assert timedelta(minutes=1) == scheduler.DEFAULT_HEARTBEAT_INTERVAL
    assert timedelta(minutes=1) <= scheduler.DEFAULT_HEARTBEAT_INTERVAL


def test_the_default_cadences_are_the_ones_spec_9_4_names() -> None:
    """Spec 9.4: incremental sync "runs daily", full sync "on first setup and
    weekly". The other two carry no spec number and are not pinned here."""
    assert DEFAULT_SCHEDULES[scheduler.JobKind.CONNECTIONS_INCREMENTAL].interval == timedelta(
        days=1
    )
    assert DEFAULT_SCHEDULES[scheduler.JobKind.CONNECTIONS_FULL].interval == timedelta(days=7)


def test_only_the_full_sync_runs_on_first_setup() -> None:
    """Spec 9.4 says "runs on first setup" of the full sync and of nothing else."""
    assert {kind for kind, sch in DEFAULT_SCHEDULES.items() if sch.run_on_first_setup} == {
        scheduler.JobKind.CONNECTIONS_FULL
    }


# --- first setup: the full sync does not wait a week (#161) --------------------

FULL = DEFAULT_SCHEDULES[scheduler.JobKind.CONNECTIONS_FULL]


def _assert_in_catchup_window(due: datetime, now: datetime) -> None:
    assert now + timedelta(minutes=scheduler.CATCHUP_MIN_MINUTES) <= due
    assert due <= now + timedelta(minutes=scheduler.CATCHUP_MAX_MINUTES)


def test_first_setup_is_due_after_the_catchup_jitter_not_an_interval_out() -> None:
    due, is_catchup, reason = scheduler.compute_due(
        None, now=NOW, interval=timedelta(days=7), rng=Random(0), first_setup=True
    )
    _assert_in_catchup_window(due, NOW)
    assert is_catchup is False  # nothing was missed
    assert reason == "first setup"


def test_first_setup_does_not_change_a_restored_or_stale_due_time() -> None:
    """``first_setup`` only answers the ``prior_due is None`` question."""
    future = NOW + timedelta(days=3)
    assert scheduler.compute_due(
        future, now=NOW, interval=timedelta(days=7), rng=Random(0), first_setup=True
    ) == (future, False, "restored")
    _, is_catchup, _ = scheduler.compute_due(
        NOW - timedelta(days=1),
        now=NOW,
        interval=timedelta(days=7),
        rng=Random(0),
        first_setup=True,
    )
    assert is_catchup is True


def test_a_fresh_full_sync_is_due_within_the_catchup_window(writer: Session, user: User) -> None:
    result = _establish(writer, user, schedule=FULL)

    _assert_in_catchup_window(result.due, NOW)
    assert result.is_catchup is False
    assert scheduler.stored_due(writer, user, ACCOUNT, FULL.kind) == result.due


def test_a_fresh_full_sync_outside_active_hours_waits_for_the_window(
    writer: Session, user: User
) -> None:
    late = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)  # outside 08:30-21:30

    result = _establish(
        writer, user, now=late, schedule=FULL, active_start=time(8, 30), active_end=time(21, 30)
    )

    assert result.due == datetime(2026, 9, 21, 8, 30, tzinfo=UTC)


def test_a_fresh_install_keeps_the_other_kinds_first_due_and_the_gap(
    writer: Session, user: User
) -> None:
    """Every default kind at once, outside active hours: the full sync snaps to
    08:30 and so do enrich and inbox (22:00 + 3 h), so the interleave gap has
    real work to do. The other three keep their one-interval first fire."""
    late = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)
    window = datetime(2026, 9, 21, 8, 30, tzinfo=UTC)

    results = scheduler.sync_account_schedule(
        writer,
        user,
        ACCOUNT,
        now=late,
        rng=Random(0),
        tz="UTC",
        active_start=time(8, 30),
        active_end=time(21, 30),
    )

    dues = sorted(r.due for r in results.values())
    assert results[scheduler.JobKind.CONNECTIONS_FULL].due >= window
    assert results[scheduler.JobKind.CONNECTIONS_FULL].due < window + timedelta(hours=1)
    # One interval out (22:00 the next day), then snapped to the window after it.
    assert results[scheduler.JobKind.CONNECTIONS_INCREMENTAL].due == window + timedelta(days=1)
    for kind in (
        scheduler.JobKind.CONNECTIONS_INCREMENTAL,
        scheduler.JobKind.ENRICH,
        scheduler.JobKind.INBOX,
    ):
        assert results[kind].reason == "no stored schedule yet"
    assert all(b - a >= scheduler.MIN_JOB_KIND_GAP for a, b in pairwise(dues))


def test_restarting_an_established_full_sync_does_not_run_it_again(
    writer: Session, user: User
) -> None:
    """The cold path's "restored unchanged": a restart is not first setup."""
    first = _establish(writer, user, schedule=FULL)
    fired_at = first.due
    scheduler.record_fired(
        writer,
        user,
        ACCOUNT,
        FULL.kind,
        due=fired_at,
        schedule=FULL,
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )

    restart = _establish(writer, user, now=fired_at + timedelta(hours=1), schedule=FULL)

    assert restart.changed is False
    assert restart.due == fired_at + timedelta(days=7)


def _fire(writer: Session, user: User, due: datetime, *, handler_ran: bool = True) -> None:
    scheduler.record_fired(
        writer,
        user,
        ACCOUNT,
        FULL.kind,
        due=due,
        schedule=FULL,
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        handler_ran=handler_ran,
    )


def test_retiming_a_full_sync_that_has_fired_is_one_interval_out(
    writer: Session, user: User
) -> None:
    """Once the first full sync has run, a timing change is an ordinary retime."""
    first = _establish(writer, user, schedule=FULL)
    _fire(writer, user, first.due)
    later = first.due + timedelta(hours=1)

    retimed = _establish(
        writer, user, now=later, schedule=FULL, active_start=time(0, 0), active_end=time(23, 59)
    )

    assert retimed.reason == "timing changed"
    assert retimed.due == later + timedelta(days=7)


def test_a_second_retime_after_the_first_fire_is_still_one_interval_out(
    writer: Session, user: User
) -> None:
    """A retime carries the fired flag forward rather than resetting it."""
    first = _establish(writer, user, schedule=FULL)
    _fire(writer, user, first.due)
    _establish(
        writer,
        user,
        now=first.due + timedelta(hours=1),
        schedule=FULL,
        active_start=time(0, 0),
        active_end=time(23, 59),
    )
    later = first.due + timedelta(hours=2)

    again = _establish(
        writer, user, now=later, schedule=FULL, active_start=time(1, 0), active_end=time(23, 59)
    )

    assert again.due == later + timedelta(days=7)


def test_retiming_a_full_sync_before_its_first_fire_keeps_it_soon(
    writer: Session, user: User
) -> None:
    """The review's case: set up at 12:00, active hours edited at 12:10, before
    the pending first full sync fired. It stays due soon, snapped into the *new*
    hours (13:00-21:00, so 12:15-12:30 moves to 13:00) -- not a week out."""
    _establish(writer, user, schedule=FULL)  # NOW is 12:00
    edited = NOW + timedelta(minutes=10)

    retimed = _establish(
        writer, user, now=edited, schedule=FULL, active_start=time(13, 0), active_end=time(21, 0)
    )

    assert retimed.reason == "timing changed before the first fire"
    assert retimed.due == datetime(2026, 9, 20, 13, 0, tzinfo=UTC)


def test_retiming_before_the_first_fire_inside_the_new_hours_keeps_the_jitter(
    writer: Session, user: User
) -> None:
    _establish(writer, user, schedule=FULL)
    edited = NOW + timedelta(minutes=10)

    retimed = _establish(
        writer, user, now=edited, schedule=FULL, active_start=time(9, 0), active_end=time(17, 0)
    )

    _assert_in_catchup_window(retimed.due, edited)


def test_a_heat_skipped_first_fire_has_not_run_the_full_sync(writer: Session, user: User) -> None:
    """A skip advances the cadence but builds no baseline, so first setup still stands."""
    first = _establish(writer, user, schedule=FULL)
    _fire(writer, user, first.due, handler_ran=False)
    later = first.due + timedelta(hours=1)

    retimed = _establish(
        writer, user, now=later, schedule=FULL, active_start=time(0, 0), active_end=time(23, 59)
    )

    _assert_in_catchup_window(retimed.due, later)


def test_a_row_from_before_the_fired_flag_counts_as_fired(writer: Session, user: User) -> None:
    """The conservative reading of an old row: no extra full sync on a retime."""
    first = _establish(writer, user, schedule=FULL)
    key = f"scheduler.job.{ACCOUNT}.{FULL.kind.value}"
    stored = get_setting(writer, user, key)
    assert isinstance(stored, dict)
    raw = dict(stored)
    del raw["fired_once"]
    set_setting(writer, user, key, raw)
    later = first.due - timedelta(minutes=1)

    retimed = _establish(
        writer, user, now=later, schedule=FULL, active_start=time(0, 0), active_end=time(23, 59)
    )

    assert retimed.due == later + timedelta(days=7)


def test_the_first_setup_retry_is_one_hour() -> None:
    assert timedelta(hours=1) == scheduler.FIRST_SETUP_RETRY


@pytest.mark.parametrize("late", [timedelta(0), timedelta(hours=3)], ids=["on time", "3h late"])
async def test_a_heat_skipped_first_full_sync_is_offered_again_soon(
    session_factory: sessionmaker[Session], late: timedelta
) -> None:
    """Driven through ``poll_and_fire``'s real heat skip, not ``record_fired``
    directly: the skip must leave the flag unset and retry an hour after the
    poll that skipped it. Counted from the poll, not the stale due time, so a
    late poll does not leave a retry that is already due again."""
    calls: list[scheduler.JobContext] = []

    async def recording_handler(ctx: scheduler.JobContext) -> None:
        calls.append(ctx)

    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        first = scheduler.establish_schedule(
            session,
            owner,
            ACCOUNT,
            FULL.kind,
            now=NOW,
            schedule=FULL,
            rng=Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )
        for _ in range(3):  # 3.0 >= the 2.5 threshold, at the poll
            heat.raise_heat(session, owner, ACCOUNT, now=first.due + late, settings=HEAT_SETTINGS)

    result = await scheduler.poll_and_fire(
        session_factory,
        owner,
        ACCOUNT,
        FULL.kind,
        now=first.due + late,
        schedule=FULL,
        registry={FULL.kind: recording_handler},
        heat_settings=HEAT_SETTINGS,
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        armed=scheduler.ARMING_NOT_REQUIRED,
    )

    assert calls == []
    assert result is not None
    assert result.skipped_reason == "heat"
    assert result.next_due == first.due + late + timedelta(hours=1)
    with session_scope(session_factory) as session:
        state = scheduler._load_state(session, owner, ACCOUNT, FULL.kind)
    assert state is not None
    assert state.fired_once is False


async def test_a_route_changed_breaker_skipped_first_full_sync_is_offered_again_soon(
    session_factory: sessionmaker[Session],
) -> None:
    """#189 item 1: a tripped breaker's skip must not consume first-setup standing
    either -- the same mechanism the heat skip test above pins, driven through the
    real ``route_changed_breaker`` gate this time (spec: "same as disarmed/flagged
    skips")."""
    calls: list[scheduler.JobContext] = []

    async def recording_handler(ctx: scheduler.JobContext) -> None:
        calls.append(ctx)

    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        first = scheduler.establish_schedule(
            session,
            owner,
            ACCOUNT,
            FULL.kind,
            now=NOW,
            schedule=FULL,
            rng=Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )
    _tripped(session_factory, owner)

    result = await scheduler.poll_and_fire(
        session_factory,
        owner,
        ACCOUNT,
        FULL.kind,
        now=first.due,
        schedule=FULL,
        registry={FULL.kind: recording_handler},
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        armed=scheduler.ARMING_NOT_REQUIRED,
        heat_settings=scheduler.HEAT_SKIP_DISABLED,
    )

    assert calls == []
    assert result is not None
    assert result.skipped_reason == "route_changed_breaker"
    assert result.next_due == first.due + timedelta(hours=1)
    with session_scope(session_factory) as session:
        state = scheduler._load_state(session, owner, ACCOUNT, FULL.kind)
    assert state is not None
    assert state.fired_once is False


def test_a_heat_skip_after_the_first_run_keeps_the_weekly_cadence(
    writer: Session, user: User
) -> None:
    first = _establish(writer, user, schedule=FULL)
    _fire(writer, user, first.due)
    second = first.due + timedelta(days=7)

    after_skip = scheduler.record_fired(
        writer,
        user,
        ACCOUNT,
        FULL.kind,
        due=second,
        schedule=FULL,
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        handler_ran=False,
        now=second,
    )

    assert after_skip == second + timedelta(days=7)


def test_the_default_heat_gate_is_the_config_default_not_an_ad_hoc_one() -> None:
    """Spec 9.7's skip is unconditional, so the default is config's own
    ``[linkedin.heat]`` -- never off, and never numbers invented here."""
    assert HeatSettings() == scheduler.DEFAULT_HEAT_SETTINGS
    assert isinstance(scheduler.DEFAULT_HEAT_SETTINGS, HeatSettings)


# --- writer-session guards ----------------------------------------------------


def test_record_fired_requires_a_writer_session(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as setup:
        owner = factories.make_user(setup, timezone="UTC")
        _establish(setup, owner)
    owner_again = session.get(User, owner.id)
    assert owner_again is not None
    with pytest.raises(RuntimeError, match=re.escape("scheduler.record_fired needs a writer")):
        scheduler.record_fired(
            session,
            owner_again,
            ACCOUNT,
            SCHEDULE.kind,
            due=NOW,
            schedule=SCHEDULE,
            tz="UTC",
        )


def test_defer_as_catchup_requires_a_writer_session(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as setup:
        owner = factories.make_user(setup, timezone="UTC")
        _establish(setup, owner)
    owner_again = session.get(User, owner.id)
    assert owner_again is not None
    state = scheduler._load_state(session, owner_again, ACCOUNT, SCHEDULE.kind)
    assert state is not None
    with pytest.raises(RuntimeError, match=re.escape("scheduler._defer_as_catchup needs a writer")):
        scheduler._defer_as_catchup(
            session,
            owner_again,
            ACCOUNT,
            SCHEDULE.kind,
            state=state,
            now=state.due + SCHEDULE.interval * 2,
            schedule=SCHEDULE,
            rng=Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )


def test_store_state_requires_a_writer_session(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as setup:
        owner = factories.make_user(setup, timezone="UTC")
    owner_again = session.get(User, owner.id)
    assert owner_again is not None
    state = scheduler._JobState(
        due=NOW,
        fingerprint=scheduler.ScheduleFingerprint.of(
            SCHEDULE, tz="UTC", active_start=ALL_DAY[0], active_end=ALL_DAY[1]
        ),
    )
    with pytest.raises(RuntimeError, match=re.escape("scheduler._store_state needs a writer")):
        scheduler._store_state(session, owner_again, ACCOUNT, SCHEDULE.kind, state)


# --- build_scheduler wires the heat skip, without being asked to -------------


def _make_due_and_hot(
    session_factory: sessionmaker[Session], owner: User, kind: scheduler.JobKind
) -> None:
    """Rewind ``kind``'s established due time by a minute (due, but nowhere near
    a whole interval late) and raise heat past the *config default* threshold."""
    with session_scope(session_factory, write=True) as session:
        state = scheduler._load_state(session, owner, ACCOUNT, kind)
        assert state is not None
        scheduler._store_state(
            session,
            owner,
            ACCOUNT,
            kind,
            scheduler._JobState(due=utcnow() - timedelta(minutes=1), fingerprint=state.fingerprint),
        )
        settings = scheduler.DEFAULT_HEAT_SETTINGS
        while not heat.should_skip(session, owner, ACCOUNT, now=utcnow(), settings=settings):
            heat.raise_heat(session, owner, ACCOUNT, now=utcnow(), settings=settings)


async def test_build_scheduler_skips_a_hot_account_without_being_handed_heat_settings(
    session_factory: sessionmaker[Session],
) -> None:
    """P2-06 will wire this scheduler up. If the heat gate were opt-in, that
    would silently produce a scheduler with no heat protection at all (spec
    9.7 makes the skip unconditional), and nothing in the suite would notice."""
    calls: list[scheduler.JobContext] = []

    async def recording_handler(ctx: scheduler.JobContext) -> None:
        calls.append(ctx)

    schedules = {scheduler.JobKind.ENRICH: THREE_HOURLY}
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    built = scheduler.build_scheduler(
        session_factory,
        lambda: [(owner, ACCOUNT)],
        registry={scheduler.JobKind.ENRICH: recording_handler},
        schedules=schedules,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        rng=Random(0),
        armed=scheduler.ARMING_NOT_REQUIRED,
    )  # no heat_settings argument at all -- the case P2-06 will hit
    _make_due_and_hot(session_factory, owner, scheduler.JobKind.ENRICH)

    job = built.get_job(scheduler.HEARTBEAT_JOB_ID)
    assert job is not None
    await job.func()  # run the heartbeat body once; the scheduler is never started

    assert calls == []  # the browser job did not run
    with session_scope(session_factory) as session:
        due = scheduler.stored_due(session, owner, ACCOUNT, scheduler.JobKind.ENRICH)
    assert due is not None
    assert due > utcnow()  # the poll did happen: a skip advances the cadence


async def test_build_scheduler_with_the_skip_explicitly_disabled_still_fires(
    session_factory: sessionmaker[Session],
) -> None:
    """Anti-coincidence for the test above: the same hot account, the same
    heartbeat, with the gate explicitly named off -- it fires, so the skip
    above is caused by the default gate and not by the scenario."""
    calls: list[scheduler.JobContext] = []

    async def recording_handler(ctx: scheduler.JobContext) -> None:
        calls.append(ctx)

    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    built = scheduler.build_scheduler(
        session_factory,
        lambda: [(owner, ACCOUNT)],
        registry={scheduler.JobKind.ENRICH: recording_handler},
        schedules={scheduler.JobKind.ENRICH: THREE_HOURLY},
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        heat_settings=scheduler.HEAT_SKIP_DISABLED,
        rng=Random(0),
        armed=scheduler.ARMING_NOT_REQUIRED,
    )
    _make_due_and_hot(session_factory, owner, scheduler.JobKind.ENRICH)

    job = built.get_job(scheduler.HEARTBEAT_JOB_ID)
    assert job is not None
    await job.func()

    assert len(calls) == 1


# --- poll_once's interleave gap, covered directly ----------------------------


def _establish_two(
    session_factory: sessionmaker[Session],
    schedules: dict[scheduler.JobKind, scheduler.JobSchedule],
    dues: dict[scheduler.JobKind, datetime],
) -> User:
    """A user with both kinds persisted at the given due times."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        for kind, schedule in schedules.items():
            scheduler._store_state(
                session,
                owner,
                ACCOUNT,
                kind,
                scheduler._JobState(
                    due=dues[kind],
                    fingerprint=scheduler.ScheduleFingerprint.of(
                        schedule, tz="UTC", active_start=ALL_DAY[0], active_end=ALL_DAY[1]
                    ),
                ),
            )
    return owner


TWO_KINDS = {
    scheduler.JobKind.ENRICH: scheduler.JobSchedule(scheduler.JobKind.ENRICH, timedelta(hours=3)),
    scheduler.JobKind.INBOX: scheduler.JobSchedule(scheduler.JobKind.INBOX, timedelta(hours=3)),
}


async def test_poll_once_defers_the_second_of_two_kinds_due_at_the_same_instant(
    session_factory: sessionmaker[Session],
) -> None:
    """Two cadences drifting into alignment over a long uptime. Staggering the
    due times against each other cannot fix this at poll time -- both are
    already at `now` -- so the gap is enforced against the fire instead."""
    calls: list[scheduler.JobKind] = []

    async def recording_handler(ctx: scheduler.JobContext) -> None:
        calls.append(ctx.kind)

    owner = _establish_two(session_factory, TWO_KINDS, dict.fromkeys(TWO_KINDS, NOW))

    fired = await scheduler.poll_once(
        session_factory,
        [(owner, ACCOUNT)],
        now=NOW,
        registry=dict.fromkeys(TWO_KINDS, recording_handler),
        schedules=TWO_KINDS,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        armed=scheduler.ARMING_NOT_REQUIRED,
    )
    assert len(fired) == 1
    assert len(calls) == 1

    deferred = next(kind for kind in TWO_KINDS if kind != calls[0])
    with session_scope(session_factory) as session:
        due = scheduler.stored_due(session, owner, ACCOUNT, deferred)
        fired_next = scheduler.stored_due(session, owner, ACCOUNT, calls[0])
    assert due == NOW + scheduler.MIN_JOB_KIND_GAP  # a full gap past the fire
    assert fired_next == NOW + TWO_KINDS[calls[0]].interval  # the fired kind advanced normally


async def test_a_heat_skipped_kind_does_not_consume_the_interleave_slot(
    session_factory: sessionmaker[Session],
) -> None:
    """The gap exists to keep two *browser runs* out of one minute. A heat skip
    runs no browser work, so it must not push the other kind out of the poll --
    both cadences advance together while the account is hot (spec 9.7)."""
    calls: list[scheduler.JobKind] = []

    async def recording_handler(ctx: scheduler.JobContext) -> None:
        calls.append(ctx.kind)

    owner = _establish_two(session_factory, TWO_KINDS, dict.fromkeys(TWO_KINDS, NOW))
    with session_scope(session_factory, write=True) as session:
        while not heat.should_skip(session, owner, ACCOUNT, now=NOW, settings=HEAT_SETTINGS):
            heat.raise_heat(session, owner, ACCOUNT, now=NOW, settings=HEAT_SETTINGS)

    fired = await scheduler.poll_once(
        session_factory,
        [(owner, ACCOUNT)],
        now=NOW,
        registry=dict.fromkeys(TWO_KINDS, recording_handler),
        schedules=TWO_KINDS,
        heat_settings=HEAT_SETTINGS,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        armed=scheduler.ARMING_NOT_REQUIRED,
    )
    assert calls == []
    assert len(fired) == 2
    assert all(result.skipped_reason == "heat" for result in fired)
    with session_scope(session_factory) as session:
        for kind in TWO_KINDS:
            assert (
                scheduler.stored_due(session, owner, ACCOUNT, kind)
                == NOW + TWO_KINDS[kind].interval
            )
