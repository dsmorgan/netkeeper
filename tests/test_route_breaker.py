"""netkeeper.services.route_breaker: the route-changed breaker (spec 9.3, ADR 0006; #189 item 1).

The core-side half of the seam: this module opens sessions and reads/writes
``settings_kv``, the same shape :mod:`netkeeper.services.heat` uses.
``tests/test_connections_sync.py`` exercises it end to end through a real
connections sync; ``tests/test_scheduler.py`` exercises the scheduler's own
gate that reads :func:`~netkeeper.services.route_breaker.tripped`. This file
is the module on its own.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.models import User
from netkeeper.services import route_breaker
from netkeeper.services.settings_kv import set_setting

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
ACCOUNT = 1


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


# --- the constant is pinned, not self-referential (CLAUDE.md) ----------------


def test_the_threshold_is_two() -> None:
    assert route_breaker.THRESHOLD == 2


# --- a never-recorded account reads clear -------------------------------------


def test_a_never_recorded_account_is_not_tripped(writer: Session, user: User) -> None:
    assert not route_breaker.tripped(
        writer,
        user,
        ACCOUNT,
    )
    state = route_breaker.state(writer, user, ACCOUNT)
    assert (state.count, state.since, state.tripped) == (0, None, False)


# --- writer-session guards ----------------------------------------------------


def test_record_requires_a_writer_session(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as setup:
        user_id = factories.make_user(setup).id
    owner = session.get(User, user_id)
    assert owner is not None
    with pytest.raises(RuntimeError, match="writer session"):
        route_breaker.record(session, owner, ACCOUNT, route_changed=True, succeeded=False, now=NOW)


def test_reset_requires_a_writer_session(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as setup:
        user_id = factories.make_user(setup).id
    owner = session.get(User, user_id)
    assert owner is not None
    with pytest.raises(RuntimeError, match="writer session"):
        route_breaker.reset(session, owner, ACCOUNT)


# --- the streak: two route_changed runs trip it -------------------------------


def test_one_route_changed_run_extends_the_streak_but_does_not_trip_it(
    writer: Session, user: User
) -> None:
    updated = route_breaker.record(
        writer, user, ACCOUNT, route_changed=True, succeeded=False, now=NOW
    )
    assert (updated.count, updated.tripped) == (1, False)
    assert not route_breaker.tripped(writer, user, ACCOUNT)


def test_two_route_changed_runs_in_a_row_trip_it(writer: Session, user: User) -> None:
    route_breaker.record(writer, user, ACCOUNT, route_changed=True, succeeded=False, now=NOW)
    updated = route_breaker.record(
        writer, user, ACCOUNT, route_changed=True, succeeded=False, now=NOW + timedelta(hours=1)
    )
    assert (updated.count, updated.tripped) == (2, True)
    assert route_breaker.tripped(writer, user, ACCOUNT)


def test_since_is_the_first_route_changed_run_of_the_streak(writer: Session, user: User) -> None:
    route_breaker.record(writer, user, ACCOUNT, route_changed=True, succeeded=False, now=NOW)
    later = NOW + timedelta(hours=1)
    updated = route_breaker.record(
        writer, user, ACCOUNT, route_changed=True, succeeded=False, now=later
    )
    assert updated.since == NOW  # not `later`: the streak started at the first one


def test_a_success_in_between_resets_the_count(writer: Session, user: User) -> None:
    """One route_changed, then a run that reaches a natural end, then another
    route_changed: the streak never reaches two in a row, so it never trips."""
    route_breaker.record(writer, user, ACCOUNT, route_changed=True, succeeded=False, now=NOW)
    assert route_breaker.state(writer, user, ACCOUNT).count == 1

    reset_state = route_breaker.record(
        writer, user, ACCOUNT, route_changed=False, succeeded=True, now=NOW
    )
    assert (reset_state.count, reset_state.since) == (0, None)

    updated = route_breaker.record(
        writer, user, ACCOUNT, route_changed=True, succeeded=False, now=NOW
    )
    assert (updated.count, updated.tripped) == (1, False)


def test_a_success_after_tripping_clears_it(writer: Session, user: User) -> None:
    for _ in range(route_breaker.THRESHOLD):
        route_breaker.record(writer, user, ACCOUNT, route_changed=True, succeeded=False, now=NOW)
    assert route_breaker.tripped(writer, user, ACCOUNT)

    route_breaker.record(writer, user, ACCOUNT, route_changed=False, succeeded=True, now=NOW)

    assert not route_breaker.tripped(writer, user, ACCOUNT)
    assert route_breaker.state(writer, user, ACCOUNT).count == 0


def test_neither_route_changed_nor_succeeded_leaves_the_count_unchanged(
    writer: Session, user: User
) -> None:
    """A budget stop, a cancel, a checkpoint: none of these says anything about
    whether the connections list's own route is readable."""
    route_breaker.record(writer, user, ACCOUNT, route_changed=True, succeeded=False, now=NOW)
    before = route_breaker.state(writer, user, ACCOUNT)

    after = route_breaker.record(
        writer, user, ACCOUNT, route_changed=False, succeeded=False, now=NOW
    )

    assert after == before
    assert route_breaker.state(writer, user, ACCOUNT) == before


