"""#405: why a run's visits or answers could not be read, read back from the run.

The service (:mod:`netkeeper.services.run_diagnostics`), ``GET
/linkedin/runs/{id}/diagnostics``, and ``netkeeper linkedin run <id>``, over runs
recorded directly: what the runner writes is ``tests/test_enrichment.py``'s.
"""

from __future__ import annotations

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
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin.enrich import UnreadableCause
from netkeeper.models import SyncRun, SyncRunKind, SyncRunStatus, SyncRunTrigger, User, UserKind
from netkeeper.scoping import install_scope_guard
from netkeeper.services import run_diagnostics, runs
from netkeeper.services.users import ensure_local_user

NOW = datetime(2026, 10, 4, 15, 30, tzinfo=UTC)


def _run(
    session: Session,
    user: User,
    *,
    kind: SyncRunKind = SyncRunKind.ENRICH,
    counts: dict[str, Any] | None = None,
    progress: dict[str, Any] | None = None,
    finish: bool = True,
) -> SyncRun:
    run = runs.create_run(session, user, kind, trigger=SyncRunTrigger.MANUAL, now=NOW)
    if progress is not None:
        runs.record_progress(session, user, run.id, progress)
    if finish:
        runs.finish_run(
            session,
            user,
            run.id,
            status=SyncRunStatus.ABORTED,
            now=NOW,
            stop_reason="route_changed",
            counts=counts,
        )
    return run


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A writer session: recording runs needs one."""
    with session_scope(session_factory, write=True) as session:
        yield session


def _visit(number: int, contact_id: int, reason: str) -> dict[str, Any]:
    return {"visit": number, "contact_id": contact_id, "reason": reason}


# --- the service ------------------------------------------------------------------------------


def test_each_visit_reads_back_with_its_reason_and_the_contacts_name(writer: Session) -> None:
    session = writer
    user = factories.make_user(session)
    ada = factories.make_contact(session, user, first_name="Ada", last_name="Fake")
    bo = factories.make_contact(session, user, first_name="Bo", last_name=None)
    run = _run(
        session,
        user,
        counts={
            "unreadable_visits": [
                _visit(4, ada.id, "overlay_never_answered"),
                _visit(9, bo.id, "id_mismatch"),
            ]
        },
    )

    found = run_diagnostics.diagnose(session, user, run)

    assert [(v.visit, v.contact_id, v.reason) for v in found.unreadable_visits] == [
        (4, ada.id, "overlay_never_answered"),
        (9, bo.id, "id_mismatch"),
    ]
    first, second = found.unreadable_visits
    assert (first.first_name, first.last_name, first.contact_exists) == ("Ada", "Fake", True)
    assert first.reason_text == "the Contact info overlay never answered"
    assert second.first_name == "Bo" and second.reason_text.startswith("the profile's id")
    assert found.lost_answers == ()


def test_a_running_run_reads_its_progress_record(writer: Session) -> None:
    session = writer
    user = factories.make_user(session)
    ada = factories.make_contact(session, user)
    run = _run(
        session,
        user,
        progress={"unreadable_visits": [_visit(1, ada.id, "landed_off_profile")]},
        finish=False,
    )
    (visit,) = run_diagnostics.diagnose(session, user, run).unreadable_visits
    assert (visit.visit, visit.reason) == (1, "landed_off_profile")


def test_another_users_contact_is_never_named(writer: Session) -> None:
    """A record can only hold the user's own ids, but the lookup is scoped all the same."""
    session = writer
    user = factories.make_user(session)
    other = factories.make_user(session)
    theirs = factories.make_contact(session, other, first_name="Secret", last_name="Name")
    run = _run(session, user, counts={"unreadable_visits": [_visit(1, theirs.id, "unknown")]})
    (visit,) = run_diagnostics.diagnose(session, user, run).unreadable_visits
    assert (visit.contact_exists, visit.first_name, visit.last_name) == (False, None, None)


