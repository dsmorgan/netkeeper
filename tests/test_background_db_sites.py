"""#266: a regression test per background database site #265 moved off the event loop.

#265 moved every session the browser worker, both runners, the scheduler, and the
served job handlers open onto :func:`netkeeper.db.off_loop`'s thread. Its review put
each one back on the loop, one at a time, and ten of those mutations survived the
suite. Each test here runs one background path under
:func:`db_loop_guard.request_holding_the_write_lock` -- a request holding the write
lock, a short busy timeout, and a record of every statement the loop's own thread
ran -- and asserts the path ran none, and recorded what it should.

Also here: the two gaps #266 found that predate #265 (a refused connections sync is
recorded ``failed``; the worker's own ``interrupted`` ending on a cancel during
attach), and the shutdown race (a write in flight that fails after a cancel is
recorded ``failed``, not ``interrupted``).
"""

from __future__ import annotations

import asyncio
import random
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import factories
import pytest
from db_loop_guard import BUSY_TIMEOUT_MS, OnTheLoop, request_holding_the_write_lock
from inbox_fakes import FakeInboxSource, a_thread_with, profile_urn
from inbox_fakes import delta as inbox_delta
from profile_fakes import FakeBrowser
from run_fakes import Clock, ConnectionsContext, fake_provider, fast_profiles, no_sleep
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from test_connections_sync import _many, _sync
from test_enrichment import SEED, Sleeps, _people, _setup
from test_runs_serve import START, heartbeat, served
from voyager_pages import FakeConnectionsSource

from netkeeper.config import Settings
from netkeeper.db import CancelledWhileFailing, off_loop, session_scope
from netkeeper.linkedin.browser import BrowserUnavailable
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.inbox import InboxReadStopped
from netkeeper.models import SyncRun, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.scoping import scoped
from netkeeper.services import enrich_plan, runs, scheduler
from netkeeper.services import heat as heat_service
from netkeeper.services.connections_sync import SessionFlagged
from netkeeper.services.enrichment import enrich_contacts
from netkeeper.services.events import EventBus
from netkeeper.services.inbox_poll import poll_inbox
from netkeeper.services.linkedin_accounts import arm_scheduled_runs, ensure_account
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.scheduled_runs import serve_registry
from netkeeper.services.tasks import TaskRunner
from netkeeper.worker import BrowserWorker

#: Wednesday 14:00 in New York: inside the default active window.
INSIDE = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
SETTINGS = Settings()
CHECKPOINT_URL = "https://example.invalid/checkpoint/challenge"


@pytest.fixture(autouse=True)
def _short_busy_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set before any engine here connects, so a write stuck on the loop fails fast."""
    monkeypatch.setattr("netkeeper.db.SQLITE_BUSY_TIMEOUT_MS", BUSY_TIMEOUT_MS)


@asynccontextmanager
async def watched(engine: Engine) -> AsyncIterator[OnTheLoop]:
    """The harness, asserting on the way out that nothing ran on the loop."""
    async with request_holding_the_write_lock(engine) as on_the_loop:
        yield on_the_loop
    assert on_the_loop.statements == [], "\n".join(on_the_loop.statements)


def _user(factory: sessionmaker[Session], user_id: int) -> User:
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        session.expunge(user)
        return user


def _run(factory: sessionmaker[Session], run_id: int, user_id: int) -> SyncRun:
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        run = runs.get_run(session, user, run_id)
        session.expunge(run)
        return run


def _new_run(
    factory: sessionmaker[Session],
    kind: SyncRunKind = SyncRunKind.CONNECTIONS_INCREMENTAL,
    *,
    flagged: bool = False,
) -> tuple[int, int]:
    with session_scope(factory, write=True) as session:
        user = factories.make_user(session)
        run = runs.create_run(session, user, kind, trigger=SyncRunTrigger.MANUAL, now=INSIDE)
        if flagged:
            flag_session(session, user, Outcome.CHECKPOINT, url=CHECKPOINT_URL)
        return run.id, user.id


def _worker(provider: Any, factory: sessionmaker[Session]) -> BrowserWorker:
    return BrowserWorker(
        provider,
        factory,
        SETTINGS.linkedin,
        clock=Clock(INSIDE),
        sleep=no_sleep,
        profiles=fast_profiles,
    )


# --- the worker: _facts, _refusal, _status, and every _finish -------------------------


async def test_worker_a_whole_run_facts_refusal_and_status_stay_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    run_id, user_id = _new_run(session_factory)
    provider, connector = fake_provider(ConnectionsContext())

    async with watched(engine):
        outcome = await _worker(provider, session_factory).execute(run_id, user_id)

    assert outcome is runs.RunOutcome.DONE and connector.attaches == 1
    assert _run(session_factory, run_id, user_id).status is SyncRunStatus.COMPLETED


async def test_worker_finish_after_a_refusal_stays_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    run_id, user_id = _new_run(session_factory, flagged=True)
    provider, connector = fake_provider()

    async with watched(engine):
        await _worker(provider, session_factory).execute(run_id, user_id)

    run = _run(session_factory, run_id, user_id)
    assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "session_flagged")
    assert connector.attaches == 0


async def test_worker_finish_when_the_browser_is_unavailable_stays_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    run_id, user_id = _new_run(session_factory)
    provider, _ = fake_provider(error=BrowserUnavailable("Chrome is not running"))

    async with watched(engine):
        outcome = await _worker(provider, session_factory).execute(run_id, user_id)

    run = _run(session_factory, run_id, user_id)
    assert outcome is runs.RunOutcome.RETRY_LATER
    assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "browser_unavailable")


class _Provider:
    """A provider whose attach hangs, or raises, so the worker's own endings run."""

    mode = "attach"

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.raises = raises
        self.entered = asyncio.Event()

    @asynccontextmanager
    async def run(self, key: str) -> AsyncIterator[Any]:
        self.entered.set()
        if self.raises is not None:
            raise self.raises
        await asyncio.Event().wait()
        yield None  # pragma: no cover - never reached


