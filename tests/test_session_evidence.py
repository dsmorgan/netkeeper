"""#282: the posture report's LinkedIn session row, when nothing probes the browser.

The Settings page's posture report never probes (a request handler may not touch
the browser), so its session row used to read "unknown / not probed" even after a
preflight and a week of runs that plainly worked. It now answers from the last
evidence the database holds: what ``netkeeper preflight`` or ``posture --probe``
recorded, or the newest run that read LinkedIn without flagging the session.

Cookie names only, never a value; nothing here is a real cookie.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_preflight import FAKE_CSRF_VALUE, cookie, make_context, make_provider, provider_for
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import SyncRunKind, SyncRunStatus, SyncRunTrigger, User, UserKind
from netkeeper.scoping import install_scope_guard
from netkeeper.services import runs
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import (
    SESSION_EVIDENCE_KEY,
    last_session_evidence,
    record_session_evidence,
    recorded_evidence,
    run_evidence,
)
from netkeeper.services.linkedin_session import flag_session as raise_flag
from netkeeper.services.posture import (
    SESSION_EVIDENCE_FRESH_FOR,
    PostureReport,
    Protection,
    Status,
    posture,
)
from netkeeper.services.scheduler import sync_account_schedule
from netkeeper.services.settings_kv import set_setting
from netkeeper.services.users import ensure_local_user

#: A Wednesday, 14:00 in New York, inside the default window.
NOW = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
DEFAULTS = Settings()


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    user = factories.make_user(
        writer, timezone=DEFAULTS.linkedin.timezone, created_at=NOW - timedelta(days=3)
    )
    sync_account_schedule(
        writer, user, _account(writer, user), now=NOW, rng=random.Random(4), tz=user.timezone
    )
    return user


def _account(session: Session, user: User) -> int:
    return ensure_account(session, user).id


def _row(session: Session, user: User, *, now: datetime = NOW) -> Protection:
    report: PostureReport = posture(
        session, user, _account(session, user), now=now, settings=DEFAULTS, probe=None
    )
    return next(row for row in report.protections if row.name == "linkedin session")


def _run(
    session: Session,
    user: User,
    *,
    ended: datetime,
    kind: SyncRunKind = SyncRunKind.CONNECTIONS_INCREMENTAL,
    status: SyncRunStatus = SyncRunStatus.COMPLETED,
    counts: dict[str, Any] | None = None,
) -> int:
    run = runs.create_run(session, user, kind, trigger=SyncRunTrigger.SCHEDULED, now=ended)
    runs.finish_run(
        session,
        user,
        run.id,
        status=status,
        now=ended,
        stop_reason="caught_up",
        counts=counts
        if counts is not None
        else {"connections": 40, "session_flagged": False, "outcome": None},
    )
    return run.id


@pytest.fixture(autouse=True)
def _armed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scheduled run needs an armed account; these rows only need to exist."""
    monkeypatch.setattr(runs, "scheduled_runs_armed", lambda *_: True)


# --- the posture row, with no probe ---------------------------------------------


def test_no_evidence_at_all_is_unknown_and_says_how_to_find_out(
    writer: Session, user: User
) -> None:
    row = _row(writer, user)

    assert row.status is Status.UNKNOWN
    assert "netkeeper preflight" in row.warnings[0]
    assert "this report shows what it finds" in row.warnings[0]


def test_a_recent_preflight_reads_on_with_when_and_who(writer: Session, user: User) -> None:
    record_session_evidence(
        writer,
        user,
        logged_in=True,
        source="preflight",
        cookie_names=("li_at",),
        now=NOW - timedelta(hours=2),
    )

    row = _row(writer, user)

    assert row.status is Status.ON
    assert row.warnings == ()
    # 16:00 UTC is 12:00 in New York: the local time, not UTC.
    assert row.value == "last confirmed logged in 2026-09-23 12:00 by preflight (2 h ago)"


def test_a_recent_run_that_read_linkedin_reads_on(writer: Session, user: User) -> None:
    run_id = _run(writer, user, ended=NOW - timedelta(minutes=25))

    row = _row(writer, user)

    assert row.status is Status.ON
    assert row.value == f"last confirmed logged in 2026-09-23 13:35 by run {run_id} (25 min ago)"


def test_stale_evidence_still_reads_on_but_warns(writer: Session, user: User) -> None:
    record_session_evidence(
        writer, user, logged_in=True, source="preflight", now=NOW - timedelta(days=3)
    )

    row = _row(writer, user)

    assert row.status is Status.ON
    assert "3 days ago" in row.value
    assert "may have expired" in row.warnings[0]
    assert "netkeeper preflight" in row.warnings[0]