@pytest.mark.parametrize(
    "record",
    [
        "not a list",
        [1, "two", None],
        [{"visit": "1", "contact_id": 1, "reason": "x"}],
        [{"visit": 1, "contact_id": None, "reason": "x"}],
        [{"visit": 1, "contact_id": 1}],
    ],
)
def test_a_record_in_an_unknown_shape_reads_as_nothing(writer: Session, record: Any) -> None:
    session = writer
    user = factories.make_user(session)
    run = _run(session, user, counts={"unreadable_visits": record, "lost": record})
    found = run_diagnostics.diagnose(session, user, run)
    assert found.unreadable_visits == () and found.lost_answers == ()


def test_a_run_from_before_405_has_nothing_to_show(writer: Session) -> None:
    session = writer
    user = factories.make_user(session)
    run = _run(session, user, counts={"planned": 3, "unreadable": 2, "lost": 0})
    found = run_diagnostics.diagnose(session, user, run)
    assert found.unreadable_visits == () and found.lost_answers == ()


def test_a_connections_runs_lost_answers_read_back(writer: Session) -> None:
    session = writer
    user = factories.make_user(session)
    run = _run(
        session,
        user,
        kind=SyncRunKind.CONNECTIONS_FULL,
        counts={
            "lost": [
                {"start": 80, "cause": "Error (no resource)", "ending": "the page moved past it"},
                {"start": 120, "cause": "Error (no resource)"},  # recorded before #405
            ]
        },
    )
    found = run_diagnostics.diagnose(session, user, run)
    assert [(a.start, a.cause, a.ending) for a in found.lost_answers] == [
        (80, "Error (no resource)", "the page moved past it"),
        (120, "Error (no resource)", None),
    ]


def test_every_cause_has_words() -> None:
    assert set(run_diagnostics.REASON_TEXT) == {cause.value for cause in UnreadableCause}
    assert run_diagnostics.reason_text("from_the_future") == "from_the_future"


# --- the API ----------------------------------------------------------------------------------


