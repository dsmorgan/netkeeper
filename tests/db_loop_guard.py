"""A reusable harness for background database work under ``serve`` (#259, #266).

Every background session -- the browser worker's, the runners', the scheduler's --
runs whole on :func:`netkeeper.db.off_loop`'s thread. One put back on the event
loop is a deadlock waiting for a request: a request's write transaction opens in
a worker thread and commits in a teardown that needs the loop, so a background
write blocked *on the loop* in SQLite's busy handler keeps that commit from ever
running, and both sit out ``busy_timeout``.

:func:`request_holding_the_write_lock` reproduces that shape for any background
path, and makes the result deterministic:

* **A request holds the write lock.** A thread (neither the loop's nor the
  database thread) takes ``BEGIN IMMEDIATE`` on the same file and commits only
  when the loop gets round to a callback ``hold_s`` later -- the way a request's
  commit needs the loop. A background write made on the loop meanwhile blocks the
  loop, the callback never runs, and the write fails "database is locked" after
  the (shortened) busy timeout. Off the loop, it waits and goes on.
* **Every statement run on the loop's thread is recorded**, with the netkeeper
  line that ran it. The lock covers the start of a path; this covers all of it,
  reads included (in WAL mode a read never waits for the lock, so it cannot
  deadlock, but it still blocks the loop for as long as SQLite takes). A test
  asserts :attr:`OnTheLoop.statements` is empty.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, event

#: Short enough that a write stuck on the loop fails fast. Patch
#: ``netkeeper.db.SQLITE_BUSY_TIMEOUT_MS`` to this before the engine connects.
BUSY_TIMEOUT_MS = 300


@dataclass
class OnTheLoop:
    """Statements the event loop's own thread ran while the harness was watching."""

    loop_thread: threading.Thread
    statements: list[str] = field(default_factory=list)

    def record(self, statement: str) -> None:
        if threading.current_thread() is not self.loop_thread:
            return
        self.statements.append(f"{_netkeeper_site()}: {' '.join(statement.split())[:80]}")


def _netkeeper_site() -> str:
    """The innermost frame in the netkeeper package, outside ``db.py``: the site to fix."""
    for frame in reversed(traceback.extract_stack()):
        path = frame.filename.replace("\\", "/")
        if "/netkeeper/" in path and "/tests/" not in path and not path.endswith("/db.py"):
            if "/site-packages/" in path:
                continue
            return f"{path.rsplit('/netkeeper/', 1)[1]}:{frame.lineno} in {frame.name}"
    return "outside netkeeper"


@asynccontextmanager
async def request_holding_the_write_lock(
    engine: Engine, *, hold_s: float = 0.1
) -> AsyncIterator[OnTheLoop]:
    """Hold the write lock the way a request does, and watch the loop, while the body runs.

    See the module docstring. ``engine`` must be a SQLite file engine; its
    connections should carry :data:`BUSY_TIMEOUT_MS`.
    """
    database = engine.url.database
    assert database, "the harness needs a SQLite file"
    loop = asyncio.get_running_loop()
    watch = OnTheLoop(loop_thread=threading.current_thread())

    def before_cursor_execute(*args: Any) -> None:
        watch.record(str(args[2]))

    acquired, release = threading.Event(), threading.Event()
    failure: list[BaseException] = []

    def request() -> None:
        connection = sqlite3.connect(database, isolation_level=None, timeout=5)
        try:
            connection.execute("BEGIN IMMEDIATE")
            acquired.set()
            release.wait(5)
            connection.execute("COMMIT")
        except BaseException as exc:  # pragma: no cover - reported below
            failure.append(exc)
            acquired.set()
        finally:
            connection.close()

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    holder = threading.Thread(target=request, name="request-holding-the-write-lock")
    holder.start()
    try:
        assert await asyncio.to_thread(acquired.wait, 5), "the request never took the lock"
        # The commit waits for the loop, as a request's teardown does.
        loop.call_later(hold_s, release.set)
        yield watch
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 5)
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
    assert not failure, failure
