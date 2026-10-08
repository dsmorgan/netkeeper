"""#259: background database work under ``serve`` never blocks the event loop.

A POST's write transaction opens in a worker thread (the sync ``current_user``
dependency's first statement is its ``BEGIN IMMEDIATE``) and commits in the
dependency's teardown, which needs the event loop to get there. Background
work -- the browser worker, the runners, the scheduler's heartbeat -- used to
write *on the loop*: a background write that landed while a request held the
write lock blocked the loop in SQLite's busy handler, the request could not
reach its commit, and both sat out ``busy_timeout`` before the background
write failed "database is locked" (S22's diagnosis on #255).

Each test here is that repro, made deterministic: an engine ``begin`` listener
sets an :class:`asyncio.Event` when a writer transaction opens in a thread that
is neither the loop's nor the background-database thread (so: the request's),
and a loop task waiting on it then runs a real background path. The busy
timeout is cut to 300 ms, so on the old code each test fails in 300 ms with
``OperationalError: database is locked``. With the background work off the
loop, the write waits for the request's commit and goes through.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import factories
import pytest
from fastapi import FastAPI
from run_fakes import Clock, fake_provider
from sqlalchemy import Connection, Engine, event, select
from sqlalchemy.orm import Session
from test_runs_serve import HEADERS, START, client_for, heartbeat, served

from netkeeper.config import Settings
from netkeeper.db import IMMEDIATE_OPTION, database_url, make_engine, off_loop, session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import SyncRunKind, SyncRunStatus, SyncRunTrigger, User, UserKind
from netkeeper.services import runs, scheduler
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import flag_session
from netkeeper.worker import BrowserWorker

#: Short enough that the old deadlock fails fast; the fixed path never waits it out.
BUSY_TIMEOUT_MS = 300

#: The short busy timeout runs while another thread holds the write lock, and a full
#: garbage collection on CI (2-3 s) stops every thread but the busy wait itself, so
#: one landing there used to fail a correct test (#472). These bodies hold collection.
pytestmark = pytest.mark.wall_clock


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Engine]:
    """A fresh database whose every connection has the short busy timeout."""
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "no-dist"))
    monkeypatch.setattr("netkeeper.db.SQLITE_BUSY_TIMEOUT_MS", BUSY_TIMEOUT_MS)
    engine = make_engine(database_url(tmp_path / "db"))
    yield engine
    engine.dispose()


class RequestWriteOpened:
    """Sets :attr:`event` when a writer transaction begins in a request's thread.

    Armed only between :meth:`arm` and the first such ``begin``, so the setup's
    own writes (on the loop's thread) and the background thread's never count.
    """

    def __init__(self, engine: Engine) -> None:
        self.event = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.current_thread()
        self._armed = False
        self._engine = engine
        event.listen(engine, "begin", self._on_begin)

    def arm(self) -> None:
        self._armed = True

    def close(self) -> None:
        event.remove(self._engine, "begin", self._on_begin)

    def _on_begin(self, connection: Connection) -> None:
        thread = threading.current_thread()
        if (
            self._armed
            and connection.get_execution_options().get(IMMEDIATE_OPTION)
            and thread is not self._loop_thread
            and not thread.name.startswith("netkeeper-db")
        ):
            self._armed = False
            self._loop.call_soon_threadsafe(self.event.set)


@asynccontextmanager
async def request_write(engine: Engine) -> AsyncIterator[RequestWriteOpened]:
    opened = RequestWriteOpened(engine)
    try:
        yield opened
    finally:
        opened.close()


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


async def _pin_during(app: FastAPI, opened: RequestWriteOpened, contact_id: int) -> int:
    """POST a pin with ``opened`` armed; the pin's write transaction is the one it sees."""
    opened.arm()
    async with client_for(app) as client:
        response = await client.post(
            "/api/v1/linkedin/pins", json={"contact_id": contact_id}, headers=HEADERS
        )
    return response.status_code


async def test_a_worker_run_finish_waits_for_a_request_commit_instead_of_deadlocking(
    engine: Engine,
) -> None:
    """The worker's run finish (a refused run's ``_finish``) lands inside a POST's
    write transaction: it waits for the commit, and the run is recorded."""
    settings = Settings()
    provider, connector = fake_provider()
    async with served(engine, settings, provider, Clock(START)) as app:
        factory = app.state.session_factory
        with session_scope(factory, write=True) as session:
            user = _local(session)
            ensure_account(session, user)
            contact_id = factories.make_contact(session, user).id
            run_id = runs.create_run(
                session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=START
            ).id
            # A flagged session: the worker refuses before the browser, and its first
            # write is the refusal's ending.
            flag_session(session, user, Outcome.CHECKPOINT, url="https://example.invalid/")
            user_id = user.id
        worker = app.state.executor
        assert isinstance(worker, BrowserWorker)

        async with request_write(engine) as opened:

            async def run_in_the_background() -> runs.RunOutcome:
                await opened.event.wait()
                outcome: runs.RunOutcome = await worker.execute(run_id, user_id)
                return outcome

            background = asyncio.create_task(run_in_the_background())
            status = await _pin_during(app, opened, contact_id)
            outcome = await asyncio.wait_for(background, timeout=3)

        with session_scope(factory) as session:
            run = runs.get_run(session, _local(session), run_id)

    assert opened.event.is_set()  # the background write really overlapped the request
    assert status == 200
    assert outcome is runs.RunOutcome.DONE
    assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "session_flagged")
    assert connector.attaches == 0


