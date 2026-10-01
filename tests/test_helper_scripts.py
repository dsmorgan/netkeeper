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
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlparse

import pytest

from netkeeper.services import browser_launch

ROOT = Path(__file__).resolve().parent.parent
CHROME = ROOT / "scripts" / "chrome.sh"
RESET = ROOT / "scripts" / "reset-data.sh"
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None or sys.platform == "win32", reason="needs a POSIX sh")


def _config_cdp_port(config_path: str) -> str | None:
    """The port in a NETKEEPER_CONFIG file's ``[linkedin] cdp_url``, or
    ``None`` if the file cannot be read, has no ``cdp_url``, or that URL
    names no explicit port (#183 re-review gap: a config with no ``cdp_url``,
    or one with no port at all, must not be read as "pinned" just because
    the key was set)."""
    try:
        text = Path(config_path).read_text()
    except OSError:
        return None
    match = re.search(r'cdp_url\s*=\s*"([^"]*)"', text)
    if match is None:
        return None
    port = urlparse(match.group(1)).port
    return None if port is None else str(port)


def _require_pinned_port(
    args: tuple[str, ...], env: dict[str, str], pinned_port: int | str | None
) -> None:
    """Refuse to run chrome.sh in a test unless this call pins its port away
    from 9222 -- the real default, and David's real Chrome's port.

    Falling through to chrome.sh's own DEFAULT_PORT, on a machine where a real
    `netkeeper` and a real Chrome both happen to be reachable, reaches that
    real Chrome: a #183 re-review probe did exactly this. ``--port`` is the
    direct way to pin it; ``NETKEEPER_CONFIG`` (a test's own ``cdp_url``) is
    another -- read for real, not trusted on sight: a config with no
    ``cdp_url``, or one that still says 9222, must not pass (#183 re-review
    gap). ``pinned_port`` is for a test that controls the port through a fake
    netkeeper's own JSON answer instead of either -- it must state the value
    it configured that fake with, so this guard can check it rather than
    trusting the test to have gotten it right silently.
    """
    literal: str | None = None
    args_list = list(args)
    for i, arg in enumerate(args_list):
        if arg == "--port" and i + 1 < len(args_list):
            literal = args_list[i + 1]
        elif arg.startswith("--port="):
            literal = arg[len("--port=") :]
    if literal is not None:
        assert literal != "9222", "a chrome.sh test must pin a port other than 9222"
        return
    if "NETKEEPER_CONFIG" in env:
        config_port = _config_cdp_port(env["NETKEEPER_CONFIG"])
        if config_port is None:
            raise AssertionError(
                "a chrome.sh test's NETKEEPER_CONFIG must set a [linkedin] cdp_url "
                "with an explicit port -- this one had none, or could not be read"
            )
        assert config_port != "9222", "a chrome.sh test must pin a port other than 9222"
        return
    if pinned_port is not None:
        assert str(pinned_port) != "9222", "a chrome.sh test must pin a port other than 9222"
        return
    raise AssertionError(
        "a chrome.sh invocation in tests must pin a port other than 9222 -- pass "
        "--port, set NETKEEPER_CONFIG to a cdp_url with an explicit non-9222 port, "
        "or pass run_sh(..., pinned_port=N) for a fake netkeeper's own JSON answer. "
        "Never let a test fall through to the real default: that can reach a real "
        "Chrome on a machine where one is actually running on it."
    )


def _curl_shim(shim_dir: Path) -> None:
    """A `curl` on PATH that refuses any invocation naming port 9222, as a
    backstop that does not depend on a test author remembering to pin one
    (#183 re-review gap): `_require_pinned_port` only catches what a test
    *says* it will do; this catches what chrome.sh actually tries to connect
    to, no matter how it got there. The real curl's path is resolved before
    the shim is built, since by the time it runs, PATH has this directory
    in front of it.
    """
    real = shutil.which("curl")
    assert real is not None, "curl must be on PATH for the shim to fall back to"
    shim = shim_dir / "curl"
    shim.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        "    *:9222*)\n"
        "      echo 'refused: 9222' >&2\n"
        "      exit 1\n"
        "      ;;\n"
        "  esac\n"
        "done\n"
        f'exec "{real}" "$@"\n'
    )
    shim.chmod(0o755)


def run_sh(
    script: Path,
    *args: str,
    env: dict[str, str],
    stdin: str | None = None,
    pinned_port: int | str | None = None,
    allow_no_curl: bool = False,
) -> subprocess.CompletedProcess[str]:
    """``allow_no_curl`` skips the curl-shim backstop below, for the one test
    that deliberately runs with no curl at all, to pin chrome.sh's own
    ``command -v curl`` check (C9): the shim's fallback always finds a real
    curl, so leaving it in would quietly hand that test a working curl and
    defeat its own point. Nothing unsafe about the opt-out either way -- a
    missing curl can place no network call at all, let alone one to 9222."""
    assert SH is not None
    if script.name == "chrome.sh":
        _require_pinned_port(args, env, pinned_port)
    # The interpreter's own bin first: chrome.sh finds netkeeper as <repo>/.venv/bin or on
    # PATH, and a sibling worktree has no .venv of its own (#210).
    path = os.pathsep.join(
        (str(Path(sys.executable).parent), os.environ.get("PATH", "/usr/bin:/bin"))
    )
    full_env = {"PATH": path, "HOME": "/nonexistent", **env}
    if allow_no_curl:
        return subprocess.run(
            [SH, str(script), *args],
            capture_output=True,
            text=True,
            env=full_env,
            input=stdin,
            timeout=60,
        )
    # A curl shim ahead of whatever PATH was just built -- even a test's own
    # custom one, which otherwise would have replaced the line above entirely:
    # the backstop for #183's guard above, so a port 9222 that reached curl
    # despite it is refused too, whether or not a test caller remembered to
    # pin one. lsof and ps are read-only (they report on an existing
    # listener; they never open a connection to it themselves), so they need
    # no shim.
    shim_dir = Path(tempfile.mkdtemp(prefix="chrome-sh-curl-shim-"))
    try:
        _curl_shim(shim_dir)
        full_env["PATH"] = os.pathsep.join((str(shim_dir), full_env.get("PATH", "")))
        return subprocess.run(
            [SH, str(script), *args],
            capture_output=True,
            text=True,
            env=full_env,
            input=stdin,
            timeout=60,
        )
    finally:
        shutil.rmtree(shim_dir, ignore_errors=True)


