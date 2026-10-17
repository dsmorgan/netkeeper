"""The nightly backup ``netkeeper serve`` takes (P6-04, spec section 15).

Every :data:`TICK_S` seconds the job reads the local user's ``backup.nightly`` and
``backup.keep`` as they stand now (``config.toml``, then the Settings page, then the
defaults), and when the night's backup is due (:func:`~netkeeper.services.backup.nightly_due`)
it writes one with :func:`~netkeeper.services.backup.create_backup` and prunes to
``keep``, the same as ``netkeeper backup create``. The work runs in a thread, off the
event loop. Only a SQLite database is backed up; with PostgreSQL the job never starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.logging_setup import personal
from netkeeper.services import ui_settings
from netkeeper.services.backup import create_backup, list_backups, nightly_due, prune_backups
from netkeeper.services.users import local_owner

log = logging.getLogger(__name__)

TICK_S: Final = 600.0
"""How often the job looks at whether a backup is due: a setting changed on the page
applies within this long."""

FIRST_CHECK_S: Final = 60.0
"""How long after ``serve`` starts the first look waits, so startup is not slowed."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def backs_up(engine: Engine) -> bool:
    """Whether the job can back up ``engine``'s database: a SQLite file."""
    url = engine.url
    return url.get_backend_name() == "sqlite" and bool(url.database) and url.database != ":memory:"


class NightlyBackups:
    """The background job. :meth:`back_up_if_due` is one look, and is what tests call."""

    def __init__(
        self,
        engine: Engine,
        factory: sessionmaker[Session],
        base: Settings,
        backups_dir: Path,
        *,
        clock: Callable[[], datetime] = _utcnow,
        tick_s: float = TICK_S,
        first_check_s: float = FIRST_CHECK_S,
    ) -> None:
        self._engine = engine
        self._factory = factory
        self._base = base
        self.backups_dir = backups_dir
        self._clock = clock
        self._tick_s = tick_s
        self._first_check_s = first_check_s
        self._task: asyncio.Task[None] | None = None
        self._backing_up = False

    @property
    def busy(self) -> bool:
        """A nightly backup is being written (#597: a restart waits for it)."""
        return self._backing_up

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="nightly-backup")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def back_up_if_due(self) -> Path | None:
        """Back up and prune when the night's backup is due and turned on; return the
        file written, or None when nothing was due."""
        settings = self._settings()
        if not settings.backup.nightly:
            return None
        keep = settings.backup.keep
        if keep < 1:
            log.warning("nightly backup skipped: backup.keep must be at least 1, got %d", keep)
            return None
        now = self._clock()
        # A pre-restore backup holds the database a restore replaced, not tonight's.
        found = [info for info in list_backups(self.backups_dir) if not info.pre_restore]
        newest = found[0].created_at if found else None
        if not nightly_due(newest, now, settings.linkedin.timezone):
            return None
        self._backing_up = True
        try:
            written = create_backup(self._engine, self.backups_dir, now=now)
            removed = prune_backups(self.backups_dir, keep, now=now)
        finally:
            self._backing_up = False
        log.info(
            "nightly backup wrote %s; pruned %d (keeping %d)", personal(written), len(removed), keep
        )
        return written

    def _settings(self) -> Settings:
        """The local user's settings in force now, or the file's when there is no user."""
        with session_scope(self._factory) as session:
            user = local_owner(session)
            if user is None:
                return self._base
            return ui_settings.resolve(session, user, self._base)

    async def _run(self) -> None:
        await asyncio.sleep(self._first_check_s)
        while True:
            try:
                await asyncio.to_thread(self.back_up_if_due)
            except Exception:
                log.exception("nightly backup failed; trying again at the next check")
            await asyncio.sleep(self._tick_s)
