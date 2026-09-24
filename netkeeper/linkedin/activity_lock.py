"""The activity lock's cross-process half: one OS file lock per LinkedIn account (spec 9.9).

Two CDP clients on one browser drop each other's connection, and once jobs are armed
they are also two request streams no budget counter knows about. So every browser path
in every netkeeper process claims its account here *before* it attaches: ``netkeeper
serve``'s jobs, ``netkeeper preflight``, ``netkeeper posture --probe``, and ``netkeeper
rehearse`` all meet at the same file, and whichever arrives second is refused.

**The mechanism is ``flock(2)``** on ``<data dir>/locks/browser-<account>.lock``
(``browser-account-<id>.lock``, :func:`account_key`, plus the legacy
``browser-local.lock`` a hold of the local account also claims, :data:`LEGACY_SHARED_KEY`), taken
with ``LOCK_EX | LOCK_NB``. The kernel owns the lock and drops it when the descriptor
closes, and the descriptor closes when the process exits *however* it exits, ``SIGKILL``
included. A crashed holder therefore cannot park the lock: there is no heartbeat, no
staleness rule, and no clock involved, because nothing stale can exist. The file's
contents (pid, command, since) are a note for the busy message and are never what
decides whether the lock is held; a note left behind by a crash is overwritten by the
next holder and ignored until then.

The price is scope. The lock binds processes that share this data directory on this
machine. That is exactly netkeeper's deployment today (one machine, one Chrome, one data
directory); a ``settings_kv`` claim would reach across machines sharing a database, at
the cost of a heartbeat, a staleness window during which a crashed holder still blocks,
and a database write on the browser path. ``services/posture.py`` states the scope as
a gap.

Nothing here imports the ORM, opens a session, or starts a process (spec 9.10).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from netkeeper.paths import data_dir

#: Directory under the data directory that holds one lock file per LinkedIn account.
LOCKS_DIRNAME = "locks"


def account_key(account_id: int) -> str:
    """The lock key of ``linkedin_accounts`` row ``account_id`` (#169 F): ``account-<id>``.

    Budgets, heat, and this lock belong to the account row (ADR 0005), so two
    accounts never block each other while two runs on one account always do.
    """
    if account_id < 1:
        raise ValueError(f"an account id is a positive integer, not {account_id}")
    return f"account-{account_id}"


#: The key of the account a caller with no database uses: account 1, the first
#: user's account (migration 0011). ``netkeeper preflight`` and ``rehearse`` on a
#: fresh install, before any database exists, hold this one.
SINGLE_ACCOUNT_KEY = account_key(1)

#: The key every browser path used before the lock was keyed by account (#169 F).
#: A netkeeper process started from older code and still running (a ``serve``,
#: a ``preflight``) holds ``browser-local.lock`` and knows nothing of
#: ``browser-account-<id>.lock``. Older code only ever acted for the local user's
#: account, so :class:`~netkeeper.linkedin.browser.ActivityLocks` claims this file
#: *as well as* that account's own, first, whenever it holds that account (its
#: ``legacy_partner``: the local user's account, whatever its id, when the caller
#: can read the database; :data:`SINGLE_ACCOUNT_KEY` when it cannot). An old
#: holder and a new one can never both attach. Any other account is not affected
#: and never waits on it. Dropping this co-claim, once no
#: pre-P2-10 process can still be running, is a follow-up.
LEGACY_SHARED_KEY = "local"

_SAFE_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")


def locks_dir() -> Path:
    """Where the lock files live: ``<data dir>/locks``. Resolved per call, never cached."""
    return data_dir() / LOCKS_DIRNAME


def lock_path(account: str, directory: Path | None = None) -> Path:
    """The lock file for ``account``. A key that is not a plain name is hex-encoded."""
    name = account if _SAFE_KEY.fullmatch(account) else "x" + account.encode().hex()
    return (locks_dir() if directory is None else directory) / f"browser-{name}.lock"


@dataclass(frozen=True, slots=True)
class Holder:
    """Who holds an account's lock, from the note the holder wrote into the lock file."""

    pid: int
    command: str
    since: datetime | None

    def describe(self) -> str:
        """``netkeeper serve (pid 4242, since 2026-09-23 14:02 UTC)``."""
        when = f", since {self.since:%Y-%m-%d %H:%M UTC}" if self.since is not None else ""
        return f"{self.command or 'a netkeeper process'} (pid {self.pid}{when})"


@dataclass(frozen=True, slots=True)
class LockState:
    """Whether an account's lock is held right now, and by whom when the note says."""

    account: str
    path: Path
    held: bool
    holder: Holder | None = None


class Claim:
    """A held lock. :meth:`release` lets go; so does the process ending, however it ends."""

    def __init__(self, account: str, path: Path, fd: int) -> None:
        self.account = account
        self.path = path
        self._fd: int | None = fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        """Erase the note, unlock, close. Idempotent.

        The file itself stays. Unlinking a lock file lets a process that opened the
        old inode lock it while a newcomer locks a fresh one, which is two holders.
        """
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):  # the note is advisory; the lock is what matters
            os.ftruncate(fd, 0)
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def try_claim(account: str, directory: Path | None = None) -> Claim | None:
    """Take ``account``'s lock without waiting. ``None`` when another descriptor holds it.

    The lock is per open file description, so a second claim from this same process is
    refused as well: the caller need not trust its own bookkeeping.

    A lock on a file that is no longer at ``path`` locks nothing anyone else will
    open: if the file was deleted or replaced between this claim's ``open`` and its
    ``flock``, the next claimant opens the new file and locks that alongside it. So
    after locking, the claim checks that its descriptor is still the file at
    ``path``, and starts over on the file that is there now when it is not.
    """
    path = lock_path(account, directory)
    for _ in range(_REOPEN_ATTEMPTS):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return None
            raise
        if _is_file_at(fd, path):
            _write_note(fd)
            return Claim(account, path, fd)
        os.close(fd)  # locked an unlinked inode; that lock guards nothing
    raise OSError(f"the lock file at {path} kept changing under this claim")


