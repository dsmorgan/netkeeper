"""``GET /posture``: the same report `netkeeper posture` prints, as data (P2-12).

Read-only, no browser probe (CLAUDE.md: never await browser work in a request
handler) -- the session protection here comes from recorded evidence (#282), never
checked live. The two-user isolation of this per-user report is in
``tests/isolation/test_isolation.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import User, UserKind
from netkeeper.models.base import utcnow
from netkeeper.services import posture as posture_service
from netkeeper.services.linkedin_accounts import account_id_for
from netkeeper.services.linkedin_session import flag_session

NOW = datetime.now(UTC)


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


def _factory(app: FastAPI):  # type: ignore[no-untyped-def]
    return app.state.session_factory


async def test_the_shape_is_a_full_report(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    report = (await client.get("/api/v1/posture")).json()

    assert "you are safe" not in report["verdict"].lower()
    assert len(report["protections"]) > 0
    assert all({"name", "status", "value", "warnings"} <= set(row) for row in report["protections"])
    assert report["timezone"]
    assert report["checked_at"]
    assert report["local_time"]


async def test_with_no_evidence_the_browser_session_is_unknown_never_assumed_healthy(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """This endpoint never attaches (CLAUDE.md: no browser work in a request
    handler), so it always calls `posture()` with `probe=None`. The
    linkedin-session row answers from recorded evidence (#282,
    tests/test_session_evidence.py); with none -- no preflight, no run that read
    LinkedIn -- it is unknown, and `ok` cannot read true: this report cannot
    see what nothing ever checked."""
    report = (await client.get("/api/v1/posture")).json()

    session_row = next(row for row in report["protections"] if row["name"] == "linkedin session")
    assert session_row["status"] == "unknown"
    assert session_row["value"] == "not checked yet"
    assert session_row["warnings"] != []
    assert "netkeeper preflight" in session_row["warnings"][0]
    assert report["ok"] is False
    assert report["verdict"].startswith("NOT clear")


async def test_a_flagged_session_warns_and_the_verdict_says_not_clear(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        user = _local(session)
        flag_session(session, user, Outcome.CHECKPOINT, url="https://example.invalid/checkpoint/x")

    report = (await client.get("/api/v1/posture")).json()

    assert report["ok"] is False
    assert report["warnings"] != []
    assert report["verdict"].startswith("NOT clear")
    # "on" here, not "off": a raised flag is the protection catching a
    # checkpoint, not the protection failing (Status.ON's own docstring).
    session_row = next(row for row in report["protections"] if row["name"] == "session flag")
    assert session_row["status"] == "on"
    assert session_row["warnings"] != []


async def test_gaps_are_not_warnings(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    report = (await client.get("/api/v1/posture")).json()

    assert isinstance(report["gaps"], list)
    assert all(isinstance(gap, str) for gap in report["gaps"])
    # A gap alone never makes a clean report unclear.
    if report["gaps"] and not report["warnings"]:
        assert report["ok"] is True


async def test_the_verdict_is_the_services_own_sentence(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """`verdict` must be `posture_service.verdict()`'s exact text, not a
    paraphrase built here — the wording ("nothing is misconfigured", never
    "you are safe") is the whole point of that function existing."""
    report = (await client.get("/api/v1/posture")).json()

    with session_scope(_factory(running_app)) as session:
        user = _local(session)
        account_id = account_id_for(session, user)
        expected = posture_service.posture(
            session, user, account_id, now=utcnow(), settings=Settings(), probe=None
        )
    assert report["verdict"] == posture_service.verdict(expected)


async def test_each_row_carries_its_summary_beside_its_full_value(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """#340: the summary is extra, for the collapsed view. ``value`` keeps the full
    detail, so the long rows' summaries are shorter than their values."""
    report = (await client.get("/api/v1/posture")).json()
    rows = {row["name"]: row for row in report["protections"]}

    assert all(row["summary"] for row in rows.values())
    budget = rows["budget profile_visits"]
    assert "hard max" in budget["value"] and "hard max" not in budget["summary"]
    assert rows["reply poll"]["summary"] == "no armed mailbox"
    assert rows["linkedin session"]["summary"] == rows["linkedin session"]["value"]
