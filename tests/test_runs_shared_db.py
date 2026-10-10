"""Two data directories sharing one database (#177 G1, #467).

Two ``NETKEEPER_DATA`` directories can share one database through an explicit
``NETKEEPER_DATABASE_URL``. The browser lock lives in the data directory, so B
can't see A's lock. What B can see is A's run's heartbeat (#467): while it is
fresh, B refuses to start a run (``create_run``), never fails A's run at its own
start (``fail_interrupted_runs``), and a cancel from B only sets the flag. Once
the heartbeat is ``STALE_AFTER`` old, A's runner really died, and B recovers the
run. The first tests here have no heartbeat at all, the case where A's heartbeat
writes failed: B may then mark A's run ``failed``, and what they pin is that A's
runner still stops: B's cancel sets the flag, and A's gates stop once the row is
no longer ``running``.

Both data directories and the database are under ``tmp_path``. Nothing here
changes how the database URL resolves: every session comes from the
``session_factory`` fixture, whose SQLite file the first test checks is a temp path.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Final

import factories
import pytest
from browser_fakes import FakeBrowser, FakeConnector
from run_fakes import CDP_URL, Clock, ConnectionsContext, Gate
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from test_migrations import BACKENDS, PG_ENV
from voyager_pages import Person

from netkeeper import migrations
from netkeeper.config import Settings
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin import activity_lock
from netkeeper.linkedin.browser import ActivityLocks, AttachBrowserProvider
from netkeeper.linkedin.enrich import StopReason
from netkeeper.models import SyncRun, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.scoping import install_scope_guard
from netkeeper.services import connections_sync, enrichment, runs
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.worker import BrowserWorker

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
LATER = NOW + runs.STALE_AFTER


@pytest.fixture
def data_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session]
) -> tuple[Path, Path]:
    """Directory A and directory B, both under ``tmp_path``; B is the current one."""
    database = session_factory.kw["bind"].url.database
    assert database is not None
    assert Path(database).resolve().is_relative_to(tmp_path.resolve())
    a, b = tmp_path / "data-a", tmp_path / "data-b"
    monkeypatch.setenv("NETKEEPER_DATA", str(b))
    return a, b


@pytest.fixture
def user_id(session_factory: sessionmaker[Session]) -> int:
    with session_scope(session_factory, write=True) as session:
        return factories.make_user(session, timezone="UTC").id


@pytest.fixture
def live_in_a(
    session_factory: sessionmaker[Session], user_id: int, data_dirs: tuple[Path, Path]
) -> Iterator[SyncRun]:
    """A's run, ``running``, with A holding the account's lock (and the legacy one) in A."""
    a, _ = data_dirs
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        run = runs.create_run(
            session, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
        key = activity_lock.account_key(run.linkedin_account_id)
    locks = a / activity_lock.LOCKS_DIRNAME
    claims = [
        activity_lock.try_claim(activity_lock.LEGACY_SHARED_KEY, locks),
        activity_lock.try_claim(key, locks),
    ]
    assert all(claim is not None for claim in claims)
    try:
        # A sees its own lock held; B, reading its own directory, does not.
        assert activity_lock.inspect(key, locks).held
        assert not activity_lock.inspect(key).held
        yield run
    finally:
        for claim in claims:
            assert claim is not None
            claim.release()


def _connections_gate(
    factory: sessionmaker[Session], user_id: int, run: SyncRun
) -> connections_sync._BudgetGate:
    return connections_sync._BudgetGate(
        factory=factory,
        user_id=user_id,
        account_id=run.linkedin_account_id,
        run_id=run.id,
        settings=Settings().linkedin,
        clock=lambda: LATER,
        sleep=_no_sleep,
        rng=random.Random(0),
        multiplier=1.0,
    )


def _enrich_gate(factory: sessionmaker[Session], user_id: int, run: SyncRun) -> enrichment._Gate:
    return enrichment._Gate(
        factory=factory,
        user_id=user_id,
        account_id=run.linkedin_account_id,
        run_id=run.id,
        settings=Settings().linkedin,
        window=(time(0, 0), time(23, 59)),
        clock=lambda: LATER,
        sleep=_no_sleep,
    )


def _user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    assert user is not None
    return user


async def _no_sleep(seconds: float) -> None:
    return None


def _people(count: int) -> list[Person]:
    return [Person(1000 + i, f"Given{i}", f"Family{i}", f"Role {i}") for i in range(count)]


def _stored(factory: sessionmaker[Session], user_id: int, run_id: int) -> SyncRun:
    with session_scope(factory) as session:
        run = runs.get_run(session, _user(session, user_id), run_id)
        session.expunge(run)
        return run


def test_b_cancelling_a_s_run_sets_the_flag_a_reads(
    session_factory: sessionmaker[Session], user_id: int, live_in_a: SyncRun
) -> None:
    """B judges A's run stale and fails it, and still sets the flag A's runner reads."""
    with session_scope(session_factory, write=True) as session:
        runs.request_cancel(session, _user(session, user_id), live_in_a.id, now=LATER)

    stored = _stored(session_factory, user_id, live_in_a.id)
    assert stored.status is SyncRunStatus.FAILED
    assert stored.cancel_requested_at == LATER
    with session_scope(session_factory) as session:
        user = _user(session, user_id)
        assert runs.cancel_requested(session, user, live_in_a.id)


async def test_a_s_gates_stop_after_b_cancels(
    session_factory: sessionmaker[Session], user_id: int, live_in_a: SyncRun
) -> None:
    with session_scope(session_factory, write=True) as session:
        runs.request_cancel(session, _user(session, user_id), live_in_a.id, now=LATER)

    gate = _connections_gate(session_factory, user_id, live_in_a)
    assert not await gate.before_page(2)
    assert gate.cancelled
    enrich_gate = _enrich_gate(session_factory, user_id, live_in_a)
    assert await enrich_gate.before_visit(2) is StopReason.CANCELLED


async def test_a_s_gates_stop_after_b_fails_its_run_to_start_another(
    session_factory: sessionmaker[Session], user_id: int, live_in_a: SyncRun
) -> None:
    """B's create_run fails A's run with no cancel flag; A stops on the status alone,
    so the two never keep going on one Chrome."""
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        fresh = runs.create_run(
            session, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.MANUAL, now=LATER
        )
        assert fresh.id != live_in_a.id

    stored = _stored(session_factory, user_id, live_in_a.id)
    assert (stored.status, stored.cancel_requested_at) == (SyncRunStatus.FAILED, None)

    gate = _connections_gate(session_factory, user_id, live_in_a)
    assert not await gate.before_page(2)
    assert gate.cancelled
    waiting = _connections_gate(session_factory, user_id, live_in_a)
    await waiting.between_pages()
    assert waiting.cancelled
    enrich_gate = _enrich_gate(session_factory, user_id, live_in_a)
    assert await enrich_gate.before_visit(2) is StopReason.CANCELLED
    assert not await enrich_gate.pause(30.0)

    # B's own run is untouched by any of this.
    fresh_gate = _connections_gate(
        session_factory, user_id, _stored(session_factory, user_id, fresh.id)
    )
    assert await fresh_gate.before_page(1)


def test_a_running_run_with_no_flag_is_not_cancelled(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        run = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
        assert not runs.cancel_requested(session, user, run.id)
        assert not runs.cancel_requested(session, user, run.id + 1000)  # no such run
        other = factories.make_user(session)
        ensure_account(session, other)
        assert not runs.cancel_requested(session, other, run.id)  # another user's run


@pytest.mark.parametrize("status", [SyncRunStatus.COMPLETED, SyncRunStatus.ABORTED])
def test_a_run_that_ended_any_way_reads_as_cancelled(
    session_factory: sessionmaker[Session], user_id: int, status: SyncRunStatus
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        run = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
        runs.finish_run(session, user, run.id, status=status, now=NOW)
        assert runs.cancel_requested(session, user, run.id)


# --- #467: the heartbeat ----------------------------------------------------------------


def test_the_staleness_constants_keep_their_values() -> None:
    """A heartbeat every 20 s, stale after 2 min without one: six beats must fail first."""
    assert timedelta(minutes=2) == runs.STALE_AFTER
    assert timedelta(seconds=20) == runs.HEARTBEAT_EVERY


def _beat(factory: sessionmaker[Session], user_id: int, run_id: int, at: datetime) -> bool:
    with session_scope(factory, write=True) as session:
        return runs.beat(session, _user(session, user_id), run_id, now=at)


def test_b_refuses_to_start_a_run_while_a_s_heartbeat_is_fresh(
    session_factory: sessionmaker[Session], user_id: int, live_in_a: SyncRun
) -> None:
    """A's run is long past STALE_AFTER and B can't see A's lock, but A's runner beat
    10 s ago: B refuses, and A's run is left as it was."""
    much_later = NOW + timedelta(hours=1)
    assert _beat(session_factory, user_id, live_in_a.id, much_later - timedelta(seconds=10))
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        with pytest.raises(runs.RunAlreadyRunning, match=f"run {live_in_a.id} "):
            runs.create_run(
                session,
                user,
                SyncRunKind.CONNECTIONS_FULL,
                trigger=SyncRunTrigger.MANUAL,
                now=much_later,
            )
        with pytest.raises(runs.RunAlreadyRunning):
            runs.create_run(
                session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=much_later
            )
        assert runs.fail_interrupted_runs(session, now=much_later) == 0

    stored = _stored(session_factory, user_id, live_in_a.id)
    assert (stored.status, stored.cancel_requested_at) == (SyncRunStatus.RUNNING, None)
    with session_scope(session_factory) as session:
        user = _user(session, user_id)
        assert not runs.cancel_requested(session, user, live_in_a.id)
        assert [run.id for run in runs.list_runs(session, user)[0]] == [live_in_a.id]