async def test_the_api_answers_the_runs_reasons(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    factory = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        ada = factories.make_contact(session, user, first_name="Ada", last_name="Fake")
        run_id = _run(
            session,
            user,
            counts={"unreadable_visits": [_visit(2, ada.id, "contact_info_control_missing")]},
        ).id
        ada_id = ada.id

    answer = await client.get(f"/api/v1/linkedin/runs/{run_id}/diagnostics")
    missing = await client.get("/api/v1/linkedin/runs/999/diagnostics")

    assert answer.status_code == 200
    assert answer.json() == {
        "unreadable_visits": [
            {
                "visit": 2,
                "contact_id": ada_id,
                "contact_exists": True,
                "first_name": "Ada",
                "last_name": "Fake",
                "reason": "contact_info_control_missing",
                "reason_text": "no Contact info control on the page",
            }
        ],
        "lost_answers": [],
        "stopped_by": [],
    }
    assert missing.status_code == 404


async def test_another_users_run_is_not_found(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    factory = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        other = factories.make_user(session)
        theirs = factories.make_contact(session, other, first_name="Secret")
        run_id = _run(
            session, other, counts={"unreadable_visits": [_visit(1, theirs.id, "unknown")]}
        ).id
    answer = await client.get(f"/api/v1/linkedin/runs/{run_id}/diagnostics")
    assert answer.status_code == 404 and "Secret" not in answer.text


def test_the_visit_that_stopped_a_run_at_once_reads_back(writer: Session) -> None:
    session = writer
    user = factories.make_user(session)
    ada = factories.make_contact(session, user, first_name="Ada", last_name="Fake")
    run = _run(
        session,
        user,
        counts={"unreadable_visits": [], "stopped_by": _visit(2, ada.id, "profile_status")},
    )
    found = run_diagnostics.diagnose(session, user, run)
    assert found.unreadable_visits == ()
    assert found.stopped_by is not None
    assert (found.stopped_by.visit, found.stopped_by.reason, found.stopped_by.first_name) == (
        2,
        "profile_status",
        "Ada",
    )


@pytest.mark.parametrize("stored", [None, "x", {"visit": 1}, []])
def test_a_stopped_by_in_an_unknown_shape_reads_as_none(writer: Session, stored: Any) -> None:
    user = factories.make_user(writer)
    run = _run(writer, user, counts={"stopped_by": stored})
    assert run_diagnostics.diagnose(writer, user, run).stopped_by is None


def _run_at(session: Session, user: User, at: datetime, counts: dict[str, Any], **kind: Any) -> int:
    run = runs.create_run(
        session, user, kind.get("kind", SyncRunKind.ENRICH), trigger=SyncRunTrigger.MANUAL, now=at
    )
    runs.finish_run(session, user, run.id, status=SyncRunStatus.ABORTED, now=at, counts=counts)
    return run.id


def test_recurring_groups_reasons_by_contact_and_code_across_runs(writer: Session) -> None:
    session = writer
    user = factories.make_user(session)
    other = factories.make_user(session)
    ada = factories.make_contact(session, user, first_name="Ada", last_name="Fake")
    bo = factories.make_contact(session, user, first_name="Bo")
    old = _run_at(
        session, user, NOW - timedelta(days=20), {"unreadable_visits": [_visit(1, ada.id, "x")]}
    )
    first = _run_at(
        session,
        user,
        NOW - timedelta(days=2),
        {
            "unreadable_visits": [
                _visit(1, ada.id, "overlay_never_answered"),
                _visit(4, bo.id, "id_mismatch"),
            ]
        },
    )
    second = _run_at(
        session,
        user,
        NOW - timedelta(days=1),
        {
            "unreadable_visits": [_visit(2, ada.id, "overlay_never_answered")],
            "stopped_by": _visit(3, bo.id, "profile_status"),
        },
    )
    _run_at(  # another user's runs and a sync's lost answers are never read here
        session, other, NOW, {"unreadable_visits": [_visit(1, ada.id, "overlay_never_answered")]}
    )
    _run_at(
        session,
        user,
        NOW,
        {"lost": [{"start": 0, "cause": "c"}]},
        kind=SyncRunKind.CONNECTIONS_FULL,
    )

    found = run_diagnostics.recurring(session, user, since=NOW - timedelta(days=14))

    assert [(r.contact_id, r.reason, r.visits, r.run_ids) for r in found] == [
        (ada.id, "overlay_never_answered", 2, (first, second)),
        (bo.id, "profile_status", 1, (second,)),
        (bo.id, "id_mismatch", 1, (first,)),
    ]
    assert found[0].first_name == "Ada" and found[0].last_seen == NOW - timedelta(days=1)
    assert old not in {run_id for r in found for run_id in r.run_ids}


# --- the CLI ----------------------------------------------------------------------------------


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


def test_linkedin_run_prints_each_visits_reason(cli_db: sessionmaker[Session]) -> None:
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session, settings=Settings())
        ada = factories.make_contact(session, user, first_name="Ada", last_name="Fake")
        gone = factories.make_contact(session, user)
        visits = [_visit(3, ada.id, "overlay_never_answered"), _visit(5, gone.id, "id_mismatch")]
        run_id = _run(
            session, user, counts={"planned": 44, "unreadable": 1, "unreadable_visits": visits}
        ).id
        ada_id, gone_id = ada.id, gone.id
        session.delete(gone)

    result = CliRunner().invoke(cli, ["linkedin", "run", str(run_id)])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "unreadable visits:" in lines
    table = lines[lines.index("unreadable visits:") + 1 :]
    assert table[0].split() == ["VISIT", "CONTACT", "NAME", "REASON"]
    assert table[1].split()[:4] == ["3", str(ada_id), "Ada", "Fake"]
    assert table[1].endswith("the Contact info overlay never answered (overlay_never_answered)")
    assert table[2].split()[:3] == ["5", str(gone_id), "(deleted)"]
    # The list is its own table, never a cell of the field table.
    assert not any(line.startswith("unreadable visits ") for line in lines)


def test_linkedin_run_says_which_visit_stopped_a_run_at_once(
    cli_db: sessionmaker[Session],
) -> None:
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session, settings=Settings())
        ada = factories.make_contact(session, user, first_name="Ada", last_name="Fake")
        stopped = _run(
            session,
            user,
            counts={"unreadable_visits": [], "stopped_by": _visit(2, ada.id, "profile_status")},
        ).id
        bare = _run(session, user, counts={"planned": 3}).id
        ada_id = ada.id

    shown = CliRunner().invoke(cli, ["linkedin", "run", str(stopped)])
    assert shown.exit_code == 0, shown.output
    assert (
        f"stopped at once by the page's answer on visit 2 (contact {ada_id}, Ada Fake):"
        " the profile answered a status that stopped the run at once (profile_status)"
    ) in shown.output
    old = CliRunner().invoke(cli, ["linkedin", "run", str(bare)])
    assert "no per-visit reasons were recorded for this run" in old.output


