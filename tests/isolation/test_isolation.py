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

from netkeeper.db import session_scope
from netkeeper.models import User, UserKind
from netkeeper.web.app import API_PREFIX
from netkeeper.web.security import CLIENT_HEADER, CLIENT_HEADER_VALUE

from .harness import acting_as, assert_isolated
from .registry import REGISTRY, ListEndpoint, seed_contacts

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
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
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