def test_route_changed_and_succeeded_together_is_refused(writer: Session, user: User) -> None:
    with pytest.raises(ValueError, match="route_changed"):
        route_breaker.record(writer, user, ACCOUNT, route_changed=True, succeeded=True, now=NOW)


# --- reset: the same effect as a manual success, on demand -------------------


def test_reset_clears_a_tripped_breaker(writer: Session, user: User) -> None:
    for _ in range(route_breaker.THRESHOLD):
        route_breaker.record(writer, user, ACCOUNT, route_changed=True, succeeded=False, now=NOW)
    assert route_breaker.tripped(writer, user, ACCOUNT)

    cleared = route_breaker.reset(writer, user, ACCOUNT)

    assert (cleared.count, cleared.since) == (0, None)
    assert not route_breaker.tripped(writer, user, ACCOUNT)


def test_reset_is_idempotent(writer: Session, user: User) -> None:
    route_breaker.reset(writer, user, ACCOUNT)
    route_breaker.reset(writer, user, ACCOUNT)
    assert route_breaker.state(writer, user, ACCOUNT).count == 0


# --- scoping -------------------------------------------------------------------


def test_the_breaker_is_scoped_by_account_id(writer: Session, user: User) -> None:
    """Two accounts under the same user must not share a streak."""
    route_breaker.record(writer, user, 1, route_changed=True, succeeded=False, now=NOW)
    route_breaker.record(writer, user, 1, route_changed=True, succeeded=False, now=NOW)
    assert route_breaker.tripped(writer, user, 1)
    assert not route_breaker.tripped(writer, user, 2)


def test_the_breaker_is_scoped_by_user(writer: Session) -> None:
    owner = factories.make_user(writer)
    other = factories.make_user(writer)
    route_breaker.record(writer, owner, ACCOUNT, route_changed=True, succeeded=False, now=NOW)
    route_breaker.record(writer, owner, ACCOUNT, route_changed=True, succeeded=False, now=NOW)
    assert route_breaker.tripped(writer, owner, ACCOUNT)
    assert not route_breaker.tripped(writer, other, ACCOUNT)


# --- fail closed: a corrupt row reads as tripped, never a crash (#191 review, F7) --


def _key(account_id: int) -> str:
    return f"linkedin.route_changed_breaker.{account_id}"


def test_a_row_that_is_not_an_object_reads_as_tripped_not_a_crash(
    writer: Session, user: User
) -> None:
    """A posture report, or the scheduler's gate, is the last thing that should
    crash on a corrupt row: fail closed instead."""
    set_setting(writer, user, _key(ACCOUNT), "not an object")
    state = route_breaker.state(writer, user, ACCOUNT)
    assert (state.readable, state.tripped) == (False, True)
    assert route_breaker.tripped(writer, user, ACCOUNT)


def test_a_row_missing_count_reads_as_tripped_not_a_crash(writer: Session, user: User) -> None:
    set_setting(writer, user, _key(ACCOUNT), {"since": None})
    assert route_breaker.tripped(writer, user, ACCOUNT)
    assert route_breaker.state(writer, user, ACCOUNT).readable is False


def test_a_row_whose_count_is_not_a_number_reads_as_tripped_not_a_crash(
    writer: Session, user: User
) -> None:
    set_setting(writer, user, _key(ACCOUNT), {"count": "two", "since": None})
    assert route_breaker.tripped(writer, user, ACCOUNT)
    assert route_breaker.state(writer, user, ACCOUNT).readable is False


def test_a_row_whose_since_does_not_parse_reads_as_tripped_not_a_crash(
    writer: Session, user: User
) -> None:
    set_setting(writer, user, _key(ACCOUNT), {"count": 1, "since": "not-a-date"})
    assert route_breaker.tripped(writer, user, ACCOUNT)
    assert route_breaker.state(writer, user, ACCOUNT).readable is False


def test_recording_after_a_corrupt_row_heals_it(writer: Session, user: User) -> None:
    """The next record() (or reset()) overwrites the row with a well-formed one --
    corruption never persists past one call."""
    set_setting(writer, user, _key(ACCOUNT), "not an object")
    route_breaker.record(writer, user, ACCOUNT, route_changed=False, succeeded=True, now=NOW)
    state = route_breaker.state(writer, user, ACCOUNT)
    assert (state.readable, state.count, state.tripped) == (True, 0, False)


def test_a_route_changed_run_after_a_corrupt_row_keeps_it_tripped(
    writer: Session, user: User
) -> None:
    """#191 review N2: a corrupt row reads as tripped. One more wall run must not
    rewrite it as count=1, which would read as clear."""
    set_setting(writer, user, _key(ACCOUNT), "not an object")
    updated = route_breaker.record(
        writer, user, ACCOUNT, route_changed=True, succeeded=False, now=NOW
    )
    assert updated.readable is True
    assert updated.count == 2
    assert route_breaker.tripped(writer, user, ACCOUNT)
