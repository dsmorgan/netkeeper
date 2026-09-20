"""scripts/install-launchd.sh and scripts/serve-launchd.sh, run through sh.

launchctl is never called: every installer run here is a dry run, and the
installer honors NETKEEPER_LAUNCHD_UNAME so the same tests run on macOS and Linux.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install-launchd.sh"
WRAPPER = ROOT / "scripts" / "serve-launchd.sh"
LABEL = "fun.tnkr.netkeeper"
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None or sys.platform == "win32", reason="needs a POSIX sh")


def run_sh(
    script: Path, *args: str, env: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    assert SH is not None
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
    return subprocess.run(
        [SH, str(script), *args],
        capture_output=True,
        text=True,
        env=full_env,
        cwd=cwd,
        check=False,
    )


def run_installer(
    *args: str, home: Path, uname: str = "Darwin", cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {"HOME": str(home), "NETKEEPER_LAUNCHD_UNAME": uname}
    return run_sh(INSTALLER, *args, env=env, cwd=cwd)


def plist_in(stdout: str) -> dict[str, Any]:
    start = stdout.index("<?xml")
    end = stdout.index("</plist>") + len("</plist>")
    parsed: dict[str, Any] = plistlib.loads(stdout[start:end].encode())
    return parsed


def agent_plist(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def test_dry_run_renders_the_agent_plist(tmp_path: Path) -> None:
    data = tmp_path / "data"
    proc = run_installer(
        "--dry-run",
        "--data-dir",
        str(data),
        "--host",
        "127.0.0.1",
        "--port",
        "8791",
        home=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    plist = plist_in(proc.stdout)
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == [str(WRAPPER), "--host", "127.0.0.1", "--port", "8791"]
    assert plist["WorkingDirectory"] == str(ROOT)
    assert plist["EnvironmentVariables"] == {"NETKEEPER_DATA": str(data)}
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ThrottleInterval"] == 10
    assert plist["StandardOutPath"] == str(data / "logs" / "serve.log")
    assert plist["StandardErrorPath"] == str(data / "logs" / "serve.err.log")
    uid = os.getuid()
    assert f"launchctl bootout gui/{uid}/{LABEL}" in proc.stdout
    assert f"launchctl bootstrap gui/{uid} {agent_plist(tmp_path)}" in proc.stdout
    assert f"launchctl print gui/{uid}/{LABEL}" in proc.stdout
    # A dry run touches nothing.
    assert not data.exists()
    assert not (tmp_path / "Library").exists()


def test_dry_run_without_data_dir_leaves_the_platform_default(tmp_path: Path) -> None:
    proc = run_installer("--dry-run", home=tmp_path)
    assert proc.returncode == 0, proc.stderr
    plist = plist_in(proc.stdout)
    assert plist["ProgramArguments"] == [str(WRAPPER)]
    assert "EnvironmentVariables" not in plist
    logs = tmp_path / "Library" / "Application Support" / "netkeeper" / "logs"
    assert plist["StandardOutPath"] == str(logs / "serve.log")
    assert plist["StandardErrorPath"] == str(logs / "serve.err.log")


def test_relative_data_dir_is_made_absolute(tmp_path: Path) -> None:
    proc = run_installer("--dry-run", "--data-dir", "nk-data", home=tmp_path, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    plist = plist_in(proc.stdout)
    assert plist["EnvironmentVariables"] == {"NETKEEPER_DATA": str(tmp_path / "nk-data")}


def test_help_exits_zero(tmp_path: Path) -> None:
    proc = run_installer("--help", home=tmp_path, uname="Linux")
    assert proc.returncode == 0, proc.stderr
    assert "Usage:" in proc.stdout
    for flag in ("--data-dir", "--host", "--port", "--uninstall", "--dry-run"):
        assert flag in proc.stdout


def test_uninstall_dry_run_prints_bootout_and_rm(tmp_path: Path) -> None:
    proc = run_installer("--uninstall", "--dry-run", home=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert f"launchctl bootout gui/{os.getuid()}/{LABEL}" in proc.stdout
    assert f"rm -f {agent_plist(tmp_path)}" in proc.stdout
    assert "<?xml" not in proc.stdout


def test_refuses_to_run_off_macos(tmp_path: Path) -> None:
    proc = run_installer("--dry-run", home=tmp_path, uname="Linux")
    assert proc.returncode == 1
    assert "macOS only" in proc.stderr
    assert "Linux" in proc.stderr
    assert "<?xml" not in proc.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["--dry-run", "--bogus"],
        ["--dry-run", "--port", "eighty"],
        ["--dry-run", "--port"],
        ["--dry-run", "--data-dir"],
        ["--dry-run", "--host"],
    ],
)
def test_bad_arguments_exit_2_with_usage(tmp_path: Path, args: list[str]) -> None:
    proc = run_installer(*args, home=tmp_path)
    assert proc.returncode == 2
    assert "Usage:" in proc.stderr
    assert "<?xml" not in proc.stdout


def test_wrapper_migrates_then_execs_serve(tmp_path: Path) -> None:
    calls = tmp_path / "calls.log"
    fake = tmp_path / "netkeeper"
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n')
    fake.chmod(0o755)
    proc = run_sh(
        WRAPPER, "--host", "127.0.0.1", "--port", "8791", env={"NETKEEPER_BIN": str(fake)}
    )
    assert proc.returncode == 0, proc.stderr
    assert calls.read_text().splitlines() == ["db upgrade", "serve --host 127.0.0.1 --port 8791"]
    assert "applying migrations" in proc.stdout
    assert "starting netkeeper serve --host 127.0.0.1 --port 8791" in proc.stdout


def test_wrapper_fails_clearly_without_the_venv(tmp_path: Path) -> None:
    proc = run_sh(WRAPPER, env={"NETKEEPER_BIN": str(tmp_path / "missing")})
    assert proc.returncode == 1
    assert "make install" in proc.stderr