async def test_worker_records_its_own_interrupted_ending_off_the_loop_on_a_cancel_during_attach(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """M08 (on the loop) and M09 (dropped): a cancel while attaching is the worker's to
    record, since no runner has started."""
    run_id, user_id = _new_run(session_factory)
    provider = _Provider()
    task = asyncio.create_task(_worker(provider, session_factory).execute(run_id, user_id))
    await asyncio.wait_for(provider.entered.wait(), 5)

    async with watched(engine):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run = _run(session_factory, run_id, user_id)
    assert (run.status, run.stop_reason, run.error) == (
        SyncRunStatus.ABORTED,
        "interrupted",
        runs.INTERRUPTED,
    )


class _ProviderWhoseWriteFailsOnCancel:
    """A provider whose attach waits on a database write that fails after a cancel lands.

    Stands in for any cancel that reaches the worker, not the runner, while background
    database work is in flight: the worker's own handler has to record it.
    """

    mode = "attach"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def _write(self) -> None:
        self.started.set()
        self.release.wait(5)
        raise RuntimeError("disk I/O error")

    @asynccontextmanager
    async def run(self, key: str) -> AsyncIterator[Any]:
        await off_loop(self._write)
        yield None  # pragma: no cover - the write never succeeds


async def test_worker_records_a_cancel_that_carries_a_failed_write_as_failed(
    session_factory: sessionmaker[Session],
) -> None:
    """Review mutant M7: the worker's own ``CancelledWhileFailing`` branch."""
    run_id, user_id = _new_run(session_factory)
    provider = _ProviderWhoseWriteFailsOnCancel()
    task = asyncio.create_task(_worker(provider, session_factory).execute(run_id, user_id))
    assert await asyncio.to_thread(provider.started.wait, 5)
    task.cancel()
    await asyncio.sleep(0.02)
    provider.release.set()
    with pytest.raises(CancelledWhileFailing):
        await task

    run = _run(session_factory, run_id, user_id)
    assert (run.status, run.stop_reason, run.error) == (
        SyncRunStatus.FAILED,
        "error",
        "RuntimeError: disk I/O error",
    )


@pytest.mark.parametrize("carries_a_failed_write", [False, True])
async def test_a_failed_ending_write_on_a_cancel_never_replaces_the_cancel(
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    carries_a_failed_write: bool,
) -> None:
    """#294: the database just failed, so the worker's own ending write fails too. The
    task still ends cancelled (``TaskRunner`` reads ``task.cancelled()``), and the
    failed write is logged."""
    run_id, user_id = _new_run(session_factory)
    failing, hanging = _ProviderWhoseWriteFailsOnCancel(), _Provider()
    worker = _worker(failing if carries_a_failed_write else hanging, session_factory)

    def finish(*args: Any) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(worker, "_finish", finish)
    task = asyncio.create_task(worker.execute(run_id, user_id))
    if carries_a_failed_write:
        assert await asyncio.to_thread(failing.started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.02)
        failing.release.set()
    else:
        await asyncio.wait_for(hanging.entered.wait(), 5)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert f"could not record how run {run_id} ended" in caplog.text
    assert _run(session_factory, run_id, user_id).status is SyncRunStatus.RUNNING


async def test_worker_finish_after_an_error_before_the_runner_stays_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    run_id, user_id = _new_run(session_factory)
    provider = _Provider(raises=RuntimeError("the attach broke"))

    async with watched(engine):
        await _worker(provider, session_factory).execute(run_id, user_id)

    run = _run(session_factory, run_id, user_id)
    assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "error")
    assert run.error == "RuntimeError: the attach broke"


