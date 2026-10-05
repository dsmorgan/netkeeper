"""``/linkedin``: the confirmed session-flag and heat clears, and browser health (#181).

The clears are the web half of ``netkeeper linkedin clear-flag`` and of spec
9.7's manual heat clear. Both clear a LinkedIn safety signal, so each needs
``confirm: true`` and the exact state the person was shown, and refuses
anything else without writing. Browser health reads only what was recorded;
nothing here, and nothing it calls, attaches to a browser.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_runs_serve import HEADERS

from netkeeper.config import Settings
from netkeeper.crm.inbox_apply import record_short_first_poll, short_first_poll
from netkeeper.db import session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import SyncRunKind, SyncRunStatus, SyncRunTrigger, User, UserKind
from netkeeper.services import heat, runs
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import (
    SESSION_FLAG_KEY,
    FlagClearRefused,
    clear_confirmed_flag,
    flag_session,
    record_session_evidence,
    same_instant,
    session_flag,
)
from netkeeper.services.settings_kv import set_setting

FLAG_URL = "/api/v1/linkedin/session-flag/clear"
HEAT_URL = "/api/v1/linkedin/heat/clear"


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


def _flag(app: FastAPI, outcome: Outcome = Outcome.CHECKPOINT) -> None:
    with session_scope(app.state.session_factory, write=True) as session:
        flag_session(session, _local(session), outcome, url="/checkpoint/challenge?ctx=x")


def _stored_flag(app: FastAPI):  # type: ignore[no-untyped-def]
    with session_scope(app.state.session_factory) as session:
        return session_flag(session, _local(session))


# --- the session flag -----------------------------------------------------------------


async def test_clear_flag_clears_exactly_the_flag_that_was_shown(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _flag(running_app)
    status = (await client.get("/api/v1/linkedin/status")).json()

    response = await client.post(
        FLAG_URL,
        json={
            "confirm": True,
            "outcome": status["session_flag"],
            "flagged_at": status["session_flagged_at"],
        },
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["session_flag"] is None
    assert _stored_flag(running_app) is None


async def test_clear_flag_needs_confirm_true_and_leaves_the_flag(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _flag(running_app)
    status = (await client.get("/api/v1/linkedin/status")).json()

    response = await client.post(
        FLAG_URL,
        json={
            "confirm": False,
            "outcome": status["session_flag"],
            "flagged_at": status["session_flagged_at"],
        },
        headers=HEADERS,
    )

    assert response.status_code == 422
    assert "confirm: true" in response.json()["detail"]
    assert _stored_flag(running_app) is not None


async def test_clear_flag_refuses_when_no_flag_is_set(client: httpx.AsyncClient) -> None:
    response = await client.post(
        FLAG_URL,
        json={"confirm": True, "outcome": "checkpoint", "flagged_at": "2026-01-01T00:00:00Z"},
        headers=HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "no session flag is set"


async def test_clear_flag_refuses_a_flag_raised_again_since_the_confirm(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """The CLI's re-read after its prompt: an answer about one flag never clears another."""
    _flag(running_app)
    seen = (await client.get("/api/v1/linkedin/status")).json()
    later = datetime.fromisoformat(seen["session_flagged_at"]) + timedelta(minutes=5)
    with session_scope(running_app.state.session_factory, write=True) as session:
        set_setting(
            session,
            _local(session),
            SESSION_FLAG_KEY,
            {"outcome": "checkpoint", "url": "/checkpoint", "flagged_at": later.isoformat()},
        )

    response = await client.post(
        FLAG_URL,
        json={
            "confirm": True,
            "outcome": seen["session_flag"],
            "flagged_at": seen["session_flagged_at"],
        },
        headers=HEADERS,
    )

    assert response.status_code == 409
    assert "changed" in response.json()["detail"]
    stored = _stored_flag(running_app)
    assert stored is not None and same_instant(stored.flagged_at, later)


