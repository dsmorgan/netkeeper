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
from netkeeper.models import SyncRunKind, User
from netkeeper.services import route_breaker
from netkeeper.services.settings_kv import get_setting, set_setting

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


# --- #199: the answer-lost limit, a streak per connections kind -------------------


FULL = SyncRunKind.CONNECTIONS_FULL
INCREMENTAL = SyncRunKind.CONNECTIONS_INCREMENTAL


def _lost_key(account_id: int, kind: SyncRunKind = FULL) -> str:
    return f"linkedin.answer_lost_breaker.{kind.value}.{account_id}"


def _answer_lost(
    writer: Session, user: User, account_id: int = ACCOUNT, kind: SyncRunKind = FULL
) -> None:
    route_breaker.record_answer_lost(
        writer, user, account_id, kind=kind, answer_lost=True, clean_end=False, now=NOW
    )


def _clean(writer: Session, user: User, kind: SyncRunKind) -> None:
    route_breaker.record_answer_lost(
        writer, user, ACCOUNT, kind=kind, answer_lost=False, clean_end=True, now=NOW
    )


def test_the_answer_lost_threshold_is_three() -> None:
    assert route_breaker.ANSWER_LOST_THRESHOLD == 3


def test_the_answer_lost_kinds_are_the_two_connections_kinds() -> None:
    assert route_breaker.ANSWER_LOST_KINDS == (
        SyncRunKind.CONNECTIONS_FULL,
        SyncRunKind.CONNECTIONS_INCREMENTAL,
    )


def test_enrichment_has_no_answer_lost_streak(writer: Session, user: User) -> None:
    with pytest.raises(ValueError, match="connections runs only"):
        route_breaker.record_answer_lost(
            writer,
            user,
            ACCOUNT,
            kind=SyncRunKind.ENRICH,
            answer_lost=True,
            clean_end=False,
            now=NOW,
        )


def test_a_never_recorded_account_has_no_answer_lost_streak(writer: Session, user: User) -> None:
    for kind in (FULL, INCREMENTAL):
        state = route_breaker.answer_lost_state(writer, user, ACCOUNT, kind)
        assert (state.count, state.since, state.tripped, state.threshold) == (0, None, False, 3)
    assert not route_breaker.answer_lost_tripped(writer, user, ACCOUNT)


@pytest.mark.parametrize("kind", [FULL, INCREMENTAL])
def test_three_answer_lost_runs_of_one_kind_trip_the_limit_and_two_do_not(
    writer: Session, user: User, kind: SyncRunKind
) -> None:
    _answer_lost(writer, user, kind=kind)
    later = NOW + timedelta(days=1)
    route_breaker.record_answer_lost(
        writer, user, ACCOUNT, kind=kind, answer_lost=True, clean_end=False, now=later
    )
    state = route_breaker.answer_lost_state(writer, user, ACCOUNT, kind)
    assert (state.count, state.since, state.tripped) == (2, NOW, False)
    assert not route_breaker.answer_lost_tripped(writer, user, ACCOUNT)
    _answer_lost(writer, user, kind=kind)
    assert route_breaker.answer_lost_state(writer, user, ACCOUNT, kind).count == 3
    assert route_breaker.answer_lost_tripped(writer, user, ACCOUNT)


def test_the_kinds_count_separately(writer: Session, user: User) -> None:
    """Two full and two incremental answer_lost runs are two of each, not four."""
    for kind in (FULL, INCREMENTAL, FULL, INCREMENTAL):
        _answer_lost(writer, user, kind=kind)
    states = route_breaker.answer_lost_states(writer, user, ACCOUNT)
    assert [(k, s.count) for k, s in states.items()] == [(FULL, 2), (INCREMENTAL, 2)]
    assert not route_breaker.answer_lost_tripped(writer, user, ACCOUNT)


def test_a_clean_incremental_does_not_clear_the_full_syncs_streak(
    writer: Session, user: User
) -> None:
    """#199 review, M2: losses that bite only the weekly full sync's long read must
    trip the limit even while every daily incremental completes."""
    for _ in range(route_breaker.ANSWER_LOST_THRESHOLD):
        _answer_lost(writer, user, kind=FULL)
        _clean(writer, user, INCREMENTAL)
    assert route_breaker.answer_lost_state(writer, user, ACCOUNT, FULL).count == 3
    assert route_breaker.answer_lost_tripped(writer, user, ACCOUNT)


