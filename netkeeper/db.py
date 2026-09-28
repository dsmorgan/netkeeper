"""Engine and session plumbing (spec section 15).

Nothing here is created at import time: callers build an engine from a URL when
they need one and pass it around explicitly.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
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
# How long a SQLite connection waits for a writer to finish before failing with
# "database is locked". Two request handlers, or a request and the scheduler,
# writing at once is normal; failing at once is not.
#
# This covers writer-versus-writer contention only: a transaction that wants the
# write lock while another holds it waits. It does not cover a transaction that
# reads first and writes second. If another writer commits between that read and
# that write, SQLite refuses the upgrade at once (SQLITE_BUSY_SNAPSHOT) and never
# consults the busy handler, because the read snapshot is stale and waiting cannot
# make it fresh. Writer sessions therefore start every transaction with
# BEGIN IMMEDIATE (see mark_for_write), taking the write lock before their first
# read, so they wait at BEGIN under this timeout instead of failing at the write.
SQLITE_BUSY_TIMEOUT_MS = 5000
# Execution option on a connection that makes the SQLite ``begin`` listener emit
# BEGIN IMMEDIATE. Set through mark_for_write, never by hand.
IMMEDIATE_OPTION = "netkeeper_immediate"

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

    For SQLite this also creates the database file's parent directory, turns on WAL,
    foreign-key enforcement, and a busy timeout on every connection, and takes over
    transaction control from pysqlite so that DDL is transactional and savepoints
    work.
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
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def _sqlite_on_begin(connection: Connection) -> None:
    # A writer takes the write lock here; a reader takes no lock until its first read.
    if connection.get_execution_options().get(IMMEDIATE_OPTION):
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
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


def mark_for_write(session: Session) -> None:
    """Make every transaction ``session`` starts begin as a writer.

    On SQLite that is ``BEGIN IMMEDIATE``: the write lock is taken up front, so a
    transaction that reads and then writes waits for a concurrent writer (up to
    ``SQLITE_BUSY_TIMEOUT_MS``) instead of failing at once with
    ``SQLITE_BUSY_SNAPSHOT`` when that writer commits between the read and the
    write. PostgreSQL ignores the mark; its MVCC never refuses an upgrade.

    The mark rides on the session's bind, an engine carrying the
    ``IMMEDIATE_OPTION`` execution option, so it outlives ``commit()``: a block
    that commits and goes on is still a writer. It has to be set before the
    session's first statement, because that statement starts the transaction.
    A read-only session is left unmarked so it never waits for the write lock.
    """
    if session.info.get(IMMEDIATE_OPTION):
        return
    if session.in_transaction():
        raise RuntimeError("mark_for_write() must run before the session's first statement")
    bind = session.get_bind()
    if not isinstance(bind, Engine):
        raise TypeError("mark_for_write() needs a session bound to an Engine, not a Connection")
    session.bind = bind.execution_options(**{IMMEDIATE_OPTION: True})
    session.info[IMMEDIATE_OPTION] = True


def is_writer(session: Session) -> bool:
    """True when :func:`mark_for_write` was applied to ``session``."""
    return bool(session.info.get(IMMEDIATE_OPTION))


@contextmanager
def session_scope(factory: sessionmaker[Session], *, write: bool = False) -> Iterator[Session]:
    """A session that commits on success, rolls back and re-raises on error, and always closes.

    ``write=True`` marks the session as a writer (:func:`mark_for_write`). Use it
    for any block that may write, above all one that reads first; leave it off
    for a read-only block so it never waits for the SQLite write lock.
    """
    session = factory()
    if write:
        mark_for_write(session)
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


# --- background database work, off the event loop (#259) ---------------------------

_db_executor: ThreadPoolExecutor | None = None
_db_executor_lock = threading.Lock()


class CancelledWhileFailing(asyncio.CancelledError):
    """A cancel that arrived while :func:`off_loop` work was in flight, and the work failed.

    Still a :class:`asyncio.CancelledError`, so the task is cancelled exactly as
    before; ``error`` is what the work itself raised, so a handler recording the
    run's ending can record the failure (``failed``, "error") rather than a
    clean interruption (#266). The failure is also this exception's ``__cause__``.
    """

    def __init__(self, error: BaseException) -> None:
        super().__init__(f"cancelled while background database work failed: {error!r}")
        self.error = error


def _executor() -> ThreadPoolExecutor:
    global _db_executor
    with _db_executor_lock:
        if _db_executor is None:
            _db_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="netkeeper-db")
        return _db_executor


async def off_loop[**P, T](fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Run ``fn(*args, **kwargs)`` on the background-database thread and return its result.

    Background work under ``netkeeper serve`` (the browser worker, the runners,
    the scheduler's heartbeat) puts each whole :func:`session_scope` -- open,
    work, commit -- in one ``fn`` and awaits it here, so a transaction never
    spans an ``await`` and no SQLite call blocks the event loop (#259). A
    request's write transaction is opened in a worker thread and committed by a
    dependency teardown that needs the loop; a writer blocked *on the loop* in
    SQLite's busy handler kept that commit from ever running, and both sides sat
    out ``busy_timeout`` before the background write failed "database is
    locked". Blocked here instead, the write waits for the request's commit and
    goes on.

    **One thread, shared by every caller in the process.** Background writes run
    one at a time in the order they were submitted, so they never contend with
    each other for the write lock, and a write submitted after another (a run's
    "interrupted" ending after its last progress write) lands after it. Never
    call :func:`off_loop` for work that waits on something outside the database
    (a network call, a browser): it would hold up every background write behind
    it. ``MailboxMonitor`` stays on ``asyncio.to_thread`` for that reason.

    **A started transaction always finishes.** Blocking code on the loop could
    not be interrupted halfway; the cancel landed at the next ``await``. To keep
    that, a cancel that arrives while ``fn`` is queued or running waits for
    ``fn`` to finish (its transaction committed or rolled back) and then
    propagates, so a shutdown never abandons a write partway or races the
    ending a cancel handler records next. Its result is discarded, as the code
    after it would not have run either. An exception it raised is logged, and
    the cancel propagates as :class:`CancelledWhileFailing` carrying it, so the
    handler that records the run's ending can tell a write that failed from one
    that was merely interrupted (#266).
    """
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    call = functools.partial(context.run, fn, *args, **kwargs)
    future = loop.run_in_executor(_executor(), call)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.wait({future})
            except asyncio.CancelledError:
                continue
        if not future.cancelled() and (error := future.exception()) is not None:
            log.warning(
                "background database work failed while its task was being cancelled",
                exc_info=error,
            )
            raise CancelledWhileFailing(error) from error
        raise