async def test_clear_flag_refuses_a_different_outcome(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _flag(running_app, Outcome.CHECKPOINT)
    seen = (await client.get("/api/v1/linkedin/status")).json()

    response = await client.post(
        FLAG_URL,
        json={
            "confirm": True,
            "outcome": "logged_out",
            "flagged_at": seen["session_flagged_at"],
        },
        headers=HEADERS,
    )

    assert response.status_code == 409
    assert _stored_flag(running_app) is not None


async def test_clear_flag_goes_through_the_csrf_guard(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _flag(running_app)
    seen = (await client.get("/api/v1/linkedin/status")).json()

    response = await client.post(
        FLAG_URL,
        json={
            "confirm": True,
            "outcome": seen["session_flag"],
            "flagged_at": seen["session_flagged_at"],
        },
    )

    assert response.status_code == 403
    assert _stored_flag(running_app) is not None


async def test_clear_flag_leaves_a_warning_in_the_log(
    client: httpx.AsyncClient, running_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """#364 S1: a manual clear of a safety signal is never silent."""
    _flag(running_app)
    seen = (await client.get("/api/v1/linkedin/status")).json()

    with caplog.at_level(logging.WARNING, logger="netkeeper.services.linkedin_session"):
        response = await client.post(
            FLAG_URL,
            json={
                "confirm": True,
                "outcome": seen["session_flag"],
                "flagged_at": seen["session_flagged_at"],
            },
            headers=HEADERS,
        )

    assert response.status_code == 200
    [record] = [r for r in caplog.records if "session flag cleared by hand" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert "checkpoint" in record.getMessage()
    assert "/checkpoint/challenge" in record.getMessage()
    assert "?ctx" not in record.getMessage()


def test_clear_confirmed_flag_compares_the_url_when_given(running_app: FastAPI) -> None:
    """#364 N1: the CLI shows the url in its prompt, so its identity check includes it."""
    _flag(running_app)
    stored = _stored_flag(running_app)
    assert stored is not None
    factory = running_app.state.session_factory
    with session_scope(factory, write=True) as session, pytest.raises(FlagClearRefused):
        clear_confirmed_flag(
            session,
            _local(session),
            outcome=stored.outcome.value,
            flagged_at=stored.flagged_at,
            url="/somewhere/else",
        )
    assert _stored_flag(running_app) is not None


def test_same_instant_reads_a_naive_datetime_as_utc() -> None:
    aware = datetime(2026, 3, 1, 12, 0, 0, 123456, tzinfo=UTC)
    assert same_instant(aware, aware.replace(tzinfo=None))
    assert not same_instant(aware, aware + timedelta(microseconds=1))


# --- heat -----------------------------------------------------------------------------


def _raise_heat(app: FastAPI, now: datetime) -> None:
    settings = Settings().linkedin.heat
    with session_scope(app.state.session_factory, write=True) as session:
        user = _local(session)
        heat.raise_heat(session, user, ensure_account(session, user).id, now=now, settings=settings)


def _stored_heat(app: FastAPI):  # type: ignore[no-untyped-def]
    with session_scope(app.state.session_factory) as session:
        user = _local(session)
        return heat.state(session, user, ensure_account(session, user).id)


async def test_heat_clear_clears_the_heat_that_was_shown(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _raise_heat(running_app, datetime.now(UTC))
    seen = (await client.get("/api/v1/linkedin/heat")).json()
    assert seen["score"] > 0 and seen["last_raised_at"] is not None

    response = await client.post(
        HEAT_URL, json={"confirm": True, "last_raised_at": seen["last_raised_at"]}, headers=HEADERS
    )

    body = response.json()
    assert response.status_code == 200
    assert body["score"] == 0.0 and body["last_raised_at"] is None
    assert body["cleared_at"] is not None
    stored = _stored_heat(running_app)
    assert stored is not None and stored.score == 0.0


async def test_heat_clear_leaves_a_warning_in_the_log(
    client: httpx.AsyncClient, running_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """#364 S1: the log names the score and when heat was last raised."""
    _raise_heat(running_app, datetime.now(UTC))
    seen = (await client.get("/api/v1/linkedin/heat")).json()

    with caplog.at_level(logging.WARNING, logger="netkeeper.services.heat"):
        response = await client.post(
            HEAT_URL,
            json={"confirm": True, "last_raised_at": seen["last_raised_at"]},
            headers=HEADERS,
        )

    assert response.status_code == 200
    [record] = [r for r in caplog.records if "heat cleared by hand" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert "last raised" in record.getMessage()


async def test_heat_clear_needs_confirm_true(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _raise_heat(running_app, datetime.now(UTC))
    seen = (await client.get("/api/v1/linkedin/heat")).json()

    response = await client.post(
        HEAT_URL, json={"confirm": False, "last_raised_at": seen["last_raised_at"]}, headers=HEADERS
    )

    assert response.status_code == 422
    stored = _stored_heat(running_app)
    assert stored is not None and stored.score > 0


async def test_heat_clear_refuses_when_never_raised(client: httpx.AsyncClient) -> None:
    response = await client.post(
        HEAT_URL,
        json={"confirm": True, "last_raised_at": "2026-01-01T00:00:00Z"},
        headers=HEADERS,
    )

    assert response.status_code == 409
    assert "never raised" in response.json()["detail"]


async def test_heat_clear_refuses_when_already_cleared(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _raise_heat(running_app, datetime.now(UTC))
    seen = (await client.get("/api/v1/linkedin/heat")).json()
    body = {"confirm": True, "last_raised_at": seen["last_raised_at"]}
    assert (await client.post(HEAT_URL, json=body, headers=HEADERS)).status_code == 200

    again = await client.post(HEAT_URL, json=body, headers=HEADERS)

    assert again.status_code == 409
    assert "already cleared" in again.json()["detail"]


async def test_heat_clear_refuses_heat_raised_again_since_the_confirm(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    first = datetime.now(UTC) - timedelta(minutes=10)
    _raise_heat(running_app, first)
    seen = (await client.get("/api/v1/linkedin/heat")).json()
    _raise_heat(running_app, first + timedelta(minutes=5))  # a new throttle in between

    response = await client.post(
        HEAT_URL, json={"confirm": True, "last_raised_at": seen["last_raised_at"]}, headers=HEADERS
    )

    assert response.status_code == 409
    assert "raised again" in response.json()["detail"]
    stored = _stored_heat(running_app)
    assert stored is not None and stored.score > 0


async def test_heat_clear_goes_through_the_csrf_guard(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _raise_heat(running_app, datetime.now(UTC))
    seen = (await client.get("/api/v1/linkedin/heat")).json()

    response = await client.post(
        HEAT_URL, json={"confirm": True, "last_raised_at": seen["last_raised_at"]}
    )

    assert response.status_code == 403
    stored = _stored_heat(running_app)
    assert stored is not None and stored.score > 0


# --- browser health -------------------------------------------------------------------


async def test_browser_health_says_unknown_before_anything_checked(
    client: httpx.AsyncClient,
) -> None:
    health = (await client.get("/api/v1/linkedin/browser/health")).json()

    assert health["session_status"] == "unknown"
    assert health["session_summary"] == "not checked yet"
    assert any("netkeeper preflight" in w for w in health["session_warnings"])
    assert health["chrome_unreachable_at"] is None
    assert health["can_start_runs"] is False
    assert health["running_run_id"] is None


async def test_browser_health_reports_recorded_evidence_and_a_newer_unreachable_run(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    now = datetime.now(UTC)
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        record_session_evidence(
            session, user, logged_in=True, source="preflight", now=now - timedelta(minutes=30)
        )
        older = runs.create_run(
            session,
            user,
            SyncRunKind.CONNECTIONS_INCREMENTAL,
            trigger=SyncRunTrigger.MANUAL,
            now=now - timedelta(hours=2),
        )
        runs.finish_run(
            session,
            user,
            older.id,
            status=SyncRunStatus.FAILED,
            now=now - timedelta(hours=1),
            stop_reason="browser_unavailable",
        )

    before = (await client.get("/api/v1/linkedin/browser/health")).json()
    assert before["session_status"] == "on"
    assert "by preflight" in before["session_summary"]
    # The unreachable run is older than the preflight that found a session.
    assert before["chrome_unreachable_at"] is None

    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        newer = runs.create_run(
            session,
            user,
            SyncRunKind.CONNECTIONS_INCREMENTAL,
            trigger=SyncRunTrigger.MANUAL,
            now=now - timedelta(minutes=5),
        )
        runs.finish_run(
            session,
            user,
            newer.id,
            status=SyncRunStatus.FAILED,
            now=now - timedelta(minutes=4),
            stop_reason="browser_unavailable",
        )

    after = (await client.get("/api/v1/linkedin/browser/health")).json()
    assert after["chrome_unreachable_run_id"] == newer.id
    assert after["chrome_unreachable_at"] is not None


async def test_browser_health_puts_the_session_flag_over_the_evidence(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(running_app.state.session_factory, write=True) as session:
        record_session_evidence(session, _local(session), logged_in=True, source="preflight")
    _flag(running_app)

    health = (await client.get("/api/v1/linkedin/browser/health")).json()

    assert health["session_status"] == "off"
    assert health["session_summary"].startswith("flagged checkpoint")


async def test_browser_health_never_touches_the_browser_executor(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """The route reads rows only: an executor that fails on any use stays untouched."""

    class Untouchable:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"browser health used the executor's {name}")

    original = running_app.state.executor
    running_app.state.executor = Untouchable()
    try:
        response = await client.get("/api/v1/linkedin/browser/health")
    finally:
        running_app.state.executor = original

    assert response.status_code == 200
    assert response.json()["can_start_runs"] is True


# --- the first inbox poll's warning (#383) ------------------------------------------------

INBOX_ACK_URL = "/api/v1/linkedin/inbox/acknowledge"


async def test_acknowledge_clears_the_first_polls_warning(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    short_of = datetime(2030, 1, 2, tzinfo=UTC)
    with session_scope(running_app.state.session_factory, write=True) as session:
        record_short_first_poll(session, _local(session), short_of)

    posture = (await client.get("/api/v1/posture")).json()
    [row] = [r for r in posture["protections"] if r["key"] == "linkedin_first_poll_short"]
    assert row["name"] == "linkedin reply poll"
    assert "click Acknowledge in Settings, Posture" in row["warnings"][0]
    [manual] = [r for r in posture["protections"] if r["key"] == "manual_linkedin_sends"]
    assert (manual["name"], manual["status"]) == ("manual linkedin sends", "on")

    response = await client.post(INBOX_ACK_URL, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"cleared": True}
    with session_scope(running_app.state.session_factory) as session:
        assert short_first_poll(session, _local(session)) is None
    posture = (await client.get("/api/v1/posture")).json()
    assert all(row["key"] != "linkedin_first_poll_short" for row in posture["protections"])

    again = await client.post(INBOX_ACK_URL, headers=HEADERS)
    assert again.json() == {"cleared": False}


async def test_acknowledge_goes_through_the_csrf_guard(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(running_app.state.session_factory, write=True) as session:
        record_short_first_poll(session, _local(session), datetime(2030, 1, 2, tzinfo=UTC))

    response = await client.post(INBOX_ACK_URL)

    assert response.status_code == 403
    with session_scope(running_app.state.session_factory) as session:
        assert short_first_poll(session, _local(session)) is not None