def chrome_flags(line: str) -> list[str]:
    return [f for f in re.findall(r"(?<!\S)--[a-z-]+", line) if f != "--args"]


def isolated_chrome_script(tmp_path: Path) -> Path:
    """A copy of chrome.sh under a fake repo root with no ``.venv`` of its own.

    chrome.sh always prefers ``<repo>/.venv/bin/netkeeper`` over anything on
    PATH, and this repo's own ``.venv`` exists once ``make install`` has run
    -- so a test of the PATH/``command -v netkeeper`` branch (a `uv tool`/pipx
    install, #183 review should-fix 2) has to run the script from somewhere
    that branch actually gets a chance to run, not the real checkout.
    """
    repo = tmp_path / "isolated-repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    copy = scripts / "chrome.sh"
    copy.write_text(CHROME.read_text())
    copy.chmod(0o755)
    return copy


def fake_netkeeper_cli(
    path: Path,
    *,
    port: int | str,
    profile: Path,
    remote: str | None = None,
    shebang: str | None = None,
) -> None:
    """A stand-in `netkeeper` whose `browser launch --json` answers like the real
    CLI's, with a real python shebang -- enough for chrome.sh to ask it for the
    port and profile the way it would ask the genuine one. ``port`` may be a
    non-numeric string, to simulate a malformed answer. ``shebang`` defaults to
    a direct absolute-path interpreter (what `uv sync`/pip normally write);
    pass e.g. ``"/usr/bin/env python3"`` for an env-style entry point instead.
    """
    path.write_text(
        f"#!{shebang or sys.executable}\n"
        "import json, sys\n"
        "if sys.argv[1:4] == ['browser', 'launch', '--json']:\n"
        "    print(json.dumps({\n"
        f"        'cdp_url': 'http://127.0.0.1:{port}',\n"
        f"        'port': {port!r},\n"
        f"        'profile': {str(profile)!r},\n"
        f"        'remote': {remote!r},\n"
        "    }))\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def fake_netkeeper_cli_sh_shim(path: Path, *, port: int, profile: Path) -> None:
    """A stand-in `netkeeper` whose entry point is itself a ``#!/bin/sh``
    relaunch shim (what `uv tool`'s distlib-style launcher uses) rather than
    python directly. The JSON it prints must still get parsed by a *real*
    python found separately -- never by handing the json.load snippet to this
    shell itself (#183 re-review should-fix 2).
    """
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = browser ] && [ "$2" = launch ] && [ "$3" = --json ]; then\n'
        f'  printf \'{{"cdp_url": "http://127.0.0.1:{port}", "port": {port},'
        f' "profile": "{profile}", "remote": null}}\'\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    path.chmod(0o755)


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
def process_named_holding_open(directory: Path, name: str, held: Path) -> Iterator[int]:
    """A live process named ``directory/name`` (``ps -o comm=`` reports the symlink's
    own name, not the interpreter it points to) that keeps ``held`` open until killed.

    What tells an actual holder of a profile apart from a pid that merely matches
    by name or number (#183 review bug 1): ``lock_holder()`` now requires the
    locking pid to have a file open under the profile, not just to be alive and
    Chrome-named.
    """
    held.parent.mkdir(parents=True, exist_ok=True)
    held.touch(exist_ok=True)
    exe = directory / name
    exe.symlink_to(sys.executable)
    proc = subprocess.Popen(
        [str(exe), "-c", f"f = open({str(held)!r}); import time; time.sleep(60)"]
    )
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


def test_run_sh_refuses_an_unpinned_chrome_invocation() -> None:
    """Safety gap: the harness itself must refuse to run chrome.sh without an
    explicit, non-9222 port pinned some way -- a re-review probe reached
    David's real Chrome by falling through to chrome.sh's own default."""
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", env={})


def test_run_sh_refuses_an_explicit_port_9222() -> None:
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", "--port", "9222", env={})


def test_run_sh_refuses_pinned_port_9222() -> None:
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", env={}, pinned_port=9222)


def test_run_sh_accepts_netkeeper_config_with_no_explicit_port(tmp_path: Path) -> None:
    """NETKEEPER_CONFIG alone satisfies the guard: the test's own cdp_url pins
    the port, even with no --port flag on the command line."""
    config = tmp_path / "config.toml"
    config.write_text(f'[linkedin]\ncdp_url = "http://127.0.0.1:{free_port()}"\n')
    run_sh(CHROME, "--dry-run", env={"NETKEEPER_CONFIG": str(config)})


def test_run_sh_refuses_netkeeper_config_with_no_cdp_url(tmp_path: Path) -> None:
    """#183 re-review gap: a NETKEEPER_CONFIG with no cdp_url at all must not
    be read as pinning anything -- it falls through to chrome.sh's own 9222
    default exactly as surely as having no NETKEEPER_CONFIG would."""
    config = tmp_path / "config.toml"
    config.write_text("[linkedin]\n")
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", env={"NETKEEPER_CONFIG": str(config)})


def test_run_sh_refuses_netkeeper_config_with_an_unreadable_path(tmp_path: Path) -> None:
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", env={"NETKEEPER_CONFIG": str(tmp_path / "missing.toml")})


