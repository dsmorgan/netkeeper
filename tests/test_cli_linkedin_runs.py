"""``netkeeper linkedin sync|enrich|runs|run|cancel|schedule`` (P2-10).

The supervised first run at CP4 is ``netkeeper linkedin sync`` then
``netkeeper linkedin enrich --max-visits 5``, by hand, while scheduled runs stay
disarmed; ``netkeeper linkedin schedule arm`` afterwards is a separate,
confirmed act. The browser here is a fake Chrome (``run_fakes``): nothing
attaches to a real one and nothing leaves this machine.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from run_fakes import ConnectionsContext, fake_provider
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import cli as cli_module
from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.scoping import install_scope_guard
from netkeeper.services import runs
from netkeeper.services.linkedin_accounts import ensure_account, scheduled_runs_armed
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.users import ensure_local_user

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A migrated database with the local user, under tmp_path only."""
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


def _user(session: Session) -> User:
    return ensure_local_user(session, settings=Settings())


def _armed(factory: sessionmaker[Session]) -> bool:
    with session_scope(factory, write=True) as session:
        user = _user(session)
        return scheduled_runs_armed(session, user, ensure_account(session, user).id)


@pytest.fixture
def fake_chrome(monkeypatch: pytest.MonkeyPatch) -> ConnectionsContext:
    context = ConnectionsContext()
    provider, _ = fake_provider(context)
    monkeypatch.setattr(cli_module, "_provider", lambda settings: provider)
    return context


def test_arming_asks_first_and_no_leaves_it_disarmed(cli_db: sessionmaker[Session]) -> None:
    runner = CliRunner()
    declined = runner.invoke(cli, ["linkedin", "schedule", "arm"], input="n\n")
    assert declined.exit_code == 1 and "stay disarmed" in declined.output
    assert not _armed(cli_db)

    status = runner.invoke(cli, ["linkedin", "schedule", "status"])
    assert status.exit_code == 0 and status.output.startswith("disarmed")

    armed = runner.invoke(cli, ["linkedin", "schedule", "arm"], input="y\n")
    assert armed.exit_code == 0, armed.output
    assert _armed(cli_db)
    assert "armed since" in runner.invoke(cli, ["linkedin", "schedule", "status"]).output

    runner.invoke(cli, ["linkedin", "schedule", "disarm"])
    assert not _armed(cli_db)
    assert runner.invoke(cli, ["linkedin", "schedule", "arm", "--yes"]).exit_code == 0
    assert _armed(cli_db)


def test_a_sync_by_hand_runs_while_disarmed_and_reports_itself(
    cli_db: sessionmaker[Session], fake_chrome: ConnectionsContext
) -> None:
    result = CliRunner().invoke(cli, ["linkedin", "sync"])

    assert result.exit_code == 0, result.output
    assert "run 1 (connections_incremental) started" in result.output
    assert "completed" in result.output
    assert fake_chrome.fetches
    with session_scope(cli_db) as session:
        run = runs.get_run(session, _user(session), 1)
        assert (run.kind, run.trigger, run.status) == (
            SyncRunKind.CONNECTIONS_INCREMENTAL,
            SyncRunTrigger.MANUAL,
            SyncRunStatus.COMPLETED,
        )
    assert not _armed(cli_db)  # a run by hand arms nothing


def test_a_flagged_session_refuses_the_run_and_records_nothing(
    cli_db: sessionmaker[Session], fake_chrome: ConnectionsContext
) -> None:
    with session_scope(cli_db, write=True) as session:
        flag_session(session, _user(session), Outcome.CHECKPOINT, url="/checkpoint/x")

    result = CliRunner().invoke(cli, ["linkedin", "enrich", "--max-visits", "5"])

    assert result.exit_code == 1 and "flagged" in result.output
    assert fake_chrome.pages == []
    with session_scope(cli_db) as session:
        assert runs.list_runs(session, _user(session))[1] == 0


def test_max_visits_must_be_positive(cli_db: sessionmaker[Session]) -> None:
    result = CliRunner().invoke(cli, ["linkedin", "enrich", "--max-visits", "0"])
    assert result.exit_code != 0


def test_runs_run_and_cancel(cli_db: sessionmaker[Session]) -> None:
    runner = CliRunner()
    assert runner.invoke(cli, ["linkedin", "runs"]).output == "no runs yet\n"
    with session_scope(cli_db, write=True) as session:
        user = _user(session)
        running = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW, max_visits=5
        ).id

    listed = runner.invoke(cli, ["linkedin", "runs"])
    assert listed.exit_code == 0 and "enrich" in listed.output and "running" in listed.output
    shown = runner.invoke(cli, ["linkedin", "run", str(running)])
    assert shown.exit_code == 0 and "max visits  5" in shown.output

    cancelled = runner.invoke(cli, ["linkedin", "cancel", str(running)])
    assert cancelled.exit_code == 0, cancelled.output
    with session_scope(cli_db) as session:
        assert runs.cancel_requested(session, _user(session), running)
    assert runner.invoke(cli, ["linkedin", "run", "999"]).exit_code == 1


def test_resuming_what_cannot_be_resumed_says_why(cli_db: sessionmaker[Session]) -> None:
    result = CliRunner().invoke(cli, ["linkedin", "enrich", "--resume", "42"])
    assert result.exit_code == 1 and "no enrichment run 42" in result.output
