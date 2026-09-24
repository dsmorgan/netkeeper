"""netkeeper.services.runs, the arm gate, retry parking, and the pinned constants (P2-10).

The end-to-end property -- ``netkeeper serve`` never reaches the browser while
disarmed -- is ``tests/test_runs_serve.py``. This module pins each of the three
checks behind it on its own, so a mutation of any one layer fails a test even
while the other two still hold: the scheduler's arm gate, ``create_run``'s
refusal to record a scheduled run, and the worker's refusal to attach for one.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.linkedin import enrich
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.services import connections_sync, runs, scheduler
from netkeeper.services.linkedin_accounts import (
    arm_scheduled_runs,
    disarm_scheduled_runs,
    ensure_account,
    scheduled_runs_armed,
)
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.scheduled_runs import RUN_KIND

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
ALL_DAY = (time(0, 0), time(23, 59))


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer, timezone="UTC")


# --- constants, pinned literally ---------------------------------------------------------


def test_the_safety_constants_are_the_specs() -> None:
    """Spec 9.9's retry window, #172's unreadable cap, the cancel slices, what serve runs."""
    assert scheduler.RETRY_MIN_MINUTES == 20.0
    assert scheduler.RETRY_MAX_MINUTES == 50.0
    assert enrich.MAX_UNREADABLE_PER_RUN == 3
    assert enrich.MAX_UNREADABLE_IN_A_ROW == 2
    assert connections_sync.CANCEL_SLICE_S == 5.0
    assert set(scheduler.SERVED_SCHEDULES) == {
        scheduler.JobKind.CONNECTIONS_FULL,
        scheduler.JobKind.CONNECTIONS_INCREMENTAL,
        scheduler.JobKind.ENRICH,
    }
    assert set(RUN_KIND) == set(scheduler.SERVED_SCHEDULES)
    assert {
        SyncRunKind.CONNECTIONS_FULL,
        SyncRunKind.CONNECTIONS_INCREMENTAL,
        SyncRunKind.ENRICH,
    } == runs.RUNNABLE_KINDS


# --- arming ---------------------------------------------------------------------------


def test_every_account_starts_disarmed_and_arming_is_explicit(writer: Session, user: User) -> None:
    account = ensure_account(writer, user)
    assert account.scheduled_runs_armed_at is None
    assert not scheduled_runs_armed(writer, user, account.id)

    arm_scheduled_runs(writer, user, now=NOW)
    assert scheduled_runs_armed(writer, user, account.id)
    arm_scheduled_runs(writer, user, now=NOW + timedelta(days=1))
    assert account.scheduled_runs_armed_at == NOW  # the first arming stands

    disarm_scheduled_runs(writer, user)
    assert not scheduled_runs_armed(writer, user, account.id)


def test_another_users_arming_arms_nothing_here(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    arm_scheduled_runs(writer, other, now=NOW)
    mine = ensure_account(writer, user)
    theirs = ensure_account(writer, other)
    assert not scheduled_runs_armed(writer, user, mine.id)
    assert not scheduled_runs_armed(writer, user, theirs.id)  # not this user's row at all


def test_no_config_value_arms_anything() -> None:
    """Config seeds settings_kv; the arm flag is a column nothing in config names."""
    rendered = repr(Settings()).lower()
    assert "arm" not in rendered.replace("warmup", "").replace("alarm", "")


def test_arming_needs_a_writer(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        reader = factories.make_user(session)
        with pytest.raises(RuntimeError, match="writer session"):
            arm_scheduled_runs(session, reader, now=NOW)


# --- create_run: the one door ------------------------------------------------------------


def test_a_scheduled_run_is_refused_on_a_disarmed_account(writer: Session, user: User) -> None:
    with pytest.raises(runs.ScheduledRunsDisarmed):
        runs.create_run(
            writer, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.SCHEDULED, now=NOW
        )
    assert runs.list_runs(writer, user)[1] == 0

    arm_scheduled_runs(writer, user, now=NOW)
    run = runs.create_run(
        writer, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.SCHEDULED, now=NOW
    )
    assert (run.status, run.trigger) == (SyncRunStatus.RUNNING, SyncRunTrigger.SCHEDULED)


def test_a_manual_run_is_allowed_while_disarmed(writer: Session, user: User) -> None:
    run = runs.create_run(
        writer, user, SyncRunKind.CONNECTIONS_INCREMENTAL, trigger=SyncRunTrigger.MANUAL, now=NOW
    )
    assert run.status is SyncRunStatus.RUNNING and run.started_at == NOW


def test_one_run_per_account_at_a_time(writer: Session, user: User) -> None:
    first = runs.create_run(
        writer, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
    )
    with pytest.raises(runs.RunAlreadyRunning, match=f"run {first.id}"):
        runs.create_run(
            writer, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
    runs.finish_run(writer, user, first.id, status=SyncRunStatus.COMPLETED, now=NOW)
    runs.create_run(
        writer, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.MANUAL, now=NOW
    )


@pytest.mark.parametrize("kind", [SyncRunKind.INBOX, SyncRunKind.MESSAGE_SEND])
def test_a_kind_with_no_runner_is_refused(writer: Session, user: User, kind: SyncRunKind) -> None:
    with pytest.raises(runs.RunError, match="no runner"):
        runs.create_run(writer, user, kind, trigger=SyncRunTrigger.MANUAL, now=NOW)


def test_max_visits_is_for_enrichment_and_positive(writer: Session, user: User) -> None:
    with pytest.raises(runs.RunError, match="enrichment runs only"):
        runs.create_run(
            writer,
            user,
            SyncRunKind.CONNECTIONS_FULL,
            trigger=SyncRunTrigger.MANUAL,
            now=NOW,
            max_visits=5,
        )
    with pytest.raises(runs.RunError, match="at least 1"):
        runs.create_run(
            writer, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW, max_visits=0
        )


# --- finishing, cancelling, the startup sweep ---------------------------------------------


def test_a_finished_run_stays_finished(writer: Session, user: User) -> None:
    run = runs.create_run(writer, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW)
    runs.finish_run(
        writer, user, run.id, status=SyncRunStatus.ABORTED, now=NOW, stop_reason="cancelled"
    )
    runs.finish_run(writer, user, run.id, status=SyncRunStatus.FAILED, now=NOW, error="late")
    assert (run.status, run.stop_reason, run.error) == (SyncRunStatus.ABORTED, "cancelled", None)
    with pytest.raises(runs.RunFinished):
        runs.request_cancel(writer, user, run.id, now=NOW)


def test_cancel_is_a_flag_on_the_row_and_idempotent(writer: Session, user: User) -> None:
    run = runs.create_run(writer, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW)
    assert not runs.cancel_requested(writer, user, run.id)
    runs.request_cancel(writer, user, run.id, now=NOW)
    runs.request_cancel(writer, user, run.id, now=NOW + timedelta(minutes=5))
    assert runs.cancel_requested(writer, user, run.id)
    assert run.cancel_requested_at == NOW
    other = factories.make_user(writer)
    with pytest.raises(runs.RunNotFound):
        runs.request_cancel(writer, other, run.id, now=NOW)


def test_the_error_line_is_one_line(writer: Session, user: User) -> None:
    run = runs.create_run(writer, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW)
    runs.finish_run(
        writer,
        user,
        run.id,
        status=SyncRunStatus.FAILED,
        now=NOW,
        error="first line\nSELECT ... [parameters: ('someone-slug',)]",
    )
    assert run.error == "first line"


def test_the_startup_sweep_fails_what_nobody_holds(writer: Session, user: User) -> None:
    held = factories.make_user(writer)
    stale = runs.create_run(
        writer, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
    )
    live = runs.create_run(writer, held, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW)
    held_account = live.linkedin_account_id

    count = runs.fail_interrupted_runs(
        writer, now=NOW, browser_held=lambda account_id: account_id == held_account
    )

    assert count == 1
    assert (stale.status, stale.error) == (SyncRunStatus.FAILED, runs.INTERRUPTED)
    assert live.status is SyncRunStatus.RUNNING


def test_refusals_come_before_anything(writer: Session, user: User) -> None:
    account = ensure_account(writer, user).id
    settings = Settings().linkedin
    runs.refuse_if_flagged_or_hot(writer, user, account, now=NOW, settings=settings)
    flag_session(writer, user, Outcome.LOGGED_OUT, url="/authwall")
    with pytest.raises(runs.SessionFlagged):
        runs.refuse_if_flagged_or_hot(writer, user, account, now=NOW, settings=settings)


# --- the scheduler's arm gate --------------------------------------------------------------

SCHEDULE = scheduler.JobSchedule(scheduler.JobKind.ENRICH, timedelta(hours=3))
FIRST = scheduler.JobSchedule(
    scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7), run_on_first_setup=True
)


def _establish(
    factory: sessionmaker[Session], owner: User, account: int, schedule: scheduler.JobSchedule
) -> datetime:
    with session_scope(factory, write=True) as session:
        result = scheduler.establish_schedule(
            session,
            owner,
            account,
            schedule.kind,
            now=NOW,
            schedule=schedule,
            rng=random.Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )
    return result.due


async def _poll(
    factory: sessionmaker[Session],
    owner: User,
    account: int,
    schedule: scheduler.JobSchedule,
    at: datetime,
    calls: list[scheduler.JobContext],
    **gate: object,
) -> scheduler.FireResult | None:
    async def handler(ctx: scheduler.JobContext) -> None:
        calls.append(ctx)

    return await scheduler.poll_and_fire(
        factory,
        owner,
        account,
        schedule.kind,
        now=at,
        schedule=schedule,
        registry={schedule.kind: handler},
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        **gate,  # type: ignore[arg-type]
    )


async def test_the_default_gate_skips_a_disarmed_account(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        account = ensure_account(session, owner).id
    due = _establish(session_factory, owner, account, SCHEDULE)
    calls: list[scheduler.JobContext] = []

    result = await _poll(session_factory, owner, account, SCHEDULE, due, calls)

    assert calls == []
    assert result is not None and (result.fired, result.skipped_reason) == (False, "disarmed")
    assert result.next_due == due + SCHEDULE.interval  # the cadence still moves on


async def test_the_default_gate_fires_an_armed_account(
    session_factory: sessionmaker[Session],
) -> None:
    """Anti-coincidence for the skip above: the same poll, armed."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        account = ensure_account(session, owner).id
        arm_scheduled_runs(session, owner, now=NOW)
    due = _establish(session_factory, owner, account, SCHEDULE)
    calls: list[scheduler.JobContext] = []

    result = await _poll(session_factory, owner, account, SCHEDULE, due, calls)

    assert len(calls) == 1 and result is not None and result.fired