def test_run_sh_refuses_netkeeper_config_with_port_9222(tmp_path: Path) -> None:
    """#183 re-review gap: a cdp_url that still says 9222 must not pass either."""
    config = tmp_path / "config.toml"
    config.write_text('[linkedin]\ncdp_url = "http://127.0.0.1:9222"\n')
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", env={"NETKEEPER_CONFIG": str(config)})


def test_run_sh_refuses_netkeeper_config_with_no_port_in_the_url(tmp_path: Path) -> None:
    """#183 re-review gap: a cdp_url naming no explicit port at all must not
    pass either -- there is nothing here to confirm isn't 9222."""
    config = tmp_path / "config.toml"
    config.write_text('[linkedin]\ncdp_url = "http://127.0.0.1"\n')
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", env={"NETKEEPER_CONFIG": str(config)})


def test_curl_shim_refuses_a_9222_argument_without_a_network_call(tmp_path: Path) -> None:
    """#183 re-review gap: the curl-shim backstop must refuse a :9222 URL by
    inspecting its arguments alone, never by attempting the request first and
    failing some other way. A non-routable address (RFC 5737) proves it: if
    the shim fell through to a real request, this would hang or time out
    instead of refusing at once."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _curl_shim(shim_dir)
    start = time.monotonic()
    result = subprocess.run(
        [str(shim_dir / "curl"), "-fsS", "--max-time", "2", "http://192.0.2.1:9222/json/version"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    elapsed = time.monotonic() - start
    assert result.returncode != 0
    assert "refused: 9222" in result.stderr
    assert elapsed < 1, "the shim must refuse instantly, not attempt the request first"


def test_curl_shim_passes_through_anything_else(tmp_path: Path) -> None:
    """The shim must still work as curl for every other argument."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _curl_shim(shim_dir)
    result = subprocess.run(
        [str(shim_dir / "curl"), "--version"], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0
    assert "curl" in result.stdout.lower()


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
    # 192.0.2.0/24 (RFC 5737) is non-routable: this test is about the host
    # mismatch, not the port, so the port is deliberately not 9222 anyway (the
    # run_sh guard below requires that of every NETKEEPER_CONFIG it is handed).
    config.write_text('[linkedin]\ncdp_url = "http://192.0.2.10:9333"\n')
    result = run_sh(
        CHROME,
        "--dry-run",
        env={"NETKEEPER_DATA": str(tmp_path), "NETKEEPER_CONFIG": str(config)},
    )
    assert result.returncode == 1
    assert "192.0.2.10" in result.stderr


def test_chrome_script_finds_port_and_profile_through_a_symlinked_netkeeper(
    tmp_path: Path,
) -> None:
    """#183 review should-fix 2: a `uv tool`/pipx install puts a symlink on
    PATH with no interpreter beside it. Reading python from the entry point's
    own shebang (which `head` follows straight through the symlink) must still
    work, with no `<repo>/.venv` around to fall back on."""
    script = isolated_chrome_script(tmp_path)
    port = free_port()
    profile = tmp_path.resolve() / "profile"
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    fake_netkeeper_cli(real_dir / "netkeeper", port=port, profile=profile)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "netkeeper").symlink_to(real_dir / "netkeeper")  # no python beside the symlink
    path = f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    result = run_sh(script, "--status", env={"PATH": path}, pinned_port=port)
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
    assert f"profile  {profile}" in result.stdout
    assert f"port     {port}: nothing is listening" in result.stdout


def test_chrome_script_resolves_an_env_shebang_entry_point(tmp_path: Path) -> None:
    """#183 re-review should-fix 2: an `#!/usr/bin/env python3` entry point
    (not a direct absolute-path shebang) must be resolved through PATH and
    used to parse the JSON -- honoring whatever custom profile it answers
    with, the same as a direct-shebang entry point would."""
    script = isolated_chrome_script(tmp_path)
    port = free_port()
    profile = tmp_path.resolve() / "a custom profile"
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    fake_netkeeper_cli(
        real_dir / "netkeeper", port=port, profile=profile, shebang="/usr/bin/env python3"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "netkeeper").symlink_to(real_dir / "netkeeper")
    # python3 must resolve through PATH for /usr/bin/env to find it; the venv
    # this suite runs under always has one beside `python` (uv/pip's doing).
    python_dir = Path(sys.executable).parent
    assert (python_dir / "python3").exists()
    path = f"{bin_dir}:{python_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    result = run_sh(script, "--status", env={"PATH": path}, pinned_port=port)
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
    assert f"profile  {profile}" in result.stdout


def test_chrome_script_falls_back_to_python3_for_a_sh_shim_entry_point(tmp_path: Path) -> None:
    """#183 re-review should-fix 2: an entry point that is itself a
    ``#!/bin/sh`` relaunch shim (uv tool's distlib-style launcher) must never
    be handed the json.load snippet -- that is not python, and running it
    under sh would just fail or garble. It falls back to plain `python3` on
    PATH to parse the answer instead."""
    script = isolated_chrome_script(tmp_path)
    port = free_port()
    profile = tmp_path.resolve() / "profile"
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    fake_netkeeper_cli_sh_shim(real_dir / "netkeeper", port=port, profile=profile)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "netkeeper").symlink_to(real_dir / "netkeeper")
    python_dir = Path(sys.executable).parent
    path = f"{bin_dir}:{python_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    result = run_sh(script, "--status", env={"PATH": path}, pinned_port=port)
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
    assert f"profile  {profile}" in result.stdout