async def test_worker_finish_for_a_kind_with_no_runner_stays_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        run = SyncRun(
            user_id=user.id,
            linkedin_account_id=ensure_account(session, user).id,
            kind=SyncRunKind.MESSAGE_SEND,
            status=SyncRunStatus.RUNNING,
            trigger=SyncRunTrigger.MANUAL,
            started_at=INSIDE,
        )
        session.add(run)
        session.flush()
        run_id, user_id = run.id, user.id

    async with watched(engine):
        await _worker(fake_provider()[0], session_factory).execute(run_id, user_id)

    assert _run(session_factory, run_id, user_id).stop_reason == "no_runner"


# --- enrichment: _start, _plan, the gate, apply_harvest, record_progress, finish -------


async def _enrich(
    factory: sessionmaker[Session], user_id: int, browser: FakeBrowser, **kwargs: Any
) -> Any:
    sleep = Sleeps()
    return await enrich_contacts(
        factory,
        user_id,
        browser.source(sleep=sleep),
        settings=SETTINGS.linkedin,
        clock=Clock(INSIDE),
        sleep=sleep,
        rng=random.Random(SEED),
        **kwargs,
    )


async def test_enrichment_every_session_of_a_whole_run_stays_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    people = _people(3)
    user_id, _ = _setup(session_factory, people)

    async with watched(engine):
        report = await _enrich(session_factory, user_id, FakeBrowser.of(people))

    assert len(report.result.completed) == 3
    assert _run(session_factory, report.run_id, user_id).status is SyncRunStatus.COMPLETED


async def test_enrichment_writes_progress_before_it_reports_it(
    session_factory: sessionmaker[Session],
) -> None:
    """M30: a listener told of progress (the SSE stream) can read it back at once."""
    people = _people(3)
    user_id, _ = _setup(session_factory, people)
    seen: list[tuple[int, object]] = []

    async def on_progress(event: Any) -> None:
        def stored() -> object:
            with session_scope(session_factory) as session:
                user = session.get(User, user_id)
                assert user is not None
                run = session.scalars(scoped(user, SyncRun).order_by(SyncRun.id.desc())).first()
                assert run is not None and run.progress_json is not None
                return run.progress_json["visited"]

        seen.append((event.visited, await off_loop(stored)))

    await _enrich(session_factory, user_id, FakeBrowser.of(people), on_progress=on_progress)

    assert seen and all(visited == stored for visited, stored in seen)


@pytest.mark.parametrize("refusal", ["session_flagged", "heat_skip"])
async def test_recording_a_refused_enrichment_stays_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session], refusal: str
) -> None:
    """M22: ``runs.recording``'s refusal endings are written off the loop."""
    people = _people(2)
    user_id, _ = _setup(session_factory, people)
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        if refusal == "session_flagged":
            flag_session(session, user, Outcome.CHECKPOINT, url=CHECKPOINT_URL)
        else:
            account = ensure_account(session, user).id
            while not heat_service.should_skip(
                session, user, account, now=INSIDE, settings=SETTINGS.linkedin.heat
            ):
                heat_service.raise_heat(
                    session, user, account, now=INSIDE, settings=SETTINGS.linkedin.heat
                )

    async with watched(engine):
        with pytest.raises((runs.SessionFlagged, runs.HeatSkipped)):
            await _enrich(session_factory, user_id, FakeBrowser.of(people))

    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        run = runs.latest_run(session, user, SyncRunKind.ENRICH)
        assert run is not None
        assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, refusal)