def test_linkedin_unreadable_groups_reasons_across_runs(cli_db: sessionmaker[Session]) -> None:
    now = datetime.now(UTC)
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session, settings=Settings())
        ada = factories.make_contact(session, user, first_name="Ada", last_name="Fake")
        visits = {"unreadable_visits": [_visit(1, ada.id, "overlay_never_answered")]}
        first = _run_at(session, user, now - timedelta(days=2), visits)
        second = _run_at(session, user, now - timedelta(days=1), visits)
        _run_at(session, user, now - timedelta(days=30), visits)
        ada_id = ada.id

    result = CliRunner().invoke(cli, ["linkedin", "unreadable", "--since", "14d"])

    assert result.exit_code == 0, result.output
    header, row = result.output.splitlines()
    assert header.split() == ["CONTACT", "NAME", "REASON", "VISITS", "RUNS", "LAST", "(UTC)"]
    assert row.split()[:6] == [
        str(ada_id),
        "Ada",
        "Fake",
        "overlay_never_answered",
        "2",
        f"{first},{second}",
    ]


@pytest.mark.parametrize("since", ["3651d", "87601h", "999999d", "99999999999d"])
def test_linkedin_unreadable_refuses_a_window_past_ten_years(
    cli_db: sessionmaker[Session], since: str
) -> None:
    result = CliRunner().invoke(cli, ["linkedin", "unreadable", "--since", since])
    assert result.exit_code == 2 and "at most 3650d" in result.output


def test_linkedin_unreadable_takes_ten_years(cli_db: sessionmaker[Session]) -> None:
    result = CliRunner().invoke(cli, ["linkedin", "unreadable", "--since", "3650d"])
    assert result.exit_code == 0, result.output


def test_a_sync_stopped_route_changed_says_nothing_about_visits(
    cli_db: sessionmaker[Session],
) -> None:
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session, settings=Settings())
        sync = _run(session, user, kind=SyncRunKind.CONNECTIONS_FULL, counts={"pages": 2}).id
    shown = CliRunner().invoke(cli, ["linkedin", "run", str(sync)])
    assert shown.exit_code == 0, shown.output
    assert "per-visit" not in shown.output


@pytest.mark.parametrize("since", ["14", "two weeks", "0d", "-3d"])
def test_linkedin_unreadable_refuses_a_window_it_cannot_read(
    cli_db: sessionmaker[Session], since: str
) -> None:
    result = CliRunner().invoke(cli, ["linkedin", "unreadable", "--since", since])
    assert result.exit_code == 2 and "like 14d or 36h" in result.output


def test_linkedin_unreadable_with_nothing_recorded(cli_db: sessionmaker[Session]) -> None:
    result = CliRunner().invoke(cli, ["linkedin", "unreadable", "--since", "36h"])
    assert result.exit_code == 0
    assert result.output.strip() == "no unreadable visits recorded in the last 36h"


def test_linkedin_run_prints_a_syncs_lost_answers(cli_db: sessionmaker[Session]) -> None:
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session, settings=Settings())
        run_id = _run(
            session,
            user,
            kind=SyncRunKind.CONNECTIONS_FULL,
            counts={
                "lost": [
                    {
                        "start": 80,
                        "cause": "Error (no resource)",
                        "ending": "the page moved past it",
                    }
                ]
            },
        ).id

    result = CliRunner().invoke(cli, ["linkedin", "run", str(run_id)])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    table = lines[lines.index("lost answers:") + 1 :]
    assert table[0].split() == ["START", "CAUSE", "THEN"]
    assert table[1].split()[0] == "80" and "the page moved past it" in table[1]
