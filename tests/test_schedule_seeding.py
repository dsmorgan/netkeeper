"""Seeding a job kind that has no due time, at ``serve`` start and at arm (#327).

A schedule established before a kind existed has no due time for that kind, and a
kind with no due time never fires. ``serve``'s start (``build_scheduler``) and arming
(``seed_served_schedule``) give each missing kind its normal first due time, and never
move one that is already set. The LinkedIn inbox poll has no runner, so ``serve`` does
not schedule it, and arming does not seed it either.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.models import User
from netkeeper.services import scheduler
from netkeeper.services.linkedin_accounts import arm_scheduled_runs, ensure_account
from netkeeper.services.scheduled_runs import _local_accounts, seed_served_schedule
from netkeeper.services.scheduler import (
    DEFAULT_SCHEDULES,
    MIN_JOB_KIND_GAP,
    SERVED_SCHEDULES,
    JobKind,
    JobSchedule,
    stored_due,
)
from netkeeper.services.users import ensure_local_user

#: A Wednesday, 14:00 in New York (the default zone), inside the default active hours.
NOW = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
#: The restart, three minutes later: before any due time set at ``NOW`` (the full
#: sync's first-setup fire is at least five minutes out), so none is a catch-up.
LATER = NOW + timedelta(minutes=3)
SETTINGS = Settings()

FOUR = dict(DEFAULT_SCHEDULES)
THREE: dict[JobKind, JobSchedule] = {
    kind: schedule for kind, schedule in FOUR.items() if kind is not JobKind.ENRICH
}


def _local(session: Session) -> tuple[User, int]:
    user = ensure_local_user(session, settings=SETTINGS)
    return user, ensure_account(session, user).id


def _dues(factory: sessionmaker[Session]) -> dict[JobKind, datetime | None]:
    with session_scope(factory) as session:
        user, account = _local(session)
        return {kind: stored_due(session, user, account, kind) for kind in JobKind}


def _armed_with(
    factory: sessionmaker[Session], schedules: Mapping[JobKind, JobSchedule]
) -> dict[JobKind, datetime | None]:
    """Arm, and establish ``schedules`` as ``serve`` did before the rest existed."""
    with session_scope(factory, write=True) as session:
        user, account = _local(session)
        arm_scheduled_runs(session, user, now=NOW)
        scheduler.sync_account_schedule(
            session,
            user,
            account,
            now=NOW,
            schedules=schedules,
            rng=random.Random(1),
            tz=user.timezone,
        )
    return _dues(factory)


def _clock(at: datetime) -> Callable[[], datetime]:
    return lambda: at


def test_a_kind_added_after_arming_is_seeded_on_restart(
    session_factory: sessionmaker[Session],
) -> None:
    """Arm with three kinds, add the fourth, restart: the fourth gets its first due time
    (one interval out from the restart) and the other three keep theirs exactly."""
    before = _armed_with(session_factory, THREE)
    assert before[JobKind.ENRICH] is None
    assert all(before[kind] is not None for kind in THREE)

    scheduler.build_scheduler(
        session_factory,
        _local_accounts(session_factory),
        schedules=FOUR,
        rng=random.Random(2),
        clock=_clock(LATER),
    )  # built, never started: establishing the schedule is the restart's whole effect

    after = _dues(session_factory)
    assert after[JobKind.ENRICH] == LATER + FOUR[JobKind.ENRICH].interval
    assert {kind: after[kind] for kind in THREE} == {kind: before[kind] for kind in THREE}


def test_arming_seeds_a_missing_kind_without_a_restart(
    session_factory: sessionmaker[Session],
) -> None:
    before = _armed_with(session_factory, THREE)

    with session_scope(session_factory, write=True) as session:
        user, account = _local(session)
        seeded = scheduler.seed_missing_kinds(
            session,
            user,
            account,
            now=LATER,
            schedules=FOUR,
            rng=random.Random(3),
            tz=user.timezone,
        )

    assert list(seeded) == [JobKind.ENRICH]
    assert seeded[JobKind.ENRICH].due == LATER + FOUR[JobKind.ENRICH].interval
    after = _dues(session_factory)
    assert after[JobKind.ENRICH] == seeded[JobKind.ENRICH].due
    assert {kind: after[kind] for kind in THREE} == {kind: before[kind] for kind in THREE}


def test_seeding_twice_changes_nothing(session_factory: sessionmaker[Session]) -> None:
    _armed_with(session_factory, THREE)
    with session_scope(session_factory, write=True) as session:
        user, account = _local(session)
        first = scheduler.seed_missing_kinds(
            session,
            user,
            account,
            now=LATER,
            schedules=FOUR,
            rng=random.Random(3),
            tz=user.timezone,
        )
    once = _dues(session_factory)
    with session_scope(session_factory, write=True) as session:
        user, account = _local(session)
        again = scheduler.seed_missing_kinds(
            session,
            user,
            account,
            now=LATER + timedelta(days=2),
            schedules=FOUR,
            rng=random.Random(4),
            tz=user.timezone,
        )
    assert list(first) == [JobKind.ENRICH] and again == {}
    assert _dues(session_factory) == once


def test_a_seeded_kind_steps_clear_of_an_existing_due_time_and_never_moves_it(
    session_factory: sessionmaker[Session],
) -> None:
    """The interleave gap (spec 9.5) moves the new due time later, never the old one."""
    with session_scope(session_factory, write=True) as session:
        user, account = _local(session)
        scheduler.establish_schedule(
            session,
            user,
            account,
            JobKind.CONNECTIONS_INCREMENTAL,
            now=NOW,
            schedule=JobSchedule(JobKind.CONNECTIONS_INCREMENTAL, timedelta(hours=3)),
            rng=random.Random(1),
            tz=user.timezone,
        )
        existing = stored_due(session, user, account, JobKind.CONNECTIONS_INCREMENTAL)
        seeded = scheduler.seed_missing_kinds(
            session,
            user,
            account,
            now=NOW + timedelta(seconds=30),
            schedules={JobKind.ENRICH: JobSchedule(JobKind.ENRICH, timedelta(hours=3))},
            rng=random.Random(1),
            tz=user.timezone,
        )
        assert stored_due(session, user, account, JobKind.CONNECTIONS_INCREMENTAL) == existing
        new = stored_due(session, user, account, JobKind.ENRICH)
    assert existing is not None and new is not None
    assert new == seeded[JobKind.ENRICH].due
    assert new - existing >= MIN_JOB_KIND_GAP


def test_arming_seeds_every_served_kind_and_not_the_inbox_poll(
    session_factory: sessionmaker[Session],
) -> None:
    """The inbox poll has no runner: ``serve`` does not schedule it, so arming leaves it
    without a due time rather than give a job that does nothing one."""
    with session_scope(session_factory, write=True) as session:
        user, _ = _local(session)
        arm_scheduled_runs(session, user, now=NOW)
        seeded = seed_served_schedule(session, user, SETTINGS.linkedin, now=NOW)

    assert set(seeded) == set(SERVED_SCHEDULES)
    assert JobKind.INBOX not in SERVED_SCHEDULES
    dues = _dues(session_factory)
    assert dues[JobKind.INBOX] is None
    assert all(dues[kind] is not None for kind in SERVED_SCHEDULES)


def test_arming_keeps_a_due_time_already_set(session_factory: sessionmaker[Session]) -> None:
    served_but_enrich: dict[JobKind, JobSchedule] = {
        k: s for k, s in SERVED_SCHEDULES.items() if k is not JobKind.ENRICH
    }
    before = _armed_with(session_factory, served_but_enrich)
    with session_scope(session_factory, write=True) as session:
        user, _ = _local(session)
        seeded = seed_served_schedule(session, user, SETTINGS.linkedin, now=LATER)
    after = _dues(session_factory)
    assert seeded == [JobKind.ENRICH]
    assert after[JobKind.ENRICH] == LATER + SERVED_SCHEDULES[JobKind.ENRICH].interval
    assert {k: after[k] for k in served_but_enrich} == {k: before[k] for k in served_but_enrich}
    assert after[JobKind.INBOX] is None