def test_the_freshness_boundary_is_inclusive(writer: Session, user: User) -> None:
    record_session_evidence(
        writer, user, logged_in=True, source="preflight", now=NOW - SESSION_EVIDENCE_FRESH_FOR
    )
    assert _row(writer, user).warnings == ()
    assert _row(writer, user, now=NOW + timedelta(minutes=1)).warnings != ()


def test_a_flag_raised_after_the_evidence_reads_flagged(writer: Session, user: User) -> None:
    _run(writer, user, ended=NOW - timedelta(hours=3))
    raise_flag(writer, user, Outcome.CHECKPOINT, url="https://example.invalid/checkpoint/x")

    row = _row(writer, user)

    assert row.status is Status.OFF
    assert row.value.startswith("flagged checkpoint")
    assert "session flag row" in row.warnings[0]


def test_a_flag_wins_even_over_newer_evidence(writer: Session, user: User) -> None:
    """A live cookie is not proof a checkpoint is resolved (spec 9.7)."""
    raise_flag(writer, user, Outcome.CHECKPOINT, url="https://example.invalid/checkpoint/x")
    record_session_evidence(
        writer, user, logged_in=True, source="preflight", now=datetime.now(UTC) + timedelta(hours=1)
    )

    assert _row(writer, user).status is Status.OFF


def test_a_check_that_found_no_session_reads_off(writer: Session, user: User) -> None:
    _run(writer, user, ended=NOW - timedelta(hours=5))
    record_session_evidence(
        writer, user, logged_in=False, source="preflight", now=NOW - timedelta(hours=1)
    )

    row = _row(writer, user)

    assert row.status is Status.OFF
    assert "no session" in row.value and "preflight" in row.value
    assert "Log in once" in row.warnings[0]


def test_a_newer_run_outranks_an_older_no_session_check(writer: Session, user: User) -> None:
    record_session_evidence(
        writer, user, logged_in=False, source="preflight", now=NOW - timedelta(hours=5)
    )
    run_id = _run(writer, user, ended=NOW - timedelta(hours=1))

    row = _row(writer, user)

    assert row.status is Status.ON
    assert f"run {run_id}" in row.value


def test_on_a_tie_no_session_wins(writer: Session, user: User) -> None:
    at = NOW - timedelta(hours=1)
    _run(writer, user, ended=at)
    record_session_evidence(writer, user, logged_in=False, source="preflight", now=at)

    evidence = last_session_evidence(writer, user, _account(writer, user))

    assert evidence is not None and evidence.logged_in is False


def test_a_live_probe_still_takes_precedence(writer: Session, user: User) -> None:
    from test_posture import LOGGED_IN

    record_session_evidence(
        writer, user, logged_in=False, source="preflight", now=NOW - timedelta(hours=1)
    )
    report = posture(
        writer, user, _account(writer, user), now=NOW, settings=DEFAULTS, probe=LOGGED_IN
    )
    row = next(row for row in report.protections if row.name == "linkedin session")

    assert row.status is Status.ON
    assert row.value.startswith("logged in (")


# --- which runs count as evidence ------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "status", "counts", "counts_as_evidence"),
    [
        (SyncRunKind.CONNECTIONS_FULL, SyncRunStatus.COMPLETED, None, True),
        (
            SyncRunKind.CONNECTIONS_INCREMENTAL,
            SyncRunStatus.ABORTED,
            {"connections": 12, "session_flagged": False, "outcome": "throttled"},
            True,
        ),
        (
            SyncRunKind.ENRICH,
            SyncRunStatus.ABORTED,
            {"completed": 3, "session_flagged": False, "outcome": None},
            True,
        ),
        (
            SyncRunKind.ENRICH,
            SyncRunStatus.ABORTED,
            {"completed": 0, "session_flagged": False, "outcome": None},
            False,
        ),
        (
            SyncRunKind.CONNECTIONS_FULL,
            SyncRunStatus.ABORTED,
            {"connections": 12, "session_flagged": True, "outcome": "logged_out"},
            False,
        ),
        (
            SyncRunKind.CONNECTIONS_FULL,
            SyncRunStatus.ABORTED,
            {"connections": 12, "outcome": None},
            False,
        ),
        (
            SyncRunKind.CONNECTIONS_FULL,
            SyncRunStatus.FAILED,
            {"connections": 12, "session_flagged": False, "outcome": None},
            False,
        ),
        (SyncRunKind.CONNECTIONS_FULL, SyncRunStatus.COMPLETED, {}, False),
        (
            # A flag-worthy outcome counts against the run even when the flag was not
            # recorded (#294, mutant M6).
            SyncRunKind.CONNECTIONS_FULL,
            SyncRunStatus.ABORTED,
            {"connections": 12, "session_flagged": False, "outcome": "checkpoint"},
            False,
        ),
    ],
    ids=[
        "full-sync-completed",
        "aborted-after-reading",
        "enrichment-harvested",
        "enrichment-read-nothing",
        "flagged",
        "no-flag-field",
        "failed",
        "no-counts",
        "checkpoint-not-flagged",
    ],
)
def test_only_a_run_that_read_linkedin_and_flagged_nothing_is_evidence(
    writer: Session,
    user: User,
    kind: SyncRunKind,
    status: SyncRunStatus,
    counts: dict[str, Any] | None,
    counts_as_evidence: bool,
) -> None:
    _run(writer, user, ended=NOW - timedelta(hours=1), kind=kind, status=status, counts=counts)

    found = run_evidence(writer, user, _account(writer, user))

    assert (found is not None) is counts_as_evidence


