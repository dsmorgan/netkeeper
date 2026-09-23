"""netkeeper.services.linkedin_accounts: one account per user, keyed like the budgets (ADR 0005)."""

from __future__ import annotations

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.models import LinkedInAccount
from netkeeper.scoping import scoped
from netkeeper.services.linkedin_accounts import (
    LEGACY_ACCOUNT_ID,
    account_id_for,
    ensure_account,
    find_account,
)


def test_the_legacy_account_id_is_one() -> None:
    """Every counter written before the table existed is under account 1 (migration 0011)."""
    assert LEGACY_ACCOUNT_ID == 1


def test_ensure_creates_once_and_then_finds(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        assert find_account(session, user) is None
        first = ensure_account(session, user)
        again = ensure_account(session, user)
        assert first.id == again.id
        assert first.label == "default"
        assert len(session.scalars(scoped(user, LinkedInAccount)).all()) == 1


def test_each_user_has_their_own(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory, write=True) as session:
        one, two = factories.make_user(session), factories.make_user(session)
        a, b = ensure_account(session, one), ensure_account(session, two)
        assert a.id != b.id
        assert (a.user_id, b.user_id) == (one.id, two.id)
        assert find_account(session, one) is a
        assert account_id_for(session, two) == b.id


def test_a_reader_gets_the_legacy_id_until_the_row_exists(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        first = factories.make_user(session)
        second = factories.make_user(session)
        ensure_account(session, first)  # takes id 1
        second_id = second.id
    with session_scope(session_factory) as reader:
        user = reader.get(type(second), second_id)
        assert user is not None
        assert account_id_for(reader, user) == LEGACY_ACCOUNT_ID
        with pytest.raises(RuntimeError, match="writer"):
            ensure_account(reader, user)