def test_chrome_script_warns_when_no_python_can_parse_a_sh_shim_answer(tmp_path: Path) -> None:
    """#183 re-review should-fix 2: when netkeeper answered but no python could
    be found to parse it, the warning must say so specifically, not the
    generic "could not ask netkeeper" (which means something else: it was
    never asked at all, or never answered)."""
    script = isolated_chrome_script(tmp_path)
    port = free_port()
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    fake_netkeeper_cli_sh_shim(
        real_dir / "netkeeper", port=port, profile=tmp_path.resolve() / "profile"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "netkeeper").symlink_to(real_dir / "netkeeper")
    # Everything chrome.sh needs except any python: no python3 for it to fall
    # back to.
    minimal_bin = tmp_path / "minimal-bin"
    minimal_bin.mkdir()
    for tool in (
        "sh",
        "curl",
        "lsof",
        "ps",
        "awk",
        "sed",
        "uname",
        "mkdir",
        "basename",
        "dirname",
        "tr",
        "rm",
        "cat",
        "kill",
        "grep",
        "head",
    ):
        found = shutil.which(tool)
        if found:
            (minimal_bin / tool).symlink_to(found)
    path = f"{bin_dir}:{minimal_bin}"
    result = run_sh(script, "--dry-run", "--port", str(free_port()), env={"PATH": path})
    assert result.returncode == 0, result.stderr
    assert "netkeeper answered but no Python was found to parse it" in result.stderr


def test_chrome_script_round_trips_a_non_ascii_profile_path(tmp_path: Path) -> None:
    """#183 review should-fix 2: sed parsing the JSON directly mangled a
    profile path containing a quote, a backslash, or non-ASCII text (json.dumps
    escapes the last as \\uXXXX) -- a real parser must round-trip it intact."""
    port = free_port()
    config = tmp_path / "config.toml"
    config.write_text(f'[linkedin]\ncdp_url = "http://127.0.0.1:{port}"\n')
    data_dir = tmp_path / "José's Área"
    result = run_sh(
        CHROME,
        "--status",
        env={"NETKEEPER_DATA": str(data_dir), "NETKEEPER_CONFIG": str(config)},
    )
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
    profile = data_dir.resolve() / browser_launch.CHROME_PROFILE_DIRNAME
    assert f"profile  {profile}" in result.stdout


def test_chrome_script_refuses_a_non_numeric_port_from_json(tmp_path: Path) -> None:
    """#183 review nit 4: with a real parser, a malformed answer's port must
    still be checked before reaching curl/lsof, not just --port's."""
    script = isolated_chrome_script(tmp_path)
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    fake_netkeeper_cli(
        real_dir / "netkeeper", port="not-a-port", profile=tmp_path.resolve() / "profile"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "netkeeper").symlink_to(real_dir / "netkeeper")
    path = f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    result = run_sh(script, "--dry-run", env={"PATH": path}, pinned_port="not-a-port")
    assert result.returncode == 1
    assert "must be a number" in result.stderr


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
    """A Chrome started with a spelling the script cannot match still holds its lock,
    as long as it genuinely has the profile open. Found only through the lock
    file, not a command-line match, so the message names it (#183 review bug 1)."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    with process_named_holding_open(tmp_path, "Google Chrome", profile / "held") as pid:
        (profile / "SingletonLock").symlink_to(f"otherhost-{pid}")
        result = start(profile)
    assert result.returncode == 1
    assert f"pid {pid}" in result.stderr
    assert f"(per {profile}/SingletonLock)" in result.stderr
    # #183 re-review nit 4: an escape hatch for when the heuristics are wrong.
    assert (
        f"If pid {pid} isn't netkeeper's Chrome, remove {profile}/SingletonLock." in result.stderr
    )
    assert (profile / "SingletonLock").is_symlink()


def test_chrome_script_does_not_count_a_file_open_in_a_profile_whose_name_merely_starts_the_same(
    tmp_path: Path,
) -> None:
    """Pins the trailing slash in lock_holder()'s lsof check: a file open under
    chrome-profile-old must not count as chrome-profile itself being open."""
    profile = tmp_path.resolve() / "chrome-profile"
    profile.mkdir()
    sibling = tmp_path.resolve() / "chrome-profile-old"
    with process_named_holding_open(tmp_path, "Google Chrome", sibling / "held") as pid:
        (profile / "SingletonLock").symlink_to(f"thishost-{pid}")
        result = start(profile)
    assert result.returncode == 0, result.stderr
    assert "stale profile lock" in result.stdout


def test_chrome_script_accepts_a_holder_whose_own_flag_canonicalizes_to_the_profile(
    tmp_path: Path,
) -> None:
    """#183 review nit 3: a holder's own --user-data-dir is read and
    canonicalized the same way $profile is, so a spelling profile_pids()'s
    plain substring match cannot catch (here, a symlinked alias) is still
    recognized -- without needing lsof to show anything open under it."""
    real_profile = tmp_path.resolve() / "real-profile"
    real_profile.mkdir()
    alias_profile = tmp_path.resolve() / "alias-profile"
    alias_profile.symlink_to(real_profile)
    exe = tmp_path / "Google Chrome"
    exe.symlink_to(sys.executable)
    proc = subprocess.Popen(
        [str(exe), "-c", "import time; time.sleep(60)", f"--user-data-dir={alias_profile}"]
    )
    try:
        (real_profile / "SingletonLock").symlink_to(f"thishost-{proc.pid}")
        result = start(real_profile)
    finally:
        proc.kill()
        proc.wait()
    assert result.returncode == 1
    assert f"pid {proc.pid}" in result.stderr


def test_chrome_script_treats_lock_holder_as_present_when_lsof_answers_nothing(
    tmp_path: Path,
) -> None:
    """#183 review nit 3: lsof returning nothing at all for a live, Chrome-named,
    non-helper pid (a startup race, or no permission to inspect it) is not proof
    there is no holder; lock_holder() must fail closed, not clear the lock."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    with process_named(tmp_path, "Google Chrome") as pid:
        (profile / "SingletonLock").symlink_to(f"thishost-{pid}")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "lsof"
        real = shutil.which("lsof")
        assert real is not None
        # Silent on -Fn (the lock-holder check); real lsof for port_listener's
        # -iTCP check, so the rest of the script still runs normally.
        shim.write_text(f'#!/bin/sh\ncase "$*" in *-Fn*) exit 0 ;; esac\nexec "{real}" "$@"\n')
        shim.chmod(0o755)
        path = f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        result = run_sh(
            CHROME,
            "--dry-run",
            "--port",
            str(free_port()),
            "--profile-dir",
            str(profile),
            env={"PATH": path},
        )
    assert result.returncode == 1
    assert f"pid {pid}" in result.stderr


