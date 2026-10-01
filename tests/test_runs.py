"""netkeeper.services.runs, the arm gate, retry parking, and the pinned constants (P2-10).

The end-to-end property -- ``netkeeper serve`` never reaches the browser while
disarmed -- is ``tests/test_runs_serve.py``. This module pins each of the three
checks behind it on its own, so a mutation of any one layer fails a test even
while the other two still hold: the scheduler's arm gate, ``create_run``'s
refusal to record a scheduled run, and the worker's refusal to attach for one.
"""

from __future__ import annotations

import dataclasses
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
from netkeeper.models import SyncRun, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
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
    assert timedelta(days=1) == scheduler.NOT_DONE_RETRY
    assert timedelta(hours=3) == scheduler.NOT_DONE_JITTER
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

    young = factories.make_user(writer)
    just_asked = runs.create_run(
        writer,
        young,
        SyncRunKind.ENRICH,
        trigger=SyncRunTrigger.MANUAL,
        now=NOW + runs.STALE_AFTER - timedelta(seconds=1),
    )

    count = runs.fail_interrupted_runs(
        writer,
        now=NOW + runs.STALE_AFTER,
        browser_held=lambda account_id: account_id == held_account,
    )

    assert count == 1
    # #175 F7: a run asked for a moment ago may belong to a terminal about to take
    # its lock; the sweep leaves it for create_run or cancel to judge later.
    assert just_asked.status is SyncRunStatus.RUNNING
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

    # The attach attempt took a while: the retry counts from when it gave up, not
    # from when the heartbeat started (#175 review, X26).
    finished = due + timedelta(minutes=40)
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


# --- a fire that ran but was not done (#200) ------------------------------------------------


async def test_a_not_done_answer_offers_the_fire_again_a_day_after_it_ended(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    weekly = scheduler.JobSchedule(scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7))
    due = _establish(session_factory, owner, 1, weekly)

    async def incomplete(ctx: scheduler.JobContext) -> scheduler.JobOutcome:
        return scheduler.JobOutcome.NOT_DONE

    finished = due + timedelta(minutes=40)
    result = await scheduler.poll_and_fire(
        session_factory,
        owner,
        1,
        weekly.kind,
        now=due,
        schedule=weekly,
        registry={weekly.kind: incomplete},
        armed=scheduler.ARMING_NOT_REQUIRED,
        heat_settings=scheduler.HEAT_SKIP_DISABLED,
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        rng=random.Random(3),
        clock=lambda: finished,
    )

    assert result is not None and result.fired
    assert finished + timedelta(days=1) <= result.next_due <= finished + timedelta(days=1, hours=3)
    assert result.next_due != finished + timedelta(days=1)  # jittered, not exactly a day
    with session_scope(session_factory) as session:
        assert scheduler.stored_due(session, owner, 1, weekly.kind) == result.next_due


async def _fire_weekly(
    session_factory: sessionmaker[Session],
    owner: User,
    weekly: scheduler.JobSchedule,
    at: datetime,
    outcome: scheduler.JobOutcome | None,
    *,
    active: tuple[time, time] = ALL_DAY,
) -> scheduler.FireResult:
    async def handler(ctx: scheduler.JobContext) -> scheduler.JobOutcome | None:
        return outcome

    result = await scheduler.poll_and_fire(
        session_factory,
        owner,
        1,
        weekly.kind,
        now=at,
        schedule=weekly,
        registry={weekly.kind: handler},
        armed=scheduler.ARMING_NOT_REQUIRED,
        heat_settings=scheduler.HEAT_SKIP_DISABLED,
        tz="UTC",
        active_start=active[0],
        active_end=active[1],
        rng=random.Random(5),
    )
    assert result is not None and result.fired
    return result


