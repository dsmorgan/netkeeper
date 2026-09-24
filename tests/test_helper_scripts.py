"""scripts/chrome.sh and scripts/reset-data.sh, run through sh.

Nothing here starts a browser or touches a real data directory: chrome.sh only
ever runs with --dry-run or --status, against a temp profile and a port nothing
listens on, and reset-data.sh only against a temp data directory.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from netkeeper.services import browser_launch

ROOT = Path(__file__).resolve().parent.parent
CHROME = ROOT / "scripts" / "chrome.sh"
RESET = ROOT / "scripts" / "reset-data.sh"
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None or sys.platform == "win32", reason="needs a POSIX sh")


def run_sh(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert SH is not None
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent", **env}
    return subprocess.run(
        [SH, str(script), *args], capture_output=True, text=True, env=full_env, timeout=30
    )


def chrome_flags(line: str) -> list[str]:
    return [f for f in re.findall(r"(?<!\S)--[a-z-]+", line) if f != "--args"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


# --- chrome.sh -------------------------------------------------------------------


def test_chrome_script_constants_match_the_cli() -> None:
    """The script's profile name and default port are the ones `browser launch` prints."""
    text = CHROME.read_text()
    assert f'PROFILE_DIRNAME="{browser_launch.CHROME_PROFILE_DIRNAME}"' in text
    assert f"DEFAULT_PORT={browser_launch.DEFAULT_CDP_PORT}" in text


def test_chrome_script_launches_with_exactly_the_flags_the_cli_prints(tmp_path: Path) -> None:
    port = free_port()
    profile = tmp_path / "profile with space"
    result = run_sh(CHROME, "--dry-run", "--port", str(port), "--profile-dir", str(profile), env={})
    assert result.returncode == 0, result.stderr
    launch = [line for line in result.stdout.splitlines() if "remote-debugging-port" in line]
    assert len(launch) == 1
    printed = " ".join(browser_launch.chrome_launch_command(port, profile))
    # Every Chrome flag, with or without a value, so an extra one (a
    # fingerprint change) fails too; `--args` is `open`'s, not Chrome's.
    assert (
        chrome_flags(launch[0])
        == chrome_flags(printed)
        == ["--remote-debugging-port", "--user-data-dir"]
    )
    assert f"--remote-debugging-port={port}" in launch[0]
    assert f"--user-data-dir={profile}" in launch[0]
    assert not profile.exists(), "a dry run created the profile"


def test_chrome_script_defaults_to_the_data_dir_profile_and_the_cdp_url_port(
    tmp_path: Path,
) -> None:
    port = free_port()
    result = run_sh(
        CHROME,
        "--status",
        env={"NETKEEPER_DATA": str(tmp_path), "NETKEEPER_CDP_URL": f"http://127.0.0.1:{port}"},
    )
    assert result.returncode == 0, result.stderr
    assert f"profile  {tmp_path / browser_launch.CHROME_PROFILE_DIRNAME}" in result.stdout
    assert f"port     {port}: nothing is listening" in result.stdout
    assert "not running on this profile" in result.stdout


def test_chrome_script_clears_a_stale_lock_only_when_nothing_holds_the_profile(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (profile / name).symlink_to("otherhost-99999")
    result = run_sh(
        CHROME, "--dry-run", "--port", str(free_port()), "--profile-dir", str(profile), env={}
    )
    assert result.returncode == 0, result.stderr
    assert "stale profile lock" in result.stdout
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        assert f"would: rm {profile / name}" in result.stdout
        assert (profile / name).is_symlink(), "a dry run removed the lock"


def test_chrome_script_refuses_a_port_that_is_not_a_number(tmp_path: Path) -> None:
    result = run_sh(CHROME, "--dry-run", "--port", "9222x", "--profile-dir", str(tmp_path), env={})
    assert result.returncode == 1
    assert "must be a number" in result.stderr


def test_chrome_script_refuses_a_port_something_else_holds(tmp_path: Path) -> None:
    if shutil.which("lsof") is None:
        pytest.skip("needs lsof")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        result = run_sh(
            CHROME, "--dry-run", "--port", str(port), "--profile-dir", str(tmp_path), env={}
        )
    assert result.returncode == 1
    assert "not a debuggable Chrome" in result.stderr


# --- reset-data.sh ---------------------------------------------------------------


def make_db(data: Path) -> Path:
    db = data / "netkeeper.sqlite3"
    # `with sqlite3.connect(...)` commits but never closes, and an open handle is
    # exactly what reset-data.sh refuses to run under.
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("create table marker (x integer)")
        conn.execute("insert into marker values (42)")
    return db


def test_reset_dry_run_changes_nothing(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    result = run_sh(RESET, "--data-dir", str(tmp_path), "--dry-run", env={})
    assert result.returncode == 0, result.stderr
    assert db.exists()
    assert not (tmp_path / "archives").exists()


def test_reset_archives_then_removes_and_restore_puts_it_back(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    result = run_sh(RESET, "--data-dir", str(tmp_path), "--yes", env={})
    assert result.returncode == 0, result.stderr
    assert not db.exists()
    archives = sorted((tmp_path / "archives").iterdir())
    assert len(archives) == 1
    with closing(sqlite3.connect(archives[0])) as conn:
        assert conn.execute("select x from marker").fetchone() == (42,)

    listed = run_sh(RESET, "--data-dir", str(tmp_path), "--list", env={})
    assert archives[0].name in listed.stdout

    restored = run_sh(
        RESET, "--data-dir", str(tmp_path), "--restore", archives[0].name, "--yes", env={}
    )
    assert restored.returncode == 0, restored.stderr
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("select x from marker").fetchone() == (42,)


def test_reset_defaults_to_netkeeper_data(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    result = run_sh(RESET, "--dry-run", env={"NETKEEPER_DATA": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert str(db) in result.stdout