async def test_recording_a_cancelled_enrichment_stays_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """M21: ``runs.recording``'s "interrupted" ending is written off the loop."""
    people = _people(4)
    user_id, _ = _setup(session_factory, people)
    browser = FakeBrowser.of(people)
    task: asyncio.Task[Any] | None = None

    def cancel_on_the_second_visit(kind: str, value: object) -> None:
        if kind == "goto" and len(browser.visited()) == 2 and task is not None:
            task.cancel()

    browser.on_event = cancel_on_the_second_visit
    async with watched(engine):
        task = asyncio.create_task(_enrich(session_factory, user_id, browser))
        with pytest.raises(asyncio.CancelledError):
            await task

    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        run = runs.latest_run(session, user, SyncRunKind.ENRICH)
        assert run is not None
        assert (run.status, run.stop_reason) == (SyncRunStatus.ABORTED, "interrupted")


async def test_recording_a_failed_enrichment_stays_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    people = _people(3)
    user_id, _ = _setup(session_factory, people)
    browser = FakeBrowser.of(people)

    def break_on_the_second_visit(kind: str, value: object) -> None:
        if kind == "goto" and len(browser.visited()) == 2:
            raise RuntimeError("the tab broke")

    browser.on_event = break_on_the_second_visit
    async with watched(engine):
        with pytest.raises(RuntimeError, match="the tab broke"):
            await _enrich(session_factory, user_id, browser)

    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        run = runs.latest_run(session, user, SyncRunKind.ENRICH)
        assert run is not None
        assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "error")


# --- the connections sync: page apply and its three post-run transactions ---------------


@pytest.fixture
def user_id(session_factory: sessionmaker[Session]) -> int:
    with session_scope(session_factory, write=True) as session:
        return factories.make_user(session).id


async def test_sync_page_apply_and_the_post_run_transactions_stay_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session], user_id: int
) -> None:
    """The page apply (M18), then record_response, record_breakers, and finish."""
    async with watched(engine):
        report = await _sync(session_factory, user_id, FakeConnectionsSource(_many(85)))

    assert report.result.complete and report.pages.seen == 85


async def test_a_refused_connections_sync_is_recorded_failed_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session], user_id: int
) -> None:
    """M16, a gap that predates #265: a flagged (or hot) sync is refused *inside*
    ``runs.recording``, so its run is recorded ``failed``, not left ``running``."""
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        flag_session(session, user, Outcome.CHECKPOINT, url=CHECKPOINT_URL)

    async with watched(engine):
        with pytest.raises(SessionFlagged):
            await _sync(session_factory, user_id, FakeConnectionsSource(_many(3)))

    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        run = runs.latest_run(session, user, SyncRunKind.CONNECTIONS_FULL)
        assert run is not None
        assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "session_flagged")


# --- the scheduler: _due_now, _defer_past_the_gap, park_retry, offer_again --------------

ALL_DAY = (time(0, 0), time(0, 0))
ACCOUNT = 1


def _due(
    factory: sessionmaker[Session], schedules: dict[scheduler.JobKind, scheduler.JobSchedule]
) -> User:
    """A user with every kind in ``schedules`` due at ``INSIDE``, seconds apart."""
    with session_scope(factory, write=True) as session:
        owner = factories.make_user(session, timezone="UTC")
        for offset, (kind, schedule) in enumerate(schedules.items()):
            scheduler._store_state(
                session,
                owner,
                ACCOUNT,
                kind,
                scheduler._JobState(
                    due=INSIDE + timedelta(seconds=30 * offset),
                    fingerprint=scheduler.ScheduleFingerprint.of(
                        schedule, tz="UTC", active_start=ALL_DAY[0], active_end=ALL_DAY[1]
                    ),
                ),
            )
        session.expunge(owner)
        return owner


async def _poll(
    factory: sessionmaker[Session],
    owner: User,
    schedules: dict[scheduler.JobKind, scheduler.JobSchedule],
    outcome: scheduler.JobOutcome | None,
) -> list[scheduler.FireResult]:
    async def handler(ctx: scheduler.JobContext) -> scheduler.JobOutcome | None:
        return outcome

    return await scheduler.poll_once(
        factory,
        [(owner, ACCOUNT)],
        now=INSIDE + timedelta(minutes=30),
        registry=dict.fromkeys(schedules, handler),
        schedules=schedules,
        active_start=ALL_DAY[0],
        active_end=ALL_DAY[1],
        armed=scheduler.ARMING_NOT_REQUIRED,
        rng=random.Random(0),
    )