def test_chrome_script_treats_a_mismatched_user_data_dir_flag_as_proof_of_absence(
    tmp_path: Path,
) -> None:
    """#183 re-review nit 3: a --user-data-dir naming a different profile is
    positive evidence this pid is not the holder. The empty-lsof fail-closed
    rule must not override that -- it is for when there is no flag to go on
    at all, not for second-guessing one that says otherwise."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    other = tmp_path.resolve() / "other-profile"
    exe = tmp_path / "Google Chrome"
    exe.symlink_to(sys.executable)
    proc = subprocess.Popen(
        [str(exe), "-c", "import time; time.sleep(60)", f"--user-data-dir={other}"]
    )
    try:
        (profile / "SingletonLock").symlink_to(f"thishost-{proc.pid}")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "lsof"
        real = shutil.which("lsof")
        assert real is not None
        # Empty on -Fn, as if nothing were open -- the fail-closed case this
        # must NOT trigger, because the mismatched flag already answers it.
        shim.write_text(f'#!/bin/sh\ncase "$*" in *-Fn*) exit 0 ;; esac\nexec "{real}" "$@"\n')
        shim.chmod(0o755)
        path = f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        result = run_sh(
            CHROME,
            "--dry-run",
            "--port",
            str(free_port()),
            "--profile-dir",
            str(profile),
            env={"PATH": path},
        )
    finally:
        proc.kill()
        proc.wait()
    assert result.returncode == 0, result.stderr
    assert "stale profile lock" in result.stdout


def test_chrome_script_refuses_a_lock_whose_pid_was_reused_by_an_unrelated_chrome(
    tmp_path: Path,
) -> None:
    """#183 review bug 1: a lock naming a pid Chrome once held, since reused by an
    entirely different Chrome (your everyday browser, not this profile's), must not
    be mistaken for this profile's holder -- it is treated as stale instead."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with process_named_holding_open(tmp_path, "Google Chrome", elsewhere / "held") as pid:
        (profile / "SingletonLock").symlink_to(f"thishost-{pid}")
        result = start(profile)
    assert result.returncode == 0, result.stderr
    assert "stale profile lock" in result.stdout


def test_chrome_script_skips_a_reused_pid_that_is_now_a_helper_process(tmp_path: Path) -> None:
    """#183 review bug 1: a `--type=` helper (renderer, GPU, utility) never holds
    SingletonLock itself; a reused pid landing on one is not this profile's Chrome,
    even with the profile's own files open for some unrelated reason."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    exe = tmp_path / "Google Chrome Helper"
    exe.symlink_to(sys.executable)
    held = profile / "held"
    held.touch()
    proc = subprocess.Popen(
        [str(exe), "-c", f"f = open({str(held)!r}); import time; time.sleep(60)", "--type=renderer"]
    )
    try:
        (profile / "SingletonLock").symlink_to(f"thishost-{proc.pid}")
        result = start(profile)
    finally:
        proc.kill()
        proc.wait()
    assert result.returncode == 0, result.stderr
    assert "stale profile lock" in result.stdout


def test_chrome_script_treats_a_lock_naming_a_dead_pid_as_stale(tmp_path: Path) -> None:
    """A lock naming a pid that is simply not running at all -- not merely some
    other, live, non-Chrome process -- must also read as stale: the ``kill -0``
    check, not only the executable-name check that follows it."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    proc = subprocess.Popen(["sh", "-c", "exit 0"])
    proc.wait()
    dead_pid = proc.pid
    (profile / "SingletonLock").symlink_to(f"thishost-{dead_pid}")
    result = start(profile)
    assert result.returncode == 0, result.stderr
    assert "stale profile lock" in result.stdout


def test_chrome_script_reports_a_holder_on_a_different_port(tmp_path: Path) -> None:
    """#183 review bug 2: a Chrome on this profile but a different debugging port
    has one, just not this one -- "without the debugging port" is the wrong
    message for it."""
    profile = tmp_path.resolve() / "profile"
    with process_with_args("--remote-debugging-port=9333", f"--user-data-dir={profile}") as pid:
        result = start(profile, port=free_port())
    assert result.returncode == 1
    assert "on port 9333" in result.stderr
    assert "without the debugging port" not in result.stderr
    # #183 re-review nit 4: an escape hatch for when the heuristics are wrong.
    assert (
        f"If pid {pid} isn't netkeeper's Chrome, remove {profile}/SingletonLock." in result.stderr
    )


def _bin_dir_without(tmp_path: Path, missing: str) -> Path:
    """A PATH containing everything chrome.sh needs except ``missing``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in (
        "curl",
        "lsof",
        "ps",
        "awk",
        "sed",
        "uname",
        "mkdir",
        "basename",
        "dirname",
        "tr",
        "rm",
        "cat",
        "kill",
        "grep",
        "sh",
    ):
        if tool == missing:
            continue
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    return bin_dir


def test_chrome_script_refuses_without_curl(tmp_path: Path) -> None:
    """#183 review C9: curl's own tool-requirement check, exercised on its own."""
    bin_dir = _bin_dir_without(tmp_path, "curl")
    result = run_sh(
        CHROME,
        "--dry-run",
        "--port",
        str(free_port()),
        "--profile-dir",
        str(tmp_path / "profile"),
        env={"PATH": str(bin_dir)},
        allow_no_curl=True,
    )
    assert result.returncode == 1
    assert "curl is needed" in result.stderr


def test_chrome_script_refuses_without_lsof(tmp_path: Path) -> None:
    """#183 review C15: lsof's own tool-requirement check, exercised on its own."""
    bin_dir = _bin_dir_without(tmp_path, "lsof")
    result = run_sh(
        CHROME,
        "--dry-run",
        "--port",
        str(free_port()),
        "--profile-dir",
        str(tmp_path / "profile"),
        env={"PATH": str(bin_dir)},
    )
    assert result.returncode == 1
    assert "lsof is needed" in result.stderr


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

# reset-data.sh archives and verifies with the sqlite3 command-line tool, which a
# fresh cloud container lacks (#231). There these tests skip with the fix in the
# reason. CI (GitHub Actions sets CI=true) always has the tool, so they never skip
# there: a missing sqlite3 in CI fails rather than quietly leaving the script untested.
needs_sqlite3_cli = pytest.mark.skipif(
    shutil.which("sqlite3") is None and not os.environ.get("CI"),
    reason="needs the sqlite3 command-line tool: apt-get install sqlite3",
)


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


@needs_sqlite3_cli
def test_reset_dry_run_changes_nothing(data: Path) -> None:
    db = make_db(data)
    result = run_sh(RESET, "--data-dir", str(data), "--dry-run", env={})
    assert result.returncode == 0, result.stderr
    assert db.exists()
    assert not (data / "archives").exists()


@needs_sqlite3_cli
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


@needs_sqlite3_cli
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


@needs_sqlite3_cli
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


@needs_sqlite3_cli
def test_reset_answering_no_changes_nothing(data: Path) -> None:
    db = make_db(data)
    result = run_sh(RESET, "--data-dir", str(data), env={}, stdin="n\n")
    assert result.returncode == 0, result.stderr
    assert "nothing changed" in result.stdout
    assert db.exists()
    assert not (data / "archives").exists()


@needs_sqlite3_cli
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


@needs_sqlite3_cli
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


@needs_sqlite3_cli
def test_restore_refuses_a_file_that_is_not_a_database(data: Path, tmp_path: Path) -> None:
    db = make_db(data)
    junk = tmp_path / "junk.sqlite3"
    junk.write_text("not a database")
    result = run_sh(RESET, "--data-dir", str(data), "--restore", str(junk), "--yes", env={})
    assert result.returncode == 1
    assert "quick_check" in result.stderr
    assert marker(db) == 42


@needs_sqlite3_cli
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


@needs_sqlite3_cli
def test_reset_defaults_to_netkeeper_data(data: Path) -> None:
    db = make_db(data)
    result = run_sh(RESET, "--dry-run", env={"NETKEEPER_DATA": str(data)})
    assert result.returncode == 0, result.stderr
    assert str(db) in result.stdout


def _lsof_shim(tmp_path: Path, script: str) -> str:
    """A PATH with ``script`` standing in for lsof, the rest of PATH behind it."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "lsof"
    shim.write_text(script)
    shim.chmod(0o755)
    return f"{shim_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"


@needs_sqlite3_cli
def test_reset_refuses_on_a_real_lsof_error(data: Path, tmp_path: Path) -> None:
    """R2: an actual lsof error -- not a missing binary, not a benign warning --
    must still refuse the reset rather than read as "nothing holds it"."""
    make_db(data)
    path = _lsof_shim(tmp_path, "#!/bin/sh\nprintf 'lsof: permission denied\\n' >&2\nexit 1\n")
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 1
    assert "lsof could not check the database" in result.stderr
    assert (data / "netkeeper.sqlite3").exists()


@needs_sqlite3_cli
def test_reset_ignores_a_benign_lsof_warning(data: Path, tmp_path: Path) -> None:
    """#183 review bug 7: an unrelated WARNING on lsof's stderr (a stale network
    mount it scans along the way) must not block a reset when nothing actually
    holds the database. Real warnings run to two or three lines -- the
    continuation lines matter too, not just the first."""
    db = make_db(data)
    real = shutil.which("lsof")
    assert real is not None
    path = _lsof_shim(
        tmp_path,
        "#!/bin/sh\n"
        "printf 'lsof: WARNING: can'\"'\"'t stat() nfs file system /mnt/stale\\n"
        "      Output information may be incomplete.\\n"
        '      assuming "dev=1234" from mount table\\n\' >&2\n'
        f'exec "{real}" "$@"\n',
    )
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 0, result.stderr
    assert not db.exists()


@needs_sqlite3_cli
def test_reset_refuses_when_only_the_wal_sidecar_is_held_open(data: Path) -> None:
    """R3: a holder on ``-wal`` alone, with no fd on the main file, must still
    refuse the reset."""
    db = make_db(data)
    wal = Path(f"{db}-wal")
    wal.write_bytes(b"")
    proc = subprocess.Popen(
        [sys.executable, "-c", f"f = open({str(wal)!r}, 'rb'); import time; time.sleep(30)"]
    )
    try:
        time.sleep(0.3)  # let the interpreter actually get to the open() call
        result = run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    finally:
        proc.kill()
        proc.wait()
    assert result.returncode == 1
    assert "open in another process" in result.stderr
    assert marker(db) == 42


@needs_sqlite3_cli
def test_reset_rechecks_after_the_confirmation_prompt(data: Path) -> None:
    """R4: a server that starts while the prompt is waiting on an answer must
    still block the reset -- the check made again after it, not only the one
    made before the prompt was shown."""
    assert SH is not None
    db = make_db(data)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"}
    proc = subprocess.Popen(
        [SH, str(RESET), "--data-dir", str(data)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        time.sleep(0.3)  # the prompt is up, waiting on stdin
        with closing(sqlite3.connect(db)) as holder:
            holder.execute("select 1").fetchone()
            assert proc.stdin is not None
            proc.stdin.write("y\n")
            proc.stdin.flush()
            stdout, stderr = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 1, stdout
    assert "open in another process" in stderr
    assert marker(db) == 42
    assert not (data / "archives").exists()


def test_reset_script_verifies_the_archive_read_only() -> None:
    """R6/R7: pin the literal `-readonly` flag on both of ``verified()``'s sqlite3
    calls -- dropping either would let a corrupt archive be (re)written to
    instead of failing, or silently create an empty file for a path that does
    not exist instead of reporting it unreadable."""
    text = RESET.read_text()
    assert text.count("sqlite3 -readonly") == 2


@needs_sqlite3_cli
def test_reset_renames_a_failed_archive_instead_of_leaving_it_behind(
    data: Path, tmp_path: Path
) -> None:
    """#183 review bug 5: an archive that was created but does not verify must not
    sit in archives/ looking like a good one; it is renamed ``.failed`` instead."""
    db = make_db(data)
    real = shutil.which("sqlite3")
    assert real is not None
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "sqlite3"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        "  VACUUM*)\n"
        "    target=$(printf '%s' \"$2\" | sed -n \"s/^VACUUM INTO '\\\\(.*\\\\)'\\$/\\\\1/p\")\n"
        "    printf 'not a real database' > \"$target\"\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        f'exec "{real}" "$@"\n'
    )
    shim.chmod(0o755)
    path = f"{shim_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 1
    assert "quick_check" in result.stderr
    assert marker(db) == 42
    entries = sorted(p.name for p in (data / "archives").iterdir())
    assert len(entries) == 1
    assert entries[0].endswith(".failed")


@needs_sqlite3_cli
def test_reset_quarantines_a_partial_archive_when_the_raw_copy_fails(
    data: Path, tmp_path: Path
) -> None:
    """Review should-fix 1: when the raw-copy fallback fails partway (an
    unreadable -wal), the partial archive must not sit in archives/ looking
    like a normal one, and must not appear in --list either."""
    db = data / "netkeeper.sqlite3"
    db.unlink(missing_ok=True)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE marker (x integer)")
        conn.execute("INSERT INTO marker VALUES (42)")
    wal = Path(f"{db}-wal")
    wal.touch(exist_ok=True)
    wal.chmod(0o000)
    try:
        real = shutil.which("sqlite3")
        assert real is not None
        shim_dir = tmp_path / "shim"
        shim_dir.mkdir()
        shim = shim_dir / "sqlite3"
        shim.write_text(f'#!/bin/sh\ncase "$2" in VACUUM*) exit 1 ;; esac\nexec "{real}" "$@"\n')
        shim.chmod(0o755)
        path = f"{shim_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
        assert result.returncode == 1
        assert "could not copy" in result.stderr

        archives = data / "archives"
        assert not list(archives.glob("*.sqlite3")), "a partial archive looks like a good one"
        assert list(archives.glob("*.sqlite3.failed")), "the partial copy should be quarantined"

        listed = run_sh(RESET, "--data-dir", str(data), "--list", env={})
        assert listed.returncode == 0, listed.stderr
        assert "no archives in" in listed.stdout
    finally:
        wal.chmod(0o644)  # so the temp directory's own teardown can remove it


@needs_sqlite3_cli
def test_reset_does_not_clobber_a_concurrent_runs_archive(data: Path, tmp_path: Path) -> None:
    """Re-review should-fix 1: a second reset landing on the same stamp must not
    let this run's raw-copy fallback or quarantine step delete or rename an
    archive the other run already published. A sqlite3 shim plants a competing
    archive at this run's own candidate name right after VACUUM INTO finishes,
    simulating the other run winning the race to publish first; both archives
    must survive, this run's under the next free name."""
    db = make_db(data)
    real = shutil.which("sqlite3")
    assert real is not None
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "sqlite3"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        "  VACUUM*)\n"
        "    target=$(printf '%s' \"$2\" | sed -n \"s/^VACUUM INTO '\\\\(.*\\\\)'\\$/\\\\1/p\")\n"
        f'    "{real}" "$@"\n'
        "    rc=$?\n"
        '    archives=$(dirname "$(dirname "$target")")\n'
        "    stamp=$(date -u +%Y%m%dT%H%M%SZ)\n"
        '    competitor="$archives/netkeeper-$stamp.sqlite3"\n'
        '    [ -e "$competitor" ] || printf \'a concurrent run published first\' > "$competitor"\n'
        "    exit $rc\n"
        "    ;;\n"
        "esac\n"
        f'exec "{real}" "$@"\n'
    )
    shim.chmod(0o755)
    path = f"{shim_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 0, result.stderr

    entries = sorted((data / "archives").iterdir())
    assert len(entries) == 2
    contents = [p.read_bytes() for p in entries]
    assert b"a concurrent run published first" in contents

    ours = [p for p in entries if p.read_bytes() != b"a concurrent run published first"]
    assert len(ours) == 1
    with closing(sqlite3.connect(ours[0])) as check:
        assert check.execute("select x from marker").fetchone() == (42,)
    assert not db.exists()