def test_a_clean_run_clears_only_its_own_kind(writer: Session, user: User) -> None:
    for _ in range(route_breaker.ANSWER_LOST_THRESHOLD):
        _answer_lost(writer, user, kind=FULL)
        _answer_lost(writer, user, kind=INCREMENTAL)
    _clean(writer, user, FULL)
    assert route_breaker.answer_lost_state(writer, user, ACCOUNT, FULL).count == 0
    assert route_breaker.answer_lost_state(writer, user, ACCOUNT, INCREMENTAL).count == 3
    assert route_breaker.answer_lost_tripped(writer, user, ACCOUNT)
    _clean(writer, user, INCREMENTAL)
    updated = route_breaker.answer_lost_state(writer, user, ACCOUNT, INCREMENTAL)
    assert (updated.count, updated.since, updated.tripped) == (0, None, False)
    assert not route_breaker.answer_lost_tripped(writer, user, ACCOUNT)


def test_any_other_ending_leaves_the_answer_lost_streak_where_it_was(
    writer: Session, user: User
) -> None:
    _answer_lost(writer, user)
    _answer_lost(writer, user)
    before = route_breaker.answer_lost_state(writer, user, ACCOUNT, FULL)
    after = route_breaker.record_answer_lost(
        writer, user, ACCOUNT, kind=FULL, answer_lost=False, clean_end=False, now=NOW
    )
    assert after == before and before.count == 2


def test_a_run_cannot_both_lose_an_answer_and_end_cleanly(writer: Session, user: User) -> None:
    with pytest.raises(ValueError, match="both"):
        route_breaker.record_answer_lost(
            writer, user, ACCOUNT, kind=FULL, answer_lost=True, clean_end=True, now=NOW
        )


