"""``netkeeper linkedin sync|enrich|runs|run|cancel|schedule`` (P2-10).

The supervised first run at CP4 is ``netkeeper linkedin sync`` then
``netkeeper linkedin enrich --max-visits 5``, by hand, while scheduled runs stay
disarmed; ``netkeeper linkedin schedule arm`` afterwards is a separate,
confirmed act. The browser here is a fake Chrome (``run_fakes``): nothing
attaches to a real one and nothing leaves this machine.
"""

from __future__ import annotations

import functools
import random
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from run_fakes import ConnectionsContext, fake_provider, fast_profiles, no_sleep
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
from netkeeper.services import budgets, enrich_plan, route_breaker, runs
from netkeeper.services.linkedin_accounts import (
    account_id_for,
    ensure_account,
    schedule_paused,
    scheduled_runs_armed,
)
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.scheduler import (
    SERVED_SCHEDULES,
    JobKind,
    stored_due,
    sync_account_schedule,
)
from netkeeper.services.settings_kv import delete_setting, set_setting
from netkeeper.services.users import ensure_local_user
from netkeeper.worker import BrowserWorker

#: These tests start runs by hand at whatever time the suite runs (#213).
pytestmark = pytest.mark.usefixtures("inside_active_hours")

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
    # The run's pauses are the pacing tests' subject, not these: sat out in real
    # time they cost seconds a run (#210).
    monkeypatch.setattr(
        cli_module,
        "BrowserWorker",
        functools.partial(BrowserWorker, sleep=no_sleep, profiles=fast_profiles),
    )
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


def test_arming_shows_the_profile_view_notice_with_or_without_yes_and_still_arms(
    cli_db: sessionmaker[Session],
) -> None:
    """#325: informational. It comes before the question and gates nothing."""
    runner = CliRunner()
    declined = runner.invoke(cli, ["linkedin", "schedule", "arm"], input="n\n")
    assert budgets.PROFILE_VIEW_NOTICE in declined.output
    assert declined.output.index("Who viewed your profile") < declined.output.index(
        "arm scheduled LinkedIn runs?"
    )
    assert not _armed(cli_db)

    armed = runner.invoke(cli, ["linkedin", "schedule", "arm", "--yes"])
    assert armed.exit_code == 0, armed.output
    assert budgets.PROFILE_VIEW_NOTICE in armed.output
    assert _armed(cli_db)


def test_arming_seeds_each_served_kind_with_no_due_time(cli_db: sessionmaker[Session]) -> None:
    """#327: once ``serve`` has established a schedule, arming gives each kind it runs a
    due time if it has none, already armed or not, and leaves one that has a due time
    alone. Before that, arming seeds nothing. The inbox poll is served since P4-01."""

    def dues() -> dict[JobKind, datetime | None]:
        with session_scope(cli_db) as session:
            user = _user(session)
            account = account_id_for(session, user)
            return {kind: stored_due(session, user, account, kind) for kind in JobKind}

    runner = CliRunner()
    assert runner.invoke(cli, ["linkedin", "schedule", "arm", "--yes"]).exit_code == 0
    assert set(dues().values()) == {None}  # serve never ran: its first start seeds

    with session_scope(cli_db, write=True) as session:  # what serve's start does
        user = _user(session)
        sync_account_schedule(
            session,
            user,
            account_id_for(session, user),
            now=datetime.now(UTC),
            schedules=SERVED_SCHEDULES,
            rng=random.Random(1),
            tz=user.timezone,
        )
    first = dues()
    assert first[JobKind.INBOX] is not None
    assert all(first[kind] is not None for kind in SERVED_SCHEDULES)

    with session_scope(cli_db, write=True) as session:  # a kind added after arming
        user = _user(session)
        account = account_id_for(session, user)
        assert delete_setting(session, user, f"scheduler.job.{account}.enrich")
    rearmed = runner.invoke(cli, ["linkedin", "schedule", "arm", "--yes"])
    assert rearmed.exit_code == 0 and "already armed" in rearmed.output
    second = dues()
    assert second[JobKind.ENRICH] is not None
    assert {k: v for k, v in second.items() if k is not JobKind.ENRICH} == {
        k: v for k, v in first.items() if k is not JobKind.ENRICH
    }


