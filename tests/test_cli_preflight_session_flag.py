"""``netkeeper preflight`` clears the session flag on a live session (#154), and its
#168-review fix (F1): only a ``LoggedOut`` flag auto-clears. A ``Checkpoint`` flag
never does -- ``li_at`` being present is not proof a checkpoint is resolved -- and is
only ever cleared by hand, with ``netkeeper linkedin clear-flag`` (also tested here).

#144 gave `linkedin.session_flag` a writer (`flag_session`, on `Checkpoint` and
`LoggedOut`) and a way back (`clear_session_flag`), but nothing called the second
one -- logging back in to the netkeeper Chrome profile never cleared the banner
condition. Preflight is the natural place to notice for a `LoggedOut` flag: it
already learns whether the profile holds a live session (spec 9.1).

`linkedin/preflight.py` may not open a database session (spec 9.10, ADR 0005), so
the clearing happens in the CLI (`netkeeper.cli._clear_session_flag_after_login`) --
this module tests it at the command level, the way a person actually exercises it.

Fixtures here reuse `tests/test_preflight.py`'s cookie-jar helpers; nothing below is
a real cookie value.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from test_preflight import FAKE_CSRF_VALUE, cookie, make_context, make_provider, provider_for
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import User
from netkeeper.scoping import install_scope_guard
from netkeeper.services.linkedin_session import SessionFlag, flag_session, session_flag
from netkeeper.services.users import ensure_local_user

CHECKPOINT_URL = "https://www.linkedin.com/checkpoint/challenge/?ctx=abc123"
LOGIN_WALL_URL = "https://www.linkedin.com/uas/login?session_redirect=abc"


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


def _flag_logged_out(factory: sessionmaker[Session]) -> None:
    with session_scope(factory, write=True) as session:
        user = ensure_local_user(session, settings=Settings())
        flag_session(session, user, Outcome.LOGGED_OUT, url=LOGIN_WALL_URL)


def _current_flag(factory: sessionmaker[Session]) -> Outcome | None:
    with session_scope(factory) as session:
        user = ensure_local_user(session, settings=Settings())
        flag: SessionFlag | None = session_flag(session, user)
        return None if flag is None else flag.outcome


def _logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("netkeeper.cli.AttachBrowserProvider", lambda cdp_url: make_provider())


# --- F1: only LoggedOut auto-clears; Checkpoint never does -----------------------


def test_preflight_clears_a_standing_logged_out_flag_when_it_finds_a_live_session(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _flag_logged_out(cli_db)
    _logged_in(monkeypatch)

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    assert "logged in" in result.output
    assert "cleared the logged-out session flag" in result.output
    assert _current_flag(cli_db) is None


def test_preflight_leaves_a_standing_checkpoint_flag_in_place(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: li_at being present is not proof a checkpoint is solved (spec 9.7)."""
    _flag_a_checkpoint(cli_db)
    _logged_in(monkeypatch)

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    assert "leaving the checkpoint session flag in place" in result.output
    assert "netkeeper linkedin clear-flag" in result.output
    assert _current_flag(cli_db) is Outcome.CHECKPOINT, "a checkpoint flag must survive untouched"


