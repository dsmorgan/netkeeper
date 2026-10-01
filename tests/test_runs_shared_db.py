"""Two data directories sharing one database (#177 G1).

Two ``NETKEEPER_DATA`` directories can share one database through an explicit
``NETKEEPER_DATABASE_URL``. The browser lock lives in the data directory, so a
live run in directory A, older than ``STALE_AFTER``, looks left behind from
directory B. B may then mark it ``failed`` (``create_run``, ``request_cancel``);
what these tests pin is that A's runner still stops: B's cancel sets the flag,
and A's gates stop once the row is no longer ``running``.

Both data directories and the database are under ``tmp_path``. Nothing here
changes how the database URL resolves: every session comes from the
``session_factory`` fixture, whose SQLite file the first test checks is a temp path.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import UTC, datetime, time
from pathlib import Path

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.linkedin import activity_lock
from netkeeper.linkedin.enrich import StopReason
from netkeeper.models import SyncRun, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.services import connections_sync, enrichment, runs
from netkeeper.services.linkedin_accounts import ensure_account

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