#: How many times a claim starts over after finding its lock file replaced. One
#: retry covers a single delete; more than a handful means something is deleting
#: the file in a loop, and that is an error rather than something to wait out.
_REOPEN_ATTEMPTS = 5


def _is_file_at(fd: int, path: Path) -> bool:
    """Whether the open descriptor is still the file ``path`` names."""
    try:
        there = path.stat()
    except FileNotFoundError:
        return False
    held = os.fstat(fd)
    return (held.st_dev, held.st_ino) == (there.st_dev, there.st_ino)


def inspect(account: str, directory: Path | None = None) -> LockState:
    """Whether ``account``'s lock is held, without taking it and without creating anything.

    Peeks with a shared lock, which conflicts only with an exclusive holder and is
    dropped at once. A claim that lands in that instant is refused, and
    :class:`~netkeeper.linkedin.browser.ActivityLocks` tries such a claim once more
    before calling the account busy.
    """
    path = lock_path(account, directory)
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return LockState(account=account, path=path, held=False)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return LockState(account=account, path=path, held=True, holder=_read_note(fd))
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        return LockState(account=account, path=path, held=False)
    finally:
        os.close(fd)


def read_holder(account: str, directory: Path | None = None) -> Holder | None:
    """The note in ``account``'s lock file, for a busy message. Advisory only."""
    try:
        fd = os.open(lock_path(account, directory), os.O_RDONLY)
    except OSError:
        return None
    try:
        return _read_note(fd)
    finally:
        os.close(fd)


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists. Signal 0 checks and delivers nothing.

    ``EPERM`` means it exists and belongs to someone else, which still counts. A pid
    of zero or below names a process group, never one process, so it is not alive.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_label() -> str:
    """What this process is, as a person typed it: ``netkeeper serve``, ``netkeeper preflight``."""
    argv = sys.argv
    if not argv:
        return ""
    words = [Path(argv[0]).name]
    command = next((arg for arg in argv[1:] if not arg.startswith("-")), None)
    if command is not None:
        words.append(command)
    return " ".join(words)


def _write_note(fd: int) -> None:
    note = {
        "pid": os.getpid(),
        "command": process_label(),
        "since": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(note).encode(), 0)
    except OSError:
        pass  # a missing note costs the busy message a name, never the lock


def _read_note(fd: int) -> Holder | None:
    try:
        raw = os.pread(fd, 4096, 0)
        note = json.loads(raw.decode()) if raw else None
    except (OSError, ValueError):
        return None
    if not isinstance(note, dict) or not isinstance(note.get("pid"), int):
        return None
    since: datetime | None = None
    if isinstance(note.get("since"), str):
        try:
            since = datetime.fromisoformat(note["since"])
        except ValueError:
            since = None
    command = note.get("command")
    return Holder(pid=note["pid"], command=command if isinstance(command, str) else "", since=since)
