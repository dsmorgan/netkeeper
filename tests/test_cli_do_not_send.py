"""``netkeeper do-not-send list|add|remove`` (#238, Part B), against a migrated scratch
database under ``tmp_path``."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.crm import do_not_send
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models import DoNotSendReason
from netkeeper.scoping import install_scope_guard
from netkeeper.services.users import ensure_local_user

runner = CliRunner()


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session)
    yield factory
    engine.dispose()


def _entries(factory: sessionmaker[Session]) -> list[tuple[str, str]]:
    with session_scope(factory) as session:
        user = ensure_local_user(session)
        return [(e.email, e.reason.value) for e in do_not_send.entries(session, user)]


def test_an_empty_list_says_so(cli_db: sessionmaker[Session]) -> None:
    result = runner.invoke(cli, ["do-not-send", "list"])
    assert result.exit_code == 0, result.output
    assert "empty" in result.output


def test_add_then_list(cli_db: sessionmaker[Session]) -> None:
    result = runner.invoke(cli, ["do-not-send", "add", "Name+NK1@Example.test"])
    assert result.exit_code == 0, result.output
    assert "name+nk1@example.test is on the do-not-send list (manual)" in result.output
    with session_scope(cli_db, write=True) as session:
        do_not_send.add(
            session, ensure_local_user(session), "ada@example.test", DoNotSendReason.BOUNCED
        )
    listed = runner.invoke(cli, ["do-not-send", "list"])
    assert listed.exit_code == 0, listed.output
    assert "name+nk1@example.test" in listed.output and "manual" in listed.output
    assert "ada@example.test" in listed.output and "bounced" in listed.output


def test_add_refuses_anything_but_one_bare_address(cli_db: sessionmaker[Session]) -> None:
    result = runner.invoke(cli, ["do-not-send", "add", "a@x.test, b@y.test"])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert _entries(cli_db) == []


def test_remove_by_address_asks_first(cli_db: sessionmaker[Session]) -> None:
    runner.invoke(cli, ["do-not-send", "add", "ada@example.test"])
    declined = runner.invoke(cli, ["do-not-send", "remove", "ADA@example.test"], input="n\n")
    assert declined.exit_code == 1
    assert _entries(cli_db) == [("ada@example.test", "manual")]
    accepted = runner.invoke(cli, ["do-not-send", "remove", "ada@example.test"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert "ada@example.test is off the do-not-send list" in accepted.output
    assert _entries(cli_db) == []


def test_remove_by_id_with_yes(cli_db: sessionmaker[Session]) -> None:
    runner.invoke(cli, ["do-not-send", "add", "ada@example.test"])
    runner.invoke(cli, ["do-not-send", "add", "ada+x@example.test"])
    with session_scope(cli_db) as session:
        entry = do_not_send.find(session, ensure_local_user(session), "ada@example.test")
        assert entry is not None
        entry_id = entry.id
    result = runner.invoke(cli, ["do-not-send", "remove", str(entry_id), "--yes"])
    assert result.exit_code == 0, result.output
    assert _entries(cli_db) == [("ada+x@example.test", "manual")]  # the +tag is its own


def test_removing_what_is_not_there_fails(cli_db: sessionmaker[Session]) -> None:
    for entry in ("nobody@example.test", "12345"):
        result = runner.invoke(cli, ["do-not-send", "remove", entry, "--yes"])
        assert result.exit_code == 1
        assert "not on the do-not-send list" in result.output


def test_remove_warns_when_the_entry_also_bounced(cli_db: sessionmaker[Session]) -> None:
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session)
        do_not_send.add(session, user, "ada@example.test", DoNotSendReason.BOUNCED)
        do_not_send.add(session, user, "ada@example.test", DoNotSendReason.OPTED_OUT)
        do_not_send.add(session, user, "bob@example.test", DoNotSendReason.OPTED_OUT)
    warned = runner.invoke(cli, ["do-not-send", "remove", "ada@example.test"], input="n\n")
    assert "This address also bounced; removing the entry allows email to it again" in (
        warned.output
    )
    plain = runner.invoke(cli, ["do-not-send", "remove", "bob@example.test"], input="n\n")
    assert "also bounced" not in plain.output
