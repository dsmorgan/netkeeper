"""scripts/chrome.sh and scripts/reset-data.sh, run through sh.

Nothing here starts a browser or touches a real data directory: chrome.sh only
ever runs with --dry-run or --status, against a temp profile and a port nothing
of ours listens on, and reset-data.sh only against a temp data directory. HOME
points nowhere, so no default path can reach a real one.
"""

from __future__ import annotations

import http.server
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from netkeeper.services import browser_launch

ROOT = Path(__file__).resolve().parent.parent
CHROME = ROOT / "scripts" / "chrome.sh"
RESET = ROOT / "scripts" / "reset-data.sh"
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None or sys.platform == "win32", reason="needs a POSIX sh")


def run_sh(
    script: Path, *args: str, env: dict[str, str], stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    assert SH is not None
    # The interpreter's own bin first: chrome.sh finds netkeeper as <repo>/.venv/bin or on
    # PATH, and a sibling worktree has no .venv of its own (#210).
    path = os.pathsep.join(
        (str(Path(sys.executable).parent), os.environ.get("PATH", "/usr/bin:/bin"))
    )
    full_env = {"PATH": path, "HOME": "/nonexistent", **env}
    return subprocess.run(
        [SH, str(script), *args],
        capture_output=True,
        text=True,
        env=full_env,
        input=stdin,
        timeout=60,
    )


def chrome_flags(line: str) -> list[str]:
    return [f for f in re.findall(r"(?<!\S)--[a-z-]+", line) if f != "--args"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


@contextmanager
def process_with_args(*args: str) -> Iterator[int]:
    """A live `sh` whose command line carries ``args``, the way Chrome's carries its flags."""
    # `; :` keeps sh from exec-ing sleep, so the sh -- and its arguments -- stay in ps.
    proc = subprocess.Popen(["sh", "-c", "sleep 60; :", "holder", *args], start_new_session=True)
    try:
        yield proc.pid
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


@contextmanager
def process_named(directory: Path, name: str) -> Iterator[int]:
    """A live process whose executable path is ``directory/name``."""
    sleep = shutil.which("sleep")
    assert sleep is not None
    # A symlink, not a copy: macOS kills a copied system binary, and ps shows
    # the path it was started by either way.
    exe = directory / name
    exe.symlink_to(sleep)
    proc = subprocess.Popen([str(exe), "60"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


@contextmanager
def fake_cdp_endpoint() -> Iterator[int]:
    """A loopback server answering /json/version the way Chrome's does."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"Browser": "Chrome/1.0.0.0"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def start(profile: Path, *extra: str, port: int | None = None) -> subprocess.CompletedProcess[str]:
    return run_sh(
        CHROME,
        "--dry-run",
        "--port",
        str(port if port is not None else free_port()),
        "--profile-dir",
        str(profile),
        *extra,
        env={},
    )


# --- chrome.sh -------------------------------------------------------------------


def test_chrome_script_constants_match_the_cli() -> None:
    """The fallback profile name and port are the ones `browser launch` uses."""
    text = CHROME.read_text()
    assert f'PROFILE_DIRNAME="{browser_launch.CHROME_PROFILE_DIRNAME}"' in text
    assert f"DEFAULT_PORT={browser_launch.DEFAULT_CDP_PORT}" in text


def test_chrome_script_launches_with_exactly_the_flags_the_cli_prints(tmp_path: Path) -> None:
    port = free_port()
    profile = tmp_path.resolve() / "profile with space"
    result = start(profile, port=port)
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


def test_chrome_script_takes_port_and_profile_from_netkeepers_config(tmp_path: Path) -> None:
    port = free_port()
    config = tmp_path / "config.toml"
    config.write_text(f'[linkedin]\ncdp_url = "http://127.0.0.1:{port}"\n')
    result = run_sh(
        CHROME,
        "--status",
        env={"NETKEEPER_DATA": str(tmp_path), "NETKEEPER_CONFIG": str(config)},
    )
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
    profile = tmp_path.resolve() / browser_launch.CHROME_PROFILE_DIRNAME
    assert f"profile  {profile}" in result.stdout
    assert f"port     {port}: nothing is listening" in result.stdout


def test_chrome_script_refuses_a_cdp_url_on_another_host(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[linkedin]\ncdp_url = "http://192.0.2.10:9222"\n')
    result = run_sh(
        CHROME,
        "--dry-run",
        env={"NETKEEPER_DATA": str(tmp_path), "NETKEEPER_CONFIG": str(config)},
    )
    assert result.returncode == 1
    assert "192.0.2.10" in result.stderr


def test_chrome_script_normalizes_the_profile_spelling(tmp_path: Path) -> None:
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    result = start(Path(f"{profile}//"))
    assert result.returncode == 0, result.stderr
    assert f"--user-data-dir={profile}" in result.stdout
    assert f"--user-data-dir={profile}/" not in result.stdout


def test_chrome_script_clears_a_stale_lock_only_when_nothing_holds_the_profile(
    tmp_path: Path,
) -> None:
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (profile / name).symlink_to("otherhost-999999")
    result = start(profile)
    assert result.returncode == 0, result.stderr
    assert "stale profile lock" in result.stdout
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        assert f"would: rm {profile / name}" in result.stdout
        assert (profile / name).is_symlink(), "a dry run removed the lock"


def test_chrome_script_refuses_when_a_chrome_holds_the_profile_under_another_spelling(
    tmp_path: Path,
) -> None:
    """The #182 review's reproduction: a trailing slash hid a running Chrome."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to("thishost-999999")
    with process_with_args(f"--user-data-dir={profile}"):
        result = start(Path(f"{profile}/"))
    assert result.returncode == 1
    assert "running on this profile" in result.stderr
    assert (profile / "SingletonLock").is_symlink()


def test_chrome_script_trusts_the_locks_pid_over_the_command_line(tmp_path: Path) -> None:
    """A Chrome started with a spelling the script cannot match still holds its lock."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    with process_named(tmp_path, "Google Chrome") as pid:
        (profile / "SingletonLock").symlink_to(f"otherhost-{pid}")
        result = start(profile)
    assert result.returncode == 1
    assert f"pid {pid}" in result.stderr
    assert (profile / "SingletonLock").is_symlink()


def test_chrome_script_treats_a_lock_naming_a_live_non_chrome_process_as_stale(
    tmp_path: Path,
) -> None:
    """A crashed Chrome's pid can be reused by anything; that is not a holder."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    with process_named(tmp_path, "sleep") as pid:
        (profile / "SingletonLock").symlink_to(f"thishost-{pid}")
        result = start(profile)
    assert result.returncode == 0, result.stderr
    assert "stale profile lock" in result.stdout


def test_chrome_script_does_not_count_a_profile_whose_path_merely_starts_the_same(
    tmp_path: Path,
) -> None:
    profile = tmp_path.resolve() / "profile"
    with process_with_args(f"--user-data-dir={profile}-old"):
        result = run_sh(
            CHROME, "--status", "--port", str(free_port()), "--profile-dir", str(profile), env={}
        )
    assert result.returncode == 0, result.stderr
    assert "not running on this profile" in result.stdout


def test_chrome_script_refuses_a_port_that_answers_for_another_profile(tmp_path: Path) -> None:
    with fake_cdp_endpoint() as port:
        result = start(tmp_path.resolve() / "profile", port=port)
    assert result.returncode == 1
    assert "not on this profile" in result.stderr


def test_chrome_script_refuses_a_port_that_is_not_a_number(tmp_path: Path) -> None:
    result = run_sh(CHROME, "--dry-run", "--port", "9222x", "--profile-dir", str(tmp_path), env={})
    assert result.returncode == 1
    assert "must be a number" in result.stderr


def test_chrome_script_refuses_a_port_something_else_holds(tmp_path: Path) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        result = start(tmp_path.resolve(), port=s.getsockname()[1])
    assert result.returncode == 1
    assert "not a debuggable Chrome" in result.stderr


# --- reset-data.sh ---------------------------------------------------------------


@pytest.fixture
def data(tmp_path: Path) -> Path:
    """A data directory with a space in it, like the macOS default."""
    path = tmp_path / "Application Support"
    path.mkdir()
    return path


def make_db(data: Path, value: int = 42) -> Path:
    db = data / "netkeeper.sqlite3"
    db.unlink(missing_ok=True)
    # `with sqlite3.connect(...)` commits but never closes, and an open handle is
    # exactly what reset-data.sh refuses to run under.
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("create table marker (x integer)")
        conn.execute("insert into marker values (?)", (value,))
    return db


def marker(db: Path) -> int:
    with closing(sqlite3.connect(db)) as conn:
        row = conn.execute("select x from marker").fetchone()
    return int(row[0])


def test_reset_dry_run_changes_nothing(data: Path) -> None:
    db = make_db(data)
    result = run_sh(RESET, "--data-dir", str(data), "--dry-run", env={})
    assert result.returncode == 0, result.stderr
    assert db.exists()
    assert not (data / "archives").exists()


def test_reset_archives_then_removes_and_restore_puts_it_back(data: Path) -> None:
    db = make_db(data)
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    assert result.returncode == 0, result.stderr
    assert "verified" in result.stdout
    assert not db.exists()
    archives = sorted((data / "archives").iterdir())
    assert len(archives) == 1
    assert marker(archives[0]) == 42

    listed = run_sh(RESET, "--data-dir", str(data), "--list", env={})
    assert listed.returncode == 0, listed.stderr
    # One row, whole: a space in the data directory must not split the path.
    (row,) = listed.stdout.splitlines()
    assert row.startswith(f"{archives[0].name}  ")
    assert "No such file" not in listed.stderr

    restored = run_sh(
        RESET, "--data-dir", str(data), "--restore", archives[0].name, "--yes", env={}
    )
    assert restored.returncode == 0, restored.stderr
    assert marker(db) == 42


def test_reset_refuses_while_the_database_is_open(data: Path) -> None:
    db = make_db(data)
    with closing(sqlite3.connect(db)) as holder:
        holder.execute("select 1").fetchone()
        result = run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    assert result.returncode == 1
    assert "open in another process" in result.stderr
    assert marker(db) == 42
    assert not (data / "archives").exists()


def test_reset_refuses_without_lsof_rather_than_assume_the_database_is_free(
    data: Path, tmp_path: Path
) -> None:
    make_db(data)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in (
        "sqlite3",
        "sed",
        "awk",
        "date",
        "mktemp",
        "wc",
        "tr",
        "sort",
        "basename",
        "dirname",
        "cp",
        "rm",
        "mkdir",
        "ps",
        "grep",
        "cat",
        "sh",
    ):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": str(bin_dir)})
    assert result.returncode == 1
    assert "lsof is needed" in result.stderr
    assert (data / "netkeeper.sqlite3").exists()


def test_reset_keeps_the_database_when_the_archive_does_not_verify(
    data: Path, tmp_path: Path
) -> None:
    """An archive that is not a readable database must stop the delete."""
    db = make_db(data)
    real = shutil.which("sqlite3")
    assert real is not None
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "sqlite3"
    # A VACUUM INTO that claims success and writes nothing.
    shim.write_text(f'#!/bin/sh\ncase "$2" in VACUUM*) exit 0 ;; esac\nexec "{real}" "$@"\n')
    shim.chmod(0o755)
    path = f"{shim_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 1
    assert "quick_check" in result.stderr
    assert marker(db) == 42


def test_reset_answering_no_changes_nothing(data: Path) -> None:
    db = make_db(data)
    result = run_sh(RESET, "--data-dir", str(data), env={}, stdin="n\n")
    assert result.returncode == 0, result.stderr
    assert "nothing changed" in result.stdout
    assert db.exists()
    assert not (data / "archives").exists()


def test_restore_archives_the_database_in_place_first(data: Path) -> None:
    db = make_db(data, value=1)
    run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    (first,) = (data / "archives").iterdir()
    make_db(data, value=2)
    restored = run_sh(RESET, "--data-dir", str(data), "--restore", first.name, "--yes", env={})
    assert restored.returncode == 0, restored.stderr
    assert marker(db) == 1
    archived = sorted(p for p in (data / "archives").iterdir() if p != first)
    assert [marker(p) for p in archived] == [2]


def test_two_archives_in_one_second_never_share_a_name(data: Path) -> None:
    """The #182 review's reproduction: the second restore overwrote the first archive."""
    db = make_db(data, value=1)
    run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    (source,) = (data / "archives").iterdir()
    for value in (2, 3):
        make_db(data, value=value)
        result = run_sh(RESET, "--data-dir", str(data), "--restore", source.name, "--yes", env={})
        assert result.returncode == 0, result.stderr
    kept = sorted(marker(p) for p in (data / "archives").iterdir())
    assert kept == [1, 2, 3]
    assert marker(db) == 1


def test_restore_refuses_a_file_that_is_not_a_database(data: Path, tmp_path: Path) -> None:
    db = make_db(data)
    junk = tmp_path / "junk.sqlite3"
    junk.write_text("not a database")
    result = run_sh(RESET, "--data-dir", str(data), "--restore", str(junk), "--yes", env={})
    assert result.returncode == 1
    assert "quick_check" in result.stderr
    assert marker(db) == 42


def test_reset_refuses_when_the_server_uses_another_database(data: Path) -> None:
    db = make_db(data)
    result = run_sh(
        RESET,
        "--data-dir",
        str(data),
        "--yes",
        env={"NETKEEPER_DATABASE_URL": "postgresql://localhost/netkeeper"},
    )
    assert result.returncode == 1
    assert "NETKEEPER_DATABASE_URL" in result.stderr
    assert db.exists()


def test_reset_defaults_to_netkeeper_data(data: Path) -> None:
    db = make_db(data)
    result = run_sh(RESET, "--dry-run", env={"NETKEEPER_DATA": str(data)})
    assert result.returncode == 0, result.stderr
    assert str(db) in result.stdout
