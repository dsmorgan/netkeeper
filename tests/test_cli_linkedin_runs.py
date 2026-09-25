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
from netkeeper.services import route_breaker, runs
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


def _trip_breaker(factory: sessionmaker[Session]) -> int:
    with session_scope(factory, write=True) as session:
        user = _user(session)
        account_id = ensure_account(session, user).id
        for _ in range(route_breaker.THRESHOLD):
            route_breaker.record(
                session, user, account_id, route_changed=True, succeeded=False, now=NOW
            )
        return account_id


def _breaker_tripped(factory: sessionmaker[Session], account_id: int) -> bool:
    with session_scope(factory) as session:
        return route_breaker.tripped(session, _user(session), account_id)


def test_reset_breaker_asks_first_and_no_leaves_it_tripped(
    cli_db: sessionmaker[Session],
) -> None:
    account_id = _trip_breaker(cli_db)
    runner = CliRunner()

    declined = runner.invoke(cli, ["linkedin", "schedule", "reset-breaker"], input="n\n")
    assert declined.exit_code == 1 and "stays as it is" in declined.output
    assert _breaker_tripped(cli_db, account_id)

    confirmed = runner.invoke(cli, ["linkedin", "schedule", "reset-breaker"], input="y\n")
    assert confirmed.exit_code == 0, confirmed.output
    assert not _breaker_tripped(cli_db, account_id)


def test_reset_breaker_yes_skips_the_prompt(cli_db: sessionmaker[Session]) -> None:
    account_id = _trip_breaker(cli_db)
    result = CliRunner().invoke(cli, ["linkedin", "schedule", "reset-breaker", "--yes"])
    assert result.exit_code == 0, result.output
    assert not _breaker_tripped(cli_db, account_id)


def test_reset_breaker_on_a_clear_account_says_so_and_asks_nothing(
    cli_db: sessionmaker[Session],
) -> None:
    result = CliRunner().invoke(cli, ["linkedin", "schedule", "reset-breaker"])
    assert result.exit_code == 0
    assert "nothing to reset" in result.output


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
            session,
            user,
            SyncRunKind.ENRICH,
            trigger=SyncRunTrigger.MANUAL,
            now=datetime.now(UTC),
            max_visits=5,
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


def test_cancelling_a_run_nobody_is_running_fails_it(cli_db: sessionmaker[Session]) -> None:
    """#175 review F6: a CLI run killed with SIGKILL leaves its row running and its lock
    free. Cancelling it marks it failed at once; nothing would ever read the flag."""
    with session_scope(cli_db, write=True) as session:
        left = runs.create_run(
            session, _user(session), SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        ).id

    result = CliRunner().invoke(cli, ["linkedin", "cancel", str(left)])

    assert result.exit_code == 0 and "marked it failed" in result.output
    with session_scope(cli_db) as session:
        run = runs.get_run(session, _user(session), left)
        assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "interrupted")


def test_arming_asks_before_it_takes_the_write_lock(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#175 review F4: while the prompt waits on a person, another writer (serve) is not
    locked out. The concurrent write inside the prompt would wait out the busy timeout
    and fail if the command held a writer session across it."""
    wrote: list[bool] = []

    def confirm_while_serve_writes(text: str) -> bool:
        with session_scope(cli_db, write=True) as session:
            flag_session(session, _user(session), Outcome.LOGGED_OUT, url="/authwall")
        wrote.append(True)
        return True

    monkeypatch.setattr("typer.confirm", confirm_while_serve_writes)
    monkeypatch.setattr("netkeeper.db.SQLITE_BUSY_TIMEOUT_MS", 100)

    result = CliRunner().invoke(cli, ["linkedin", "schedule", "arm"])

    assert result.exit_code == 0, result.output
    assert wrote == [True] and _armed(cli_db)


def test_resuming_a_sync_says_it_has_no_plan(cli_db: sessionmaker[Session]) -> None:
    """#175 review F5: a clean message, not a traceback."""
    with session_scope(cli_db, write=True) as session:
        user = _user(session)
        sync = runs.create_run(
            session, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
        runs.finish_run(session, user, sync.id, status=SyncRunStatus.ABORTED, now=NOW)
    result = CliRunner().invoke(cli, ["linkedin", "enrich", "--resume", str(sync.id)])
    assert result.exit_code == 1 and "not an enrichment run" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("error", ["OperationalError", "ProgrammingError"])
def test_an_unreadable_schema_keys_the_lock_by_account_one(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    """#175 review F9: SQLite calls a missing column operational, PostgreSQL a
    programming error; either way preflight keys the lock by account 1, not a crash."""
    from sqlalchemy import exc

    from netkeeper.linkedin.activity_lock import SINGLE_ACCOUNT_KEY

    def schema_too_old(*args: object) -> int:
        raise getattr(exc, error)("SELECT ...", {}, Exception("no such column"))

    monkeypatch.setattr(cli_module, "account_id_for", schema_too_old)
    assert cli_module._browser_lock_key() == SINGLE_ACCOUNT_KEY


def test_the_lock_key_is_the_local_users_account(cli_db: sessionmaker[Session]) -> None:
    with session_scope(cli_db, write=True) as session:
        account = ensure_account(session, _user(session)).id
    from netkeeper.linkedin.activity_lock import account_key

    assert cli_module._browser_lock_key() == account_key(account)
    provider = cli_module._provider(Settings())
    assert provider.locks.legacy_partner == account_key(account)