def test_preflight_with_an_unreadable_cookie_jar_does_not_attempt_to_clear(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """N20: the gate must be the precise ``LoginState.LOGGED_IN`` check, not ``report.ok``.

    An unreadable cookie jar is a warning, not a problem (``PreflightReport.ok`` stays
    true), so ``login`` reads ``UNKNOWN`` while ``report.ok`` is still ``True``. Gating
    on ``report.ok`` instead of the precise login state would treat "we could not
    tell" the same as "confirmed logged in" and could clear -- or, with a live
    checkpoint flag, could have wrongly left in place for the wrong reason -- a flag
    on evidence that was never actually read.
    """
    _flag_a_checkpoint(cli_db)
    context = make_context()
    context.cookie_error = RuntimeError("Protocol error")
    monkeypatch.setattr(
        "netkeeper.cli.AttachBrowserProvider", lambda cdp_url: provider_for(context)
    )

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    assert "unknown" in result.output.lower()
    assert "cleared" not in result.output
    assert "leaving the checkpoint" not in result.output
    assert _current_flag(cli_db) is Outcome.CHECKPOINT


def test_preflight_with_no_standing_flag_and_a_live_session_stays_clear(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing an already-clear flag is a no-op, not an error."""
    _logged_in(monkeypatch)

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    assert _current_flag(cli_db) is None
    assert "cleared" not in result.output
    assert "leaving" not in result.output


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


def test_preflight_finding_no_session_leaves_a_standing_checkpoint_flag_alone(
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


def test_preflight_finding_no_session_leaves_a_standing_logged_out_flag_alone(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _flag_logged_out(cli_db)
    no_session = make_provider(cookies=[cookie("JSESSIONID", FAKE_CSRF_VALUE)])
    monkeypatch.setattr("netkeeper.cli.AttachBrowserProvider", lambda cdp_url: no_session)

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 1
    assert _current_flag(cli_db) is Outcome.LOGGED_OUT


# --- F4: preflight never requires a database, and never creates one -------------


def test_preflight_with_no_database_at_all_still_prints_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No ``cli_db`` fixture here: this is a fresh install, before `netkeeper db upgrade`.

    Preflight answered from the browser alone before this flag existed, and a
    successful attach + live session must still print and exit 0 -- there being
    nothing to clear yet is not a reason to fail the command that found the session.
    Nothing about checking for that nothing may itself create the sqlite file
    (#168 review, F4): `make_engine` does that -- and its parent directory -- as a
    side effect of merely being called, so the file's existence must be checked
    first. The data directory itself legitimately exists after this (attaching over
    CDP takes the activity lock, which lives under it, whether or not there is a
    database); only the database file is the thing this test pins.
    """
    data_dir = tmp_path / "fresh-install"
    monkeypatch.setenv("NETKEEPER_DATA", str(data_dir))
    monkeypatch.delenv("NETKEEPER_DATABASE_URL", raising=False)
    _logged_in(monkeypatch)

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    assert "logged in" in result.output
    assert not (data_dir / "netkeeper.sqlite3").exists(), (
        "preflight created the sqlite file on a fresh install"
    )


def test_a_database_file_that_exists_but_has_no_schema_is_also_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file exists (so the fast path above does not short-circuit) but is unmigrated.

    Distinct from "no file at all": this hits sqlite's own "no such table" through
    a real connection, which is the other silent case F4 asks for.
    """
    db_path = tmp_path / "netkeeper.sqlite3"
    sqlite3.connect(db_path).close()  # a real, valid, but schema-less sqlite file
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", f"sqlite:///{db_path}")
    _logged_in(monkeypatch)

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    assert "logged in" in result.output
    assert "could not clear" not in result.output


def test_an_unexpected_database_error_is_logged_and_printed_but_stays_exit_zero(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """F4: only 'no such table' and no local user are silent. Anything else -- locked,
    read-only, an I/O error -- is a WARNING and one printed line, and still exit 0:
    failing to clear an advisory flag is not a reason to fail a preflight that just
    confirmed the browser and the session are fine.
    """
    caplog.set_level(logging.WARNING, logger="netkeeper.cli")

    def raise_locked(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr("netkeeper.cli.session_flag", raise_locked)
    _logged_in(monkeypatch)

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    assert "could not clear the session flag" in result.output
    assert "database is locked" in result.output
    assert any("could not clear the session flag" in record.message for record in caplog.records)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_an_unrelated_exception_while_clearing_is_never_swallowed(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """N19: the except clause must stay narrowed to ``OperationalError``.

    Widened to a bare ``Exception``, a genuine bug elsewhere in this path -- an
    ``AttributeError`` from a typo, say -- would print the same friendly "database
    busy" line an actual database problem gets, hiding it instead of surfacing it.
    """

    def raise_unrelated(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("not a database problem at all")

    monkeypatch.setattr("netkeeper.cli.session_flag", raise_unrelated)
    _logged_in(monkeypatch)

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert "could not clear the session flag" not in result.output


# --- the other half: posture reflects what preflight did or did not clear --------


def test_posture_shows_a_logged_out_flag_cleared_after_a_preflight_that_found_a_session(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`netkeeper posture`'s own ``session flag`` row, after the CLI command that clears it.

    A separate process re-reading the persisted flag is the real-world shape of
    this: run `netkeeper preflight` once to log back in, then `netkeeper posture`
    later to check the banner is gone -- exactly what #154's "done when" asks for.
    """
    _flag_logged_out(cli_db)
    before = CliRunner().invoke(cli, ["posture", "--no-probe"])
    assert "logged_out at /uas/login" in before.output
    assert "the session was flagged logged_out" in before.output

    _logged_in(monkeypatch)
    preflight_result = CliRunner().invoke(cli, ["preflight"])
    assert preflight_result.exit_code == 0, preflight_result.output

    after = CliRunner().invoke(cli, ["posture", "--no-probe"])
    assert "the session was flagged logged_out" not in after.output
    assert "clear (checkpoint and logged-out raise it)" in after.output


def test_posture_still_shows_a_standing_checkpoint_flag_after_preflight(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: preflight must not have cleared it, and posture's own row proves that too."""
    _flag_a_checkpoint(cli_db)

    _logged_in(monkeypatch)
    preflight_result = CliRunner().invoke(cli, ["preflight"])
    assert preflight_result.exit_code == 0, preflight_result.output

    after = CliRunner().invoke(cli, ["posture", "--no-probe"])
    assert "checkpoint at /checkpoint/challenge/" in after.output
    assert "the session was flagged checkpoint" in after.output


def test_postures_checkpoint_advice_names_the_clear_flag_command(
    cli_db: sessionmaker[Session],
) -> None:
    """The advice text must name the actual way back, not describe a manual process."""
    _flag_a_checkpoint(cli_db)

    result = CliRunner().invoke(cli, ["posture", "--no-probe"])

    assert "netkeeper linkedin clear-flag" in result.output


# --- netkeeper linkedin clear-flag ------------------------------------------------


def test_clear_flag_with_nothing_set_says_so_and_changes_nothing(
    cli_db: sessionmaker[Session],
) -> None:
    result = CliRunner().invoke(cli, ["linkedin", "clear-flag"])

    assert result.exit_code == 0, result.output
    assert "no session flag is set" in result.output


def test_clear_flag_confirmed_clears_a_checkpoint_flag(cli_db: sessionmaker[Session]) -> None:
    _flag_a_checkpoint(cli_db)

    result = CliRunner().invoke(cli, ["linkedin", "clear-flag"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "session flag cleared" in result.output
    assert _current_flag(cli_db) is None


def test_clear_flag_declined_leaves_the_flag_in_place(cli_db: sessionmaker[Session]) -> None:
    _flag_a_checkpoint(cli_db)

    result = CliRunner().invoke(cli, ["linkedin", "clear-flag"], input="n\n")

    assert result.exit_code == 1
    assert "cancelled" in result.output
    assert _current_flag(cli_db) is Outcome.CHECKPOINT


def test_clear_flag_yes_skips_the_prompt(cli_db: sessionmaker[Session]) -> None:
    _flag_a_checkpoint(cli_db)

    result = CliRunner().invoke(cli, ["linkedin", "clear-flag", "--yes"])

    assert result.exit_code == 0, result.output
    assert _current_flag(cli_db) is None


def test_clear_flag_prompt_names_the_outcome_and_when_it_was_raised(
    cli_db: sessionmaker[Session],
) -> None:
    _flag_a_checkpoint(cli_db)

    result = CliRunner().invoke(cli, ["linkedin", "clear-flag"], input="n\n")

    assert "checkpoint" in result.output
    assert "/checkpoint/challenge/" in result.output


def test_clear_flag_without_a_database_does_not_exit_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unlike preflight, this command is invoked explicitly by a person to change
    something, so -- like every other write command in this CLI, `tags run-rules`
    among them -- it makes no special allowance for a database that was never set
    up; it fails the same way any of them would."""
    monkeypatch.setenv("NETKEEPER_DATA", str(tmp_path / "fresh-install"))
    monkeypatch.delenv("NETKEEPER_DATABASE_URL", raising=False)

    result = CliRunner().invoke(cli, ["linkedin", "clear-flag", "--yes"])

    assert result.exit_code != 0


# --- #170 item 1: the write lock is not held across the prompt -------------------


def test_clear_flag_prompt_does_not_hold_the_write_lock(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent writer -- standing in for a `netkeeper serve` process -- must
    succeed while the confirmation prompt is on screen. Before the #170 item 1 fix,
    `clear-flag` opened its own writer (`BEGIN IMMEDIATE`) before ever asking, so a
    second engine's writer below would have blocked behind it for the whole
    `SQLITE_BUSY_TIMEOUT_MS` and then raised "database is locked" -- this monkeypatch
    of `typer.confirm` runs the concurrent write *during* the prompt itself, the one
    place in the command's control flow where the bug could actually bite."""
    _flag_a_checkpoint(cli_db)
    concurrent_write_succeeded = False
    started = time.monotonic()

    def confirm_with_a_concurrent_writer(*_args: object, **_kwargs: object) -> bool:
        nonlocal concurrent_write_succeeded
        url = database_url()
        engine = make_engine(url)
        try:
            factory = make_session_factory(engine)
            with session_scope(factory, write=True) as session:
                ensure_local_user(session, settings=Settings())
            concurrent_write_succeeded = True
        finally:
            engine.dispose()
        return True

    monkeypatch.setattr("typer.confirm", confirm_with_a_concurrent_writer)

    result = CliRunner().invoke(cli, ["linkedin", "clear-flag"])
    elapsed = time.monotonic() - started

    assert result.exit_code == 0, result.output
    assert concurrent_write_succeeded, "a concurrent writer must succeed while the prompt is up"
    assert elapsed < 2.0, f"took {elapsed:.1f}s -- looks like it waited on the busy timeout"
    assert _current_flag(cli_db) is None


def test_clear_flag_re_checks_the_flag_after_the_prompt(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#173 review, R1 (mutation survived: the re-check neutered to ``if False``).
    A flag that changed *while the prompt was on screen* must not be cleared by an
    answer given about the flag that used to be there -- a job raising a fresh
    flag during the wait is real evidence, and a stale "yes" must not erase it."""
    _flag_a_checkpoint(cli_db)

    def confirm_after_a_concurrent_change(*_args: object, **_kwargs: object) -> bool:
        # Stands in for a job raising a *different* flag while the person was
        # answering the prompt about the original one.
        _flag_logged_out(cli_db)
        return True

    monkeypatch.setattr("typer.confirm", confirm_after_a_concurrent_change)

    result = CliRunner().invoke(cli, ["linkedin", "clear-flag"])

    assert result.exit_code == 1
    assert "changed while waiting for an answer" in result.output
    assert _current_flag(cli_db) is Outcome.LOGGED_OUT  # untouched by the stale "yes"


# --- #170 item 6: preflight takes no write lock to report a CHECKPOINT flag ------


def test_preflight_with_a_checkpoint_flag_never_opens_a_writer(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading a flag to report on it -- never clearing it -- costs no write lock."""
    _flag_a_checkpoint(cli_db)
    _logged_in(monkeypatch)
    writes: list[bool] = []

    def recording_session_scope(
        factory: sessionmaker[Session], *, write: bool = False
    ) -> AbstractContextManager[Session]:
        writes.append(write)
        return session_scope(factory, write=write)

    monkeypatch.setattr("netkeeper.cli.session_scope", recording_session_scope)

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    assert "leaving the checkpoint session flag in place" in result.output
    assert writes == [False], f"a checkpoint report must only ever read, got write={writes}"


def test_preflight_re_checks_the_flag_before_clearing_it(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#173 review, R2 (mutation survived: the re-check neutered to skip the
    ``!= flag`` half). A flag that changed between preflight's read and its write
    must not be cleared on stale evidence -- a job raising a fresh flag in that
    gap is real evidence a `LoggedOut` clear must not erase."""
    _flag_logged_out(cli_db)
    _logged_in(monkeypatch)
    real_session_flag = session_flag
    calls = 0

    def session_flag_that_changes_after_the_first_read(
        session: Session, user: User
    ) -> SessionFlag | None:
        nonlocal calls
        calls += 1
        result = real_session_flag(session, user)
        if calls == 1:
            # Stands in for a job raising a *different* flag in the gap between
            # preflight's read and its write.
            _flag_a_checkpoint(cli_db)
        return result

    monkeypatch.setattr(
        "netkeeper.cli.session_flag", session_flag_that_changes_after_the_first_read
    )

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    assert "cleared" not in result.output  # the write refused: no longer LoggedOut
    assert _current_flag(cli_db) is Outcome.CHECKPOINT  # untouched by the stale clear attempt
