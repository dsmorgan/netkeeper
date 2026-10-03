"""The dashboard's run controls (#324): pause and resume a run, pause the schedule,
and the last few contacts a run touched.

Every control here goes through the run, worker, and scheduler paths that were
already there: a pause is the cancel flag plus a marker, a resume is
``enrich_plan.start_resume``, and a paused schedule is one more reason the
scheduler's gate skips a due fire. Nothing here attaches to a real browser: the
runners read a fake tab (``profile_fakes``, ``voyager_pages``) and the served app
a fake Chrome (``run_fakes``).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta

import factories
import httpx
import pytest
from fastapi import FastAPI
from profile_fakes import FakeBrowser
from run_fakes import Clock as ServeClock
from run_fakes import fake_provider
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from test_enrichment import Sleeps, _enrich, _people, _plan, _setup
from test_runs_serve import (
    HEADERS,
    START,
    _local,
    _rows,
    client_for,
    drive,
    served,
)
from voyager_pages import FakeConnectionsSource

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.linkedin.connections import SyncMode
from netkeeper.linkedin.enrich import StopReason
from netkeeper.models import Contact, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.scoping import scoped
from netkeeper.services import enrich_plan, run_contacts, runs, scheduler
from netkeeper.services.connections_sync import sync_connections
from netkeeper.services.linkedin_accounts import (
    arm_scheduled_runs,
    disarm_scheduled_runs,
    ensure_account,
    pause_schedule,
    schedule_pause_state,
    schedule_paused,
    unpause_schedule,
)
from netkeeper.services.settings_kv import set_setting

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
ALL_DAY = (time(0, 0), time(23, 59))


def _held(account_id: int) -> bool:
    """Every account's browser lock is held: no run here is left behind."""
    return True


def _enrich_run(session: Session, user: User, *, now: datetime = NOW) -> int:
    return runs.create_run(
        session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=now
    ).id


# --- pause a run: runs.request_pause -----------------------------------------------------