async def test_a_disarmed_first_setup_kind_keeps_its_first_setup_standing(
    session_factory: sessionmaker[Session],
) -> None:
    """#164: the day-0 full sync is not spent by a disarmed skip; it is offered again
    FIRST_SETUP_RETRY later, and runs the first time it is offered armed."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        account = ensure_account(session, owner).id
    due = _establish(session_factory, owner, account, FIRST)
    calls: list[scheduler.JobContext] = []

    skipped = await _poll(session_factory, owner, account, FIRST, due, calls)
    assert skipped is not None and skipped.next_due == due + scheduler.FIRST_SETUP_RETRY

    with session_scope(session_factory, write=True) as session:
        arm_scheduled_runs(session, owner, now=NOW)
    fired = await _poll(session_factory, owner, account, FIRST, skipped.next_due, calls)
    assert fired is not None and fired.fired and len(calls) == 1


async def test_an_account_with_no_row_is_never_armed(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    due = _establish(session_factory, owner, 99, SCHEDULE)
    calls: list[scheduler.JobContext] = []
    result = await _poll(session_factory, owner, 99, SCHEDULE, due, calls)
    assert calls == [] and result is not None and result.skipped_reason == "disarmed"


# --- retry parking (spec 9.9) ---------------------------------------------------------------


async def test_a_retry_later_answer_parks_one_retry_twenty_to_fifty_minutes_out(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    due = _establish(session_factory, owner, 1, SCHEDULE)

    async def unreachable(ctx: scheduler.JobContext) -> scheduler.JobOutcome:
        return scheduler.JobOutcome.RETRY_LATER

    finished = due + timedelta(minutes=3)  # the attach attempt took a moment
    result = await scheduler.poll_and_fire(
        session_factory,
        owner,
        1,
        SCHEDULE.kind,
        now=due,
        schedule=SCHEDULE,
        registry={SCHEDULE.kind: unreachable},
        armed=scheduler.ARMING_NOT_REQUIRED,
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        rng=random.Random(3),
        clock=lambda: finished,
    )

    assert result is not None
    assert finished + timedelta(minutes=20) <= result.next_due <= finished + timedelta(minutes=50)
    with session_scope(session_factory) as session:
        assert scheduler.stored_due(session, owner, 1, SCHEDULE.kind) == result.next_due


def test_a_retry_never_pushes_a_sooner_due_time_later(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    due = _establish(session_factory, owner, 1, SCHEDULE)
    kept = scheduler.park_retry(
        session_factory,
        owner,
        1,
        SCHEDULE.kind,
        now=due - timedelta(minutes=5),
        schedule=SCHEDULE,
        rng=random.Random(0),
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )
    assert kept == due


def test_many_draws_stay_inside_the_retry_window(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    rng = random.Random(42)
    for _ in range(50):
        with session_scope(session_factory, write=True) as session:
            scheduler.establish_schedule(
                session,
                owner,
                1,
                SCHEDULE.kind,
                now=NOW,
                schedule=scheduler.JobSchedule(SCHEDULE.kind, timedelta(days=30)),
                rng=rng,
                tz="UTC",
                active_start=ALL_DAY[0],
                active_end=ALL_DAY[1],
            )
        parked = scheduler.park_retry(
            session_factory,
            owner,
            1,
            SCHEDULE.kind,
            now=NOW,
            schedule=SCHEDULE,
            rng=rng,
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )
        assert NOW + timedelta(minutes=20) <= parked <= NOW + timedelta(minutes=50)


def test_only_the_busy_heartbeat_warning_is_dropped() -> None:
    """The heartbeat awaits a run on purpose; APScheduler's per-minute "skipped" warning
    about it is noise. Any other warning still goes through."""
    import logging

    from netkeeper.services.scheduled_runs import _BusyHeartbeat

    def record(message: str) -> logging.LogRecord:
        return logging.LogRecord(
            "apscheduler.scheduler", logging.WARNING, __file__, 1, message, None, None
        )

    busy = record(
        'Execution of job "heartbeat" skipped: maximum number of running instances reached (1)'
    )
    assert not _BusyHeartbeat().filter(busy)
    assert _BusyHeartbeat().filter(record("Run time of job was missed by 0:05:00"))
