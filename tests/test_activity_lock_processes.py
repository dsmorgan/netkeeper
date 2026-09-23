"""One browser client per account across real OS processes (spec 9.9, issue #153).

Every test here starts separate Python interpreters with ``subprocess``: not
coroutines, not threads. Each process builds its own provider with its own fresh
:class:`ActivityLocks`, which is what ``netkeeper serve`` and ``netkeeper preflight``
in a terminal each do, so nothing they share lives in Python -- only the lock file
under the data directory both are pointed at.

Offline throughout: every process attaches through ``tests/browser_fakes.FakeConnector``
and reports how many times it attached. The number that matters is the refused
process's: zero, meaning it never reached a connector at all.

Nothing can hang. Reading the holder's first line, waiting for it to exit, and every
contender process run under :data:`TIMEOUT_S`; a contender that blocks past it is a
test failure with a message, not a stuck suite.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from browser_fakes import FakeConnector
from typer.testing import CliRunner

from netkeeper.cli import app as cli
from netkeeper.linkedin import activity_lock
from netkeeper.linkedin.browser import AttachBrowserProvider

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock(2) is POSIX")

HELPER = Path(__file__).with_name("activity_lock_proc.py")
ACCOUNT = activity_lock.SINGLE_ACCOUNT_KEY
TIMEOUT_S = 20.0
SERVE = "netkeeper serve"


class Holder:
    """A process that entered ``provider.run`` and is sitting on the account's lock."""

    def __init__(self, data: Path, account: str = ACCOUNT, label: str = SERVE) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(HELPER), "hold", account, "--as", label],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env(data),
            text=True,
        )
        self.first = self._line()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def _line(self) -> dict[str, Any]:
        assert self.proc.stdout is not None
        ready, _, _ = select.select([self.proc.stdout], [], [], TIMEOUT_S)
        if not ready:
            self.kill()
            pytest.fail(f"the holder printed nothing within {TIMEOUT_S}s")
        line = self.proc.stdout.readline()
        if not line:
            _, err = self.proc.communicate(timeout=TIMEOUT_S)
            pytest.fail(f"the holder exited before holding the lock:\n{err}")
        parsed: dict[str, Any] = json.loads(line)
        return parsed

    def release(self) -> dict[str, Any]:
        """Close stdin: the holder leaves ``provider.run`` the ordinary way."""
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        last = self._line()
        self.proc.wait(timeout=TIMEOUT_S)
        return last

    def kill(self) -> None:
        """``SIGKILL``: no ``finally``, no ``atexit``, no release. The crash case."""
        if self.proc.poll() is None:
            os.kill(self.proc.pid, signal.SIGKILL)
        self.proc.wait(timeout=TIMEOUT_S)


def _env(data: Path) -> dict[str, str]:
    return {**os.environ, "NETKEEPER_DATA": str(data)}


def contend(data: Path, mode: str, *args: str, account: str = ACCOUNT) -> dict[str, Any]:
    """Run a contender process to completion and return what it printed.

    A contender still running after :data:`TIMEOUT_S` is killed and fails the test:
    "the second process never got the lock" must read as a failure, never as a hang.
    """
    try:
        done = subprocess.run(
            [sys.executable, str(HELPER), mode, account, *args],
            capture_output=True,
            text=True,
            env=_env(data),
            timeout=TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"the {mode} process was still blocked after {TIMEOUT_S}s")
    assert done.returncode == 0, done.stderr
    parsed: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return parsed


@pytest.fixture
def data(tmp_path: Path) -> Path:
    return tmp_path / "shared-data"


@pytest.fixture
def holders() -> Iterator[list[Holder]]:
    started: list[Holder] = []
    yield started
    for holder in started:
        holder.kill()


def test_a_second_process_is_refused_before_it_reaches_the_connector(
    data: Path, holders: list[Holder]
) -> None:
    """The issue in one test: `serve` holds the browser, another process asks for it."""
    holder = Holder(data)
    holders.append(holder)
    assert holder.first == {"pid": holder.pid, "state": "held", "attaches": 1}

    refused = contend(data, "try")

    assert refused["outcome"] == "busy"
    assert refused["attaches"] == 0, "the refused process opened a CDP client"
    assert f"in use by {SERVE} (pid {holder.pid}" in refused["message"]

    assert holder.release()["state"] == "released"
    after = contend(data, "try")
    assert after == {"pid": after["pid"], "outcome": "attached", "attaches": 1}


def test_preflight_in_another_process_answers_busy(data: Path, holders: list[Holder]) -> None:
    holder = Holder(data)
    holders.append(holder)

    report = contend(data, "preflight")

    assert report["attaches"] == 0
    assert report["attached"] is False
    assert report["ok"] is False
    assert any(f"{SERVE} (pid {holder.pid}" in problem for problem in report["problems"])


def test_a_different_account_is_not_blocked(data: Path, holders: list[Holder]) -> None:
    """ADR 0005: the lock is per account, so the file is too."""
    holders.append(Holder(data, account="account-1"))

    assert contend(data, "try", account="account-2")["outcome"] == "attached"
    assert contend(data, "try", account="account-1")["outcome"] == "busy"


def test_a_killed_holder_does_not_park_the_lock(data: Path, holders: list[Holder]) -> None:
    """A holder that dies without releasing must not block the next one.

    The contender *waits* for the lock rather than asking once, so a lock the dead
    holder parked shows up as a contender still blocked at :data:`TIMEOUT_S` -- a
    failure with a message -- rather than as a busy answer that could be mistaken
    for a live holder.
    """
    holder = Holder(data)
    holders.append(holder)
    holder.kill()
    assert holder.proc.returncode == -signal.SIGKILL

    # The crash left its note behind: this is the stale state that must not count.
    note = activity_lock.lock_path(ACCOUNT, data / activity_lock.LOCKS_DIRNAME).read_text()
    assert f'"pid": {holder.pid}' in note

    waited = contend(data, "try", "--wait")
    assert waited["outcome"] == "attached"
    assert waited["attaches"] == 1
    assert contend(data, "try")["outcome"] == "attached", "and asking once works too"


def test_netkeeper_preflight_tells_the_user_who_holds_the_browser(
    data: Path, holders: list[Holder], monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a person sees typing `netkeeper preflight` while `netkeeper serve` runs a job."""
    holder = Holder(data)
    holders.append(holder)
    connector = FakeConnector()
    monkeypatch.setenv("NETKEEPER_DATA", str(data))
    monkeypatch.setattr(
        "netkeeper.cli.AttachBrowserProvider",
        lambda cdp_url: AttachBrowserProvider(cdp_url, connector=connector),
    )

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 1, result.output
    assert connector.attaches == 0
    assert f"in use by {SERVE} (pid {holder.pid}, since " in result.output
    assert "one browser client per account" in result.output
    assert result.output.rstrip().endswith("not ready")
