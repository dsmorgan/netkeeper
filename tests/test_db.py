from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from netkeeper.db import (
    SQLITE_BUSY_TIMEOUT_MS,
    database_url,
    make_engine,
    make_session_factory,
    session_scope,
    sqlite_foreign_keys_disabled,
)
from netkeeper.models import SettingKV, User, UserKind, UTCDateTime

# --- database_url -----------------------------------------------------------


def test_database_url_defaults_to_the_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NETKEEPER_DATA", str(tmp_path / "data"))
    assert database_url() == f"sqlite:///{tmp_path / 'data' / 'netkeeper.sqlite3'}"


def test_database_url_takes_an_explicit_data_dir(tmp_path: Path) -> None:
    assert database_url(tmp_path) == f"sqlite:///{tmp_path / 'netkeeper.sqlite3'}"


def test_database_url_env_var_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", "postgresql+psycopg://nk@localhost/nk")
    assert database_url(tmp_path) == "postgresql+psycopg://nk@localhost/nk"


def test_database_url_empty_env_var_is_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", "")
    assert database_url(tmp_path).startswith("sqlite:///")


# --- make_engine ------------------------------------------------------------


def test_make_engine_creates_the_parent_directory_and_sets_pragmas(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested"
    engine = make_engine(database_url(target))
    try:
        assert target.is_dir()
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    finally:
        engine.dispose()


def test_sqlite_connections_wait_for_a_busy_database(engine: Engine) -> None:
    with engine.connect() as connection:
        timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert timeout == SQLITE_BUSY_TIMEOUT_MS == 5000


def test_foreign_keys_are_enforced(session: Session) -> None:
    session.add(SettingKV(user_id=999, key="k", value=1))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.flush()


def test_sqlite_foreign_keys_disabled_brackets_the_block(engine: Engine) -> None:
    with engine.connect() as connection:
        with sqlite_foreign_keys_disabled(connection):
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
            connection.rollback()  # the pragma is ignored inside a transaction
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_ddl_is_transactional(engine: Engine) -> None:
    """The pysqlite takeover in make_engine: a rolled-back CREATE TABLE leaves nothing."""
    with engine.connect() as connection:
        connection.exec_driver_sql("CREATE TABLE scratch (id INTEGER PRIMARY KEY)")
        connection.rollback()
        names = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE name = 'scratch'"
        ).all()
    assert names == []


# --- session_scope ----------------------------------------------------------


def _user_count(engine: Engine) -> int:
    with make_session_factory(engine)() as check:
        return check.scalar(select(func.count()).select_from(User)) or 0


@pytest.fixture
def closed_sessions(monkeypatch: pytest.MonkeyPatch) -> list[Session]:
    seen: list[Session] = []
    original = Session.close

    def spy(self: Session) -> None:
        seen.append(self)
        original(self)

    monkeypatch.setattr(Session, "close", spy)
    return seen


def test_session_scope_commits_and_closes(engine: Engine, closed_sessions: list[Session]) -> None:
    with session_scope(make_session_factory(engine)) as session:
        session.add(User(kind=UserKind.LOCAL))
    assert closed_sessions == [session]
    assert _user_count(engine) == 1


def test_session_scope_rolls_back_reraises_and_closes(
    engine: Engine, closed_sessions: list[Session]
) -> None:
    factory = make_session_factory(engine)
    with pytest.raises(RuntimeError, match="boom"), session_scope(factory) as session:
        session.add(User(kind=UserKind.LOCAL))
        session.flush()
        raise RuntimeError("boom")
    assert closed_sessions == [session]
    assert _user_count(engine) == 0


def test_session_scope_returns_readable_objects_after_commit(engine: Engine) -> None:
    with session_scope(make_session_factory(engine)) as session:
        user = User(kind=UserKind.LOCAL)
        session.add(user)
    assert user.id == 1
    assert user.timezone == "UTC"


# --- UTCDateTime ------------------------------------------------------------

EASTERN = timezone(timedelta(hours=-4))


def test_utcdatetime_round_trips_aware_utc(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        user = User(kind=UserKind.LOCAL, created_at=datetime(2026, 9, 20, 8, 30, tzinfo=EASTERN))
        session.add(user)
    with factory() as check:
        loaded = check.get_one(User, user.id)
        assert loaded.created_at == datetime(2026, 9, 20, 12, 30, tzinfo=UTC)
        assert loaded.created_at.tzinfo is UTC
    with engine.connect() as connection:
        stored = connection.exec_driver_sql("SELECT created_at FROM users").scalar()
    assert str(stored).startswith("2026-09-20 12:30:00")  # naive UTC on disk


def test_utcdatetime_rejects_naive_on_flush(session: Session) -> None:
    session.add(User(kind=UserKind.LOCAL, created_at=datetime(2026, 9, 20, 8, 30)))
    with pytest.raises(StatementError) as info:
        session.flush()
    assert isinstance(info.value.orig, ValueError)


def test_utcdatetime_rejects_naive_directly() -> None:
    with pytest.raises(ValueError, match="naive"):
        UTCDateTime().process_bind_param(datetime(2026, 9, 20), sqlite.dialect())


def test_utcdatetime_passes_none_through() -> None:
    column = UTCDateTime()
    assert column.process_bind_param(None, sqlite.dialect()) is None
    assert column.process_result_value(None, sqlite.dialect()) is None


def test_utcdatetime_result_is_aware_even_if_the_driver_returns_aware() -> None:
    aware = datetime(2026, 9, 20, 8, 30, tzinfo=EASTERN)
    result = UTCDateTime().process_result_value(aware, sqlite.dialect())
    assert result == datetime(2026, 9, 20, 12, 30, tzinfo=UTC)
    assert result is not None and result.tzinfo is UTC