async def test_a_re_offer_is_offered_at_most_once_per_interval(
    session_factory: sessionmaker[Session],
) -> None:
    """#201 review, M1: the fire was not done, so it is offered again about a day later;
    that re-offer was not done either, so the kind goes back to its normal weekly due
    time -- never a daily full sync for as long as answers keep being lost."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    weekly = scheduler.JobSchedule(scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7))
    due = _establish(session_factory, owner, 1, weekly)
    not_done = scheduler.JobOutcome.NOT_DONE

    first = await _fire_weekly(session_factory, owner, weekly, due, not_done)
    assert first.next_due < due + timedelta(days=2)
    again = await _fire_weekly(session_factory, owner, weekly, first.next_due, not_done)
    assert again.next_due == due + timedelta(days=7)
    # The normal fire after that may be re-offered once again: one per interval.
    third = await _fire_weekly(session_factory, owner, weekly, again.next_due, not_done)
    assert third.next_due < again.next_due + timedelta(days=2)


async def test_a_re_offer_that_is_done_goes_back_to_the_normal_cadence(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    weekly = scheduler.JobSchedule(scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7))
    due = _establish(session_factory, owner, 1, weekly)
    first = await _fire_weekly(session_factory, owner, weekly, due, scheduler.JobOutcome.NOT_DONE)
    done = await _fire_weekly(session_factory, owner, weekly, first.next_due, None)
    assert done.next_due == due + timedelta(days=7)


async def test_a_re_offer_that_lapses_in_downtime_is_still_a_re_offer(
    session_factory: sessionmaker[Session],
) -> None:
    """#196 item 9, restart: ``serve`` comes back after the re-offer's due time, so the
    schedule is re-established through the downtime catch-up path. That catch-up is
    still the re-offer: not done again, it goes back to the weekly due time, never a
    second re-offer a day later."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    weekly = scheduler.JobSchedule(scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7))
    due = _establish(session_factory, owner, 1, weekly)
    not_done = scheduler.JobOutcome.NOT_DONE

    first = await _fire_weekly(session_factory, owner, weekly, due, not_done)
    assert first.next_due < due + timedelta(days=2)
    back = first.next_due + timedelta(hours=2)  # the restart, after the re-offer was due
    with session_scope(session_factory, write=True) as session:
        result = scheduler.establish_schedule(
            session,
            owner,
            1,
            weekly.kind,
            now=back,
            schedule=weekly,
            rng=random.Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )
        state = scheduler._load_state(session, owner, 1, weekly.kind)
    assert result.is_catchup
    assert state is not None and state.resume_due == due + timedelta(days=7)
    again = await _fire_weekly(session_factory, owner, weekly, result.due, not_done)
    assert again.next_due == due + timedelta(days=7)


@pytest.mark.parametrize("outcome", [scheduler.JobOutcome.NOT_DONE, None], ids=["not-done", "done"])
async def test_a_restart_just_before_the_weekly_time_never_doubles_the_full_sync(
    session_factory: sessionmaker[Session], outcome: scheduler.JobOutcome | None
) -> None:
    """#309 review: the re-offer was due on day 1 and ``serve`` was down until 30
    minutes before the weekly time. The catch-up fires minutes before it; going back to
    the weekly time would fire a second full sync minutes later. The next one is a
    whole interval after the catch-up instead."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    weekly = scheduler.JobSchedule(scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7))
    due = _establish(session_factory, owner, 1, weekly)
    first = await _fire_weekly(session_factory, owner, weekly, due, scheduler.JobOutcome.NOT_DONE)
    weekly_time = due + timedelta(days=7)
    back = weekly_time - timedelta(minutes=30)
    with session_scope(session_factory, write=True) as session:
        catchup = scheduler.establish_schedule(
            session,
            owner,
            1,
            weekly.kind,
            now=back,
            schedule=weekly,
            rng=random.Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )
    assert catchup.is_catchup and first.next_due < catchup.due < weekly_time
    fired = await _fire_weekly(session_factory, owner, weekly, catchup.due, outcome)
    assert fired.next_due == catchup.due + timedelta(days=7)
    assert fired.next_due > weekly_time  # later than before the fix, never sooner


async def test_a_re_offer_that_fires_late_after_a_sleep_never_doubles_the_full_sync(
    session_factory: sessionmaker[Session],
) -> None:
    """#309 review, the same on main before #196: the machine slept through the
    re-offer and woke 30 minutes before the weekly time, so the re-offer fires then
    (it lapsed by less than an interval, so it is no catch-up). Its next fire is a
    whole interval after it actually ran, never minutes later."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    weekly = scheduler.JobSchedule(scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7))
    due = _establish(session_factory, owner, 1, weekly)
    first = await _fire_weekly(session_factory, owner, weekly, due, scheduler.JobOutcome.NOT_DONE)
    weekly_time = due + timedelta(days=7)
    woke = weekly_time - timedelta(minutes=30)
    fired = await _fire_weekly(session_factory, owner, weekly, woke, scheduler.JobOutcome.NOT_DONE)
    assert fired.due == first.next_due and not fired.is_catchup
    assert fired.next_due == woke + timedelta(days=7)
    assert fired.next_due > first.next_due + timedelta(days=7)  # later than due + interval


