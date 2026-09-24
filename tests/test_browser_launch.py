"""``netkeeper/services/browser_launch.py``: the launch command as data (P2-12).

This is the module ``GET /linkedin/browser`` uses instead of importing
``netkeeper.linkedin.browser`` (forbidden for anything under ``netkeeper/web/`` --
see ``BROWSER_MODULES`` in ``tests/test_browser_safety.py``). It duplicates one
constant from that module rather than importing it; one test here pins the two
copies together so they cannot silently drift apart. ``netkeeper browser launch``
(``netkeeper/cli.py``) imports this module's functions directly rather than
keeping its own copy of the command-building logic (M3, #179 review); the last
test asserts its printed command is exactly what those functions return, so an
import forgetting to happen fails loudly rather than passing by coincidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.linkedin.browser import CHROME_PROFILE_DIRNAME as EXTRACTOR_PROFILE_DIRNAME
from netkeeper.paths import data_dir
from netkeeper.services.browser_launch import (
    CHROME_PROFILE_DIRNAME,
    DEFAULT_CDP_PORT,
    cdp_port,
    chrome_launch_command,
    remote_host_note,
)


def test_cdp_port_reads_the_configured_port() -> None:
    assert cdp_port("http://127.0.0.1:9333") == 9333


def test_cdp_port_falls_back_to_chromes_default() -> None:
    assert cdp_port("http://127.0.0.1") == DEFAULT_CDP_PORT


def test_cdp_port_falls_back_on_an_unparseable_url() -> None:
    assert cdp_port("not a url at all") == DEFAULT_CDP_PORT


def test_macos_command_uses_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    lines = chrome_launch_command(9222, Path("/profile"))
    assert lines == [
        'open -na "Google Chrome" --args \\',
        "  --remote-debugging-port=9222 \\",
        '  --user-data-dir="/profile"',
    ]


def test_linux_command_uses_the_binary_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    lines = chrome_launch_command(9222, Path("/profile"))
    assert lines[0] == "google-chrome \\"
    assert lines[1] == "  --remote-debugging-port=9222 \\"
    assert lines[2] == '  --user-data-dir="/profile"'


def test_remote_host_note_is_none_for_loopback() -> None:
    for url in ("http://127.0.0.1:9222", "http://localhost:9222", "http://[::1]:9222"):
        assert remote_host_note(url) is None


def test_remote_host_note_warns_off_loopback() -> None:
    note = remote_host_note("http://10.0.0.5:9222")
    assert note is not None
    assert "10.0.0.5" in note
    assert "loopback" in note


def test_the_duplicated_profile_dirname_matches_the_extractors() -> None:
    """The one thing this module copies instead of imports (see the module docstring).

    A mutation that lets the two spellings drift apart is caught here, not by
    `test_browser_safety.py`'s import scan, which only ever sees this module's
    constant, never the extractor's.
    """
    assert CHROME_PROFILE_DIRNAME == EXTRACTOR_PROFILE_DIRNAME


def test_cli_browser_launch_prints_exactly_this_modules_command() -> None:
    """`netkeeper browser launch` calls `chrome_launch_command`/`cdp_port` (M3);
    its printed lines must be exactly what those functions return, not a
    hand-kept copy that could say something different."""
    settings = Settings()
    profile = data_dir() / CHROME_PROFILE_DIRNAME
    expected = chrome_launch_command(cdp_port(settings.linkedin.cdp_url), profile)

    result = CliRunner().invoke(cli, ["browser", "launch"])

    assert result.exit_code == 0, result.output
    for line in expected:
        assert f"  {line}" in result.output