def test_pause_flags_the_run_and_its_ending_reads_paused(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        run_id = _enrich_run(session, user)

        paused = runs.request_pause(session, user, run_id, now=NOW, browser_held=_held)
        assert paused.status is SyncRunStatus.RUNNING
        assert paused.cancel_requested_at is not None
        assert runs.pause_requested(session, user, run_id)
        assert runs.cancel_requested(session, user, run_id)  # the runner stops the same way
        again = runs.request_pause(session, user, run_id, now=NOW, browser_held=_held)
        assert again.cancel_requested_at == paused.cancel_requested_at  # idempotent

        # The runner records a cancelled stop; the run reads paused, and the marker is gone.
        ended = runs.finish_run(
            session, user, run_id, status=SyncRunStatus.ABORTED, now=NOW, stop_reason="cancelled"
        )
        assert ended.stop_reason == "paused"
        assert not runs.pause_requested(session, user, run_id)
        assert runs.view(ended).stop_reason_text == "paused; resume it to continue its plan"


def test_a_paused_run_that_stops_for_another_reason_keeps_that_reason(
    session_factory: sessionmaker[Session],
) -> None:
    """The budget ran out before the pause was read: the run says budget, and the
    marker still goes."""
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        run_id = _enrich_run(session, user)
        runs.request_pause(session, user, run_id, now=NOW, browser_held=_held)
        ended = runs.finish_run(
            session, user, run_id, status=SyncRunStatus.ABORTED, now=NOW, stop_reason="budget"
        )
        assert ended.stop_reason == "budget"
        assert not runs.pause_requested(session, user, run_id)


def test_a_cancel_after_a_pause_wins(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        run_id = _enrich_run(session, user)
        runs.request_pause(session, user, run_id, now=NOW, browser_held=_held)
        runs.request_cancel(session, user, run_id, now=NOW, browser_held=_held)
        assert not runs.pause_requested(session, user, run_id)
        ended = runs.finish_run(
            session, user, run_id, status=SyncRunStatus.ABORTED, now=NOW, stop_reason="cancelled"
        )
        assert ended.stop_reason == "cancelled"


def test_what_cannot_be_paused_is_refused(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        sync = runs.create_run(
            session, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.MANUAL, now=NOW
        ).id
        with pytest.raises(runs.RunNotPausable, match="only an enrichment run"):
            runs.request_pause(session, user, sync, now=NOW, browser_held=_held)
        runs.finish_run(session, user, sync, status=SyncRunStatus.COMPLETED, now=NOW)

        enrich = _enrich_run(session, user)
        runs.request_cancel(session, user, enrich, now=NOW, browser_held=_held)
        with pytest.raises(runs.RunError, match="already being cancelled"):
            runs.request_pause(session, user, enrich, now=NOW, browser_held=_held)
        runs.finish_run(session, user, enrich, status=SyncRunStatus.ABORTED, now=NOW)
        with pytest.raises(runs.RunFinished):
            runs.request_pause(session, user, enrich, now=NOW, browser_held=_held)
        with pytest.raises(runs.RunNotFound):
            runs.request_pause(session, user, 999, now=NOW, browser_held=_held)


def test_pausing_a_run_left_behind_fails_it_and_leaves_no_marker(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        run_id = _enrich_run(session, user, now=NOW - timedelta(hours=1))
        run = runs.request_pause(session, user, run_id, now=NOW, browser_held=lambda _: False)
        assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "interrupted")
        assert not runs.pause_requested(session, user, run_id)


# --- pause a run, end to end: the runner stops and the resume continues -----------------------


def _pause_after(
    factory: sessionmaker[Session], user_id: int, visits: int
) -> Callable[[str, object], None]:
    """An ``on_event`` that pauses the running enrichment once ``visits`` clicks are made."""
    seen = 0

    def on_event(kind: str, value: object) -> None:
        nonlocal seen
        if kind != "click":
            return
        seen += 1
        if seen != visits:
            return
        with session_scope(factory, write=True) as session:
            user = session.get(User, user_id)
            assert user is not None
            running = runs.running_run(session, user, ensure_account(session, user).id)
            assert running is not None
            runs.request_pause(session, user, running.id, now=NOW, browser_held=_held)

    return on_event


async def test_a_paused_enrichment_keeps_its_place_and_resume_continues_it(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(5)
    user_id, ids = _setup(session_factory, people)

    first = await _enrich(
        session_factory,
        user_id,
        FakeBrowser.of(people, on_event=_pause_after(session_factory, user_id, 2)),
    )

    assert first.result.reason is StopReason.CANCELLED
    plan = _plan(session_factory, user_id, first)
    assert plan.status == "aborted" and plan.completed == (ids[101], ids[102])
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        assert runs.get_run(session, user, first.run_id).stop_reason == "paused"
        touched = run_contacts.recent(session, user, runs.get_run(session, user, first.run_id))
    # Newest first, named, linked by id, with what happened.
    assert [(t.contact_id, t.outcome) for t in touched] == [
        (ids[102], "applied"),
        (ids[101], "applied"),
    ]
    assert touched[0].first_name == people[1].first

    browser = FakeBrowser.of(people)
    second = await _enrich(session_factory, user_id, browser, resume_of=first.run_id)

    assert browser.visited() == [p.slug for p in people[2:]]
    assert second.result.reason is StopReason.END_OF_PLAN
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        old = runs.get_run(session, user, first.run_id)
        new = runs.get_run(session, user, second.run_id)
        # The new run's record replaced the paused one's: one run's handful at a time.
        assert run_contacts.recent(session, user, old) == []
        assert [t.contact_id for t in run_contacts.recent(session, user, new)] == [
            ids[p.n] for p in reversed(people[2:])
        ]


# --- the last few contacts a run touched ------------------------------------------------------


def test_the_record_keeps_the_newest_few_of_one_run(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        run_id = _enrich_run(session, user)
        run = runs.get_run(session, user, run_id)
        contacts = [factories.make_contact(session, user).id for _ in range(15)]
        for contact_id in contacts:
            run_contacts.record(
                session, user, run.linkedin_account_id, run_id, [(contact_id, "not_found")]
            )
        shown = run_contacts.recent(session, user, run)
        assert run_contacts.RECENT_CONTACTS_MAX == 10
        assert [t.contact_id for t in shown] == list(reversed(contacts))[:10]
        assert shown[0].outcome_text == "profile not found"


def test_an_unreadable_record_reads_as_nothing(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        run = runs.get_run(session, user, _enrich_run(session, user))
        set_setting(
            session, user, f"linkedin.runs.{run.linkedin_account_id}.recent_contacts", "junk"
        )
        assert run_contacts.recent(session, user, run) == []


async def test_a_sync_records_the_connections_it_added(
    session_factory: sessionmaker[Session],
) -> None:
    from voyager_pages import PEOPLE

    with session_scope(session_factory, write=True) as session:
        user_id = factories.make_user(session).id
    report = await sync_connections(
        session_factory,
        user_id,
        SyncMode.FULL,
        FakeConnectionsSource(list(PEOPLE)),
        settings=Settings().linkedin,
        clock=lambda: NOW,
        sleep=Sleeps(),
        rng=random.Random(7),
    )
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        touched = run_contacts.recent(session, user, runs.get_run(session, user, report.run_id))
    assert len(touched) == run_contacts.RECENT_CONTACTS_MAX
    assert {t.outcome for t in touched} == {"added"}
    assert touched[0].outcome_text == "new connection added"
    assert {t.contact_id for t in touched} <= report.pages.created_contact_ids
    # Newest first is the page's order backwards: the last people on the page lead.
    urns = {c.id: c.li_urn for c in _contacts_of(session_factory, user_id)}
    assert [urns[t.contact_id] for t in touched] == [p.urn for p in reversed(PEOPLE)][:10]


def _contacts_of(factory: sessionmaker[Session], user_id: int) -> list[Contact]:
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        rows = list(session.scalars(scoped(user, Contact)))
        session.expunge_all()
    return rows


def test_a_pages_touched_contacts_keep_the_pages_order_not_their_ids(
    session_factory: sessionmaker[Session],
) -> None:
    """The contact made last is first on the page: it is recorded first, not last."""
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        early = factories.make_contact(session, user, li_urn="urn:li:fsd_profile:EARLY")
        late = factories.make_contact(session, user, li_urn="urn:li:fsd_profile:LATE")
        stray = factories.make_contact(session, user, li_urn=None)
        ordered = run_contacts.in_page_order(
            session,
            user,
            {early.id: "added", late.id: "confirmed", stray.id: "added"},
            ["urn:li:fsd_profile:LATE", None, "urn:li:fsd_profile:EARLY"],
        )
    assert ordered == [(late.id, "confirmed"), (early.id, "added"), (stray.id, "added")]


# --- pause the schedule: the scheduler's gate -------------------------------------------------

SCHEDULE = scheduler.JobSchedule(scheduler.JobKind.ENRICH, timedelta(hours=3))


def _armed_account(factory: sessionmaker[Session]) -> tuple[User, int]:
    with session_scope(factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        account = ensure_account(session, owner).id
        arm_scheduled_runs(session, owner, now=NOW)
    return owner, account


def _establish(factory: sessionmaker[Session], owner: User, account: int, at: datetime) -> datetime:
    with session_scope(factory, write=True) as session:
        return scheduler.establish_schedule(
            session,
            owner,
            account,
            SCHEDULE.kind,
            now=at,
            schedule=SCHEDULE,
            rng=random.Random(0),
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        ).due


async def _poll(
    factory: sessionmaker[Session],
    owner: User,
    account: int,
    at: datetime,
    calls: list[scheduler.JobContext],
) -> scheduler.FireResult | None:
    async def handler(ctx: scheduler.JobContext) -> None:
        calls.append(ctx)

    return await scheduler.poll_and_fire(
        factory,
        owner,
        account,
        SCHEDULE.kind,
        now=at,
        schedule=SCHEDULE,
        registry={SCHEDULE.kind: handler},
        tz="UTC",
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        rng=random.Random(0),
    )


async def test_a_paused_schedule_starts_nothing_and_unpausing_replays_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    """Armed and paused: every due fire is skipped as paused and the cadence moves on.
    Unpaused, nothing fires until the next due time, and then exactly once."""
    owner, account = _armed_account(session_factory)
    due = _establish(session_factory, owner, account, NOW)
    with session_scope(session_factory, write=True) as session:
        pause_schedule(session, owner, now=NOW)
    calls: list[scheduler.JobContext] = []

    skipped = []
    for i in range(3):  # three due times pass while paused
        result = await _poll(session_factory, owner, account, due + i * SCHEDULE.interval, calls)
        assert result is not None
        skipped.append((result.fired, result.skipped_reason))
    assert calls == []
    assert skipped == [(False, "paused")] * 3

    with session_scope(session_factory, write=True) as session:
        unpause_schedule(session, owner)
    unpaused_at = due + 2 * SCHEDULE.interval + timedelta(minutes=30)
    next_due = due + 3 * SCHEDULE.interval
    # Every heartbeat between unpausing and the next due time fires nothing.
    at = unpaused_at
    while at < next_due:
        assert await _poll(session_factory, owner, account, at, calls) is None
        at += timedelta(minutes=1)
    assert calls == []
    fired = await _poll(session_factory, owner, account, next_due, calls)
    assert fired is not None and fired.fired and len(calls) == 1


async def test_a_long_pause_across_downtime_still_owes_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    """Paused, then the process was down for days: the restart's single catch-up is
    skipped as paused like any other fire, and unpausing waits for the next due time."""
    owner, account = _armed_account(session_factory)
    due = _establish(session_factory, owner, account, NOW)
    with session_scope(session_factory, write=True) as session:
        pause_schedule(session, owner, now=NOW)
    restart = due + timedelta(days=3)
    catch_up = _establish(session_factory, owner, account, restart)
    assert restart < catch_up <= restart + timedelta(minutes=20)
    calls: list[scheduler.JobContext] = []
    result = await _poll(session_factory, owner, account, catch_up, calls)
    assert result is not None and result.skipped_reason == "paused"
    with session_scope(session_factory, write=True) as session:
        unpause_schedule(session, owner)
    assert (
        await _poll(session_factory, owner, account, catch_up + timedelta(hours=1), calls) is None
    )
    assert calls == []


async def test_unpaused_the_same_poll_fires(session_factory: sessionmaker[Session]) -> None:
    """Anti-coincidence for the skip above: armed and never paused, it fires."""
    owner, account = _armed_account(session_factory)
    due = _establish(session_factory, owner, account, NOW)
    calls: list[scheduler.JobContext] = []
    result = await _poll(session_factory, owner, account, due, calls)
    assert result is not None and result.fired and len(calls) == 1


def test_pause_is_idempotent_and_an_unreadable_one_counts_as_paused(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session)
        account = ensure_account(session, owner).id
        assert not schedule_pause_state(session, owner, account).paused
        pause_schedule(session, owner, now=NOW)
        pause_schedule(session, owner, now=NOW + timedelta(hours=1))
        assert schedule_pause_state(session, owner, account).paused_at == NOW  # the first time
        unpause_schedule(session, owner)
        assert not schedule_paused(session, owner, account)
        set_setting(session, owner, f"linkedin.schedule.{account}.paused_at", "not a time")
        assert schedule_paused(session, owner, account)  # fail closed: nothing starts
        state = schedule_pause_state(session, owner, account)
        assert (state.paused, state.paused_at, state.unreadable) == (True, None, True)


def test_another_users_pause_pauses_nothing_here(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory, write=True) as session:
        mine = factories.make_user(session)
        theirs = factories.make_user(session)
        account = ensure_account(session, mine).id
        pause_schedule(session, theirs, now=NOW)
        assert not schedule_paused(session, mine, account)


async def test_the_served_handler_records_nothing_while_paused(
    session_factory: sessionmaker[Session],
) -> None:
    """The gap between the scheduler's gate and the handler: paused there, no run."""
    from netkeeper.services.events import EventBus
    from netkeeper.services.scheduled_runs import serve_registry
    from netkeeper.services.tasks import TaskRunner

    owner, account = _armed_account(session_factory)
    with session_scope(session_factory, write=True) as session:
        pause_schedule(session, owner, now=NOW)

    class Never:
        async def execute(self, run_id: int, user_id: int) -> runs.RunOutcome:
            raise AssertionError("a paused schedule reached the worker")

    registry = serve_registry(session_factory, Never(), TaskRunner(EventBus()), clock=lambda: NOW)
    ctx = scheduler.JobContext(
        user_id=owner.id,
        account_id=account,
        kind=scheduler.JobKind.ENRICH,
        due=NOW,
        catch_up=False,
    )
    assert await registry[scheduler.JobKind.ENRICH](ctx) is scheduler.JobOutcome.PAUSED_AFTER_GATE
    with session_scope(session_factory) as session:
        user = session.get(User, owner.id)
        assert user is not None
        assert runs.list_runs(session, user)[1] == 0


FIRST = scheduler.JobSchedule(
    scheduler.JobKind.CONNECTIONS_FULL, timedelta(days=7), run_on_first_setup=True
)


class _Race:
    """An armed account's weekly full sync, and ``serve``'s real handler for it, for
    a pause (or a disarm) that lands between the scheduler's gate and the handler."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        from netkeeper.services.events import EventBus
        from netkeeper.services.scheduled_runs import serve_registry
        from netkeeper.services.tasks import TaskRunner

        class Never:
            async def execute(self, run_id: int, user_id: int) -> runs.RunOutcome:
                raise AssertionError("a fire stopped after the gate reached the worker")

        self.factory = factory
        self.owner, self.account = _armed_account(factory)
        with session_scope(factory, write=True) as session:
            self.due = scheduler.establish_schedule(
                session,
                self.owner,
                self.account,
                FIRST.kind,
                now=NOW,
                schedule=FIRST,
                rng=random.Random(0),
                tz="UTC",
                active_start=ALL_DAY[0],
                active_end=ALL_DAY[1],
            ).due
        self.served = serve_registry(factory, Never(), TaskRunner(EventBus()), clock=lambda: NOW)
        self.calls: list[scheduler.JobContext] = []

    async def pause_then_handle(self, ctx: scheduler.JobContext) -> scheduler.JobOutcome | None:
        with session_scope(self.factory, write=True) as session:
            pause_schedule(session, self.owner, now=NOW)
        return await self.served[FIRST.kind](ctx)

    async def disarm_then_handle(self, ctx: scheduler.JobContext) -> scheduler.JobOutcome | None:
        with session_scope(self.factory, write=True) as session:
            disarm_scheduled_runs(session, self.owner)
        return await self.served[FIRST.kind](ctx)

    async def record(self, ctx: scheduler.JobContext) -> None:
        self.calls.append(ctx)

    async def poll(
        self, at: datetime, handler: scheduler.JobHandler
    ) -> scheduler.FireResult | None:
        return await scheduler.poll_and_fire(
            self.factory,
            self.owner,
            self.account,
            FIRST.kind,
            now=at,
            schedule=FIRST,
            registry={FIRST.kind: handler},
            tz="UTC",
            active_start=ALL_DAY[0],
            active_end=ALL_DAY[1],
        )

    def fired_once(self) -> bool:
        with session_scope(self.factory) as session:
            state = scheduler._load_state(session, self.owner, self.account, FIRST.kind)
            assert state is not None
            return state.fired_once

    def recorded_runs(self) -> int:
        with session_scope(self.factory) as session:
            user = session.get(User, self.owner.id)
            assert user is not None
            return runs.list_runs(session, user)[1]


async def test_a_pause_that_lands_after_the_gate_keeps_first_setup_standing(
    session_factory: sessionmaker[Session],
) -> None:
    """The narrow race: the gate let the never-run full sync through, then a person
    paused before the handler recorded a run. The handler starts nothing and says
    so; the fire counts as skipped, so the full sync is offered again within the
    hour after the late poll, not a week later, and runs then."""
    race = _Race(session_factory)
    late = race.due + timedelta(minutes=30)

    raced = await race.poll(late, race.pause_then_handle)

    assert raced is not None and not raced.fired
    assert raced.skipped_reason == "paused_after_gate"
    assert raced.next_due == late + scheduler.FIRST_SETUP_RETRY  # from now, not from due
    assert race.fired_once() is False
    assert race.recorded_runs() == 0

    with session_scope(session_factory, write=True) as session:
        unpause_schedule(session, race.owner)
    again = await race.poll(raced.next_due, race.record)
    assert again is not None and again.fired and len(race.calls) == 1


async def test_a_pause_after_the_gate_on_a_later_fire_keeps_the_weekly_cadence(
    session_factory: sessionmaker[Session],
) -> None:
    """The full sync already ran once. A pause that races its next fire is skipped,
    but there is no first-setup standing to give back: the claim's week-out due time
    stands, and the kind still counts as having fired."""
    race = _Race(session_factory)
    first = await race.poll(race.due, race.record)
    assert first is not None and first.fired and race.fired_once() is True
    week_on = race.due + FIRST.interval
    assert first.next_due == week_on

    raced = await race.poll(week_on, race.pause_then_handle)

    assert raced is not None and not raced.fired
    assert raced.skipped_reason == "paused_after_gate"
    assert raced.next_due == week_on + FIRST.interval
    assert race.fired_once() is True
    assert len(race.calls) == 1 and race.recorded_runs() == 0


async def test_a_disarm_after_the_gate_is_skipped_with_its_own_reason(
    session_factory: sessionmaker[Session],
) -> None:
    race = _Race(session_factory)

    raced = await race.poll(race.due, race.disarm_then_handle)

    assert raced is not None and not raced.fired
    assert raced.skipped_reason == "disarmed_after_gate"
    assert raced.next_due == race.due + scheduler.FIRST_SETUP_RETRY
    assert race.fired_once() is False and race.recorded_runs() == 0


def test_posture_says_the_schedule_is_paused(session_factory: sessionmaker[Session]) -> None:
    from netkeeper.services.posture import posture

    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session, timezone="UTC")
        account = ensure_account(session, user).id
        arm_scheduled_runs(session, user, now=NOW)
        pause_schedule(session, user, now=NOW)
        report = posture(session, user, account, now=NOW, settings=Settings())
        (row,) = [p for p in report.protections if p.name == "scheduled runs"]
        assert row.value.startswith("armed since 2026-09-23 15:00 UTC; paused since")
        assert "schedule unpause" in row.value

        set_setting(session, user, f"linkedin.schedule.{account}.paused_at", "garbled")
        report = posture(session, user, account, now=NOW, settings=Settings())
        (row,) = [p for p in report.protections if p.name == "scheduled runs"]
        assert "paused (unreadable value)" in row.value


# --- the API ------------------------------------------------------------------------------


@pytest.mark.usefixtures("inside_active_hours")
async def test_the_api_pauses_a_run_shows_it_and_resumes_it(
    bare_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", "/nonexistent-dist")
    now = datetime.now(UTC)
    provider, connector = fake_provider()
    async with served(bare_engine, Settings(), provider, ServeClock(now)) as app:
        factory = app.state.session_factory
        with session_scope(factory, write=True) as session:
            user = _local(session)
            sync = runs.create_run(
                session,
                user,
                SyncRunKind.CONNECTIONS_FULL,
                trigger=SyncRunTrigger.MANUAL,
                now=now,
            )
            runs.finish_run(session, user, sync.id, status=SyncRunStatus.COMPLETED, now=now)
            ids = [factories.make_contact(session, user).id for _ in range(3)]
            run = runs.create_run(
                session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=now
            )
            enrich_plan.store_plan(session, user, run.id, ids)
            enrich_plan.mark_completed(session, user, run.id, ids[0])
            run_contacts.record(
                session, user, run.linkedin_account_id, run.id, [(ids[0], "applied")]
            )
            run_id, sync_id = run.id, sync.id
        async with client_for(app) as client:
            no_header = await client.post(f"/api/v1/linkedin/runs/{run_id}/pause")
            paused = await client.post(f"/api/v1/linkedin/runs/{run_id}/pause", headers=HEADERS)
            listed = (await client.get("/api/v1/linkedin/runs")).json()
            contacts = await client.get(f"/api/v1/linkedin/runs/{run_id}/contacts")
            other = await client.get(f"/api/v1/linkedin/runs/{sync_id}/contacts")
            missing = await client.get("/api/v1/linkedin/runs/999/contacts")
            a_sync = await client.post(f"/api/v1/linkedin/runs/{sync_id}/pause", headers=HEADERS)
            # The runner reads the flag and records its cancelled stop.
            with session_scope(factory, write=True) as session:
                runs.finish_run(
                    session,
                    _local(session),
                    run_id,
                    status=SyncRunStatus.ABORTED,
                    now=now,
                    stop_reason="cancelled",
                )
            ended = (await client.get(f"/api/v1/linkedin/runs/{run_id}")).json()
            too_late = await client.post(f"/api/v1/linkedin/runs/{run_id}/pause", headers=HEADERS)
            resumed = await client.post(
                f"/api/v1/linkedin/runs/{run_id}/resume",
                json={"max_visits": 1},
                headers=HEADERS,
            )
            await app.state.tasks.join()

    assert no_header.status_code == 403
    assert paused.status_code == 200, paused.text
    assert paused.json()["pause_requested"] is True and paused.json()["cancel_requested_at"]
    by_id = {item["id"]: item for item in listed["items"]}
    assert by_id[run_id]["pause_requested"] is True
    assert by_id[sync_id]["pause_requested"] is False
    assert contacts.status_code == 200
    assert contacts.json()["items"] == [
        {
            "contact_id": ids[0],
            "first_name": contacts.json()["items"][0]["first_name"],
            "last_name": contacts.json()["items"][0]["last_name"],
            "outcome": "applied",
            "outcome_text": "profile read and saved",
        }
    ]
    assert other.json() == {"items": []}  # an older run's record was replaced
    assert missing.status_code == 404
    assert a_sync.status_code == 409  # ended; a running sync is RunNotPausable, also 409
    assert (ended["status"], ended["stop_reason"], ended["pause_requested"]) == (
        "aborted",
        "paused",
        False,
    )
    assert ended["stop_reason_text"] == "paused; resume it to continue its plan"
    assert too_late.status_code == 409
    assert resumed.status_code == 202, resumed.text
    assert connector.attaches == 1  # only the resume, a person's own act, reached Chrome


async def test_the_schedule_pause_survives_a_restart_and_unpause_lets_runs_start(
    bare_engine: Engine, no_frontend: None
) -> None:
    """Paused through the API, ``serve`` restarts: still paused, the heartbeats across
    the first day's due times attach nothing. Unpaused, the next ones do."""
    settings = Settings()
    provider, connector = fake_provider()
    clock = ServeClock(START)
    async with served(bare_engine, settings, provider, clock) as app, client_for(app) as client:
        await client.post("/api/v1/linkedin/schedule/arm", json={"confirm": True}, headers=HEADERS)
        paused = await client.post("/api/v1/linkedin/schedule/pause", headers=HEADERS)
    assert paused.status_code == 200
    assert (paused.json()["armed"], paused.json()["paused"]) == (True, True)
    assert paused.json()["paused_at"] is not None

    async with served(bare_engine, settings, provider, clock) as app:
        async with client_for(app) as client:
            schedule = (await client.get("/api/v1/linkedin/schedule")).json()
            status = (await client.get("/api/v1/linkedin/status")).json()
        assert schedule["paused"] is True and schedule["armed"] is True
        assert status["schedule_paused"] is True
        # Past 08:30 in New York, when the first day's due times land.
        await drive(app, clock, clock.at + timedelta(hours=8))
        assert connector.attaches == 0
        assert _rows(bare_engine) == []

        async with client_for(app) as client:
            unpaused = await client.post("/api/v1/linkedin/schedule/unpause", headers=HEADERS)
        assert unpaused.json()["paused"] is False and unpaused.json()["paused_at"] is None
        # The first-setup full sync, skipped while paused, is offered again within the hour.
        await drive(app, clock, clock.at + scheduler.FIRST_SETUP_RETRY + timedelta(hours=1))
    assert connector.attaches > 0
    assert {run.trigger for run in _rows(bare_engine)} == {SyncRunTrigger.SCHEDULED}


@pytest.fixture
def no_frontend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", "/nonexistent-dist")


async def test_pausing_a_running_sync_is_refused_and_sets_no_flag(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    factory = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        sync = runs.create_run(
            session,
            user,
            SyncRunKind.CONNECTIONS_INCREMENTAL,
            trigger=SyncRunTrigger.MANUAL,
            now=datetime.now(UTC),
        ).id

    refused = await client.post(f"/api/v1/linkedin/runs/{sync}/pause", headers=HEADERS)

    assert refused.status_code == 409 and "only an enrichment run" in refused.json()["detail"]
    with session_scope(factory) as session:
        user = _local(session)
        run = runs.get_run(session, user, sync)
        assert run.status is SyncRunStatus.RUNNING and run.cancel_requested_at is None
        assert not runs.pause_requested(session, user, sync)


async def test_an_unreadable_pause_reads_paused_with_no_time(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        account = ensure_account(session, user).id
        set_setting(session, user, f"linkedin.schedule.{account}.paused_at", "garbled")

    schedule = (await client.get("/api/v1/linkedin/schedule")).json()
    status = (await client.get("/api/v1/linkedin/status")).json()

    assert (schedule["paused"], schedule["paused_at"]) == (True, None)
    assert status["schedule_paused"] is True
