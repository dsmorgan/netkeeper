import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError, OperationalError, StatementError
from sqlalchemy.orm import Session

from netkeeper.db import (
    SQLITE_BUSY_TIMEOUT_MS,
    database_url,
    is_writer,
    make_engine,
    make_session_factory,
    mark_for_write,
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


# --- writer sessions: BEGIN IMMEDIATE ---------------------------------------


def _record_begins(engine: Engine) -> list[str]:
    """Every BEGIN statement the engine emits from now on, in order."""
    seen: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(*args: Any) -> None:
        statement: str = args[2]
        if statement.startswith("BEGIN"):
            seen.append(statement)

    return seen


def test_writer_session_begins_every_transaction_immediate(engine: Engine) -> None:
    factory = make_session_factory(engine)
    begins = _record_begins(engine)
    with session_scope(factory, write=True) as writer:
        assert is_writer(writer)
        writer.add(User(kind=UserKind.LOCAL))
        writer.commit()  # the mark outlives a commit inside the block
        writer.add(User(kind=UserKind.HOSTED))
    with session_scope(factory) as reader:
        assert not is_writer(reader)
        assert reader.scalar(select(func.count()).select_from(User)) == 2
    assert begins == ["BEGIN IMMEDIATE", "BEGIN IMMEDIATE", "BEGIN"]


def test_mark_for_write_precedes_the_first_statement_and_is_idempotent(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with factory() as session:
        mark_for_write(session)
        mark_for_write(session)  # no-op
        session.execute(select(User))
        mark_for_write(session)  # already marked: still a no-op
    with factory() as late:
        late.execute(select(User))
        with pytest.raises(RuntimeError, match="before the session's first statement"):
            mark_for_write(late)


def test_writer_read_then_write_waits_for_a_concurrent_writer(engine: Engine) -> None:
    """The point of BEGIN IMMEDIATE: A waits at BEGIN while B holds the lock, then sees B's
    row and writes its own. Under plain BEGIN, A would read first and fail at its write."""
    factory = make_session_factory(engine)
    holder = factory()
    mark_for_write(holder)
    holder.add(User(kind=UserKind.LOCAL, display_name="from B"))
    holder.flush()  # B holds the write lock, uncommitted
    read_done = threading.Event()
    failures: list[BaseException] = []

    def a_reads_then_writes() -> None:
        try:
            with session_scope(factory, write=True) as a:
                before = a.scalar(select(func.count()).select_from(User))  # blocks at BEGIN
                read_done.set()
                a.add(User(kind=UserKind.HOSTED, display_name=f"A saw {before}"))
        except BaseException as exc:  # collected for the assertion below
            failures.append(exc)

    thread = threading.Thread(target=a_reads_then_writes)
    thread.start()
    assert not read_done.wait(0.3), "A read before B committed: it did not take the lock first"
    holder.commit()
    holder.close()
    thread.join(SQLITE_BUSY_TIMEOUT_MS / 1000 + 5)
    assert not thread.is_alive()
    assert failures == []
    with factory() as check:
        names = sorted(u.display_name or "" for u in check.scalars(select(User)))
    assert names == ["A saw 1", "from B"]


def test_plain_read_then_write_fails_after_a_concurrent_commit(engine: Engine) -> None:
    """Documented, not wanted: a reader that turns writer after another commit gets
    SQLITE_BUSY_SNAPSHOT at once, and busy_timeout never enters into it. This is why
    session_scope(write=True) exists; the test above is the same sequence as a writer."""
    factory = make_session_factory(engine)
    with factory() as a:
        assert a.scalar(select(func.count()).select_from(User)) == 0  # A's snapshot
        with session_scope(factory, write=True) as b:
            b.add(User(kind=UserKind.LOCAL))  # commits at once: a reader blocks no writer
        a.add(User(kind=UserKind.HOSTED))
        with pytest.raises(OperationalError, match="database is locked"):
            a.flush()


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
