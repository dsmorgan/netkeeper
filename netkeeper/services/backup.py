"""Database snapshots under ``<data_dir>/backups/`` (spec section 15).

A backup is one file, ``netkeeper-<UTC stamp>.sqlite3``, written by SQLite's
``VACUUM INTO`` from a read-only connection: a consistent, compacted copy taken
without blocking the server's writers. Retention keeps the newest ``[backup] keep``
files and touches nothing else in the directory. The nightly job, restore, and
the Settings panel come with P6-04.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.engine import URL, make_url

BACKUPS_DIRNAME = "backups"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
# ISO 8601 basic form: sorts chronologically as text and has no characters that
# any filesystem or shell objects to (colons in particular).
_NAME = re.compile(r"^netkeeper-(?P<stamp>\d{8}T\d{6}Z)\.sqlite3$")

log = logging.getLogger(__name__)


class BackupError(Exception):
    """A backup could not be made: wrong database kind, no file yet, or SQLite refused."""


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """One backup file. ``created_at`` comes from the name, so copying the file keeps it."""

    path: Path
    created_at: datetime
    size: int


def backup_name(created_at: datetime) -> str:
    """The file name of a backup taken at ``created_at`` (timezone-aware)."""
    return f"netkeeper-{_utc(created_at).strftime(STAMP_FORMAT)}.sqlite3"


def parse_backup_name(name: str) -> datetime | None:
    """The UTC time encoded in a backup file name, or None when ``name`` is not one."""
    match = _NAME.match(name)
    if match is None:
        return None
    return datetime.strptime(match["stamp"], STAMP_FORMAT).replace(tzinfo=UTC)


def create_backup(
    engine_or_url: Engine | str, backups_dir: Path, *, now: datetime | None = None
) -> Path:
    """Snapshot the SQLite database behind ``engine_or_url`` into ``backups_dir``.

    Returns the file written. The directory is created when missing. ``now``
    (timezone-aware) fixes the timestamp in the name; the default is the current
    UTC time. Raises :class:`BackupError` when the database is not SQLite
    (PostgreSQL has its own tools and is not covered here), when the database file
    does not exist yet, when a backup with the same name already exists, and when
    SQLite cannot write the copy.
    """
    source = _sqlite_file(engine_or_url)
    stamp = datetime.now(UTC) if now is None else _utc(now)
    backups_dir.mkdir(parents=True, exist_ok=True)
    target = backups_dir / backup_name(stamp)
    if target.exists():
        raise BackupError(f"{target}: already exists (backups are named to the second)")
    log.info("backing up %s to %s", source, target)
    # A read-only connection cannot touch the source, and autocommit mode
    # (isolation_level=None) keeps pysqlite from opening the transaction that
    # VACUUM refuses to run inside.
    uri = f"{source.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
            connection.execute("VACUUM INTO ?", (str(target),))
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise BackupError(f"{source}: VACUUM INTO {target} failed: {exc}") from exc
    return target


def prune_backups(backups_dir: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` backups in ``backups_dir``; return what was removed.

    Newest means by the timestamp in the name, not by mtime. Only files named like
    a backup are considered, so anything else in the directory (a renamed backup
    someone wants to keep, notes, a restore in progress) is left alone. Removed
    paths come back oldest first.
    """
    if keep < 1:
        raise ValueError(f"keep must be at least 1, got {keep}")
    removed: list[Path] = []
    for info in reversed(list_backups(backups_dir)[keep:]):
        info.path.unlink(missing_ok=True)
        log.info("pruned backup %s", info.path)
        removed.append(info.path)
    return removed


def list_backups(backups_dir: Path) -> list[BackupInfo]:
    """The backups in ``backups_dir``, newest first. A missing directory lists as empty."""
    if not backups_dir.is_dir():
        return []
    found: list[BackupInfo] = []
    for path in backups_dir.iterdir():
        created_at = parse_backup_name(path.name)
        if created_at is None or not path.is_file():
            continue
        found.append(BackupInfo(path=path, created_at=created_at, size=path.stat().st_size))
    found.sort(key=lambda info: info.created_at, reverse=True)
    return found


def _sqlite_file(engine_or_url: Engine | str) -> Path:
    """The database file behind ``engine_or_url``, checked to be SQLite and present."""
    url: URL = engine_or_url.url if isinstance(engine_or_url, Engine) else make_url(engine_or_url)
    if url.get_backend_name() != "sqlite":
        shown = url.render_as_string(hide_password=True)
        raise BackupError(
            f"{shown}: only a SQLite database can be backed up this way;"
            " PostgreSQL is not supported here (use pg_dump)"
        )
    if not url.database or url.database == ":memory:":
        raise BackupError("an in-memory database has no file to back up")
    source = Path(url.database)
    if not source.is_file():
        raise BackupError(
            f"{source}: no database file to back up (run `netkeeper db upgrade` first)"
        )
    return source


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("backup timestamps must be timezone-aware")
    return value.astimezone(UTC)