def _every(hours: int, *kinds: scheduler.JobKind) -> dict[scheduler.JobKind, scheduler.JobSchedule]:
    return {kind: scheduler.JobSchedule(kind, timedelta(hours=hours)) for kind in kinds}


async def test_scheduler_due_now_and_the_interleave_gap_stay_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """M24 (``_due_now``) and ``_defer_past_the_gap``: two kinds due in one poll."""
    schedules = _every(3, scheduler.JobKind.ENRICH, scheduler.JobKind.INBOX)
    owner = _due(session_factory, schedules)

    async with watched(engine):
        fired = await _poll(session_factory, owner, schedules, None)

    assert [result.kind for result in fired] == [scheduler.JobKind.ENRICH]
    with session_scope(session_factory) as session:
        deferred = scheduler.stored_due(session, owner, ACCOUNT, scheduler.JobKind.INBOX)
    assert deferred is not None and deferred > INSIDE + timedelta(minutes=30)


@pytest.mark.parametrize(
    "outcome", [scheduler.JobOutcome.RETRY_LATER, scheduler.JobOutcome.NOT_DONE]
)
async def test_scheduler_park_retry_and_offer_again_stay_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session], outcome: scheduler.JobOutcome
) -> None:
    """M28 (``park_retry``) and ``offer_again``: a fire that asks to come back sooner."""
    schedules = _every(7 * 24, scheduler.JobKind.CONNECTIONS_FULL)
    owner = _due(session_factory, schedules)

    async with watched(engine):
        fired = await _poll(session_factory, owner, schedules, outcome)

    assert fired and fired[0].fired
    assert fired[0].next_due is not None
    assert fired[0].next_due < INSIDE + timedelta(days=7)  # sooner than the cadence


async def test_the_heartbeat_reads_its_accounts_off_the_loop(
    bare_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """M25: the served heartbeat's account list, then its (disarmed) poll."""
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "no-dist"))
    provider, connector = fake_provider()
    async with (
        served(bare_engine, SETTINGS, provider, Clock(START)) as app,
        watched(bare_engine),
    ):
        await heartbeat(app)
    assert connector.attaches == 0


# --- the served job handlers: scheduled_runs.create_run and _lost_answers -------------


