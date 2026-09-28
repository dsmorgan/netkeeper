"""#213: a run stopped or refused by active hours says so, plainly, wherever runs show.

Run 13 on #31 stopped with "the active window closed before visit 0" and
``stopped by inactive``, and nothing said which window, when it reopens, or how to
change it. Now one sentence (``pacing.outside_window_message``) goes to the log,
the run's note, a refused manual run, and the API; ``stop_reason`` stays the short
word a breaker counts on, with plain words beside it (``stop_reason_text``); and
``schedule status`` and the posture report name the window and where it was set.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from profile_fakes import FakeBrowser
from run_fakes import Clock, fake_provider
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from test_enrichment import _enrich, _last_run, _people, _setup
from test_runs_serve import HEADERS, START, _rows, client_for, served
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import _stopped_by
from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin.enrich import StopReason
from netkeeper.linkedin.pacing import outside_window_message
from netkeeper.models import SyncRunKind, SyncRunStatus, SyncRunTrigger
from netkeeper.scoping import install_scope_guard
from netkeeper.services import runs
from netkeeper.services.posture import active_hours_source, describe_active_hours
from netkeeper.services.users import ensure_local_user

NEW_YORK = "America/New_York"
#: 03:00 in New York on a Wednesday: before the default 08:30 window.
THREE_AM = datetime(2026, 9, 23, 7, 0, tzinfo=UTC)
#: 22:00 in New York the same day: after the window closed.
TEN_PM = datetime(2026, 9, 24, 2, 0, tzinfo=UTC)
#: 14:00 in New York: inside it.
TWO_PM = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
DEFAULT = (time(8, 30), time(21, 30))


# --- the one sentence --------------------------------------------------------------


def test_before_the_window_it_opens_today() -> None:
    assert outside_window_message(THREE_AM, NEW_YORK, start=DEFAULT[0], end=DEFAULT[1]) == (
        "outside active hours (08:30-21:30 America/New_York); the next window opens at"
        " 08:30 today. Change `[linkedin] active_hours` in config.toml to adjust."
    )


def test_after_the_window_it_opens_tomorrow() -> None:
    message = outside_window_message(TEN_PM, ZoneInfo(NEW_YORK), start=DEFAULT[0], end=DEFAULT[1])
    assert "the next window opens at 08:30 tomorrow" in message
    assert "(08:30-21:30 America/New_York)" in message


# --- a manual run refuses up front -----------------------------------------------------


def test_the_refusal_names_the_window_and_the_setting() -> None:
    with pytest.raises(runs.OutsideActiveHours) as refused:
        runs.refuse_if_outside_active_hours(Settings().linkedin, now=TEN_PM)
    assert str(refused.value) == outside_window_message(
        TEN_PM, NEW_YORK, start=DEFAULT[0], end=DEFAULT[1]
    )


def test_inside_the_window_nothing_is_refused() -> None:
    runs.refuse_if_outside_active_hours(Settings().linkedin, now=TWO_PM)


def test_a_window_that_does_not_parse_refuses_rather_than_runs_unguarded() -> None:
    linkedin = replace(Settings().linkedin, active_hours=("8.30", "21:30"))
    with pytest.raises(runs.RunError, match="not two HH:MM times"):
        runs.refuse_if_outside_active_hours(linkedin, now=TWO_PM)


@pytest.mark.parametrize("kind", ["connections_incremental", "enrich"])
async def test_the_api_refuses_a_manual_run_outside_the_window_and_records_nothing(
    bare_engine: Engine,
    no_frontend: None,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    monkeypatch.setattr("netkeeper.web.api.linkedin.utcnow", lambda: TEN_PM)
    provider, connector = fake_provider()
    async with (
        served(bare_engine, Settings(), provider, Clock(START)) as app,
        client_for(app) as client,
    ):
        refused = await client.post("/api/v1/linkedin/runs", json={"kind": kind}, headers=HEADERS)

    assert refused.status_code == 409
    assert refused.json()["detail"].startswith("outside active hours (08:30-21:30")
    assert "`[linkedin] active_hours`" in refused.json()["detail"]
    assert connector.attaches == 0 and _rows(bare_engine) == []


@pytest.fixture
def no_frontend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "no-dist"))


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


def _closed_window_config(tmp_path: Path) -> Path:
    """A config whose one-hour window starts two hours from the real now, so now is outside."""
    local = datetime.now(ZoneInfo(NEW_YORK))
    start = (local + timedelta(hours=2)).strftime("%H:%M")
    end = (local + timedelta(hours=3)).strftime("%H:%M")
    path = tmp_path / "mut-cleanup-config.toml"
    path.write_text(
        f'[linkedin]\ntimezone = "{NEW_YORK}"\nactive_hours = ["{start}", "{end}"]\n',
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("command", [["linkedin", "sync"], ["linkedin", "enrich"]])
def test_the_cli_refuses_a_manual_run_outside_the_window_and_records_nothing(
    cli_db: sessionmaker[Session], tmp_path: Path, command: list[str]
) -> None:
    config = _closed_window_config(tmp_path)

    result = CliRunner().invoke(cli, ["--config", str(config), *command])

    assert result.exit_code == 1
    assert "outside active hours" in result.output
    assert "Change `[linkedin] active_hours` in config.toml" in result.output
    with session_scope(cli_db) as session:
        user = ensure_local_user(session, settings=Settings())
        assert runs.list_runs(session, user)[1] == 0


# --- a run the window stops says so -----------------------------------------------------


async def test_a_run_the_window_stops_says_which_window_and_when_it_opens(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="netkeeper.services.enrichment")
    user_id, _ = _setup(session_factory, _people(3))

    report = await _enrich(
        session_factory, user_id, FakeBrowser.of(_people(3)), clock=Clock(TEN_PM)
    )

    run = _last_run(session_factory, user_id)
    expected = outside_window_message(TEN_PM, NEW_YORK, start=DEFAULT[0], end=DEFAULT[1])
    assert report.result.reason is StopReason.INACTIVE
    assert (run.status, run.stop_reason) == (SyncRunStatus.ABORTED, "inactive")
    assert run.notes == f"stopped {expected}"
    assert any(expected in record.getMessage() for record in caplog.records)
    assert runs.view(run).stop_reason_text == "outside active hours"


# --- the plain words beside the stored reason --------------------------------------------


def test_every_reason_a_run_is_given_has_plain_words() -> None:
    from netkeeper.linkedin.classify import Outcome
    from netkeeper.linkedin.connections import StopReason as SyncStop
    from netkeeper.linkedin.enrich import StopReason as EnrichStop

    stored = {
        *(reason.value for reason in EnrichStop if reason is not EnrichStop.RESPONSE),
        *(reason.value for reason in SyncStop if reason is not SyncStop.RESPONSE),
        *(outcome.value for outcome in Outcome if outcome is not Outcome.OK),
        # the worker's, the runners' recording, and the stale-run sweep's own words
        "cancelled",
        "interrupted",
        "error",
        "session_flagged",
        "heat_skip",
        "disarmed",
        "route_changed_breaker",
        "answer_lost_breaker",
        "no_runner",
        "browser_busy",
        "browser_unavailable",
    }
    assert stored - set(runs.STOP_REASON_TEXT) == set()
    assert runs.STOP_REASON_TEXT["inactive"] == "outside active hours"


def test_an_unknown_reason_is_shown_as_stored_and_none_stays_none() -> None:
    assert runs.describe_stop_reason("something_new") == "something_new"
    assert runs.describe_stop_reason(None) is None
    assert _stopped_by("inactive") == "outside active hours (inactive)"
    assert _stopped_by("something_new") == "something_new"
    assert _stopped_by(None) == "-"


def test_the_cli_run_listing_uses_the_plain_words(cli_db: sessionmaker[Session]) -> None:
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session, settings=Settings())
        run = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=TEN_PM
        )
        runs.finish_run(
            session,
            user,
            run.id,
            status=SyncRunStatus.ABORTED,
            now=TEN_PM,
            stop_reason="inactive",
            notes=[f"stopped {outside_window_message(TEN_PM, NEW_YORK)}"],
        )
        run_id = run.id

    listing = CliRunner().invoke(cli, ["linkedin", "runs"])
    shown = CliRunner().invoke(cli, ["linkedin", "run", str(run_id)])

    assert "outside active hours" in listing.output
    assert "outside active hours (inactive)" in shown.output
    assert "the next window opens at 08:30 tomorrow" in shown.output


async def test_the_api_carries_the_plain_words(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    factory = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = ensure_local_user(session, settings=Settings())
        run = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=TEN_PM
        )
        runs.finish_run(
            session, user, run.id, status=SyncRunStatus.ABORTED, now=TEN_PM, stop_reason="inactive"
        )

    listed = (await client.get("/api/v1/linkedin/runs")).json()["items"][0]

    assert (listed["stop_reason"], listed["stop_reason_text"]) == (
        "inactive",
        "outside active hours",
    )


# --- where the window was set ------------------------------------------------------------


def test_the_window_names_its_source(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    assert active_hours_source(Settings()) == "the defaults (no config.toml)"
    assert active_hours_source(replace(Settings(), source_path=config)) == str(config)
    assert describe_active_hours(Settings()) == (
        "active hours: 08:30-21:30 America/New_York, set in the defaults (no config.toml)"
        " (`[linkedin] active_hours`)"
    )


def test_schedule_status_shows_the_window_and_its_file(
    cli_db: sessionmaker[Session], tmp_path: Path
) -> None:
    config = _closed_window_config(tmp_path)

    result = CliRunner().invoke(cli, ["--config", str(config), "linkedin", "schedule", "status"])

    assert result.exit_code == 0, result.output
    assert f"set in {config}" in result.output
    assert "America/New_York" in result.output


def test_posture_shows_where_the_window_was_set(
    cli_db: sessionmaker[Session], tmp_path: Path
) -> None:
    config = tmp_path / "mut-cleanup-posture.toml"
    config.write_text('[linkedin]\nactive_hours = ["09:00", "17:00"]\n', encoding="utf-8")

    result = CliRunner().invoke(cli, ["--config", str(config), "posture", "--no-probe"])

    assert "09:00-17:00" in result.output
    assert "mut-cleanup-posture.toml" in result.output