def test_record_answer_lost_requires_a_writer_session(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as setup:
        user_id = factories.make_user(setup).id
    owner = session.get(User, user_id)
    assert owner is not None
    with pytest.raises(RuntimeError, match="writer session"):
        route_breaker.record_answer_lost(
            session, owner, ACCOUNT, kind=FULL, answer_lost=True, clean_end=False, now=NOW
        )


def test_the_streaks_never_feed_each_other(writer: Session, user: User) -> None:
    """Separate rows: route_changed runs never move an answer-lost count, and
    answer_lost runs never move the route-changed one."""
    for _ in range(route_breaker.ANSWER_LOST_THRESHOLD):
        _answer_lost(writer, user)
    assert route_breaker.state(writer, user, ACCOUNT).count == 0
    route_breaker.record(writer, user, ACCOUNT, route_changed=True, succeeded=False, now=NOW)
    assert route_breaker.answer_lost_state(writer, user, ACCOUNT, FULL).count == 3
    assert not route_breaker.tripped(writer, user, ACCOUNT)
    assert route_breaker.answer_lost_tripped(writer, user, ACCOUNT)


def test_reset_clears_every_answer_lost_streak_too(writer: Session, user: User) -> None:
    """The same reset path as the breaker: one `reset-breaker` clears them all."""
    for _ in range(route_breaker.ANSWER_LOST_THRESHOLD):
        _answer_lost(writer, user, kind=FULL)
        _answer_lost(writer, user, kind=INCREMENTAL)
    for _ in range(route_breaker.THRESHOLD):
        route_breaker.record(writer, user, ACCOUNT, route_changed=True, succeeded=False, now=NOW)
    route_breaker.reset(writer, user, ACCOUNT)
    assert not route_breaker.answer_lost_tripped(writer, user, ACCOUNT)
    for kind in (FULL, INCREMENTAL):
        assert route_breaker.answer_lost_state(writer, user, ACCOUNT, kind).count == 0
    assert not route_breaker.tripped(writer, user, ACCOUNT)


def test_the_answer_lost_limit_is_scoped_by_account_id(writer: Session, user: User) -> None:
    for _ in range(route_breaker.ANSWER_LOST_THRESHOLD):
        _answer_lost(writer, user, 1)
    assert route_breaker.answer_lost_tripped(writer, user, 1)
    assert not route_breaker.answer_lost_tripped(writer, user, 2)


@pytest.mark.parametrize("kind", [FULL, INCREMENTAL])
def test_a_corrupt_answer_lost_row_reads_as_tripped_and_heals(
    writer: Session, user: User, kind: SyncRunKind
) -> None:
    set_setting(writer, user, _lost_key(ACCOUNT, kind), "not an object")
    state = route_breaker.answer_lost_state(writer, user, ACCOUNT, kind)
    assert (state.readable, state.tripped) == (False, True)
    assert route_breaker.answer_lost_tripped(writer, user, ACCOUNT)
    # One more answer_lost run keeps it tripped rather than restarting at 1.
    _answer_lost(writer, user, kind=kind)
    updated = route_breaker.answer_lost_state(writer, user, ACCOUNT, kind)
    assert (updated.readable, updated.count, updated.tripped) == (True, 3, True)
    _clean(writer, user, kind)
    assert not route_breaker.answer_lost_tripped(writer, user, ACCOUNT)


def test_any_other_ending_leaves_a_corrupt_answer_lost_row_corrupt(
    writer: Session, user: User
) -> None:
    """#199 review, L4: a budget stop or a route change writes nothing, so a corrupt
    row stays corrupt -- and so still reads as tripped."""
    set_setting(writer, user, _lost_key(ACCOUNT), "not an object")
    route_breaker.record_answer_lost(
        writer, user, ACCOUNT, kind=FULL, answer_lost=False, clean_end=False, now=NOW
    )
    state = route_breaker.answer_lost_state(writer, user, ACCOUNT, FULL)
    assert (state.readable, state.tripped) == (False, True)


# --- a negative count is corrupt, in both kinds of row (#199 review, nit) ----------


def test_a_negative_route_changed_count_reads_as_tripped(writer: Session, user: User) -> None:
    set_setting(writer, user, _key(ACCOUNT), {"count": -5, "since": None})
    state = route_breaker.state(writer, user, ACCOUNT)
    assert (state.readable, state.tripped) == (False, True)


def test_a_negative_answer_lost_count_reads_as_tripped(writer: Session, user: User) -> None:
    set_setting(writer, user, _lost_key(ACCOUNT, INCREMENTAL), {"count": -1, "since": None})
    state = route_breaker.answer_lost_state(writer, user, ACCOUNT, INCREMENTAL)
    assert (state.readable, state.tripped) == (False, True)
    assert route_breaker.answer_lost_tripped(writer, user, ACCOUNT)


# --- #424: the Contact info breaker, enrichment's own streak --------------------------


def _info_key(account_id: int) -> str:
    return f"linkedin.contact_info_breaker.{account_id}"


def _info_lost(writer: Session, user: User, account_id: int = ACCOUNT, now: datetime = NOW) -> None:
    route_breaker.record_contact_info(
        writer, user, account_id, answer_lost=True, clean_end=False, now=now
    )


def _info_read(writer: Session, user: User) -> None:
    route_breaker.record_contact_info(
        writer, user, ACCOUNT, answer_lost=False, clean_end=True, now=NOW
    )


def test_the_contact_info_threshold_is_three() -> None:
    assert route_breaker.CONTACT_INFO_THRESHOLD == 3


def test_a_never_recorded_account_has_no_contact_info_streak(writer: Session, user: User) -> None:
    state = route_breaker.contact_info_state(writer, user, ACCOUNT)
    assert (state.count, state.since, state.tripped, state.threshold) == (0, None, False, 3)
    assert not route_breaker.contact_info_tripped(writer, user, ACCOUNT)


def test_three_answer_lost_enrichment_runs_trip_the_breaker_and_two_do_not(
    writer: Session, user: User
) -> None:
    _info_lost(writer, user)
    _info_lost(writer, user, now=NOW + timedelta(hours=3))
    state = route_breaker.contact_info_state(writer, user, ACCOUNT)
    assert (state.count, state.since, state.tripped) == (2, NOW, False)
    assert not route_breaker.contact_info_tripped(writer, user, ACCOUNT)
    _info_lost(writer, user, now=NOW + timedelta(hours=6))
    state = route_breaker.contact_info_state(writer, user, ACCOUNT)
    assert (state.count, state.since, state.tripped) == (3, NOW, True)
    assert route_breaker.contact_info_tripped(writer, user, ACCOUNT)
    # The stored row, as it is written.
    assert get_setting(writer, user, _info_key(ACCOUNT)) == {
        "count": 3,
        "since": NOW.isoformat(),
    }


def test_a_run_that_reads_contact_info_again_clears_the_breaker(
    writer: Session, user: User
) -> None:
    for _ in range(4):
        _info_lost(writer, user)
    cleared = route_breaker.record_contact_info(
        writer, user, ACCOUNT, answer_lost=False, clean_end=True, now=NOW
    )
    assert (cleared.count, cleared.since, cleared.tripped) == (0, None, False)
    assert not route_breaker.contact_info_tripped(writer, user, ACCOUNT)
    # The next loss starts a new streak at one, with a new since.
    later = NOW + timedelta(days=1)
    _info_lost(writer, user, now=later)
    state = route_breaker.contact_info_state(writer, user, ACCOUNT)
    assert (state.count, state.since) == (1, later)


def test_any_other_enrichment_ending_leaves_the_streak_where_it_was(
    writer: Session, user: User
) -> None:
    _info_lost(writer, user)
    _info_lost(writer, user)
    before = route_breaker.contact_info_state(writer, user, ACCOUNT)
    after = route_breaker.record_contact_info(
        writer, user, ACCOUNT, answer_lost=False, clean_end=False, now=NOW + timedelta(days=1)
    )
    assert after == before and before.count == 2
    assert route_breaker.contact_info_state(writer, user, ACCOUNT) == before


def test_an_enrichment_run_cannot_both_lose_too_many_and_end_cleanly(
    writer: Session, user: User
) -> None:
    with pytest.raises(ValueError, match="both"):
        route_breaker.record_contact_info(
            writer, user, ACCOUNT, answer_lost=True, clean_end=True, now=NOW
        )


def test_record_contact_info_requires_a_writer_session(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as setup:
        user_id = factories.make_user(setup).id
    owner = session.get(User, user_id)
    assert owner is not None
    with pytest.raises(RuntimeError, match="writer session"):
        route_breaker.record_contact_info(
            session, owner, ACCOUNT, answer_lost=True, clean_end=False, now=NOW
        )


def test_the_contact_info_breaker_and_the_connections_streaks_never_feed_each_other(
    writer: Session, user: User
) -> None:
    for _ in range(route_breaker.CONTACT_INFO_THRESHOLD):
        _info_lost(writer, user)
    assert not route_breaker.tripped(writer, user, ACCOUNT)
    assert not route_breaker.answer_lost_tripped(writer, user, ACCOUNT)
    for _ in range(route_breaker.ANSWER_LOST_THRESHOLD):
        _answer_lost(writer, user, kind=FULL)
    for _ in range(route_breaker.THRESHOLD):
        route_breaker.record(writer, user, ACCOUNT, route_changed=True, succeeded=False, now=NOW)
    # A clean connections run of each kind clears neither the breaker nor this.
    _clean(writer, user, FULL)
    route_breaker.record(writer, user, ACCOUNT, route_changed=False, succeeded=True, now=NOW)
    assert route_breaker.contact_info_state(writer, user, ACCOUNT).count == 3
    # And clearing this clears none of theirs.
    _answer_lost(writer, user, kind=INCREMENTAL)
    _answer_lost(writer, user, kind=INCREMENTAL)
    _answer_lost(writer, user, kind=INCREMENTAL)
    _info_read(writer, user)
    assert route_breaker.answer_lost_tripped(writer, user, ACCOUNT)


def test_reset_clears_the_contact_info_breaker_too(writer: Session, user: User) -> None:
    for _ in range(route_breaker.CONTACT_INFO_THRESHOLD):
        _info_lost(writer, user)
    route_breaker.reset(writer, user, ACCOUNT)
    state = route_breaker.contact_info_state(writer, user, ACCOUNT)
    assert (state.count, state.since, state.tripped) == (0, None, False)


def test_the_contact_info_breaker_is_scoped_by_account_id(writer: Session, user: User) -> None:
    for _ in range(route_breaker.CONTACT_INFO_THRESHOLD):
        _info_lost(writer, user, 1)
    assert route_breaker.contact_info_tripped(writer, user, 1)
    assert not route_breaker.contact_info_tripped(writer, user, 2)


@pytest.mark.parametrize("raw", ["not an object", {"count": -1, "since": None}, {"since": None}])
def test_a_corrupt_contact_info_row_reads_as_tripped_and_heals(
    writer: Session, user: User, raw: object
) -> None:
    set_setting(writer, user, _info_key(ACCOUNT), raw)  # type: ignore[arg-type]
    state = route_breaker.contact_info_state(writer, user, ACCOUNT)
    assert (state.readable, state.tripped) == (False, True)
    assert route_breaker.contact_info_tripped(writer, user, ACCOUNT)
    # Another ending writes nothing, so it stays corrupt (and tripped).
    route_breaker.record_contact_info(
        writer, user, ACCOUNT, answer_lost=False, clean_end=False, now=NOW
    )
    assert not route_breaker.contact_info_state(writer, user, ACCOUNT).readable
    # One more answer_lost run keeps it tripped rather than restarting at 1.
    _info_lost(writer, user)
    updated = route_breaker.contact_info_state(writer, user, ACCOUNT)
    assert (updated.readable, updated.count, updated.tripped) == (True, 3, True)
    _info_read(writer, user)
    assert not route_breaker.contact_info_tripped(writer, user, ACCOUNT)