@pytest.mark.parametrize(
    ("kind", "interval", "slept"),
    [
        (scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7), timedelta(days=6.99)),
        (scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7), timedelta(days=6.9)),
        (scheduler.JobKind.CONNECTIONS_INCREMENTAL, timedelta(days=1), timedelta(hours=23.9)),
    ],
    ids=["full-6.99d", "full-6.9d", "incremental-23.9h"],
)
async def test_a_fire_after_a_sleep_is_followed_a_whole_interval_later(
    session_factory: sessionmaker[Session],
    kind: scheduler.JobKind,
    interval: timedelta,
    slept: timedelta,
) -> None:
    """#309 re-review, F1: ``serve`` stayed alive while the machine slept for just under
    an interval, so the fire runs on waking, not as a catch-up. Anchored to its due time,
    the next fire would follow minutes or hours later; it is a whole interval after the
    wake fire instead."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    schedule = scheduler.JobSchedule(kind, interval)
    due = _establish(session_factory, owner, 1, schedule)
    woke = due + slept
    fired = await _fire_weekly(session_factory, owner, schedule, woke, None)
    assert fired.due == due and not fired.is_catchup
    assert fired.next_due == woke + interval
    assert fired.next_due > due + interval  # later than before the fix, never sooner


@pytest.mark.parametrize("late", [timedelta(0), timedelta(minutes=1), timedelta(minutes=5)])
async def test_an_on_time_fire_keeps_its_cadence(
    session_factory: sessionmaker[Session], late: timedelta
) -> None:
    """Within :data:`LATE_FIRE_SLACK` of its due time a fire is on time: the next one is
    anchored to the due time, so the cadence never drifts with polling."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    weekly = scheduler.JobSchedule(scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7))
    due = _establish(session_factory, owner, 1, weekly)
    fired = await _fire_weekly(session_factory, owner, weekly, due + late, None)
    assert fired.next_due == due + timedelta(days=7)


def test_the_late_fire_slack_is_pinned() -> None:
    assert timedelta(minutes=5) == scheduler.LATE_FIRE_SLACK


def _record_with_resume(
    session_factory: sessionmaker[Session],
    owner: User,
    schedule: scheduler.JobSchedule,
    *,
    due: datetime,
    resume_due: datetime,
) -> datetime:
    """Store a re-offer at ``due`` standing in front of ``resume_due``, and fire it on time."""
    with session_scope(session_factory, write=True) as session:
        state = scheduler._load_state(session, owner, 1, schedule.kind)
        assert state is not None
        scheduler._store_state(
            session,
            owner,
            1,
            schedule.kind,
            dataclasses.replace(state, due=due, resume_due=resume_due),
        )
        return scheduler.record_fired(
            session,
            owner,
            1,
            schedule.kind,
            due=due,
            schedule=schedule,
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
            now=due,
        )


def test_a_re_offer_exactly_a_day_before_the_normal_time_goes_back_to_it(
    session_factory: sessionmaker[Session],
) -> None:
    """#309 re-review, F3: the boundary. ``resume_due`` exactly NOT_DONE_RETRY after the
    re-offer ran is still followed."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    weekly = scheduler.JobSchedule(scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7))
    due = _establish(session_factory, owner, 1, weekly)
    resume = due + scheduler.NOT_DONE_RETRY
    assert _record_with_resume(session_factory, owner, weekly, due=due, resume_due=resume) == resume
    # A second less than a day away, and the next fire is a whole interval out instead.
    again = _record_with_resume(
        session_factory, owner, weekly, due=due, resume_due=resume - timedelta(seconds=1)
    )
    assert again == due + timedelta(days=7)


def test_a_re_offer_on_an_interval_shorter_than_a_day_is_refused(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    """#309 re-review, F2: the re-offer logic assumes an interval of at least
    NOT_DONE_RETRY. A state that carries a re-offer on a shorter one is refused that
    logic -- never sooner than the due time it stood in front of, nor than a whole
    interval after the fire."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    short = scheduler.JobSchedule(scheduler.JobKind.ENRICH, timedelta(hours=3))
    due = _establish(session_factory, owner, 1, short)
    far = due + timedelta(days=2)
    assert _record_with_resume(session_factory, owner, short, due=due, resume_due=far) == far
    near = due + timedelta(hours=1)
    assert _record_with_resume(
        session_factory, owner, short, due=due, resume_due=near
    ) == due + timedelta(hours=3)
    assert "carries a re-offer" in caplog.text


