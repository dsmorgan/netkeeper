"""The file lock's edges inside one process: a peek in the way, and a file swapped out.

The two-process behavior is in ``test_activity_lock_processes.py``. These are the
cases that need a hand on the file descriptor at a precise moment, which is easier
and more deterministic from inside the test process -- ``flock`` locks belong to open
file descriptions, not processes, so a descriptor the test opens contends exactly as
another process's would.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from netkeeper.linkedin import activity_lock
from netkeeper.linkedin.browser import ActivityLocks, BrowserBusy

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock(2) is POSIX")

ACCOUNT = "account-7"
TIMEOUT_S = 5.0


@pytest.fixture
def lock_file(tmp_path: Path) -> Path:
    """The account's lock file, created the way a first claim creates it."""
    claim = activity_lock.try_claim(ACCOUNT, tmp_path)
    assert claim is not None
    claim.release()
    return activity_lock.lock_path(ACCOUNT, tmp_path)


@pytest.fixture
def peek(lock_file: Path) -> Iterator[int]:
    """A descriptor holding the shared lock :func:`activity_lock.inspect` takes to peek."""
    fd = os.open(lock_file, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    yield fd
    os.close(fd)


def test_the_retry_window_is_pinned() -> None:
    """Long enough to outlast a peek (microseconds), short enough not to be felt."""
    assert ActivityLocks.CONFIRM_S == 0.05
    assert ActivityLocks.POLL_S == 0.25


async def test_a_claim_that_meets_a_peek_looks_again(tmp_path: Path, peek: int) -> None:
    """A posture peek landing on a job's claim must not turn the job away as busy.

    The peek is dropped 20 ms in, inside the 50 ms the claim waits before looking
    again, so the claim's second look finds the lock free.
    """
    locks = ActivityLocks(tmp_path)
    asyncio.get_running_loop().call_later(0.02, fcntl.flock, peek, fcntl.LOCK_UN)

    async with asyncio.timeout(TIMEOUT_S), locks.hold(ACCOUNT):
        assert activity_lock.inspect(ACCOUNT, tmp_path).held


async def test_a_peek_that_outlasts_the_retry_is_busy(tmp_path: Path, peek: int) -> None:
    """The inverse: the claim looks twice, not forever. Something still in the way is busy."""
    locks = ActivityLocks(tmp_path)

    with pytest.raises(BrowserBusy, match=ACCOUNT):
        async with asyncio.timeout(TIMEOUT_S), locks.hold(ACCOUNT):
            pass


def test_a_claim_on_a_file_replaced_under_it_starts_over(
    tmp_path: Path, lock_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock on an inode no longer at the path guards nothing anyone else will open.

    Here the file is deleted and recreated between this claim's ``open`` and its
    ``flock`` -- a user's ``rm``, or a cleanup script, at the wrong moment. Locking
    the orphan and keeping it would let the next claimant lock the new file too, and
    that is two holders. The claim must notice and lock the file that is there now.
    """
    real_open = os.open
    swapped: list[bool] = []

    def open_then_swap(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)
        if not swapped and Path(path) == lock_file:
            swapped.append(True)
            lock_file.unlink()
            lock_file.touch()
        return fd

    monkeypatch.setattr(os, "open", open_then_swap)
    claim = activity_lock.try_claim(ACCOUNT, tmp_path)
    monkeypatch.setattr(os, "open", real_open)

    assert swapped, "the swap never happened, so this test proved nothing"
    assert claim is not None
    try:
        assert activity_lock.try_claim(ACCOUNT, tmp_path) is None, "a second holder got in"
        assert activity_lock.inspect(ACCOUNT, tmp_path).held
    finally:
        claim.release()
