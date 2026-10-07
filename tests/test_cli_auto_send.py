"""``netkeeper linkedin auto-send-resume`` (ADR 0008, #458 review): resume a held auto-send.

It runs only at a terminal and always asks, so a script can't resume auto-send without a
person having looked at Chrome. No browser is touched.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from campaign_fakes import NOW
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models import User
from netkeeper.scoping import install_scope_guard
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_steps import (
    AUTO_SEND_HOLD_BUBBLE,
    auto_send_hold,
    hold_auto_send,
)
from netkeeper.services.users import ensure_local_user


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
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


def _hold(factory: sessionmaker[Session]) -> None:
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User)).one()
        account = ensure_account(session, user).id
        hold_auto_send(session, user, account, reason=AUTO_SEND_HOLD_BUBBLE, now=NOW, run_id=1)


def _held(factory: sessionmaker[Session]) -> bool:
    with session_scope(factory) as session:
        user = session.scalars(select(User)).one()
        return auto_send_hold(session, user, ensure_account(session, user).id) is not None


def test_it_refuses_without_a_terminal(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold(cli_db)

    class Pipe:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr("netkeeper.cli.sys", type("S", (), {"stdin": Pipe}))
    result = CliRunner().invoke(cli, ["linkedin", "auto-send-resume"], input="y\n")
    assert result.exit_code == 2
    assert _held(cli_db)


def test_it_asks_and_resumes_only_on_yes(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold(cli_db)

    class Terminal:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr("netkeeper.cli.sys", type("S", (), {"stdin": Terminal}))
    runner = CliRunner()
    no = runner.invoke(cli, ["linkedin", "auto-send-resume"], input="n\n")
    assert no.exit_code == 1 and "stays held" in no.output
    assert _held(cli_db)
    yes = runner.invoke(cli, ["linkedin", "auto-send-resume"], input="y\n")
    assert yes.exit_code == 0 and "auto-send resumed" in yes.output
    assert "a message bubble is open in Chrome" in yes.output
    assert not _held(cli_db)
    again = runner.invoke(cli, ["linkedin", "auto-send-resume"], input="y\n")
    assert again.exit_code == 0 and "not held" in again.output