@needs_sqlite3_cli
def test_reset_folds_the_wal_when_vacuum_into_is_refused(data: Path, tmp_path: Path) -> None:
    """R9: VACUUM INTO can be refused (a read-only filesystem, a locked-down
    sqlite3 build); the raw-copy-and-fold fallback must still produce a
    complete, self-contained, verified archive with no leftover sidecars."""
    db = data / "netkeeper.sqlite3"
    db.unlink(missing_ok=True)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE marker (x integer)")
        conn.execute("INSERT INTO marker VALUES (1)")

    # A concurrent reader blocks the WAL from being fully checkpointed away on
    # close, so -wal still carries a real pending frame when reset-data.sh reads
    # it -- not just an artifact of the fallback path running at all.
    reader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3, time\n"
            f"conn = sqlite3.connect({str(db)!r})\n"
            "conn.execute('BEGIN')\n"
            "conn.execute('SELECT * FROM marker').fetchall()\n"
            "time.sleep(30)\n",
        ]
    )
    try:
        time.sleep(0.3)
        with closing(sqlite3.connect(db)) as writer, writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO marker VALUES (42)")
        assert Path(f"{db}-wal").stat().st_size > 0, "test setup needs a pending WAL"
    finally:
        reader.kill()
        reader.wait()

    real = shutil.which("sqlite3")
    assert real is not None
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "sqlite3"
    shim.write_text(f'#!/bin/sh\ncase "$2" in VACUUM*) exit 1 ;; esac\nexec "{real}" "$@"\n')
    shim.chmod(0o755)
    path = f"{shim_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"

    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 0, result.stderr
    assert "copying the raw files instead" in result.stderr
    assert "verified" in result.stdout

    (archive,) = (data / "archives").iterdir()
    assert not Path(f"{archive}-wal").exists()
    assert not Path(f"{archive}-shm").exists()
    with closing(sqlite3.connect(archive)) as check:
        rows = check.execute("SELECT x FROM marker ORDER BY x").fetchall()
    assert rows == [(1,), (42,)]


