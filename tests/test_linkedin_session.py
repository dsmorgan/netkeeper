"""netkeeper.services.linkedin_session (spec 8.4, 9.7, 9.10): the session flag
`classify()`'s Checkpoint and LoggedOut outcomes lead to, held in `settings_kv`
because `linkedin_account` does not exist yet and every runtime flag already
lives there.

This is the "core" side of the extractor boundary that `classify()` itself
(tests/test_classify.py) deliberately stays on the other side of: everything
here opens a session, which is exactly why it is not under `linkedin/`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import mark_for_write
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import JsonValue
from netkeeper.services.linkedin_session import SESSION_FLAG_KEY, flag_session, session_flag
from netkeeper.services.settings_kv import get_setting, set_setting

CHECKPOINT_URL = "https://www.linkedin.com/checkpoint/challenge/?ctx=abc123"
LOGIN_URL = "https://www.linkedin.com/uas/login?session_redirect=abc"


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A guarded writer session, as ``flag_session`` needs. Uncommitted work is
    discarded, same pattern as test_import_runs.py's ``writer`` fixture."""
    session = session_factory()
    mark_for_write(session)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_session_flag_is_none_before_anything_is_flagged(session: Session) -> None:
    user = factories.make_user(session)
    assert session_flag(session, user) is None


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(Outcome.CHECKPOINT, id="checkpoint"),
        pytest.param(Outcome.LOGGED_OUT, id="logged_out"),
    ],
)
def test_flag_session_records_the_two_outcomes_spec_9_7_says_to_flag(
    writer: Session, outcome: Outcome
) -> None:
    user = factories.make_user(writer)
    flagged = flag_session(writer, user, outcome, url=CHECKPOINT_URL)
    assert flagged.outcome is outcome
    assert flagged.url == CHECKPOINT_URL
    assert isinstance(flagged.flagged_at, datetime)
    assert session_flag(writer, user) == flagged


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(Outcome.OK, id="ok"),
        pytest.param(Outcome.THROTTLED, id="throttled"),
        pytest.param(Outcome.NOT_FOUND, id="not_found"),
        pytest.param(Outcome.ROUTE_CHANGED, id="route_changed"),
    ],
)
def test_flag_session_refuses_every_outcome_spec_9_7_does_not_ask_to_flag(
    writer: Session, outcome: Outcome
) -> None:
    user = factories.make_user(writer)
    with pytest.raises(ValueError, match="only"):
        flag_session(writer, user, outcome, url=CHECKPOINT_URL)
    assert session_flag(writer, user) is None


def test_flag_session_needs_a_writer_session(session: Session) -> None:
    """CLAUDE.md: a session that writes is marked; mirrors crm/tags.py's own guard
    (``_require_writer``) for the same reason -- fail at once, not with a stale
    "database is locked" on the first statement."""
    user = factories.make_user(session)
    with pytest.raises(RuntimeError, match="writer session"):
        flag_session(session, user, Outcome.CHECKPOINT, url=CHECKPOINT_URL)


def test_flag_session_overwrites_the_previous_flag(writer: Session) -> None:
    user = factories.make_user(writer)
    flag_session(writer, user, Outcome.CHECKPOINT, url=CHECKPOINT_URL)
    second = flag_session(writer, user, Outcome.LOGGED_OUT, url=LOGIN_URL)
    current = session_flag(writer, user)
    assert current == second
    assert current is not None
    assert current.outcome is Outcome.LOGGED_OUT
    assert current.url == LOGIN_URL


def test_session_flag_is_per_user(writer: Session) -> None:
    alice = factories.make_user(writer)
    bob = factories.make_user(writer)
    flag_session(writer, alice, Outcome.CHECKPOINT, url=CHECKPOINT_URL)
    assert session_flag(writer, alice) is not None
    assert session_flag(writer, bob) is None


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(
            {"outcome": "not_a_real_outcome", "url": "x", "flagged_at": "2024-01-01T00:00:00"},
            id="unknown_outcome_value",
        ),
        pytest.param(
            {"outcome": "checkpoint", "flagged_at": "2024-01-01T00:00:00"}, id="missing_url"
        ),
        pytest.param(
            {"outcome": "checkpoint", "url": "x", "flagged_at": "not-a-timestamp"},
            id="bad_timestamp",
        ),
        pytest.param("not even a dict", id="not_an_object"),
        pytest.param(None, id="none"),
    ],
)
def test_session_flag_ignores_a_value_it_cannot_make_sense_of(
    writer: Session, stored: JsonValue
) -> None:
    """An unreadable stored value reads as "not flagged", the same defensiveness
    crm/tags.py's ``_seeded_names`` uses for its own settings_kv value: a flag
    is advisory, and a session that is genuinely still broken raises it again
    the next time a job hits it."""
    user = factories.make_user(writer)
    set_setting(writer, user, SESSION_FLAG_KEY, stored)
    assert session_flag(writer, user) is None


def test_session_flag_round_trips_through_settings_kv_as_plain_json(writer: Session) -> None:
    """The stored shape is a plain JSON object, not a pickled dataclass -- so a
    banner in the UI (spec 9.7) can read it without importing this module."""
    user = factories.make_user(writer)
    flag_session(writer, user, Outcome.CHECKPOINT, url=CHECKPOINT_URL)
    raw = get_setting(writer, user, SESSION_FLAG_KEY)
    assert isinstance(raw, dict)
    assert raw["outcome"] == "checkpoint"
    assert raw["url"] == CHECKPOINT_URL
    assert isinstance(raw["flagged_at"], str)