async def test_a_scheduler_heartbeat_waits_for_a_request_commit_instead_of_deadlocking(
    engine: Engine,
) -> None:
    """The scheduler's real heartbeat lands inside a POST's write transaction: its
    writer session waits for the commit, and the schedule still moves on."""
    settings = Settings()
    provider, connector = fake_provider()
    clock = Clock(START)
    async with served(engine, settings, provider, clock) as app:
        factory = app.state.session_factory
        with session_scope(factory, write=True) as session:
            user = _local(session)
            account_id = ensure_account(session, user).id
            contact_id = factories.make_contact(session, user).id
        # A month later every kind has lapsed by a whole interval: the heartbeat's
        # first writer session defers each as a catch-up (the account is disarmed,
        # so nothing would fire either way).
        clock.at = START + timedelta(days=30)

        async with request_write(engine) as opened:

            async def beat_in_the_background() -> None:
                await opened.event.wait()
                await heartbeat(app)

            background = asyncio.create_task(beat_in_the_background())
            status = await _pin_during(app, opened, contact_id)
            await asyncio.wait_for(background, timeout=3)

        with session_scope(factory) as session:
            user = _local(session)
            states = {
                kind: scheduler._load_state(session, user, account_id, kind)
                for kind in scheduler.SERVED_SCHEDULES
            }

    assert opened.event.is_set()
    assert status == 200
    for kind, state in states.items():
        assert state is not None, kind
        assert state.is_catchup and state.due > clock.at, kind
    assert connector.attaches == 0


# --- off_loop itself ------------------------------------------------------------


async def test_off_loop_runs_on_the_one_background_thread_and_returns_or_raises() -> None:
    names = {await off_loop(lambda: threading.current_thread().name) for _ in range(3)}

    def boom() -> None:
        raise ValueError("from the thread")

    with pytest.raises(ValueError, match="from the thread"):
        await off_loop(boom)
    assert len(names) == 1 and next(iter(names)).startswith("netkeeper-db")
    assert threading.current_thread().name not in names


async def test_off_loop_runs_calls_one_at_a_time_in_the_order_they_were_submitted() -> None:
    order: list[int] = []
    release = threading.Event()

    def first() -> None:
        release.wait(2)
        order.append(1)

    def second() -> None:
        order.append(2)

    one = asyncio.create_task(off_loop(first))
    two = asyncio.create_task(off_loop(second))
    await asyncio.sleep(0.05)
    assert order == []  # the second waits behind the first, never beside it
    release.set()
    await asyncio.gather(one, two)
    assert order == [1, 2]


async def test_a_cancel_waits_for_the_work_in_flight_and_then_propagates() -> None:
    """Like blocking code on the loop, a started transaction is never abandoned:
    the cancel lands once the work is done, and work queued after it still runs."""
    started = threading.Event()
    release = threading.Event()
    done: list[str] = []

    def in_flight() -> None:
        started.set()
        release.wait(2)
        done.append("in flight")

    task = asyncio.create_task(off_loop(in_flight))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()  # still waiting for the work, even cancelled
    task.cancel()  # a second cancel does not cut it short either
    await asyncio.sleep(0.05)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await off_loop(lambda: done.append("after"))
    assert done == ["in flight", "after"]
