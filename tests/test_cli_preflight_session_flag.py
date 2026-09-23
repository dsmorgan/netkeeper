"""``netkeeper preflight`` clears the session flag on a live session (#154).

#144 gave `linkedin.session_flag` a writer (`flag_session`, on `Checkpoint` and
`LoggedOut`) and a way back (`clear_session_flag`), but nothing called the second
one -- logging back in to the netkeeper Chrome profile never cleared the banner
condition. Preflight is the natural place to notice: it already learns whether the
profile holds a live session (spec 9.1), it just never used to do anything with it.

`linkedin/preflight.py` may not open a database session (spec 9.10, ADR 0005), so
the clearing happens in the CLI (`netkeeper.cli._clear_session_flag_after_login`) --
this module tests it at the command level, the way a person actually exercises it.

Fixtures here reuse `tests/test_preflight.py`'s cookie-jar helpers; nothing below is
a real cookie value.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker
from test_preflight import FAKE_CSRF_VALUE, cookie, make_provider
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.scoping import install_scope_guard
from netkeeper.services.linkedin_session import SessionFlag, flag_session, session_flag
from netkeeper.services.users import ensure_local_user

CHECKPOINT_URL = "https://www.linkedin.com/checkpoint/challenge/?ctx=abc123"


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A migrated database with the local user, pointed to by ``NETKEEPER_DATABASE_URL``.

    Same shape as ``tests/test_cli_posture_rehearse.py``'s fixture of the same name.
    """
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session, settings=Settings())
    yield factory
    engine.dispose()


def _flag_a_checkpoint(factory: sessionmaker[Session]) -> None:
    with session_scope(factory, write=True) as session:
        user = ensure_local_user(session, settings=Settings())
        flag_session(session, user, Outcome.CHECKPOINT, url=CHECKPOINT_URL)


def _current_flag(factory: sessionmaker[Session]) -> Outcome | None:
    with session_scope(factory) as session:
        user = ensure_local_user(session, settings=Settings())
        flag: SessionFlag | None = session_flag(session, user)
        return None if flag is None else flag.outcome


# --- a live session clears the flag --------------------------------------------


def test_preflight_clears_a_standing_flag_when_it_finds_a_live_session(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _flag_a_checkpoint(cli_db)
    monkeypatch.setattr("netkeeper.cli.AttachBrowserProvider", lambda cdp_url: make_provider())

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    assert "logged in" in result.output
    assert _current_flag(cli_db) is None


def test_preflight_with_no_standing_flag_and_a_live_session_stays_clear(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing an already-clear flag is a no-op, not an error."""
    monkeypatch.setattr("netkeeper.cli.AttachBrowserProvider", lambda cdp_url: make_provider())

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    assert _current_flag(cli_db) is None


# --- no session found: read, never write ----------------------------------------


def test_preflight_finding_no_session_does_not_set_the_flag(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preflight's cookie-jar read is weaker evidence than a classified LoggedOut."""
    no_session = make_provider(cookies=[cookie("JSESSIONID", FAKE_CSRF_VALUE)])
    monkeypatch.setattr("netkeeper.cli.AttachBrowserProvider", lambda cdp_url: no_session)

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 1
    assert "no LinkedIn session" in result.output or "no live LinkedIn session" in result.output
    assert _current_flag(cli_db) is None, "a missing cookie must never set the flag"


def test_preflight_finding_no_session_leaves_a_standing_flag_alone(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """NO_SESSION is not LOGGED_IN's opposite: it must not clear the flag either."""
    _flag_a_checkpoint(cli_db)
    no_session = make_provider(cookies=[cookie("JSESSIONID", FAKE_CSRF_VALUE)])
    monkeypatch.setattr("netkeeper.cli.AttachBrowserProvider", lambda cdp_url: no_session)

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 1
    assert _current_flag(cli_db) is Outcome.CHECKPOINT, (
        "a preflight that could not even find a session must not touch a flag"
        " that a real classified response raised"
    )


# --- preflight never requires a database ----------------------------------------


def test_preflight_with_no_database_at_all_still_prints_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``cli_db`` fixture here: this is a fresh install, before `netkeeper db upgrade`.

    Preflight answered from the browser alone before this flag existed, and a
    successful attach + live session must still print and exit 0 -- there being
    nothing to clear yet is not a reason to fail the command that found the session.
    """
    monkeypatch.setattr("netkeeper.cli.AttachBrowserProvider", lambda cdp_url: make_provider())

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    assert "logged in" in result.output


# --- the other half: posture reflects what preflight cleared ---------------------


def test_posture_shows_the_flag_cleared_after_a_preflight_that_found_a_session(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`netkeeper posture`'s own ``session flag`` row, after the CLI command that clears it.

    A separate process re-reading the persisted flag is the real-world shape of
    this: run `netkeeper preflight` once to log back in, then `netkeeper posture`
    later to check the banner is gone -- exactly what #154's "done when" asks for.
    """
    _flag_a_checkpoint(cli_db)
    before = CliRunner().invoke(cli, ["posture", "--no-probe"])
    assert "checkpoint at /checkpoint/challenge/" in before.output
    assert "the session was flagged checkpoint" in before.output

    monkeypatch.setattr("netkeeper.cli.AttachBrowserProvider", lambda cdp_url: make_provider())
    preflight_result = CliRunner().invoke(cli, ["preflight"])
    assert preflight_result.exit_code == 0, preflight_result.output

    after = CliRunner().invoke(cli, ["posture", "--no-probe"])
    assert "the session was flagged checkpoint" not in after.output
    assert "clear (checkpoint and logged-out raise it)" in after.output