async def test_a_retried_re_offer_is_still_a_re_offer(
    session_factory: sessionmaker[Session],
) -> None:
    """#196 item 9, retry: the re-offer could not reach the browser, so a retry is
    parked. The retry is still the re-offer: not done, it goes back to the weekly due
    time, never a second re-offer a day later."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    weekly = scheduler.JobSchedule(scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7))
    due = _establish(session_factory, owner, 1, weekly)
    not_done = scheduler.JobOutcome.NOT_DONE

    first = await _fire_weekly(session_factory, owner, weekly, due, not_done)
    retry = await _fire_weekly(
        session_factory, owner, weekly, first.next_due, scheduler.JobOutcome.RETRY_LATER
    )
    assert retry.next_due < first.next_due + timedelta(hours=1)
    with session_scope(session_factory) as session:
        state = scheduler._load_state(session, owner, 1, weekly.kind)
    assert state is not None and state.resume_due == due + timedelta(days=7)
    again = await _fire_weekly(session_factory, owner, weekly, retry.next_due, not_done)
    assert again.next_due == due + timedelta(days=7)


async def test_a_retried_normal_fire_may_still_be_re_offered_once(
    session_factory: sessionmaker[Session],
) -> None:
    """The other side of #196 item 9: a retry of an ordinary fire is not a re-offer, so
    when it is not done it gets the one re-offer its interval allows."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    weekly = scheduler.JobSchedule(scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7))
    due = _establish(session_factory, owner, 1, weekly)
    retry = await _fire_weekly(
        session_factory, owner, weekly, due, scheduler.JobOutcome.RETRY_LATER
    )
    with session_scope(session_factory) as session:
        state = scheduler._load_state(session, owner, 1, weekly.kind)
    assert state is not None and state.resume_due is None
    first = await _fire_weekly(
        session_factory, owner, weekly, retry.next_due, scheduler.JobOutcome.NOT_DONE
    )
    assert first.next_due < due + timedelta(days=2)