def test_arming_shows_the_profile_visit_risk_warning_above_100_a_day_and_still_arms(
    cli_db: sessionmaker[Session], tmp_path: Path
) -> None:
    """#318: the warning comes before the question, --yes or not, and never stops arming."""
    config = tmp_path / "risky.toml"
    config.write_text("[linkedin.budget]\nprofile_visits_per_day = 180\n")
    runner = CliRunner()

    declined = runner.invoke(
        cli, ["--config", str(config), "linkedin", "schedule", "arm"], input="n\n"
    )
    assert declined.exit_code == 1
    warning = "warning: Profile visits are set to 180 a day, above the 100 a day"
    assert warning in declined.output
    assert declined.output.index(warning) < declined.output.index("arm scheduled LinkedIn runs?")
    assert not _armed(cli_db)

    armed = runner.invoke(cli, ["--config", str(config), "linkedin", "schedule", "arm", "--yes"])
    assert armed.exit_code == 0, armed.output
    assert warning in armed.output
    assert _armed(cli_db)


def test_arming_at_100_a_day_shows_no_risk_warning(
    cli_db: sessionmaker[Session], tmp_path: Path
) -> None:
    config = tmp_path / "calm.toml"
    config.write_text("[linkedin.budget]\nprofile_visits_per_day = 100\n")
    result = CliRunner().invoke(
        cli, ["--config", str(config), "linkedin", "schedule", "arm", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "warning:" not in result.output


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


def test_reset_breaker_asks_before_it_takes_the_write_lock(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#191 review F4: while the prompt waits on a person, another writer is not
    locked out, the same as arm (#175 review F4; see
    ``test_arming_asks_before_it_takes_the_write_lock``). The concurrent write
    inside the prompt would wait out the busy timeout and fail if the command
    held a writer session across it."""
    _trip_breaker(cli_db)
    wrote: list[bool] = []

    def confirm_while_serve_writes(text: str) -> bool:
        with session_scope(cli_db, write=True) as session:
            flag_session(session, _user(session), Outcome.LOGGED_OUT, url="/authwall")
        wrote.append(True)
        return True

    monkeypatch.setattr("typer.confirm", confirm_while_serve_writes)
    monkeypatch.setattr("netkeeper.db.SQLITE_BUSY_TIMEOUT_MS", 100)

    result = CliRunner().invoke(cli, ["linkedin", "schedule", "reset-breaker"])

    assert result.exit_code == 0, result.output
    assert wrote == [True]


def test_reset_breaker_clears_a_corrupt_row(cli_db: sessionmaker[Session]) -> None:
    """#191 review N1: a corrupt row reads as tripped, and posture tells the person
    to run reset-breaker, so reset-breaker has to clear it rather than saying there
    is nothing to reset."""
    with session_scope(cli_db, write=True) as session:
        user = _user(session)
        account_id = account_id_for(session, user)
        set_setting(session, user, f"linkedin.route_changed_breaker.{account_id}", "garbage")
    assert _breaker_tripped(cli_db, account_id)

    result = CliRunner().invoke(cli, ["linkedin", "schedule", "reset-breaker"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "unreadable" in result.output
    assert "nothing to reset" not in result.output
    assert not _breaker_tripped(cli_db, account_id)


def test_reset_breaker_on_a_clear_account_says_so_and_asks_nothing(
    cli_db: sessionmaker[Session],
) -> None:
    result = CliRunner().invoke(cli, ["linkedin", "schedule", "reset-breaker"])
    assert result.exit_code == 0
    assert "nothing to reset" in result.output


def _lose_answers(
    factory: sessionmaker[Session],
    runs_in_a_row: int,
    kind: SyncRunKind = SyncRunKind.CONNECTIONS_FULL,
) -> int:
    with session_scope(factory, write=True) as session:
        user = _user(session)
        account_id = ensure_account(session, user).id
        for _ in range(runs_in_a_row):
            route_breaker.record_answer_lost(
                session,
                user,
                account_id,
                kind=kind,
                answer_lost=True,
                clean_end=False,
                now=datetime.now(UTC),
            )
    return account_id


def _answer_lost_counts(factory: sessionmaker[Session], account_id: int) -> list[int]:
    with session_scope(factory) as session:
        states = route_breaker.answer_lost_states(session, _user(session), account_id)
    return [state.count for state in states.values()]


def test_schedule_status_shows_every_count(cli_db: sessionmaker[Session]) -> None:
    """#199: `schedule status` shows each kind's answer-lost count next to the
    breaker's."""
    runner = CliRunner()
    clear = runner.invoke(cli, ["linkedin", "schedule", "status"])
    assert clear.exit_code == 0, clear.output
    assert "route-changed breaker: 0 of 2 route_changed connections runs in a row" in clear.output
    assert (
        "answer-lost limit (connections_full): 0 of 3 answer_lost connections_full runs in a row"
        in clear.output
    )
    assert (
        "answer-lost limit (connections_incremental): 0 of 3 answer_lost"
        " connections_incremental runs in a row" in clear.output
    )

    _lose_answers(cli_db, 2)
    two = runner.invoke(cli, ["linkedin", "schedule", "status"]).output
    assert "(connections_full): 2 of 3 answer_lost connections_full runs in a row" in two
    assert "tripped" not in two

    _lose_answers(cli_db, 3, SyncRunKind.CONNECTIONS_INCREMENTAL)
    tripped = runner.invoke(cli, ["linkedin", "schedule", "status"]).output
    assert (
        "answer-lost limit (connections_incremental): tripped, 3 of 3 answer_lost"
        " connections_incremental runs in a row" in tripped
    )
    assert "reset-breaker" in tripped


def test_schedule_status_says_an_unreadable_answer_lost_row_is_tripped(
    cli_db: sessionmaker[Session],
) -> None:
    with session_scope(cli_db, write=True) as session:
        user = _user(session)
        account_id = ensure_account(session, user).id
        set_setting(
            session, user, f"linkedin.answer_lost_breaker.connections_full.{account_id}", "garbage"
        )
    out = CliRunner().invoke(cli, ["linkedin", "schedule", "status"]).output
    assert (
        "answer-lost limit (connections_full): stored state unreadable, treated as tripped" in out
    )


def test_reset_breaker_clears_every_answer_lost_streak(cli_db: sessionmaker[Session]) -> None:
    account_id = _lose_answers(cli_db, 3)
    _lose_answers(cli_db, 1, SyncRunKind.CONNECTIONS_INCREMENTAL)
    runner = CliRunner()
    declined = runner.invoke(cli, ["linkedin", "schedule", "reset-breaker"], input="n\n")
    assert declined.exit_code == 1
    assert "3 `answer_lost` connections_full" in declined.output
    assert "1 `answer_lost` connections_incremental" in declined.output
    assert _answer_lost_counts(cli_db, account_id) == [3, 1]

    confirmed = runner.invoke(cli, ["linkedin", "schedule", "reset-breaker"], input="y\n")
    assert confirmed.exit_code == 0, confirmed.output
    assert _answer_lost_counts(cli_db, account_id) == [0, 0]


def test_reset_breaker_clears_a_corrupt_answer_lost_row(cli_db: sessionmaker[Session]) -> None:
    with session_scope(cli_db, write=True) as session:
        user = _user(session)
        account_id = ensure_account(session, user).id
        set_setting(
            session,
            user,
            f"linkedin.answer_lost_breaker.connections_incremental.{account_id}",
            "garbage",
        )
    result = CliRunner().invoke(cli, ["linkedin", "schedule", "reset-breaker"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "unreadable" in result.output
    with session_scope(cli_db) as session:
        assert not route_breaker.answer_lost_tripped(session, _user(session), account_id)


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


def test_enrich_says_it_can_show_up_in_who_viewed_your_profile_before_it_starts(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#325: an informational note before the run starts. It asks nothing."""

    async def no_run(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("netkeeper.cli._execute_printing", no_run)

    result = CliRunner().invoke(cli, ["linkedin", "enrich", "--max-visits", "5"])

    assert "Who viewed your profile" in result.output
    assert budgets.PROFILE_VIEW_NOTICE in result.output
    assert result.output.index("Who viewed your profile") < result.output.index("started;")
    assert "never changes that setting" in result.output


def test_a_sync_by_hand_does_not_show_the_profile_view_notice(
    cli_db: sessionmaker[Session], fake_chrome: ConnectionsContext
) -> None:
    result = CliRunner().invoke(cli, ["linkedin", "sync"])
    assert "Who viewed your profile" not in result.output


def test_a_refused_enrich_does_not_show_the_profile_view_notice(
    cli_db: sessionmaker[Session],
) -> None:
    with session_scope(cli_db, write=True) as session:
        flag_session(session, _user(session), Outcome.CHECKPOINT, url="/checkpoint/x")
    result = CliRunner().invoke(cli, ["linkedin", "enrich"])
    assert result.exit_code == 1 and "Who viewed your profile" not in result.output


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


def test_resuming_an_enrichment_shows_the_profile_view_notice_before_it_starts(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#325: the resume path starts visits too, so it says the same thing first."""

    async def no_run(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("netkeeper.cli._execute_printing", no_run)
    with session_scope(cli_db, write=True) as session:
        user = _user(session)
        old = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
        enrich_plan.store_plan(session, user, old.id, [1, 2, 3])
        runs.finish_run(session, user, old.id, status=SyncRunStatus.ABORTED, now=NOW)
        old_id = old.id

    result = CliRunner().invoke(cli, ["linkedin", "enrich", "--resume", str(old_id)])

    assert budgets.PROFILE_VIEW_NOTICE in result.output
    assert result.output.index("Who viewed your profile") < result.output.index("started;")


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


# --- #324: pause a run, pause the schedule ------------------------------------------------


def test_pause_asks_a_running_enrichment_to_stop_and_keep_its_place(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "browser_held_for", lambda session: lambda account_id: True)
    with session_scope(cli_db, write=True) as session:
        user = _user(session)
        enrich = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        ).id
    runner = CliRunner()

    paused = runner.invoke(cli, ["linkedin", "pause", str(enrich)])

    assert paused.exit_code == 0, paused.output
    assert f"--resume {enrich}" in paused.output
    with session_scope(cli_db, write=True) as session:
        user = _user(session)
        assert runs.pause_requested(session, user, enrich)
        runs.finish_run(
            session,
            user,
            enrich,
            status=SyncRunStatus.ABORTED,
            now=NOW,
            stop_reason="cancelled",
        )
    shown = runner.invoke(cli, ["linkedin", "run", str(enrich)])
    assert "paused; resume it to continue its plan (paused)" in shown.output
    again = runner.invoke(cli, ["linkedin", "pause", str(enrich)])
    assert again.exit_code == 1 and "already ended" in again.output


def test_a_sync_cannot_be_paused(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "browser_held_for", lambda session: lambda account_id: True)
    with session_scope(cli_db, write=True) as session:
        sync = runs.create_run(
            session,
            _user(session),
            SyncRunKind.CONNECTIONS_FULL,
            trigger=SyncRunTrigger.MANUAL,
            now=NOW,
        ).id
    result = CliRunner().invoke(cli, ["linkedin", "pause", str(sync)])
    assert result.exit_code == 1 and "cancel a sync instead" in result.output


def test_schedule_pause_and_unpause_show_in_status(cli_db: sessionmaker[Session]) -> None:
    runner = CliRunner()
    assert runner.invoke(cli, ["linkedin", "schedule", "arm", "--yes"]).exit_code == 0

    paused = runner.invoke(cli, ["linkedin", "schedule", "pause"])
    status = runner.invoke(cli, ["linkedin", "schedule", "status"])

    assert paused.exit_code == 0 and "paused" in paused.output
    assert "armed since" in status.output
    assert "paused since" in status.output and "schedule unpause" in status.output
    assert _armed(cli_db)  # pausing does not disarm

    unpaused = runner.invoke(cli, ["linkedin", "schedule", "unpause"])
    after = runner.invoke(cli, ["linkedin", "schedule", "status"])
    assert unpaused.exit_code == 0 and "next due time" in unpaused.output
    assert "paused since" not in after.output
    with session_scope(cli_db) as session:
        user = _user(session)
        assert not schedule_paused(session, user, account_id_for(session, user))


# --- #424: the Contact info breaker -----------------------------------------------------


def _lose_contact_info(factory: sessionmaker[Session], runs_in_a_row: int) -> int:
    with session_scope(factory, write=True) as session:
        user = _user(session)
        account_id = ensure_account(session, user).id
        for _ in range(runs_in_a_row):
            route_breaker.record_contact_info(
                session, user, account_id, answer_lost=True, clean_end=False, now=NOW
            )
    return account_id


def _contact_info_count(factory: sessionmaker[Session], account_id: int) -> int:
    with session_scope(factory) as session:
        return route_breaker.contact_info_state(session, _user(session), account_id).count


def test_schedule_status_shows_the_contact_info_breaker(cli_db: sessionmaker[Session]) -> None:
    runner = CliRunner()
    clear = runner.invoke(cli, ["linkedin", "schedule", "status"]).output
    assert "Contact info breaker: 0 of 3 Contact-info-lost enrich runs in a row" in clear

    _lose_contact_info(cli_db, 3)
    tripped = runner.invoke(cli, ["linkedin", "schedule", "status"]).output
    assert (
        "Contact info breaker: tripped, 3 of 3 Contact-info-lost enrich runs in a row;"
        " scheduled enrichment runs are skipped (`netkeeper linkedin schedule reset-breaker`)"
    ) in tripped


def test_schedule_status_says_an_unreadable_contact_info_row_is_tripped(
    cli_db: sessionmaker[Session],
) -> None:
    with session_scope(cli_db, write=True) as session:
        user = _user(session)
        account_id = ensure_account(session, user).id
        set_setting(session, user, f"linkedin.contact_info_breaker.{account_id}", "garbage")
    out = CliRunner().invoke(cli, ["linkedin", "schedule", "status"]).output
    assert (
        "Contact info breaker: stored state unreadable, treated as tripped; scheduled"
        " enrichment runs are skipped"
    ) in out


def test_reset_breaker_clears_the_contact_info_breaker(cli_db: sessionmaker[Session]) -> None:
    account_id = _lose_contact_info(cli_db, 3)
    runner = CliRunner()
    declined = runner.invoke(cli, ["linkedin", "schedule", "reset-breaker"], input="n\n")
    assert declined.exit_code == 1
    assert "3 Contact-info-lost enrich" in declined.output
    assert "inbox runs will be allowed to fire again" in declined.output
    assert _contact_info_count(cli_db, account_id) == 3

    confirmed = runner.invoke(cli, ["linkedin", "schedule", "reset-breaker"], input="y\n")
    assert confirmed.exit_code == 0, confirmed.output
    assert "inbox breaker, and inbox owner breaker reset" in confirmed.output
    assert _contact_info_count(cli_db, account_id) == 0


def test_reset_breaker_clears_a_corrupt_contact_info_row(cli_db: sessionmaker[Session]) -> None:
    with session_scope(cli_db, write=True) as session:
        user = _user(session)
        account_id = ensure_account(session, user).id
        set_setting(session, user, f"linkedin.contact_info_breaker.{account_id}", "garbage")
    result = CliRunner().invoke(cli, ["linkedin", "schedule", "reset-breaker"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Contact-info-lost enrich unreadable" in result.output
    with session_scope(cli_db) as session:
        assert not route_breaker.contact_info_tripped(session, _user(session), account_id)


def _inbox_changed(factory: sessionmaker[Session], polls_in_a_row: int) -> int:
    with session_scope(factory, write=True) as session:
        user = _user(session)
        account_id = ensure_account(session, user).id
        for _ in range(polls_in_a_row):
            route_breaker.record_inbox(
                session, user, account_id, route_changed=True, completed=False, now=NOW
            )
    return account_id


def _inbox_count(factory: sessionmaker[Session], account_id: int) -> int:
    with session_scope(factory) as session:
        return route_breaker.inbox_state(session, _user(session), account_id).count


def test_schedule_status_shows_the_inbox_breaker(cli_db: sessionmaker[Session]) -> None:
    runner = CliRunner()
    clear = runner.invoke(cli, ["linkedin", "schedule", "status"]).output
    assert "inbox breaker: 0 of 2 unreadable inbox runs in a row" in clear

    _inbox_changed(cli_db, 2)
    tripped = runner.invoke(cli, ["linkedin", "schedule", "status"]).output
    assert (
        "inbox breaker: tripped, 2 of 2 unreadable inbox runs in a row;"
        " scheduled inbox runs are skipped (`netkeeper linkedin schedule reset-breaker`)"
    ) in tripped
    # The connections lines are not the inbox's.
    assert "route-changed breaker: 0 of 2 route_changed connections runs in a row" in tripped


def test_reset_breaker_clears_the_inbox_breaker(cli_db: sessionmaker[Session]) -> None:
    account_id = _inbox_changed(cli_db, 2)
    runner = CliRunner()
    declined = runner.invoke(cli, ["linkedin", "schedule", "reset-breaker"], input="n\n")
    assert declined.exit_code == 1
    assert "2 unreadable inbox" in declined.output
    assert "inbox runs will be allowed to fire again" in declined.output
    assert _inbox_count(cli_db, account_id) == 2

    confirmed = runner.invoke(cli, ["linkedin", "schedule", "reset-breaker"], input="y\n")
    assert confirmed.exit_code == 0, confirmed.output
    assert "inbox breaker" in confirmed.output and "reset" in confirmed.output
    assert _inbox_count(cli_db, account_id) == 0


def test_reset_breaker_clears_a_corrupt_inbox_row(cli_db: sessionmaker[Session]) -> None:
    with session_scope(cli_db, write=True) as session:
        user = _user(session)
        account_id = ensure_account(session, user).id
        set_setting(session, user, f"linkedin.inbox_route_changed_breaker.{account_id}", "garbage")
    result = CliRunner().invoke(cli, ["linkedin", "schedule", "reset-breaker"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "inbox breaker state unreadable" in result.output
    with session_scope(cli_db) as session:
        assert not route_breaker.inbox_tripped(session, _user(session), account_id)


def _owner_mismatches(factory: sessionmaker[Session], polls_in_a_row: int) -> int:
    with session_scope(factory, write=True) as session:
        user = _user(session)
        account_id = ensure_account(session, user).id
        for _ in range(polls_in_a_row):
            route_breaker.record_inbox_owner(
                session, user, account_id, owner_mismatch=True, completed=False, now=NOW
            )
    return account_id


def test_schedule_status_shows_the_inbox_owner_breaker_and_its_fix(
    cli_db: sessionmaker[Session],
) -> None:
    runner = CliRunner()
    clear = runner.invoke(cli, ["linkedin", "schedule", "status"]).output
    assert "inbox owner breaker: 0 of 2 owner_mismatch inbox runs in a row" in clear
    assert "to fix the mailbox mismatch" not in clear

    _owner_mismatches(cli_db, 2)
    tripped = runner.invoke(cli, ["linkedin", "schedule", "status"]).output
    assert (
        "inbox owner breaker: tripped, 2 of 2 owner_mismatch inbox runs in a row;"
        " scheduled inbox runs are skipped (`netkeeper linkedin schedule reset-breaker`)"
    ) in tripped
    assert "to fix the mailbox mismatch: if you changed LinkedIn accounts" in tripped
    assert "netkeeper linkedin inbox-forget-owner" in tripped
    # The unreadable-page breaker is a different line and stays clear.
    assert "inbox breaker: 0 of 2 unreadable inbox runs in a row" in tripped


def test_reset_breaker_clears_the_inbox_owner_breaker(cli_db: sessionmaker[Session]) -> None:
    account_id = _owner_mismatches(cli_db, 2)
    runner = CliRunner()
    declined = runner.invoke(cli, ["linkedin", "schedule", "reset-breaker"], input="n\n")
    assert declined.exit_code == 1
    assert "2 owner_mismatch inbox" in declined.output
    confirmed = runner.invoke(cli, ["linkedin", "schedule", "reset-breaker"], input="y\n")
    assert confirmed.exit_code == 0, confirmed.output
    with session_scope(cli_db) as session:
        assert not route_breaker.inbox_owner_tripped(session, _user(session), account_id)
