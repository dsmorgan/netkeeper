"""The inbox poll's runner, worker route, scheduler handler, CLI, and API (P4-08, #378).

Everything runs against :class:`inbox_fakes.FakeInboxSource`: no page loads, nothing
leaves this machine, and every URN and message is invented. The real page source is
P4-01's (#380); until it lands the worker's default source refuses, and
``netkeeper serve`` does not schedule the poll.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import factories
import pytest
from inbox_fakes import FakeInboxSource, a_thread_with, delta, profile_urn
from run_fakes import Clock, fake_provider, no_sleep, worker_extractor
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker
from test_runs_serve import HEADERS, START, _local, _rows, client_for, heartbeat, serving
from typer.testing import CliRunner

from netkeeper import cli as cli_module
from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.config import BudgetSettings, LinkedInSettings, Settings
from netkeeper.crm import inbox_apply
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin.browser import BrowserRun, BrowserUnavailable
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.inbox import BOOTSTRAP_MAX_CONVERSATIONS, InboxReadStopped, InboxSource
from netkeeper.linkedin.observe import ObservationFailed
from netkeeper.linkedin.page_inbox import PageInbox
from netkeeper.models import (
    EnrollmentStatus,
    Interaction,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    TemplateChannel,
    User,
)
from netkeeper.scoping import install_scope_guard, scoped
from netkeeper.services import budgets, route_breaker, runs, scheduler
from netkeeper.services import heat as heat_service
from netkeeper.services.budgets import ActionClass
from netkeeper.services.inbox_poll import (
    FIRST_SHORT,
    INCOMPLETE,
    MAX_CONVERSATIONS_PER_POLL,
    READ,
    poll_inbox,
)
from netkeeper.services.linkedin_accounts import arm_scheduled_runs, ensure_account
from netkeeper.services.linkedin_session import flag_session, session_flag
from netkeeper.services.posture import PostureReport, Status, posture
from netkeeper.services.runs import HeatSkipped, SessionFlagged
from netkeeper.services.scheduled_runs import serve_registry
from netkeeper.services.scheduler import SERVED_SCHEDULES, JobKind
from netkeeper.services.settings_kv import get_setting, set_setting
from netkeeper.services.users import ensure_local_user
from netkeeper.worker import BrowserWorker, inbox_source

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
SETTINGS = LinkedInSettings()
ADA = profile_urn("ada")
SECRET = "an invented sentence nobody may log"


@pytest.fixture
def user_id(session_factory: sessionmaker[Session]) -> int:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        ada = factories.make_contact(session, user, li_urn=ADA)
        factories.make_enrollment(session, factories.make_campaign(session, user), ada)
        return user.id


def _user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    assert user is not None
    return user


def _run(factory: sessionmaker[Session], user_id: int, run_id: int) -> SyncRun:
    with session_scope(factory) as session:
        run = runs.get_run(session, _user(session, user_id), run_id)
        session.expunge(run)
        return run


def _spent(factory: sessionmaker[Session], user_id: int) -> int:
    with session_scope(factory) as session:
        user = _user(session, user_id)
        return budgets.status(
            session,
            user,
            ensure_account(session, user).id,
            ActionClass.INBOX_POLLS,
            now=NOW,
            settings=SETTINGS.budget,
        ).day.count


def _interactions(factory: sessionmaker[Session], user_id: int) -> int:
    with session_scope(factory) as session:
        return len(session.scalars(scoped(_user(session, user_id), Interaction)).all())


# --- the runner -------------------------------------------------------------------------


async def test_a_poll_applies_what_it_read_and_completes(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    source = FakeInboxSource(delta(a_thread_with(ADA, NOW - timedelta(hours=1))))
    report = await poll_inbox(
        session_factory, user_id, source, settings=SETTINGS, clock=lambda: NOW
    )

    run = _run(session_factory, user_id, report.run_id)
    assert (run.kind, run.status, run.stop_reason) == (
        SyncRunKind.INBOX,
        SyncRunStatus.COMPLETED,
        READ,
    )
    assert run.counts_json == {
        "conversations_read": 1,
        "matched": 1,
        "ignored_unknown": 0,
        "skipped_group": 0,
        "skipped_other": 0,
        "messages_new": 2,
    }
    assert _spent(session_factory, user_id) == 1  # one poll, one unit
    assert _interactions(session_factory, user_id) == 2
    (spec,) = source.specs
    assert spec.since is None  # the first poll, and nothing sent to read back to
    assert spec.watched_urns == {ADA}
    assert spec.max_conversations == BOOTSTRAP_MAX_CONVERSATIONS == 200  # a first poll


async def test_the_next_poll_reads_from_the_last_complete_one(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    first = await poll_inbox(
        session_factory, user_id, FakeInboxSource(), settings=SETTINGS, clock=lambda: NOW
    )
    later = NOW + timedelta(hours=3)
    partial = FakeInboxSource(delta(complete=False))
    second = await poll_inbox(
        session_factory, user_id, partial, settings=SETTINGS, clock=lambda: later
    )
    third_source = FakeInboxSource()
    await poll_inbox(
        session_factory,
        user_id,
        third_source,
        settings=SETTINGS,
        clock=lambda: later + timedelta(hours=3),
    )

    assert _run(session_factory, user_id, first.run_id).started_at == NOW
    run = _run(session_factory, user_id, second.run_id)
    assert (run.status, run.stop_reason) == (SyncRunStatus.ABORTED, INCOMPLETE)
    assert partial.specs[0].since == NOW
    assert third_source.specs[0].since == NOW  # an incomplete poll moves nothing on


async def test_the_budget_stops_a_poll_before_it_reads(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    settings = LinkedInSettings(budget=BudgetSettings(inbox_polls_per_day=1))
    for _ in range(2):  # the second spends the one unit of overshoot spec 9.6 allows
        await poll_inbox(
            session_factory, user_id, FakeInboxSource(), settings=settings, clock=lambda: NOW
        )
    source = FakeInboxSource(delta(a_thread_with(ADA, NOW)))
    report = await poll_inbox(
        session_factory, user_id, source, settings=settings, clock=lambda: NOW
    )

    assert source.specs == []  # nothing read
    run = _run(session_factory, user_id, report.run_id)
    assert (run.status, run.stop_reason) == (SyncRunStatus.ABORTED, "budget")
    assert _interactions(session_factory, user_id) == 0


async def test_a_session_flag_refuses_a_poll(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    with session_scope(session_factory, write=True) as session:
        flag_session(session, _user(session, user_id), Outcome.CHECKPOINT, url="https://x/c")
    source = FakeInboxSource()
    with pytest.raises(SessionFlagged):
        await poll_inbox(session_factory, user_id, source, settings=SETTINGS, clock=lambda: NOW)

    assert source.specs == [] and _spent(session_factory, user_id) == 0
    with session_scope(session_factory) as session:
        (run,) = session.scalars(scoped(_user(session, user_id), SyncRun)).all()
        assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "session_flagged")


async def test_heat_over_the_threshold_refuses_a_poll(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        account = ensure_account(session, user).id
        while not heat_service.should_skip(session, user, account, now=NOW, settings=SETTINGS.heat):
            heat_service.raise_heat(session, user, account, now=NOW, settings=SETTINGS.heat)
    source = FakeInboxSource()
    with pytest.raises(HeatSkipped):
        await poll_inbox(session_factory, user_id, source, settings=SETTINGS, clock=lambda: NOW)
    assert source.specs == [] and _spent(session_factory, user_id) == 0


@pytest.mark.parametrize(
    ("outcome", "heat", "flag"),
    [
        (Outcome.CHECKPOINT, True, True),
        (Outcome.LOGGED_OUT, False, True),
        (Outcome.THROTTLED, True, False),
        (Outcome.ROUTE_CHANGED, False, False),
    ],
)
async def test_a_wall_is_classified_and_nothing_is_retried(
    session_factory: sessionmaker[Session],
    user_id: int,
    outcome: Outcome,
    heat: bool,
    flag: bool,
) -> None:
    source = FakeInboxSource(InboxReadStopped(outcome, final_url="https://x.invalid/wall?q=1"))
    report = await poll_inbox(
        session_factory, user_id, source, settings=SETTINGS, clock=lambda: NOW
    )

    assert len(source.specs) == 1  # one read, no retry
    run = _run(session_factory, user_id, report.run_id)
    assert (run.status, run.stop_reason) == (SyncRunStatus.ABORTED, outcome.value)
    with session_scope(session_factory) as session:
        user = _user(session, user_id)
        account = ensure_account(session, user).id
        warm = heat_service.read(session, user, account, now=NOW, settings=SETTINGS.heat) > 0
        assert warm is heat
        assert (session_flag(session, user) is not None) is flag
    assert (report.heat_raised, report.session_flagged) == (heat, flag)


async def test_a_cancel_before_the_read_stops_it(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        run = runs.create_run(
            session, user, SyncRunKind.INBOX, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
        runs.request_cancel(session, user, run.id, now=NOW, browser_held=lambda _: True)
        run_id = run.id
    source = FakeInboxSource()
    await poll_inbox(
        session_factory, user_id, source, settings=SETTINGS, run_id=run_id, clock=lambda: NOW
    )
    assert source.specs == [] and _spent(session_factory, user_id) == 0
    assert _run(session_factory, user_id, run_id).stop_reason == "cancelled"


async def test_no_message_text_reaches_a_log_the_run_or_an_error(
    session_factory: sessionmaker[Session],
    user_id: int,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    thread = a_thread_with(ADA, NOW)
    read = delta(
        type(thread)(
            conversation_urn=thread.conversation_urn,
            counterpart_urn=ADA,
            last_activity_at=NOW,
            messages=tuple(
                type(m)(
                    message_urn=m.message_urn,
                    sender_urn=m.sender_urn,
                    outbound=m.outbound,
                    at=m.at,
                    text_snippet=SECRET,
                )
                for m in thread.messages
            ),
        )
    )
    ok = await poll_inbox(
        session_factory, user_id, FakeInboxSource(read), settings=SETTINGS, clock=lambda: NOW
    )

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError(f"a database error that quotes its parameters: {SECRET}")

    monkeypatch.setattr(inbox_apply, "add_interaction", broken)
    with pytest.raises(Exception) as failed:
        await poll_inbox(
            session_factory,
            user_id,
            FakeInboxSource(delta(a_thread_with(ADA, NOW, name="two"))),
            settings=SETTINGS,
            clock=lambda: NOW,
        )
    source_failed = FakeInboxSource(ValueError(f"the page said {SECRET}"))
    with pytest.raises(Exception) as unreadable:
        await poll_inbox(
            session_factory, user_id, source_failed, settings=SETTINGS, clock=lambda: NOW
        )

    assert SECRET not in caplog.text
    assert SECRET not in str(failed.value) and failed.value.__cause__ is None
    assert SECRET not in str(unreadable.value)
    with session_scope(session_factory) as session:
        for run in session.scalars(scoped(_user(session, user_id), SyncRun)):
            assert SECRET not in f"{run.counts_json} {run.progress_json} {run.error} {run.notes}"
            if run.id != ok.run_id:
                assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "error")
    assert _interactions(session_factory, user_id) == 2  # the failed apply rolled back


async def test_a_lost_browser_is_recorded_as_one(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    source = FakeInboxSource(BrowserUnavailable("the tab went away"))
    with pytest.raises(BrowserUnavailable):
        await poll_inbox(session_factory, user_id, source, settings=SETTINGS, clock=lambda: NOW)
    with session_scope(session_factory) as session:
        (run,) = session.scalars(scoped(_user(session, user_id), SyncRun)).all()
        assert run.stop_reason == "browser_unavailable"


# --- the worker ---------------------------------------------------------------------------


def _manual_inbox_run(factory: sessionmaker[Session], user_id: int) -> int:
    with session_scope(factory, write=True) as session:
        return runs.create_run(
            session,
            _user(session, user_id),
            SyncRunKind.INBOX,
            trigger=SyncRunTrigger.MANUAL,
            now=NOW,
        ).id


async def test_the_worker_routes_an_inbox_run_to_the_poll(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    source = FakeInboxSource(delta(a_thread_with(ADA, NOW)))
    provider, connector = fake_provider()

    def factory(run: BrowserRun, *, sleep: object) -> InboxSource:
        return source

    worker = BrowserWorker(
        provider,
        session_factory,
        SETTINGS,
        clock=lambda: NOW,
        sleep=no_sleep,
        inbox_sources=factory,
    )
    run_id = _manual_inbox_run(session_factory, user_id)
    assert await worker.execute(run_id, user_id) is runs.RunOutcome.DONE

    assert connector.attaches == 1 and len(source.specs) == 1
    assert _run(session_factory, user_id, run_id).stop_reason == READ


def test_the_worker_reads_an_inbox_run_through_the_page_source() -> None:
    """P4-01: the default factory is the page source, built without touching the page."""
    source = inbox_source(object(), sleep=no_sleep)  # type: ignore[arg-type]
    assert isinstance(source, PageInbox)
    assert source.threads_opened == 0


async def test_a_disarmed_scheduled_inbox_run_reaching_the_worker_attaches_nothing(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """The worker is the third gate: a forged scheduled run on a disarmed account."""
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        forged = SyncRun(
            user_id=user.id,
            linkedin_account_id=ensure_account(session, user).id,
            kind=SyncRunKind.INBOX,
            status=SyncRunStatus.RUNNING,
            trigger=SyncRunTrigger.SCHEDULED,
            started_at=NOW,
        )
        session.add(forged)
        session.flush()
        run_id = forged.id
    provider, connector = fake_provider()
    source = FakeInboxSource()
    worker = BrowserWorker(
        provider,
        session_factory,
        SETTINGS,
        clock=lambda: NOW,
        inbox_sources=lambda run, *, sleep: source,
    )
    await worker.execute(run_id, user_id)
    assert connector.attaches == 0 and source.specs == []
    assert _run(session_factory, user_id, run_id).stop_reason == "disarmed"


# --- the scheduler -------------------------------------------------------------------------


def test_the_inbox_kind_is_served() -> None:
    assert JobKind.INBOX in SERVED_SCHEDULES
    assert scheduler.DEFAULT_SCHEDULES[JobKind.INBOX].interval == timedelta(hours=3)
    assert scheduler.DEFAULT_SCHEDULES[JobKind.INBOX].respect_active_hours
    assert scheduler.JobOutcome.NOTHING_TO_WATCH in scheduler.SKIPPED_AFTER_GATE
    assert scheduler.JobOutcome.NOTHING_TO_WATCH.value == "nothing_to_watch"


def _job(user_id: int, account: int) -> scheduler.JobContext:
    return scheduler.JobContext(
        user_id=user_id, account_id=account, kind=JobKind.INBOX, due=NOW, catch_up=False
    )


@pytest.fixture
def settings() -> Settings:
    return Settings()


async def test_nothing_to_watch_records_nothing_and_attaches_nothing(
    bare_engine: Engine, settings: Settings
) -> None:
    provider, connector = fake_provider()
    clock = Clock(START)
    async with serving(
        bare_engine, settings, worker_extractor(provider, settings, clock=clock)
    ) as app:
        factory = app.state.session_factory
        with session_scope(factory, write=True) as session:
            user = _local(session)
            account = ensure_account(session, user).id
            arm_scheduled_runs(session, user, now=START)
            user_id = user.id
        registry = serve_registry(factory, app.state.executor, app.state.tasks, clock=clock)
        assert JobKind.INBOX in registry
        outcome = await registry[JobKind.INBOX](_job(user_id, account))
        await app.state.tasks.join()
    assert outcome is scheduler.JobOutcome.NOTHING_TO_WATCH
    assert connector.attaches == 0
    assert _rows(bare_engine) == []


async def test_nothing_to_watch_is_a_skipped_fire(session_factory: sessionmaker[Session]) -> None:
    """Through ``poll_and_fire``: the fire is skipped as ``nothing_to_watch``."""
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        account = ensure_account(session, user).id
        schedule = scheduler.DEFAULT_SCHEDULES[JobKind.INBOX]
        scheduler.establish_schedule(
            session,
            user,
            account,
            JobKind.INBOX,
            now=NOW - schedule.interval,
            schedule=schedule,
            rng=random.Random(0),
            tz=user.timezone,
            active_start=time(0, 0),
            active_end=time(23, 59),
        )

    async def nothing(ctx: scheduler.JobContext) -> scheduler.JobOutcome:
        return scheduler.JobOutcome.NOTHING_TO_WATCH

    result = await scheduler.poll_and_fire(
        session_factory,
        user,
        account,
        JobKind.INBOX,
        now=NOW + timedelta(minutes=1),
        schedule=schedule,
        registry={JobKind.INBOX: nothing},
        armed=scheduler.ARMING_NOT_REQUIRED,
        heat_settings=scheduler.HEAT_SKIP_DISABLED,
        tz=user.timezone,
        active_start=time(0, 0),
        active_end=time(23, 59),
    )
    assert result is not None
    assert (result.fired, result.skipped_reason) == (False, "nothing_to_watch")


async def test_a_disarmed_scheduled_poll_is_refused_at_all_three_gates(
    bare_engine: Engine, settings: Settings
) -> None:
    provider, connector = fake_provider()
    clock = Clock(START)
    async with serving(
        bare_engine, settings, worker_extractor(provider, settings, clock=clock)
    ) as app:
        factory = app.state.session_factory
        with session_scope(factory, write=True) as session:
            user = _local(session)
            account = ensure_account(session, user).id
            ada = factories.make_contact(session, user, li_urn=ADA)
            factories.make_enrollment(session, factories.make_campaign(session, user), ada)
            user_id = user.id

        # 1: the scheduler's arm gate, with the inbox kind scheduled by hand.
        schedule = scheduler.DEFAULT_SCHEDULES[JobKind.INBOX]
        with session_scope(factory, write=True) as session:
            user = _local(session)
            scheduler.establish_schedule(
                session,
                user,
                account,
                JobKind.INBOX,
                now=clock.at - schedule.interval,
                schedule=schedule,
                rng=random.Random(0),
                tz=user.timezone,
                active_start=time(0, 0),
                active_end=time(23, 59),
            )
        registry = serve_registry(factory, app.state.executor, app.state.tasks, clock=clock)
        result = await scheduler.poll_and_fire(
            factory,
            user,
            account,
            JobKind.INBOX,
            now=clock.at + timedelta(minutes=1),
            schedule=schedule,
            registry=registry,
            tz=user.timezone,
            active_start=time(0, 0),
            active_end=time(23, 59),
        )
        assert result is not None and (result.fired, result.skipped_reason) == (False, "disarmed")

        # 2: past the gate, create_run refuses it.
        # (No inbox poll has completed, so the first-poll gate answers before create_run.)
        outcome = await registry[JobKind.INBOX](_job(user_id, account))
        assert outcome is scheduler.JobOutcome.FIRST_POLL_BY_HAND
        await app.state.tasks.join()
        assert _rows(bare_engine) == []

        # 3: a forged scheduled run reaches the worker, which refuses to attach.
        with session_scope(factory, write=True) as session:
            forged = SyncRun(
                user_id=user_id,
                linkedin_account_id=account,
                kind=SyncRunKind.INBOX,
                status=SyncRunStatus.RUNNING,
                trigger=SyncRunTrigger.SCHEDULED,
                started_at=clock.at,
            )
            session.add(forged)
            session.flush()
            forged_id = forged.id
        await app.state.executor.execute(forged_id, user_id)
    assert connector.attaches == 0
    ((row),) = _rows(bare_engine)
    assert (row.kind, row.status, row.stop_reason) == (
        SyncRunKind.INBOX,
        SyncRunStatus.FAILED,
        "disarmed",
    )


async def test_serve_seeds_the_inbox_poll_and_a_fire_runs_it(
    bare_engine: Engine, settings: Settings
) -> None:
    provider, _ = fake_provider()
    clock = Clock(START)
    async with serving(
        bare_engine, settings, worker_extractor(provider, settings, clock=clock)
    ) as app:
        factory = app.state.session_factory
        with session_scope(factory, write=True) as session:
            user = _local(session)
            account = ensure_account(session, user).id
            arm_scheduled_runs(session, user, now=START)
            ada = factories.make_contact(session, user, li_urn=ADA)
            factories.make_enrollment(session, factories.make_campaign(session, user), ada)
        for _ in range(12):
            clock.at += timedelta(hours=1)
            await heartbeat(app)
            await app.state.tasks.join()
        with session_scope(factory) as session:
            user = _local(session)
            assert scheduler.stored_due(session, user, account, JobKind.INBOX) is not None
    # No inbox poll has ever completed, so every scheduled fire is skipped: the first poll
    # is a person's, by hand (P4-01). Nothing attached, nothing recorded.
    assert [run for run in _rows(bare_engine) if run.kind is SyncRunKind.INBOX] == []


def _complete_a_manual_poll(factory: sessionmaker[Session], user_id: int, at: datetime) -> None:
    with session_scope(factory, write=True) as session:
        user = _user(session, user_id)
        run = runs.create_run(
            session, user, SyncRunKind.INBOX, trigger=SyncRunTrigger.MANUAL, now=at
        )
        runs.finish_run(
            session, user, run.id, status=SyncRunStatus.COMPLETED, now=at, stop_reason=READ
        )


async def test_once_a_poll_has_completed_a_scheduled_fire_runs(
    bare_engine: Engine, settings: Settings
) -> None:
    provider, connector = fake_provider()
    clock = Clock(START)
    async with serving(
        bare_engine, settings, worker_extractor(provider, settings, clock=clock)
    ) as app:
        factory = app.state.session_factory
        with session_scope(factory, write=True) as session:
            user = _local(session)
            account = ensure_account(session, user).id
            arm_scheduled_runs(session, user, now=START)
            ada = factories.make_contact(session, user, li_urn=ADA)
            factories.make_enrollment(session, factories.make_campaign(session, user), ada)
            user_id = user.id
        registry = serve_registry(factory, app.state.executor, app.state.tasks, clock=clock)
        blocked = await registry[JobKind.INBOX](_job(user_id, account))
        assert blocked is scheduler.JobOutcome.FIRST_POLL_BY_HAND
        _complete_a_manual_poll(factory, user_id, START)
        assert await registry[JobKind.INBOX](_job(user_id, account)) is None
        await app.state.tasks.join()
    scheduled = [
        r
        for r in _rows(bare_engine)
        if r.kind is SyncRunKind.INBOX and r.trigger is SyncRunTrigger.SCHEDULED
    ]
    # The fake Chrome's page loads no conversation list: an unknown shape, aborted.
    assert [(r.status, r.stop_reason) for r in scheduled] == [
        (SyncRunStatus.ABORTED, "route_changed")
    ]
    assert connector.attaches == 1


async def test_the_worker_refuses_a_scheduled_inbox_run_before_the_first_poll_completed(
    bare_engine: Engine, settings: Settings
) -> None:
    provider, connector = fake_provider()
    clock = Clock(START)
    async with serving(
        bare_engine, settings, worker_extractor(provider, settings, clock=clock)
    ) as app:
        factory = app.state.session_factory
        with session_scope(factory, write=True) as session:
            user = _local(session)
            account = ensure_account(session, user).id
            arm_scheduled_runs(session, user, now=START)
            forged = SyncRun(
                user_id=user.id,
                linkedin_account_id=account,
                kind=SyncRunKind.INBOX,
                status=SyncRunStatus.RUNNING,
                trigger=SyncRunTrigger.SCHEDULED,
                started_at=clock.at,
            )
            session.add(forged)
            session.flush()
            forged_id, user_id = forged.id, user.id
        await app.state.executor.execute(forged_id, user_id)
    assert connector.attaches == 0
    ((row),) = _rows(bare_engine)
    assert (row.status, row.stop_reason) == (SyncRunStatus.FAILED, "first_inbox_poll")


# --- by hand: the CLI and the API ---------------------------------------------------------


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A migrated database with the local user, under tmp_path only."""
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session, settings=Settings())
    yield factory
    engine.dispose()