async def test_a_re_offer_is_snapped_into_active_hours(
    session_factory: sessionmaker[Session],
) -> None:
    """#201 review, M8: a re-offer a day plus jitter after a fire late in the window
    would land after it closes; it moves to the next window's start."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    weekly = scheduler.JobSchedule(scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7))
    window = (time(9, 0), time(17, 0))
    with session_scope(session_factory, write=True) as session:
        scheduler.establish_schedule(
            session,
            owner,
            1,
            weekly.kind,
            now=NOW,
            schedule=weekly,
            rng=random.Random(1),
            tz="UTC",
            active_start=window[0],
            active_end=window[1],
        )
        due = scheduler.stored_due(session, owner, 1, weekly.kind)
    assert due is not None
    late = due.replace(hour=16, minute=55)
    with session_scope(session_factory, write=True) as session:
        state = scheduler._load_state(session, owner, 1, weekly.kind)
        assert state is not None
        scheduler._store_state(session, owner, 1, weekly.kind, dataclasses.replace(state, due=late))
    result = await _fire_weekly(
        session_factory, owner, weekly, late, scheduler.JobOutcome.NOT_DONE, active=window
    )
    next_day = (late + timedelta(days=1)).date()
    assert result.next_due.date() >= next_day
    assert time(9, 0) <= result.next_due.time() <= time(17, 0)


def test_offering_again_never_pushes_a_sooner_due_time_later(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
    due = _establish(session_factory, owner, 1, SCHEDULE)
    kept = scheduler.offer_again(
        session_factory,
        owner,
        1,
        SCHEDULE.kind,
        now=due - timedelta(hours=1),
        schedule=SCHEDULE,
        rng=random.Random(0),
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
    )
    assert kept == due


@pytest.mark.parametrize(
    ("kind", "lost", "expected"),
    [
        (scheduler.JobKind.CONNECTIONS_FULL, [{"start": 40, "cause": "Error (no data)"}], True),
        (scheduler.JobKind.CONNECTIONS_FULL, [], False),
        (
            scheduler.JobKind.CONNECTIONS_INCREMENTAL,
            [{"start": 40, "cause": "Error (no data)"}],
            False,
        ),
    ],
    ids=["full-with-losses", "full-without", "incremental-with-losses"],
)
async def test_a_scheduled_full_sync_that_lost_answers_is_not_done(
    session_factory: sessionmaker[Session],
    kind: scheduler.JobKind,
    lost: list[dict[str, object]],
    expected: bool,
) -> None:
    """#200: the weekly full sync is not done while its run lost any answers; an
    incremental sync, which never ages anyone, keeps its cadence."""
    from netkeeper.services.events import EventBus
    from netkeeper.services.scheduled_runs import serve_registry
    from netkeeper.services.tasks import TaskRunner

    class Finishes:
        async def execute(self, run_id: int, user_id: int) -> runs.RunOutcome:
            with session_scope(session_factory, write=True) as session:
                found = session.get(User, user_id)
                assert found is not None
                runs.finish_run(
                    session,
                    found,
                    run_id,
                    status=SyncRunStatus.ABORTED if lost else SyncRunStatus.COMPLETED,
                    now=NOW,
                    stop_reason="answer_lost" if lost else "end_of_list",
                    counts={"lost": lost},
                )
            return runs.RunOutcome.DONE

    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session)
        account = ensure_account(session, owner).id
        arm_scheduled_runs(session, owner, now=NOW)
    registry = serve_registry(
        session_factory, Finishes(), TaskRunner(EventBus()), clock=lambda: NOW
    )
    context = scheduler.JobContext(
        user_id=owner.id, account_id=account, kind=kind, due=NOW, catch_up=False
    )
    outcome = await registry[kind](context)
    assert (outcome is scheduler.JobOutcome.NOT_DONE) is expected
    if not expected:
        assert outcome is None


def test_lost_answers_reads_only_a_list() -> None:
    run = SyncRun(counts_json={"lost": [{"start": 40}, {"start": 90}]})
    assert runs.lost_answers(run) == 2
    for counts in (None, {}, {"lost": None}, {"lost": {"start": 40}}, {"lost": 3}):
        assert runs.lost_answers(SyncRun(counts_json=counts)) == 0


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


# --- #175 review ---------------------------------------------------------------------------


async def test_an_armed_but_flagged_account_is_skipped_before_any_run(
    session_factory: sessionmaker[Session],
) -> None:
    """F3: the scheduler checks the session flag too, so a flagged session gets no run row."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        account = ensure_account(session, owner).id
        arm_scheduled_runs(session, owner, now=NOW)
        flag_session(session, owner, Outcome.CHECKPOINT, url="/checkpoint/x")
    due = _establish(session_factory, owner, account, SCHEDULE)
    calls: list[scheduler.JobContext] = []

    result = await _poll(session_factory, owner, account, SCHEDULE, due, calls)

    assert calls == []
    assert result is not None and result.skipped_reason == "session_flagged"


def _stale_setup(writer: Session, user: User) -> SyncRun:
    return runs.create_run(writer, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW)


def test_a_run_left_behind_does_not_block_the_next_one(writer: Session, user: User) -> None:
    """F6: a SIGKILLed CLI run leaves its row running and its lock free. Past the grace,
    the next create_run marks it failed instead of refusing forever."""
    left = _stale_setup(writer, user)
    later = NOW + runs.STALE_AFTER
    with pytest.raises(runs.RunAlreadyRunning):  # someone holds the lock: it is live
        runs.create_run(
            writer,
            user,
            SyncRunKind.CONNECTIONS_FULL,
            trigger=SyncRunTrigger.MANUAL,
            now=later,
            browser_held=lambda account_id: True,
        )
    with pytest.raises(runs.RunAlreadyRunning):  # too young to judge
        runs.create_run(
            writer,
            user,
            SyncRunKind.CONNECTIONS_FULL,
            trigger=SyncRunTrigger.MANUAL,
            now=later - timedelta(seconds=1),
            browser_held=lambda account_id: False,
        )

    fresh = runs.create_run(
        writer,
        user,
        SyncRunKind.CONNECTIONS_FULL,
        trigger=SyncRunTrigger.MANUAL,
        now=later,
        browser_held=lambda account_id: False,
    )

    assert (left.status, left.stop_reason, left.error) == (
        SyncRunStatus.FAILED,
        "interrupted",
        runs.INTERRUPTED,
    )
    assert fresh.status is SyncRunStatus.RUNNING


