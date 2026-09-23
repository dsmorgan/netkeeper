"""netkeeper.services.heat: persisted heat state, keyed by LinkedIn account (spec 9.7, 9.10).

The core-side half of the seam: this module opens sessions and reads/writes
``settings_kv``; the decay math it calls into (``netkeeper.linkedin.heat``) is
tested standalone in tests/test_linkedin_heat.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import HeatSettings
from netkeeper.db import session_scope
from netkeeper.models import User
from netkeeper.services import heat

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
SETTINGS = HeatSettings(per_block=1.0, half_life_hours=6, skip_threshold=2.5)
ACCOUNT = 1


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def test_a_never_raised_account_reads_cold(writer: Session, user: User) -> None:
    assert heat.read(writer, user, ACCOUNT, now=NOW, settings=SETTINGS) == 0.0


def test_raise_heat_requires_a_writer_session(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as setup:
        user_id = factories.make_user(setup).id
    owner = session.get(User, user_id)
    assert owner is not None
    with pytest.raises(RuntimeError, match="writer session"):
        heat.raise_heat(session, owner, ACCOUNT, now=NOW, settings=SETTINGS)


def test_clear_requires_a_writer_session(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as setup:
        user_id = factories.make_user(setup).id
    owner = session.get(User, user_id)
    assert owner is not None
    with pytest.raises(RuntimeError, match="writer session"):
        heat.clear(session, owner, ACCOUNT, now=NOW)


def test_raise_heat_persists_and_is_read_back_decayed(writer: Session, user: User) -> None:
    heat.raise_heat(writer, user, ACCOUNT, now=NOW, settings=SETTINGS)
    later = NOW + timedelta(hours=SETTINGS.half_life_hours)
    assert heat.read(writer, user, ACCOUNT, now=later, settings=SETTINGS) == pytest.approx(0.5)


def test_reading_twice_with_no_new_event_gives_a_lower_score_the_second_time(
    writer: Session, user: User
) -> None:
    """Spec 9.7's "decay on read": the stored row never changes between these two
    reads, only ``now`` does, and the second read must be strictly smaller."""
    heat.raise_heat(writer, user, ACCOUNT, now=NOW, settings=SETTINGS)
    first = heat.read(writer, user, ACCOUNT, now=NOW + timedelta(hours=1), settings=SETTINGS)
    second = heat.read(writer, user, ACCOUNT, now=NOW + timedelta(hours=2), settings=SETTINGS)
    assert second < first


def test_raise_heat_compounds_from_the_decayed_score(writer: Session, user: User) -> None:
    heat.raise_heat(writer, user, ACCOUNT, now=NOW, settings=SETTINGS)
    later = NOW + timedelta(hours=SETTINGS.half_life_hours)  # first block decayed to 0.5 by now
    heat.raise_heat(writer, user, ACCOUNT, now=later, settings=SETTINGS)
    assert heat.read(writer, user, ACCOUNT, now=later, settings=SETTINGS) == pytest.approx(1.5)


def test_clear_resets_a_warm_account_to_cold(writer: Session, user: User) -> None:
    heat.raise_heat(writer, user, ACCOUNT, now=NOW, settings=SETTINGS)
    heat.raise_heat(writer, user, ACCOUNT, now=NOW, settings=SETTINGS)
    heat.clear(writer, user, ACCOUNT, now=NOW)
    assert heat.read(writer, user, ACCOUNT, now=NOW, settings=SETTINGS) == 0.0
    later = NOW + timedelta(hours=1)
    assert heat.read(writer, user, ACCOUNT, now=later, settings=SETTINGS) == 0.0


def test_should_skip_crosses_the_configured_threshold(writer: Session, user: User) -> None:
    assert not heat.should_skip(writer, user, ACCOUNT, now=NOW, settings=SETTINGS)
    for _ in range(3):
        heat.raise_heat(writer, user, ACCOUNT, now=NOW, settings=SETTINGS)
    assert heat.should_skip(writer, user, ACCOUNT, now=NOW, settings=SETTINGS)


def test_cooldown_multiplier_is_the_floor_when_cold(writer: Session, user: User) -> None:
    assert heat.cooldown_multiplier(writer, user, ACCOUNT, now=NOW, settings=SETTINGS) == 1.0


def test_cooldown_multiplier_rises_after_a_raise(writer: Session, user: User) -> None:
    heat.raise_heat(writer, user, ACCOUNT, now=NOW, settings=SETTINGS)
    assert heat.cooldown_multiplier(writer, user, ACCOUNT, now=NOW, settings=SETTINGS) > 1.0


def test_heat_is_scoped_by_account_id(writer: Session, user: User) -> None:
    """Two accounts under the same user must not share a heat state."""
    heat.raise_heat(writer, user, 1, now=NOW, settings=SETTINGS)
    heat.raise_heat(writer, user, 1, now=NOW, settings=SETTINGS)
    assert heat.read(writer, user, 1, now=NOW, settings=SETTINGS) == pytest.approx(2.0)
    assert heat.read(writer, user, 2, now=NOW, settings=SETTINGS) == 0.0


def test_heat_is_scoped_by_user(writer: Session) -> None:
    owner = factories.make_user(writer)
    other = factories.make_user(writer)
    heat.raise_heat(writer, owner, ACCOUNT, now=NOW, settings=SETTINGS)
    assert heat.read(writer, other, ACCOUNT, now=NOW, settings=SETTINGS) == 0.0