@needs_sqlite3_cli
def test_reset_removes_the_copied_wal_shm_even_when_the_fold_pragma_fails(
    data: Path, tmp_path: Path
) -> None:
    """Re-review nit 5: pin the explicit `rm -f -- "$work-wal" "$work-shm"` in
    the raw-copy fallback. When the fold (wal_checkpoint/journal_mode) pragma
    succeeds, it already removes those files itself as a side effect, which
    left this line a surviving mutant -- untested, because its own absence
    changed nothing observable. Forcing the fold pragma to fail too (not just
    VACUUM INTO) makes this explicit cleanup the only thing that would remove
    them, and a sqlite3 shim records whether the copied -wal still sits next
    to the work file at the moment verified() reads it -- the one point in
    the run where it would still matter, before the whole temp directory is
    removed either way."""
    db = data / "netkeeper.sqlite3"
    db.unlink(missing_ok=True)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE marker (x integer)")
        conn.execute("INSERT INTO marker VALUES (1)")
    reader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3, time\n"
            f"conn = sqlite3.connect({str(db)!r})\n"
            "conn.execute('BEGIN')\n"
            "conn.execute('SELECT * FROM marker').fetchall()\n"
            "time.sleep(30)\n",
        ]
    )
    try:
        time.sleep(0.3)
        with closing(sqlite3.connect(db)) as writer, writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO marker VALUES (42)")
        assert Path(f"{db}-wal").stat().st_size > 0, "test setup needs a pending WAL"
    finally:
        reader.kill()
        reader.wait()

    real = shutil.which("sqlite3")
    assert real is not None
    marker_file = tmp_path / "wal-at-verify-time"
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "sqlite3"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        "  VACUUM*) exit 1 ;;\n"
        "  *wal_checkpoint*) exit 1 ;;\n"
        "esac\n"
        'case "$3" in\n'
        "  *quick_check*)\n"
        '    if [ -e "$2-wal" ]; then\n'
        f"      printf present > {str(marker_file)!r}\n"
        "    else\n"
        f"      printf absent > {str(marker_file)!r}\n"
        "    fi\n"
        "    ;;\n"
        "esac\n"
        f'exec "{real}" "$@"\n'
    )
    shim.chmod(0o755)
    path = f"{shim_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"

    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    # verified() itself still fails here -- a WAL-mode header with no -wal/-shm
    # beside it cannot be opened read-only either -- but that is not what this
    # pins: the marker shows rm -f already ran before verified() was reached,
    # which is the one thing a successful fold's own side effect would
    # otherwise have hidden.
    assert result.returncode == 1
    assert "quick_check" in result.stderr
    assert marker_file.read_text() == "absent"