def test_run_evidence_is_the_newest_qualifying_run(writer: Session, user: User) -> None:
    older = _run(writer, user, ended=NOW - timedelta(hours=6))
    _run(
        writer,
        user,
        ended=NOW - timedelta(hours=1),
        status=SyncRunStatus.ABORTED,
        counts={"connections": 0, "session_flagged": False, "outcome": None},
    )

    found = run_evidence(writer, user, _account(writer, user))

    assert found is not None and found.source == f"run {older}"


def test_an_unreadable_record_reads_as_nothing_recorded(writer: Session, user: User) -> None:
    for junk in ("yes", {"logged_in": "true", "source": "x", "observed_at": "2026-09-23"}):
        set_setting(writer, user, SESSION_EVIDENCE_KEY, junk)
        assert recorded_evidence(writer, user) is None
    set_setting(
        writer,
        user,
        SESSION_EVIDENCE_KEY,
        {"logged_in": True, "source": "preflight", "observed_at": "not a time"},
    )
    assert recorded_evidence(writer, user) is None


def test_recording_needs_a_writer(session_factory: sessionmaker[Session], user: User) -> None:
    with (
        session_scope(session_factory) as reader,
        pytest.raises(RuntimeError, match="writer session"),
    ):
        record_session_evidence(reader, user, logged_in=True, source="preflight")


def test_the_freshness_window_is_a_day() -> None:
    assert timedelta(hours=24) == SESSION_EVIDENCE_FRESH_FOR


# --- the CLI records what it found -------------------------------------------------


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


def _recorded(factory: sessionmaker[Session]) -> Any:
    with session_scope(factory) as session:
        local = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        return recorded_evidence(session, local)


def test_preflight_records_a_live_session_with_cookie_names_only(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("netkeeper.cli.AttachBrowserProvider", lambda cdp_url, **_: make_provider())

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    evidence = _recorded(cli_db)
    assert evidence is not None
    assert (evidence.logged_in, evidence.source) == (True, "preflight")
    assert "li_at" in evidence.cookie_names
    assert FAKE_CSRF_VALUE not in repr(evidence)


def test_preflight_records_no_session_too(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    no_session = make_provider(cookies=[cookie("JSESSIONID", FAKE_CSRF_VALUE)])
    monkeypatch.setattr("netkeeper.cli.AttachBrowserProvider", lambda cdp_url: no_session)

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 1
    evidence = _recorded(cli_db)
    assert evidence is not None and evidence.logged_in is False


def test_an_unreadable_cookie_jar_records_nothing(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    context = make_context()
    context.cookie_error = RuntimeError("Protocol error")
    monkeypatch.setattr(
        "netkeeper.cli.AttachBrowserProvider", lambda cdp_url: provider_for(context)
    )

    result = CliRunner().invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    assert _recorded(cli_db) is None


def test_posture_probe_records_and_no_probe_then_shows_it(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("netkeeper.cli._provider", lambda settings: make_provider())

    CliRunner().invoke(cli, ["posture"])
    evidence = _recorded(cli_db)
    after = CliRunner().invoke(cli, ["posture", "--no-probe"])

    assert evidence is not None and evidence.source == "posture --probe"
    assert "last confirmed logged in" in after.output
    assert "by posture --probe" in after.output


# --- the web page ---------------------------------------------------------------------


async def test_the_web_report_shows_a_recent_run_without_probing(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    factory = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        local = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        run_id = _run(session, local, ended=datetime.now(UTC) - timedelta(hours=2))

    report = (await client.get("/api/v1/posture")).json()

    row = next(row for row in report["protections"] if row["name"] == "linkedin session")
    assert row["status"] == "on"
    assert f"by run {run_id} (2 h ago)" in row["value"]
    assert row["warnings"] == []
