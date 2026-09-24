"""The two-user isolation test (spec section 5, ADR 0005): one case per registered endpoint.

The registry check that fails an unregistered list endpoint is test_coverage.py, so
it keeps running while this module is skipped for an empty registry.
"""

from __future__ import annotations

from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import import_runs as import_service
from netkeeper.db import session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import User, UserKind
from netkeeper.services.linkedin_session import flag_session
from netkeeper.web.app import API_PREFIX
from netkeeper.web.security import CLIENT_HEADER, CLIENT_HEADER_VALUE

from .harness import acting_as, assert_isolated
from .registry import IMPORT_CSV, IMPORT_MAPPING, REGISTRY, ListEndpoint, seed_contacts

if not REGISTRY:
    pytest.skip("REGISTRY is empty: no list endpoints exist yet", allow_module_level=True)


@pytest.mark.parametrize("endpoint", REGISTRY, ids=[endpoint.path for endpoint in REGISTRY])
async def test_registered_list_endpoint_is_isolated(
    endpoint: ListEndpoint, running_app: FastAPI
) -> None:
    await assert_isolated(running_app, endpoint)


# --- bulk actions -----------------------------------------------------------

BULK_SELECTION: dict[str, Any] = {"filter": {"where": None}}


async def test_a_bulk_action_is_isolated(running_app: FastAPI) -> None:
    """Two users, one filter that says "everyone": neither may count or touch the other's.

    Bulk is not a list operation, so it is not in ``REGISTRY``; the risk is the
    same one and it gets the same two-user treatment. The count is the read half
    and the action is the write half, and a token minted by one user must not
    execute for the other.
    """
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        a = User(kind=UserKind.HOSTED, display_name="A")
        b = User(kind=UserKind.HOSTED, display_name="B")
        session.add_all([a, b])
        session.flush()
        seeded = {a.id: seed_contacts(session, a), b.id: seed_contacts(session, b)}
        factories.make_contact(session, b)  # B has one more, so a leak shows as a count
        seeded[b.id] += 1
        a_id, b_id = a.id, b.id

    tokens: dict[int, str] = {}
    for user_id, want in seeded.items():
        body = await _post(
            running_app,
            user_id,
            "/contacts/bulk/count",
            {
                "selection": BULK_SELECTION,
                "action": "archive",
            },
        )
        assert body["count"] == want, f"user {user_id} counted {body['count']}, not {want}"
        tokens[user_id] = body["token"]

    # A's token, presented by B, is refused before anything is counted or written.
    crossed = await _post(
        running_app,
        b_id,
        "/contacts/bulk",
        {"selection": BULK_SELECTION, "action": "archive", "token": tokens[a_id]},
        want=422,
    )
    assert crossed["reason"] == "user"

    # Each user's own action touches exactly their own rows.
    for user_id, want in seeded.items():
        applied = await _post(
            running_app,
            user_id,
            "/contacts/bulk",
            {"selection": BULK_SELECTION, "action": "archive", "token": tokens[user_id]},
        )
        assert applied == {"affected": want}


async def _post(
    app: FastAPI, user_id: int, path: str, body: dict[str, Any], *, want: int = 200
) -> dict[str, Any]:
    with acting_as(app, user_id):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.post(
                f"{API_PREFIX}{path}",
                json=body,
                headers={CLIENT_HEADER: CLIENT_HEADER_VALUE},
            )
    assert response.status_code == want, (
        f"{path} as {user_id}: {response.status_code} {response.text}"
    )
    parsed: dict[str, Any] = response.json()
    return parsed


# --- deleting a draft import run ---------------------------------------------


async def test_deleting_a_draft_import_run_is_isolated(running_app: FastAPI) -> None:
    """Delete is destructive, not a list operation, so it is not in ``REGISTRY``; it gets
    the same two-user treatment the bulk actions above get.
    """
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        a = User(kind=UserKind.HOSTED, display_name="A")
        b = User(kind=UserKind.HOSTED, display_name="B")
        session.add_all([a, b])
        session.flush()
        a_run = import_service.create_run(
            session, a, filename="a.csv", content=IMPORT_CSV, mapping=IMPORT_MAPPING
        )
        b_run = import_service.create_run(
            session, b, filename="b.csv", content=IMPORT_CSV, mapping=IMPORT_MAPPING
        )
        a_id, b_id, a_run_id, b_run_id = a.id, b.id, a_run.id, b_run.id

    # B's id does not reach A's draft: refused before anything is touched.
    await _delete(running_app, b_id, a_run_id, want=404)
    with session_scope(factory) as session:
        a_now, b_now = session.get(User, a_id), session.get(User, b_id)
        assert a_now is not None and b_now is not None
        assert import_service.list_runs(session, a_now)[1] == 1
        assert import_service.list_runs(session, b_now)[1] == 1

    # Each deletes their own.
    await _delete(running_app, a_id, a_run_id, want=204)
    await _delete(running_app, b_id, b_run_id, want=204)
    with session_scope(factory) as session:
        a_now, b_now = session.get(User, a_id), session.get(User, b_id)
        assert a_now is not None and b_now is not None
        assert import_service.list_runs(session, a_now)[1] == 0
        assert import_service.list_runs(session, b_now)[1] == 0


async def _delete(app: FastAPI, user_id: int, run_id: int, *, want: int) -> None:
    with acting_as(app, user_id):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.delete(
                f"{API_PREFIX}/imports/{run_id}", headers={CLIENT_HEADER: CLIENT_HEADER_VALUE}
            )
    assert response.status_code == want, (
        f"delete run {run_id} as {user_id}: {response.status_code} {response.text}"
    )


# --- posture -----------------------------------------------------------------


async def _get(app: FastAPI, user_id: int, path: str) -> dict[str, Any]:
    with acting_as(app, user_id):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.get(f"{API_PREFIX}{path}")
    assert response.status_code == 200, (
        f"{path} as {user_id}: {response.status_code} {response.text}"
    )
    parsed: dict[str, Any] = response.json()
    return parsed


async def test_posture_is_isolated(running_app: FastAPI) -> None:
    """Not a list operation (a single report), so ``GET /posture`` is not in
    ``REGISTRY`` either; it reads a user's own session flag, heat, and budget
    counters, so it gets the same two-user treatment as the rows above.
    """
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        a = User(kind=UserKind.HOSTED, display_name="A")
        b = User(kind=UserKind.HOSTED, display_name="B")
        session.add_all([a, b])
        session.flush()
        flag_session(session, a, Outcome.CHECKPOINT, url="https://example.invalid/checkpoint/x")
        a_id, b_id = a.id, b.id

    a_report = await _get(running_app, a_id, "/posture")
    b_report = await _get(running_app, b_id, "/posture")

    a_flag = next(row for row in a_report["protections"] if row["name"] == "session flag")
    b_flag = next(row for row in b_report["protections"] if row["name"] == "session flag")
    # A's checkpoint warns on A's own report and never reaches B's.
    assert a_flag["warnings"] != []
    assert b_flag["warnings"] == []
    assert a_report["ok"] is False
    # B's report is not "not clear" for a reason that traces back to A's flag —
    # the one unavoidable warning here (no browser probe, spec 9.1) is the
    # only thing keeping it False, not anything of A's.
    assert all("checkpoint" not in warning for warning in b_report["warnings"])