@needs_sqlite3_cli
def test_reset_removes_a_leftover_wal_sidecar(data: Path) -> None:
    """R14: remove_db() must delete a leftover ``-wal``/``-shm``, not only the
    main file."""
    db = make_db(data)
    wal = Path(f"{db}-wal")
    shm = Path(f"{db}-shm")
    wal.write_bytes(b"stale wal frames")
    shm.write_bytes(b"stale shm")
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    assert result.returncode == 0, result.stderr
    assert not db.exists()
    assert not wal.exists()
    assert not shm.exists()


def test_list_orders_a_same_second_collision_newest_first(data: Path) -> None:
    """#183 review bug 6: ``-1`` was written after the bare name (the same
    collision ``archive_path()`` resolves within one second), so it must list
    first, not second."""
    archives = data / "archives"
    archives.mkdir()
    (archives / "netkeeper-20260101T000000Z.sqlite3").write_bytes(b"first")
    (archives / "netkeeper-20260101T000000Z-1.sqlite3").write_bytes(b"second, same second")
    result = run_sh(RESET, "--data-dir", str(data), "--list", env={})
    assert result.returncode == 0, result.stderr
    names = [line.split("  ")[0] for line in result.stdout.splitlines()]
    assert names == [
        "netkeeper-20260101T000000Z-1.sqlite3",
        "netkeeper-20260101T000000Z.sqlite3",
    ]


@needs_sqlite3_cli
def test_reset_refuses_without_sqlite3(data: Path, tmp_path: Path) -> None:
    """R16: sqlite3's own tool-requirement check, exercised with lsof present --
    distinct from the missing-lsof test above."""
    make_db(data)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in (
        "lsof",
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
        "cut",
        "sh",
    ):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": str(bin_dir)})
    assert result.returncode == 1
    assert "sqlite3 is needed" in result.stderr
    assert (data / "netkeeper.sqlite3").exists()
