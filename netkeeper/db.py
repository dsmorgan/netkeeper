"""Engine and session plumbing (spec section 15).

Nothing here is created at import time: callers build an engine from a URL when
they need one and pass it around explicitly.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from netkeeper import paths

DATABASE_URL_ENV = "NETKEEPER_DATABASE_URL"
DATABASE_FILENAME = "netkeeper.sqlite3"

log = logging.getLogger(__name__)


def database_url(data_dir: Path | None = None) -> str:
    """Return the database URL.

    ``$NETKEEPER_DATABASE_URL`` wins when set. Otherwise the database is
    ``netkeeper.sqlite3`` under ``data_dir``, defaulting to :func:`paths.data_dir`.
    """
    override = os.environ.get(DATABASE_URL_ENV)
    if override:
        return override
    base = paths.data_dir() if data_dir is None else data_dir
    return f"sqlite:///{base / DATABASE_FILENAME}"


def make_engine(url: str) -> Engine:
    """Create an engine for ``url``.

    For SQLite this also creates the database file's parent directory, turns on WAL
    and foreign-key enforcement on every connection, and takes over transaction
    control from pysqlite so that DDL is transactional and savepoints work.
    """
    parsed = make_url(url)
    log.debug("creating engine for %s", parsed.render_as_string(hide_password=True))
    engine = create_engine(parsed)
    if parsed.get_backend_name() == "sqlite":
        _ensure_sqlite_parent(parsed.database)
        event.listen(engine, "connect", _sqlite_on_connect)
        event.listen(engine, "begin", _sqlite_on_begin)
    return engine


def _ensure_sqlite_parent(database: str | None) -> None:
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)


def _sqlite_on_connect(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    if isinstance(dbapi_connection, sqlite3.Connection):
        # pysqlite otherwise emits BEGIN only before DML, which leaves DDL outside
        # transactions and breaks SAVEPOINT. We emit BEGIN ourselves in _sqlite_on_begin.
        dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _sqlite_on_begin(connection: Connection) -> None:
    connection.exec_driver_sql("BEGIN")


@contextmanager
def sqlite_foreign_keys_disabled(connection: Connection) -> Iterator[None]:
    """Turn ``PRAGMA foreign_keys`` off for the block on SQLite; a no-op elsewhere.

    Alembic batch mode drops and recreates tables, and with enforcement on, dropping
    a referenced table would cascade into its children. SQLite ignores the pragma
    inside a transaction, so it goes straight to the DBAPI connection; the caller
    must not hold a transaction open when entering or leaving the block.
    """
    if connection.dialect.name != "sqlite":
        yield
        return
    _sqlite_pragma(connection, "foreign_keys", "OFF")
    try:
        yield
    finally:
        _sqlite_pragma(connection, "foreign_keys", "ON")


def _sqlite_pragma(connection: Connection, name: str, value: str) -> None:
    raw = connection.connection.dbapi_connection
    if raw is None:
        raise RuntimeError("connection has no DBAPI connection to set a pragma on")
    cursor = raw.cursor()
    try:
        cursor.execute(f"PRAGMA {name}={value}")
    finally:
        cursor.close()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """A session factory bound to ``engine``.

    ``expire_on_commit`` is off so objects returned from :func:`session_scope` stay
    readable after the block commits and closes the session.
    """
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """A session that commits on success, rolls back and re-raises on error, and always closes."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