def test_b_s_cancel_of_a_live_run_only_sets_the_flag(
    session_factory: sessionmaker[Session], user_id: int, live_in_a: SyncRun
) -> None:
    """With a fresh heartbeat, B's cancel leaves the run running for A's runner to stop
    and record, instead of failing it under that runner."""
    assert _beat(session_factory, user_id, live_in_a.id, LATER)
    with session_scope(session_factory, write=True) as session:
        runs.request_cancel(session, _user(session, user_id), live_in_a.id, now=LATER)

    stored = _stored(session_factory, user_id, live_in_a.id)
    assert (stored.status, stored.cancel_requested_at) == (SyncRunStatus.RUNNING, LATER)


def test_b_recovers_a_run_whose_heartbeat_stopped(
    session_factory: sessionmaker[Session], user_id: int, data_dirs: tuple[Path, Path]
) -> None:
    """A's process died after its last beat (its lock went with it): until STALE_AFTER
    after that beat B still refuses, and from then on B fails the run and starts its own."""
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        left = runs.create_run(
            session, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
    last_beat = NOW + timedelta(minutes=30)
    assert _beat(session_factory, user_id, left.id, last_beat)

    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        with pytest.raises(runs.RunAlreadyRunning):
            runs.create_run(
                session,
                user,
                SyncRunKind.CONNECTIONS_FULL,
                trigger=SyncRunTrigger.MANUAL,
                now=last_beat + runs.STALE_AFTER - timedelta(seconds=1),
            )
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        fresh = runs.create_run(
            session,
            user,
            SyncRunKind.CONNECTIONS_FULL,
            trigger=SyncRunTrigger.MANUAL,
            now=last_beat + runs.STALE_AFTER,
        )
        assert fresh.id != left.id

    stored = _stored(session_factory, user_id, left.id)
    assert (stored.status, stored.stop_reason, stored.error) == (
        SyncRunStatus.FAILED,
        "interrupted",
        runs.INTERRUPTED,
    )


def test_b_s_start_fails_only_a_run_whose_heartbeat_stopped(
    session_factory: sessionmaker[Session], user_id: int, data_dirs: tuple[Path, Path]
) -> None:
    """``fail_interrupted_runs`` at B's start: the dead run goes, the live one stays."""
    with session_scope(session_factory, write=True) as session:
        alive_user = _user(session, user_id)
        alive = runs.create_run(
            session, alive_user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
        dead_user = factories.make_user(session, timezone="UTC")
        dead_user_id = dead_user.id
        dead = runs.create_run(
            session, dead_user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
    start_of_b = NOW + timedelta(minutes=10)
    assert _beat(session_factory, user_id, alive.id, start_of_b - timedelta(seconds=30))
    assert _beat(session_factory, dead_user_id, dead.id, start_of_b - runs.STALE_AFTER)

    with session_scope(session_factory, write=True) as session:
        assert runs.fail_interrupted_runs(session, now=start_of_b) == 1

    assert _stored(session_factory, user_id, alive.id).status is SyncRunStatus.RUNNING
    assert _stored(session_factory, dead_user_id, dead.id).status is SyncRunStatus.FAILED


def test_a_beat_lands_only_on_the_users_own_running_run(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        run = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
        other = factories.make_user(session)
        assert not runs.beat(session, other, run.id, now=LATER)  # another user's run
        assert not runs.beat(session, user, run.id + 1000, now=LATER)  # no such run
        assert runs.beat(session, user, run.id, now=LATER)
        runs.finish_run(session, user, run.id, status=SyncRunStatus.COMPLETED, now=LATER)
        assert not runs.beat(session, user, run.id, now=LATER + timedelta(minutes=1))
    assert _stored(session_factory, user_id, run.id).heartbeat_at == LATER


def test_a_heartbeat_older_than_the_start_counts_as_none(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        run = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
        assert runs.last_sign_of_life(run) == NOW
        run.heartbeat_at = NOW - timedelta(hours=1)
        assert runs.last_sign_of_life(run) == NOW
        run.heartbeat_at = LATER
        assert runs.last_sign_of_life(run) == LATER


# --- the heartbeat a live runner keeps ---------------------------------------------------


FAST: Final = timedelta(milliseconds=10)


async def _until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while True:
            if predicate():
                return
            await asyncio.sleep(0.005)


async def test_the_heartbeat_keeps_a_run_alive_and_stops_with_its_block(
    session_factory: sessionmaker[Session], user_id: int, data_dirs: tuple[Path, Path]
) -> None:
    """The context manager beats at once and then every ``every`` with the clock's time;
    once the block is gone (its process died, say), the beats stop and B recovers."""
    with session_scope(session_factory, write=True) as session:
        run_id = runs.create_run(
            session,
            _user(session, user_id),
            SyncRunKind.ENRICH,
            trigger=SyncRunTrigger.MANUAL,
            now=NOW,
        ).id
    clock = Clock(NOW)
    stop = asyncio.Event()

    async def a_s_runner() -> None:
        async with runs.heartbeat(session_factory, user_id, run_id, clock=clock, every=FAST):
            await stop.wait()

    def beat_at(at: datetime) -> Callable[[], bool]:
        return lambda: _stored(session_factory, user_id, run_id).heartbeat_at == at

    task = asyncio.create_task(a_s_runner())
    await _until(beat_at(NOW))
    clock.at = NOW + timedelta(hours=1)
    await _until(beat_at(clock.at))
    with (
        session_scope(session_factory, write=True) as session,
        pytest.raises(runs.RunAlreadyRunning),
    ):
        runs.create_run(
            session,
            _user(session, user_id),
            SyncRunKind.ENRICH,
            trigger=SyncRunTrigger.MANUAL,
            now=clock.at + timedelta(seconds=5),
        )

    # The process goes away without recording an ending: the row stays running.
    last = clock.at
    task.cancel()
    await asyncio.wait({task})
    clock.at = last + timedelta(hours=1)
    await asyncio.sleep(FAST.total_seconds() * 5)
    stored = _stored(session_factory, user_id, run_id)
    assert (stored.status, stored.heartbeat_at) == (SyncRunStatus.RUNNING, last)
    with session_scope(session_factory, write=True) as session:
        runs.create_run(
            session,
            _user(session, user_id),
            SyncRunKind.ENRICH,
            trigger=SyncRunTrigger.MANUAL,
            now=last + runs.STALE_AFTER,
        )
    assert _stored(session_factory, user_id, run_id).status is SyncRunStatus.FAILED


async def test_the_heartbeat_stops_once_the_run_ended(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = _user(session, user_id)
        run_id = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        ).id
        runs.finish_run(session, user, run_id, status=SyncRunStatus.COMPLETED, now=NOW)
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return LATER

    async with runs.heartbeat(session_factory, user_id, run_id, clock=clock, every=FAST):
        await asyncio.sleep(FAST.total_seconds() * 10)
    assert calls == 1  # one beat found the run over, and the task stopped
    assert _stored(session_factory, user_id, run_id).heartbeat_at is None


async def test_a_failed_beat_never_stops_the_run(
    session_factory: sessionmaker[Session],
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with session_scope(session_factory, write=True) as session:
        run_id = runs.create_run(
            session,
            _user(session, user_id),
            SyncRunKind.ENRICH,
            trigger=SyncRunTrigger.MANUAL,
            now=NOW,
        ).id
    attempts = 0

    def broken(*args: object, **kwargs: object) -> bool:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("database is locked")

    monkeypatch.setattr(runs, "beat", broken)
    async with runs.heartbeat(session_factory, user_id, run_id, clock=lambda: LATER, every=FAST):
        await _until(lambda: attempts >= 3)  # it kept trying
    assert "could not write run" in caplog.text
    stored = _stored(session_factory, user_id, run_id)
    assert (stored.status, stored.heartbeat_at) == (SyncRunStatus.RUNNING, None)


async def test_the_worker_keeps_a_live_run_s_heartbeat_so_b_cannot_start_another(
    session_factory: sessionmaker[Session],
    user_id: int,
    data_dirs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: A's real worker, with its locks in A, parked mid-sync. B, reading its
    own lock files, refuses a second run for as long as A's worker runs."""
    a, _ = data_dirs
    monkeypatch.setattr(runs, "HEARTBEAT_EVERY", FAST)
    clock = Clock(NOW)
    with session_scope(session_factory, write=True) as session:
        run_id = runs.create_run(
            session,
            _user(session, user_id),
            SyncRunKind.CONNECTIONS_FULL,
            trigger=SyncRunTrigger.MANUAL,
            now=NOW,
        ).id
    connector = FakeConnector([FakeBrowser([ConnectionsContext(_people(120), first=40)])])
    provider = AttachBrowserProvider(
        CDP_URL, connector=connector, locks=ActivityLocks(a / activity_lock.LOCKS_DIRNAME)
    )
    gate = Gate()  # never opened: the run waits between pages
    worker = BrowserWorker(provider, session_factory, Settings().linkedin, clock=clock, sleep=gate)
    task = asyncio.create_task(worker.execute(run_id, user_id))
    try:
        await _until(lambda: gate.calls > 0)
        clock.at = NOW + timedelta(hours=1)
        await _until(lambda: _stored(session_factory, user_id, run_id).heartbeat_at == clock.at)
        with session_scope(session_factory, write=True) as session:
            user = _user(session, user_id)
            with pytest.raises(runs.RunAlreadyRunning):
                runs.create_run(
                    session,
                    user,
                    SyncRunKind.CONNECTIONS_FULL,
                    trigger=SyncRunTrigger.MANUAL,
                    now=clock.at + timedelta(seconds=5),
                )
            assert runs.fail_interrupted_runs(session, now=clock.at + timedelta(seconds=5)) == 0
        assert connector.attaches == 1
    finally:
        task.cancel()
        await asyncio.wait({task})
    stored = _stored(session_factory, user_id, run_id)
    assert (stored.status, stored.stop_reason) == (SyncRunStatus.ABORTED, "interrupted")


# --- the same, on each database a shared setup can use ------------------------------------


@pytest.fixture(params=BACKENDS)
def two_processes(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[tuple[sessionmaker[Session], sessionmaker[Session]]]:
    """Two engines on one migrated database, as A and B would each open it."""
    if request.param == "sqlite":
        url = database_url(tmp_path / "shared")
        yield from _migrated_pair(url)
        return
    yield from _postgres_pair()


def _migrated_pair(url: str) -> Iterator[tuple[sessionmaker[Session], sessionmaker[Session]]]:
    engines = [make_engine(url), make_engine(url)]
    try:
        migrations.upgrade(engines[0])
        factories_ = [make_session_factory(engine) for engine in engines]
        for factory in factories_:
            install_scope_guard(factory)
        yield factories_[0], factories_[1]
    finally:
        for engine in engines:
            engine.dispose()


def _postgres_pair() -> Iterator[tuple[sessionmaker[Session], sessionmaker[Session]]]:
    """A throwaway PostgreSQL database of this worker's own, dropped afterwards."""
    base = make_url(os.environ[PG_ENV])
    name = f"{base.database}_shared_runs_{os.environ.get('PYTEST_XDIST_WORKER', 'master')}"
    assert re.fullmatch(r"\w+", name), name
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        yield from _migrated_pair(base.set(database=name).render_as_string(hide_password=False))
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


def test_two_processes_on_one_database_refuse_then_recover(
    two_processes: tuple[sessionmaker[Session], sessionmaker[Session]],
) -> None:
    """A's engine beats; B's engine refuses while the beat is fresh, and recovers the run
    once it is STALE_AFTER old. Neither can see the other's lock files."""
    a, b = two_processes
    never_held: runs.BrowserHeld = lambda account_id: False  # noqa: E731
    with session_scope(a, write=True) as session:
        user = factories.make_user(session, timezone="UTC")
        user_id = user.id
        run_id = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        ).id
    last_beat = NOW + timedelta(hours=1)
    assert _beat(a, user_id, run_id, last_beat)

    def start_in_b(at: datetime) -> int:
        with session_scope(b, write=True) as session:
            return runs.create_run(
                session,
                _user(session, user_id),
                SyncRunKind.ENRICH,
                trigger=SyncRunTrigger.MANUAL,
                now=at,
                browser_held=never_held,
            ).id

    with pytest.raises(runs.RunAlreadyRunning):
        start_in_b(last_beat + timedelta(seconds=30))
    with session_scope(b, write=True) as session:
        assert runs.fail_interrupted_runs(session, now=last_beat, browser_held=never_held) == 0
    assert _stored(a, user_id, run_id).status is SyncRunStatus.RUNNING

    fresh = start_in_b(last_beat + runs.STALE_AFTER)
    assert fresh != run_id
    assert _stored(a, user_id, run_id).status is SyncRunStatus.FAILED
    with session_scope(a) as session:
        assert runs.cancel_requested(session, _user(session, user_id), run_id)
    assert not _beat(a, user_id, run_id, last_beat + runs.STALE_AFTER)
