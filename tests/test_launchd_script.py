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

from netkeeper.paths import data_dir

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
    *args: str,
    home: Path,
    uname: str = "Darwin",
    cwd: Path | None = None,
    script: Path = INSTALLER,
) -> subprocess.CompletedProcess[str]:
    env = {"HOME": str(home), "NETKEEPER_LAUNCHD_UNAME": uname}
    return run_sh(script, *args, env=env, cwd=cwd)


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


def test_default_log_dir_matches_the_apps_macos_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer hardcodes the macOS default; this fails if paths.py moves it."""
    monkeypatch.delenv("NETKEEPER_DATA", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = data_dir()
    assert expected.is_relative_to(tmp_path)
    proc = run_installer("--dry-run", home=tmp_path)
    assert proc.returncode == 0, proc.stderr
    plist = plist_in(proc.stdout)
    assert plist["StandardOutPath"] == str(expected / "logs" / "serve.log")
    assert plist["StandardErrorPath"] == str(expected / "logs" / "serve.err.log")


def test_relative_data_dir_is_made_absolute(tmp_path: Path) -> None:
    proc = run_installer("--dry-run", "--data-dir", "nk-data", home=tmp_path, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    plist = plist_in(proc.stdout)
    assert plist["EnvironmentVariables"] == {"NETKEEPER_DATA": str(tmp_path / "nk-data")}


@pytest.mark.parametrize("name", ["nk data", "nk & <data>"])
def test_data_dir_with_spaces_and_xml_characters_round_trips(tmp_path: Path, name: str) -> None:
    data = tmp_path / name
    proc = run_installer("--dry-run", "--data-dir", str(data), home=tmp_path)
    assert proc.returncode == 0, proc.stderr
    plist = plist_in(proc.stdout)
    assert plist["EnvironmentVariables"] == {"NETKEEPER_DATA": str(data)}
    assert plist["StandardOutPath"] == str(data / "logs" / "serve.log")
    assert plist["StandardErrorPath"] == str(data / "logs" / "serve.err.log")


def test_equals_forms_are_accepted(tmp_path: Path) -> None:
    data = tmp_path / "data"
    proc = run_installer(
        "--dry-run", f"--data-dir={data}", "--host=0.0.0.0", "--port=65535", home=tmp_path
    )
    assert proc.returncode == 0, proc.stderr
    plist = plist_in(proc.stdout)
    assert plist["EnvironmentVariables"] == {"NETKEEPER_DATA": str(data)}
    assert plist["ProgramArguments"] == [str(WRAPPER), "--host", "0.0.0.0", "--port", "65535"]


def test_installer_through_a_symlink_finds_the_real_repo(tmp_path: Path) -> None:
    absolute = tmp_path / "bin" / "install-launchd.sh"
    absolute.parent.mkdir()
    absolute.symlink_to(INSTALLER)
    relative = tmp_path / "bin" / "nested" / "install.sh"
    relative.parent.mkdir()
    relative.symlink_to(os.path.relpath(INSTALLER, relative.parent))
    for link in (absolute, relative):
        proc = run_installer("--dry-run", home=tmp_path, script=link)
        assert proc.returncode == 0, proc.stderr
        plist = plist_in(proc.stdout)
        assert plist["WorkingDirectory"] == str(ROOT), link
        assert plist["ProgramArguments"][0] == str(WRAPPER), link


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
        ["--dry-run", "--port", "0"],
        ["--dry-run", "--port", "65536"],
        ["--dry-run", "--port=99999999999999999999"],
        ["--dry-run", "--port"],
        ["--dry-run", "--data-dir"],
        ["--dry-run", "--data-dir="],
        ["--dry-run", "--host"],
        ["--dry-run", "--host="],
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


def test_wrapper_through_a_symlink_resolves_the_real_repo(tmp_path: Path) -> None:
    """A copy of the wrapper in a repo-shaped tmp dir, reached through a relative symlink.

    Without a .venv there, the error names the venv path under the copy's repo, not
    under the symlink's directory, which shows the resolver followed the link.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    real = repo / "scripts" / "serve-launchd.sh"
    shutil.copy(WRAPPER, real)
    link = tmp_path / "elsewhere" / "serve.sh"
    link.parent.mkdir()
    link.symlink_to(os.path.relpath(real, link.parent))
    proc = run_sh(link, env={})
    assert proc.returncode == 1
    assert str(repo.resolve() / ".venv" / "bin" / "netkeeper") in proc.stderr
    assert "elsewhere" not in proc.stderr