def test_cancelling_a_run_left_behind_fails_it(writer: Session, user: User) -> None:
    left = _stale_setup(writer, user)
    returned = runs.request_cancel(
        writer, user, left.id, now=NOW + runs.STALE_AFTER, browser_held=lambda account_id: False
    )
    assert returned.status is SyncRunStatus.FAILED
    # #177 G1: the flag is set too, so a runner this process cannot see still stops.
    assert returned.cancel_requested_at == NOW + runs.STALE_AFTER


def test_cancelling_a_live_run_only_flags_it(writer: Session, user: User) -> None:
    live = _stale_setup(writer, user)
    runs.request_cancel(
        writer, user, live.id, now=NOW + runs.STALE_AFTER, browser_held=lambda account_id: True
    )
    assert live.status is SyncRunStatus.RUNNING and live.cancel_requested_at is not None


def test_a_lock_that_cannot_be_read_counts_as_held(monkeypatch: pytest.MonkeyPatch) -> None:
    """X8: a run nobody can prove is over is left alone, never failed under its process."""
    from netkeeper.linkedin import activity_lock

    def unreadable(*args: object, **kwargs: object) -> None:
        raise PermissionError("locks/ is not readable")

    monkeypatch.setattr(activity_lock, "inspect", unreadable)
    assert runs.lock_held(1, legacy=False)
    assert runs.lock_held(1, legacy=True)


def test_the_legacy_lock_counts_for_the_local_account_only(writer: Session) -> None:
    """X7, F10: an older process holding ``browser-local.lock`` is running the local
    user's account, whatever its id; another account is not held by it."""
    from netkeeper.linkedin import activity_lock
    from netkeeper.models import UserKind

    hosted = factories.make_user(writer, kind=UserKind.HOSTED)
    other_account = ensure_account(writer, hosted).id  # id 1
    local = factories.make_user(writer)
    local_account = ensure_account(writer, local).id
    old = activity_lock.try_claim(activity_lock.LEGACY_SHARED_KEY)
    assert old is not None
    try:
        held = runs.browser_held_for(writer)
        assert held(local_account)
        assert not held(other_account)
        left = runs.create_run(
            writer, local, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
        assert runs.fail_interrupted_runs(writer, now=NOW + runs.STALE_AFTER) == 0
        assert left.status is SyncRunStatus.RUNNING
    finally:
        old.release()
    assert runs.fail_interrupted_runs(writer, now=NOW + runs.STALE_AFTER) == 1


async def test_a_scheduled_fire_behind_a_manual_run_parks_a_retry(
    session_factory: sessionmaker[Session],
) -> None:
    """X12: the account's manual run is still going, so the scheduled one is retried
    later rather than dropped until its next interval."""
    from netkeeper.services.events import EventBus
    from netkeeper.services.scheduled_runs import serve_registry
    from netkeeper.services.tasks import TaskRunner

    class NeverCalled:
        async def execute(self, run_id: int, user_id: int) -> runs.RunOutcome:
            raise AssertionError("no run may start behind a running one")

    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session)
        account = ensure_account(session, owner).id
        arm_scheduled_runs(session, owner, now=NOW)
        runs.create_run(session, owner, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW)
    registry = serve_registry(
        session_factory, NeverCalled(), TaskRunner(EventBus()), clock=lambda: NOW
    )
    context = scheduler.JobContext(
        user_id=owner.id,
        account_id=account,
        kind=scheduler.JobKind.CONNECTIONS_INCREMENTAL,
        due=NOW,
        catch_up=False,
    )
    assert await registry[context.kind](context) is scheduler.JobOutcome.RETRY_LATER


def test_posture_reports_armed_and_disarmed(writer: Session, user: User) -> None:
    """X17: the scheduled-runs row says which, and says it the whole way."""
    from netkeeper.services.posture import posture

    account = ensure_account(writer, user).id

    def row() -> str:
        report = posture(writer, user, account, now=NOW, settings=Settings())
        (found,) = [p for p in report.protections if p.name == "scheduled runs"]
        return found.value

    assert row().startswith("disarmed")
    arm_scheduled_runs(writer, user, now=NOW)
    assert row().startswith("armed since 2026-09-23 15:00 UTC")
    disarm_scheduled_runs(writer, user)
    assert row().startswith("disarmed")