class _FinishesOffTheLoop:
    """An executor that ends its run as a full sync that lost one answer, off the loop."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory

    async def execute(self, run_id: int, user_id: int) -> runs.RunOutcome:
        def finish() -> None:
            with session_scope(self.factory, write=True) as session:
                user = session.get(User, user_id)
                assert user is not None
                runs.finish_run(
                    session,
                    user,
                    run_id,
                    status=SyncRunStatus.ABORTED,
                    now=INSIDE,
                    stop_reason="answer_lost",
                    counts={"lost": [{"start": 40, "cause": "Error (no data)"}]},
                )

        await off_loop(finish)
        return runs.RunOutcome.DONE


async def test_a_served_job_records_its_run_and_reads_its_losses_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """M26 (``create_run`` in the handler) and ``_lost_answers``."""
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session)
        account = ensure_account(session, owner).id
        arm_scheduled_runs(session, owner, now=INSIDE)
        owner_id = owner.id
    registry = serve_registry(
        session_factory,
        _FinishesOffTheLoop(session_factory),
        TaskRunner(EventBus()),
        clock=lambda: INSIDE,
    )
    context = scheduler.JobContext(
        user_id=owner_id,
        account_id=account,
        kind=scheduler.JobKind.CONNECTIONS_FULL,
        due=INSIDE,
        catch_up=False,
    )

    async with watched(engine):
        outcome = await registry[scheduler.JobKind.CONNECTIONS_FULL](context)

    assert outcome is scheduler.JobOutcome.NOT_DONE


# --- the inbox poll (P4-08): start, prepare, spend, the wall, apply and finish ------------


def _inbox_user(factory: sessionmaker[Session], *, armed: bool = False) -> tuple[int, int]:
    """A user with a contact in a live enrollment, so the poll has someone to watch."""
    with session_scope(factory, write=True) as session:
        owner = factories.make_user(session)
        account = ensure_account(session, owner).id
        contact = factories.make_contact(session, owner, li_urn=profile_urn("ada"))
        factories.make_enrollment(session, factories.make_campaign(session, owner), contact)
        if armed:
            arm_scheduled_runs(session, owner, now=INSIDE)
        return owner.id, account


async def test_an_inbox_poll_stays_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    user_id, _ = _inbox_user(session_factory)
    source = FakeInboxSource(inbox_delta(a_thread_with(profile_urn("ada"), INSIDE)))

    async with watched(engine):
        report = await poll_inbox(
            session_factory, user_id, source, settings=SETTINGS.linkedin, clock=Clock(INSIDE)
        )

    assert report.counts is not None and report.counts.messages_new == 2
    assert _run(session_factory, report.run_id, user_id).status is SyncRunStatus.COMPLETED


async def test_an_inbox_poll_stopped_by_a_wall_stays_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    user_id, _ = _inbox_user(session_factory)
    source = FakeInboxSource(InboxReadStopped(Outcome.CHECKPOINT, final_url=CHECKPOINT_URL))

    async with watched(engine):
        report = await poll_inbox(
            session_factory, user_id, source, settings=SETTINGS.linkedin, clock=Clock(INSIDE)
        )

    assert report.session_flagged and report.heat_raised
    assert _run(session_factory, report.run_id, user_id).stop_reason == "checkpoint"


async def test_the_inbox_handler_checks_who_to_watch_off_the_loop(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as session:
        owner = factories.make_user(session)
        account = ensure_account(session, owner).id
        owner_id = owner.id
    registry = serve_registry(
        session_factory,
        _FinishesOffTheLoop(session_factory),
        TaskRunner(EventBus()),
        clock=lambda: INSIDE,
    )
    context = scheduler.JobContext(
        user_id=owner_id,
        account_id=account,
        kind=scheduler.JobKind.INBOX,
        due=INSIDE,
        catch_up=False,
    )

    async with watched(engine):
        outcome = await registry[scheduler.JobKind.INBOX](context)

    assert outcome is scheduler.JobOutcome.NOTHING_TO_WATCH


# --- the shutdown race: a write that fails after a cancel is a failure ---------------------


async def test_off_loop_carries_a_failure_out_with_the_cancel() -> None:
    started, release = threading.Event(), threading.Event()

    def fails() -> None:
        started.set()
        release.wait(2)
        raise ValueError("the write failed")

    task = asyncio.create_task(off_loop(fails))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.02)
    release.set()
    with pytest.raises(CancelledWhileFailing) as cancelled:
        await task

    assert isinstance(cancelled.value, asyncio.CancelledError)
    assert isinstance(cancelled.value.error, ValueError)
    assert cancelled.value.__cause__ is cancelled.value.error
    assert task.cancelled()


async def test_a_write_that_fails_after_a_cancel_is_recorded_failed_not_interrupted(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#266 item 3: the harvest write in flight when the shutdown's cancel lands fails on
    its own. The run is ``failed``/"error" with that failure, and the cancel still
    propagates so the shutdown goes on."""
    people = _people(3)
    user_id, _ = _setup(session_factory, people)
    started, release = threading.Event(), threading.Event()
    real = enrich_plan.mark_completed
    calls = 0

    def fails_on_the_second(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        real(*args, **kwargs)
        if calls == 2:
            started.set()
            release.wait(5)
            raise RuntimeError("disk I/O error")

    monkeypatch.setattr(enrich_plan, "mark_completed", fails_on_the_second)
    task = asyncio.create_task(_enrich(session_factory, user_id, FakeBrowser.of(people)))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0.02)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        run = runs.latest_run(session, user, SyncRunKind.ENRICH)
        assert run is not None
        assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "error")
        assert run.error == "RuntimeError: disk I/O error"
        plan = enrich_plan.load_plan(session, user, run.id)
    assert len(plan.completed) == 1  # the failed write rolled back; the first kept


async def test_a_cancel_with_no_failure_is_still_interrupted(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side: a write in flight that commits leaves the cancel a clean interruption."""
    people = _people(3)
    user_id, _ = _setup(session_factory, people)
    started, release = threading.Event(), threading.Event()
    real = enrich_plan.mark_completed
    calls = 0

    def waits_on_the_second(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        real(*args, **kwargs)
        if calls == 2:
            started.set()
            release.wait(5)

    monkeypatch.setattr(enrich_plan, "mark_completed", waits_on_the_second)
    task = asyncio.create_task(_enrich(session_factory, user_id, FakeBrowser.of(people)))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0.02)
    release.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task

    assert not isinstance(cancelled.value, CancelledWhileFailing)
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        run = runs.latest_run(session, user, SyncRunKind.ENRICH)
        assert run is not None
        assert (run.status, run.stop_reason) == (SyncRunStatus.ABORTED, "interrupted")