@pytest.mark.usefixtures("inside_active_hours")
def test_the_cli_polls_by_hand(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeInboxSource()
    provider, _ = fake_provider()
    monkeypatch.setattr(cli_module, "_provider", lambda settings: provider)
    monkeypatch.setattr(
        cli_module,
        "BrowserWorker",
        lambda *args, **kwargs: BrowserWorker(
            *args, **kwargs, sleep=no_sleep, inbox_sources=lambda run, *, sleep: source
        ),
    )
    result = CliRunner().invoke(cli, ["linkedin", "inbox"])

    assert result.exit_code == 0, result.output
    assert "(inbox) started" in result.output
    assert "read the inbox back to the last complete poll" in result.output
    assert len(source.specs) == 1


@pytest.mark.usefixtures("inside_active_hours", "no_browser_for_cli_runs")
def test_the_cli_refuses_a_flagged_session_before_the_browser(
    cli_db: sessionmaker[Session],
) -> None:
    with session_scope(cli_db, write=True) as session:
        user = session.scalars(select(User)).one()  # the local user
        flag_session(session, user, Outcome.LOGGED_OUT, url="https://x.invalid/login")
    result = CliRunner().invoke(cli, ["linkedin", "inbox"])
    assert result.exit_code == 1 and "flagged" in result.output


@pytest.mark.usefixtures("inside_active_hours")
async def test_the_api_starts_an_inbox_poll_and_answers_at_once(
    bare_engine: Engine, settings: Settings
) -> None:
    provider, _ = fake_provider()
    clock = Clock(START)
    async with (
        serving(bare_engine, settings, worker_extractor(provider, settings, clock=clock)) as app,
        client_for(app) as client,
    ):
        response = await client.post(
            "/api/v1/linkedin/runs", json={"kind": "inbox"}, headers=HEADERS
        )
        assert response.status_code == 202, response.text
        await app.state.tasks.join()
        run = (await client.get(f"/api/v1/linkedin/runs/{response.json()['run_id']}")).json()
    assert run["kind"] == "inbox"
    # The fake Chrome's page loads no conversation list: an unknown shape stops the poll
    # as aborted, never completed.
    assert (run["status"], run["stop_reason"]) == ("aborted", "route_changed")


async def test_the_first_poll_reads_back_to_the_earliest_live_outreach(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """Before any poll completes, ``since`` is the earliest sent outbound message of a
    live enrollment, so a first poll has an end it can prove (#388 review, S3)."""
    first_sent = NOW - timedelta(days=9)
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        campaign = factories.make_campaign(session, user)
        for days in (9, 4):
            contact = factories.make_contact(session, user, li_urn=profile_urn(f"d{days}"))
            enrollment = factories.make_enrollment(session, campaign, contact)
            factories.make_message(session, enrollment, sent_at=NOW - timedelta(days=days))
        ended = factories.make_enrollment(
            session,
            campaign,
            factories.make_contact(session, user, li_urn=profile_urn("ended")),
            status=EnrollmentStatus.COMPLETED,
        )
        # Completed, its latest send past WATCH_AFTER_COMPLETED: no longer watched.
        factories.make_message(session, ended, sent_at=NOW - timedelta(days=31))
    source = FakeInboxSource()
    report = await poll_inbox(
        session_factory, user_id, source, settings=SETTINGS, clock=lambda: NOW
    )

    assert source.specs[0].since == first_sent
    assert _run(session_factory, user_id, report.run_id).stop_reason == READ
    later = FakeInboxSource()
    await poll_inbox(
        session_factory, user_id, later, settings=SETTINGS, clock=lambda: NOW + timedelta(hours=3)
    )
    assert later.specs[0].since == NOW  # from then on, the last complete poll
    assert later.specs[0].max_conversations == MAX_CONVERSATIONS_PER_POLL == 40


async def test_a_first_poll_with_nothing_to_read_back_to_can_complete(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """With no outreach sent, ``since`` is ``None`` and a source that read
    ``max_conversations`` reports complete; the run completes and moves ``since`` on."""
    full = FakeInboxSource(delta(complete=True))
    report = await poll_inbox(session_factory, user_id, full, settings=SETTINGS, clock=lambda: NOW)
    assert full.specs[0].since is None
    assert _run(session_factory, user_id, report.run_id).status is SyncRunStatus.COMPLETED


def _outreach(factory: sessionmaker[Session], user_id: int, sent_at: datetime) -> None:
    with session_scope(factory, write=True) as session:
        user = _user(session, user_id)
        contact = factories.make_contact(session, user, li_urn=profile_urn("outreach"))
        enrollment = factories.make_enrollment(
            session, factories.make_campaign(session, user), contact
        )
        factories.make_message(session, enrollment, sent_at=sent_at)


def _posture(factory: sessionmaker[Session], user_id: int) -> PostureReport:
    with session_scope(factory, write=True) as session:
        user = _user(session, user_id)
        return posture(
            session, user, ensure_account(session, user).id, now=NOW, settings=Settings()
        )


def _posture_warnings(factory: sessionmaker[Session], user_id: int) -> tuple[str, ...]:
    """The ``linkedin reply poll`` row's warnings (P4-01: the row is always there, and on)."""
    (row,) = [p for p in _posture(factory, user_id).protections if p.name == "linkedin reply poll"]
    assert row.status is Status.ON
    return row.warnings


def test_the_linkedin_reply_poll_row_is_on_and_warns_only_for_a_short_first_poll(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    report = _posture(session_factory, user_id)
    (row,) = [p for p in report.protections if p.name == "linkedin reply poll"]
    assert row.status is Status.ON and row.warnings == ()
    assert row.value.startswith("no complete poll yet; scheduled polls wait") and row.notes == ()
    with session_scope(session_factory, write=True) as session:
        inbox_apply.record_short_first_poll(session, _user(session, user_id), NOW)
    report = _posture(session_factory, user_id)
    (row,) = [p for p in report.protections if p.name == "linkedin reply poll"]
    assert row.status is Status.ON and len(row.warnings) == 1
    assert not report.ok


def test_a_late_poll_names_how_the_newest_one_ended(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    _complete_a_manual_poll(session_factory, user_id, NOW - timedelta(hours=10))
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        run = runs.create_run(
            session,
            user,
            SyncRunKind.INBOX,
            trigger=SyncRunTrigger.MANUAL,
            now=NOW - timedelta(hours=1),
        )
        runs.finish_run(
            session,
            user,
            run.id,
            status=SyncRunStatus.ABORTED,
            now=NOW - timedelta(hours=1),
            stop_reason="route_changed",
        )
    (row,) = [
        p for p in _posture(session_factory, user_id).protections if p.name == "linkedin reply poll"
    ]
    (note,) = row.notes
    assert "the newest poll ended route_changed" in note and "older than 6.8 hours" in note


def test_old_runs_keep_the_words_for_no_source() -> None:
    assert runs.describe_stop_reason("no_source") != "no_source"


async def test_a_first_poll_that_cannot_reach_a_far_since_completes_and_warns(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """A campaign that started 60 days ago, with more conversations since than a first
    poll may read: it counts as complete, says so on the run, and posture warns."""
    started = NOW - timedelta(days=60)
    _outreach(session_factory, user_id, started)
    short = FakeInboxSource(delta(a_thread_with(ADA, NOW), complete=False))
    report = await poll_inbox(session_factory, user_id, short, settings=SETTINGS, clock=lambda: NOW)

    assert short.specs[0].since == started
    assert short.specs[0].max_conversations == BOOTSTRAP_MAX_CONVERSATIONS
    run = _run(session_factory, user_id, report.run_id)
    assert (run.status, run.stop_reason) == (SyncRunStatus.COMPLETED, FIRST_SHORT)
    assert f"could not read back to {started:%Y-%m-%d}" in (run.notes or "")
    assert run.counts_json is not None and set(run.counts_json) == {
        "conversations_read",
        "matched",
        "ignored_unknown",
        "skipped_group",
        "skipped_other",
        "messages_new",
    }
    (warning,) = _posture_warnings(session_factory, user_id)
    assert warning.startswith(
        f"the first LinkedIn inbox poll couldn't read back to {started:%Y-%m-%d};"
        " check older LinkedIn replies by hand"
    )

    # Later polls move on from it, and only a person clears the warning.
    later = FakeInboxSource()
    await poll_inbox(
        session_factory, user_id, later, settings=SETTINGS, clock=lambda: NOW + timedelta(hours=3)
    )
    assert later.specs[0].since == NOW
    assert len(_posture_warnings(session_factory, user_id)) == 1

    # Acknowledged by hand, it goes.
    with session_scope(session_factory, write=True) as session:
        assert inbox_apply.clear_short_first_poll(session, _user(session, user_id))
    assert _posture_warnings(session_factory, user_id) == ()


async def test_a_first_poll_that_reaches_its_since_completes_without_a_warning(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    _outreach(session_factory, user_id, NOW - timedelta(days=60))
    report = await poll_inbox(
        session_factory, user_id, FakeInboxSource(), settings=SETTINGS, clock=lambda: NOW
    )
    assert _run(session_factory, user_id, report.run_id).stop_reason == READ
    assert _posture_warnings(session_factory, user_id) == ()


def test_the_cli_forgets_the_recorded_owner_after_confirmation(
    cli_db: sessionmaker[Session],
) -> None:
    runner = CliRunner()
    assert (
        "nothing to forget"
        in runner.invoke(cli, ["linkedin", "inbox-forget-owner", "--yes"]).output
    )
    with session_scope(cli_db, write=True) as session:
        set_setting(session, session.scalars(select(User)).one(), inbox_apply.OWNER_KEY, "urn:x")
    declined = runner.invoke(cli, ["linkedin", "inbox-forget-owner"], input="n\n")
    assert "cancelled" in declined.output
    with session_scope(cli_db) as session:
        assert get_setting(session, session.scalars(select(User)).one(), inbox_apply.OWNER_KEY)
    done = runner.invoke(cli, ["linkedin", "inbox-forget-owner"], input="y\n")
    assert done.exit_code == 0 and "forgotten" in done.output
    with session_scope(cli_db) as session:
        assert (
            get_setting(session, session.scalars(select(User)).one(), inbox_apply.OWNER_KEY) is None
        )


def test_a_skipped_first_poll_fire_is_a_skip_and_the_owner_stop_names_its_cure() -> None:
    assert scheduler.JobOutcome.FIRST_POLL_BY_HAND in scheduler.SKIPPED_AFTER_GATE
    assert scheduler.JobOutcome.FIRST_POLL_BY_HAND.value == "first_inbox_poll"
    assert "inbox-forget-owner" in (runs.describe_stop_reason("owner_mismatch") or "")


def test_the_cli_acknowledges_a_short_first_poll(cli_db: sessionmaker[Session]) -> None:
    runner = CliRunner()
    assert "nothing to acknowledge" in runner.invoke(cli, ["linkedin", "inbox-acknowledge"]).output
    with session_scope(cli_db, write=True) as session:
        user = session.scalars(select(User)).one()
        inbox_apply.record_short_first_poll(session, user, NOW)
    result = runner.invoke(cli, ["linkedin", "inbox-acknowledge"])
    assert result.exit_code == 0 and "cleared" in result.output
    with session_scope(cli_db) as session:
        assert inbox_apply.short_first_poll(session, session.scalars(select(User)).one()) is None


# --- #437: the inbox breaker at the runner ------------------------------------------------


def _inbox_breaker(factory: sessionmaker[Session], user_id: int) -> route_breaker.BreakerState:
    with session_scope(factory, write=True) as session:
        user = _user(session, user_id)
        return route_breaker.inbox_state(session, user, ensure_account(session, user).id)


async def _poll(
    factory: sessionmaker[Session], user_id: int, answer: object, *, at: datetime = NOW
) -> str:
    source = FakeInboxSource(answer)  # type: ignore[arg-type]
    report = await poll_inbox(factory, user_id, source, settings=SETTINGS, clock=lambda: at)
    return report.stop_reason


OWNER_A = "urn:li:fsd_profile:INVENTEDA"
OWNER_B = "urn:li:fsd_profile:INVENTEDB"
_CHANGED = InboxReadStopped(Outcome.ROUTE_CHANGED, final_url="https://x.invalid/wall")


async def test_route_changed_polls_trip_the_inbox_breaker_at_two(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    assert await _poll(session_factory, user_id, _CHANGED) == "route_changed"
    state = _inbox_breaker(session_factory, user_id)
    assert (state.count, state.tripped) == (1, False)
    assert await _poll(session_factory, user_id, _CHANGED, at=NOW + timedelta(hours=3)) == (
        "route_changed"
    )
    state = _inbox_breaker(session_factory, user_id)
    assert (state.count, state.tripped, state.since) == (2, True, NOW)


async def test_a_completed_poll_resets_the_inbox_streak_and_releases_a_trip(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    await _poll(session_factory, user_id, _CHANGED)
    assert await _poll(session_factory, user_id, delta()) == READ
    assert _inbox_breaker(session_factory, user_id).count == 0
    # Only consecutive polls count: one more is a streak of 1.
    await _poll(session_factory, user_id, _CHANGED)
    assert _inbox_breaker(session_factory, user_id).count == 1
    await _poll(session_factory, user_id, _CHANGED)
    assert _inbox_breaker(session_factory, user_id).tripped
    # A manual poll that completes releases it.
    assert await _poll(session_factory, user_id, delta()) == READ
    state = _inbox_breaker(session_factory, user_id)
    assert (state.count, state.tripped) == (0, False)


async def test_a_first_short_poll_counts_as_completed_for_the_breaker(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    await _poll(session_factory, user_id, _CHANGED)
    assert _inbox_breaker(session_factory, user_id).count == 1
    _outreach(session_factory, user_id, NOW - timedelta(days=60))
    # A first poll that reaches its cap without its since ends completed, inbox_first_short.
    short = delta(a_thread_with(ADA, NOW), complete=False)
    assert await _poll(session_factory, user_id, short) == FIRST_SHORT
    assert _inbox_breaker(session_factory, user_id).count == 0


async def test_other_stops_leave_the_inbox_streak_where_it_was(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    # A completed poll records the mailbox owner; a later page showing another one ends
    # owner_mismatch, which has its own fix (inbox-forget-owner) and counts for nothing.
    assert await _poll(session_factory, user_id, replace(delta(), owner_urn=OWNER_A)) == READ
    await _poll(session_factory, user_id, _CHANGED)
    assert _inbox_breaker(session_factory, user_id).count == 1
    mismatch = await _poll(session_factory, user_id, replace(delta(), owner_urn=OWNER_B))
    assert mismatch == "owner_mismatch"
    assert _inbox_breaker(session_factory, user_id).count == 1
    # An incomplete read neither extends nor clears it.
    assert await _poll(session_factory, user_id, delta(complete=False)) == INCOMPLETE
    assert _inbox_breaker(session_factory, user_id).count == 1
    # Nor does a throttle (which raises heat) or a log-out (which sets the session flag).
    throttled = InboxReadStopped(Outcome.THROTTLED, final_url="https://x.invalid/t")
    assert await _poll(session_factory, user_id, throttled) == "throttled"
    assert _inbox_breaker(session_factory, user_id).count == 1


async def test_a_log_out_leaves_the_inbox_streak_where_it_was(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    await _poll(session_factory, user_id, _CHANGED)
    out = InboxReadStopped(Outcome.LOGGED_OUT, final_url="https://x.invalid/l")
    assert await _poll(session_factory, user_id, out) == "logged_out"
    assert _inbox_breaker(session_factory, user_id).count == 1


async def test_a_session_flag_refusal_leaves_the_inbox_streak_alone(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    await _poll(session_factory, user_id, _CHANGED)
    with session_scope(session_factory, write=True) as session:
        flag_session(session, _user(session, user_id), Outcome.CHECKPOINT, url="https://x.invalid")
    with pytest.raises(SessionFlagged):
        await _poll(session_factory, user_id, delta())
    assert _inbox_breaker(session_factory, user_id).count == 1


async def test_inbox_polls_never_touch_the_connections_streaks(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    await _poll(session_factory, user_id, _CHANGED)
    await _poll(session_factory, user_id, _CHANGED)
    with session_scope(session_factory) as session:
        user = _user(session, user_id)
        account = ensure_account(session, user).id
        assert route_breaker.state(session, user, account).count == 0
        assert not route_breaker.answer_lost_tripped(session, user, account)
        assert not route_breaker.contact_info_tripped(session, user, account)


async def test_a_poll_by_another_user_never_moves_this_users_streak(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    with session_scope(session_factory, write=True) as session:
        other = factories.make_user(session)
        other_id = other.id
        ensure_account(session, other)
    await _poll(session_factory, other_id, _CHANGED)
    await _poll(session_factory, other_id, _CHANGED)
    assert _inbox_breaker(session_factory, other_id).tripped
    assert _inbox_breaker(session_factory, user_id).count == 0
    await _poll(session_factory, user_id, delta())
    assert _inbox_breaker(session_factory, other_id).tripped


def _inbox_breaker_warning(factory: sessionmaker[Session], user_id: int) -> str:
    (row,) = [p for p in _posture(factory, user_id).protections if p.name == "Inbox breaker"]
    (warning,) = row.warnings
    return warning


def _trip_inbox(factory: sessionmaker[Session], user_id: int) -> None:
    with session_scope(factory, write=True) as session:
        user = _user(session, user_id)
        for _ in range(route_breaker.INBOX_THRESHOLD):
            route_breaker.record_inbox(
                session,
                user,
                ensure_account(session, user).id,
                route_changed=True,
                completed=False,
                now=NOW,
            )


def test_the_posture_warning_names_the_hold_only_while_it_applies(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    # A running campaign with a LinkedIn step puts LinkedIn in use.
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        ben = factories.make_contact(session, user, li_urn=profile_urn("ben"))
        campaign = factories.make_campaign(session, user, channels=(TemplateChannel.LINKEDIN,))
        factories.make_enrollment(session, campaign, ben)
    # A tripped breaker after a poll that completed an hour ago: the inbox is not stale.
    _complete_a_manual_poll(session_factory, user_id, NOW - timedelta(hours=1))
    _trip_inbox(session_factory, user_id)
    fresh = _inbox_breaker_warning(session_factory, user_id)
    assert "netkeeper linkedin inbox" in fresh and "Scheduled inbox polls are skipped" in fresh
    assert "held until a poll completes" not in fresh

    # Ten hours on, the inbox is stale and the hold applies: the warning links to it.
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        for run in session.scalars(scoped(user, SyncRun)).all():
            run.started_at = NOW - timedelta(hours=10)
            run.completed_at = NOW - timedelta(hours=10)
    stale = _inbox_breaker_warning(session_factory, user_id)
    assert "held until a poll completes" in stale and "inbox hold" in stale


def test_the_posture_warning_omits_the_hold_when_linkedin_is_not_in_use(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)  # no campaign: nothing waits on the inbox
        other_id = user.id
    _trip_inbox(session_factory, other_id)
    assert "held until a poll completes" not in _inbox_breaker_warning(session_factory, other_id)


def test_the_stop_reason_has_words() -> None:
    text = runs.describe_stop_reason("inbox_route_changed_breaker")
    assert text == "refused: the inbox breaker is tripped"


async def test_a_checkpoint_stop_leaves_the_inbox_streak_alone(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    await _poll(session_factory, user_id, _CHANGED)
    wall = InboxReadStopped(Outcome.CHECKPOINT, final_url="https://x.invalid/c")
    assert await _poll(session_factory, user_id, wall) == "checkpoint"
    assert _inbox_breaker(session_factory, user_id).count == 1


async def test_a_not_found_page_counts_toward_the_inbox_streak(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    gone = InboxReadStopped(Outcome.NOT_FOUND, final_url="https://x.invalid/gone")
    assert await _poll(session_factory, user_id, gone) == "not_found"
    assert _inbox_breaker(session_factory, user_id).count == 1
    assert await _poll(session_factory, user_id, gone, at=NOW + timedelta(hours=3)) == "not_found"
    assert _inbox_breaker(session_factory, user_id).tripped


async def test_an_observation_failure_counts_and_still_fails_the_run(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    for hours in (0, 3):
        source = FakeInboxSource(ObservationFailed("a response was dropped"))
        with pytest.raises(ObservationFailed):
            await poll_inbox(
                session_factory,
                user_id,
                source,
                settings=SETTINGS,
                clock=lambda: NOW + timedelta(hours=hours),  # noqa: B023
            )
        expected = 1 if hours == 0 else 2
        assert _inbox_breaker(session_factory, user_id).count == expected
    assert _inbox_breaker(session_factory, user_id).tripped
    with session_scope(session_factory) as session:
        runs_ = session.scalars(scoped(_user(session, user_id), SyncRun)).all()
        assert [(r.status, r.stop_reason) for r in runs_] == [(SyncRunStatus.FAILED, "error")] * 2


async def test_a_lost_browser_leaves_the_inbox_streak_alone(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    await _poll(session_factory, user_id, _CHANGED)
    with pytest.raises(BrowserUnavailable):
        await _poll(session_factory, user_id, BrowserUnavailable("the tab went away"))
    assert _inbox_breaker(session_factory, user_id).count == 1
