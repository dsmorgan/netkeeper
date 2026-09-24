"""The exports API (spec 10.6, 14.1): downloads, presets, filters, and cross-user isolation.

The generic two-user isolation harness (``tests/isolation/``) already covers this
endpoint by count (see ``registry.py``); the test here is the more pointed check
P1-11 asks for: an export run as one user must not contain another user's data
anywhere in the body, not just in the item count.
"""

from __future__ import annotations

import csv
import io
import json

import factories
import httpx
import pytest
from fastapi import FastAPI
from isolation.harness import FixedUser
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from netkeeper.crm.lists import add_members, create_list
from netkeeper.db import session_scope
from netkeeper.models import ListKind, User, UserKind

LOCAL_USER_ID = 1


def _factory(app: FastAPI) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = app.state.session_factory
    return factory


def _local_user(session: Session) -> User:
    user = session.get(User, LOCAL_USER_ID)
    assert user is not None
    return user


async def test_export_does_not_leak_a_pooled_connection(
    client: httpx.AsyncClient, running_app: FastAPI, bare_engine: Engine
) -> None:
    """``StreamingSessionDep`` must check its connection back in once the body is sent.

    Regression: swapping ``StreamingSessionDep`` for the ordinary per-request
    ``SessionDep`` left the whole suite green (nothing else exercises the pool),
    but every streamed export leaked one checked-out connection forever —
    ``Session.close()`` before the body starts sending does not stop the
    generator from opening a fresh transaction nobody is left to close. Pool
    size is 5 by default, so this would wedge the app after five exports.
    """
    pool = bare_engine.pool
    assert isinstance(pool, QueuePool)  # SQLite's default pool for a file-based engine
    with session_scope(_factory(running_app), write=True) as session:
        factories.make_contact(session, _local_user(session))
    before = pool.checkedout()
    for _ in range(3):
        response = await client.get("/api/v1/exports")
        assert response.status_code == 200
        assert response.json()  # fully drain the streamed body
    assert pool.checkedout() == before


async def test_get_needs_no_csrf_header(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    """Exports are a read (spec 14.2): no ``X-Netkeeper-Client`` header required."""
    with session_scope(_factory(running_app), write=True) as session:
        factories.make_contact(session, _local_user(session))
    response = await client.get("/api/v1/exports")
    assert response.status_code == 200


async def test_default_export_is_full_json(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        factories.make_contact(session, _local_user(session), current_company="Acme Corp")
    response = await client.get("/api/v1/exports")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert isinstance(body, list)
    assert body[0]["current_company"] == "Acme Corp"


async def test_nine_column_csv_download(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        factories.make_contact(session, _local_user(session), emails=["reach@example.test"])
    response = await client.get("/api/v1/exports?preset=nine-column&format=csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="contacts-nine-column.csv"' in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows[0]["Email Address"] == "reach@example.test"


async def test_headerless_drops_the_csv_header_row(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        factories.make_contact(session, _local_user(session))
    response = await client.get("/api/v1/exports?preset=nine-column&format=csv&headerless=true")
    assert response.status_code == 200
    assert "LinkedIn Profile URL" not in response.text.splitlines()[0]


async def test_vcard_download(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        factories.make_contact(session, _local_user(session))
    response = await client.get("/api/v1/exports?preset=full&format=vcard")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/vcard")
    assert 'filename="contacts-full.vcf"' in response.headers["content-disposition"]
    assert "BEGIN:VCARD" in response.text
    assert "END:VCARD" in response.text


async def test_invalid_filter_json_is_422(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/exports", params={"filter": "not-json"})
    assert response.status_code == 422


async def test_invalid_filter_tree_is_422(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/api/v1/exports", params={"filter": json.dumps({"where": {"op": "bogus"}})}
    )
    assert response.status_code == 422


async def test_invalid_sort_is_422(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/api/v1/exports", params={"sort": json.dumps([{"field": "not-a-field"}])}
    )
    assert response.status_code == 422


async def test_a_predicate_the_compiler_refuses_is_422_not_a_500(client: httpx.AsyncClient) -> None:
    """#95: compiling happened inside the generator, so this escaped the handler entirely.

    ``enrolled_in`` is the placeholder that is still a placeholder (campaigns,
    P3-04). ``list_member`` was the one this was found with; it compiles now.
    """
    tree = {"where": {"op": "enrolled_in", "campaign_id": 1}}
    response = await client.get("/api/v1/exports", params={"filter": json.dumps(tree)})
    assert response.status_code == 422
    assert "P3-04" in response.text


async def test_a_refused_filter_never_starts_a_body(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """The other half of #95: a 200 that stops mid-file is worse than an error.

    A ``StreamingResponse`` has sent its status line before the first chunk is
    asked for, so the refusal has to come from the compile, before the response
    exists. With contacts in the database, a lazily-compiled export would have
    emitted the opening ``[`` and nothing else, under a ``200``.
    """
    with session_scope(_factory(running_app), write=True) as session:
        for _ in range(3):
            factories.make_contact(session, _local_user(session))
    tree = {
        "where": {
            "op": "and",
            "children": [{"op": "has_li_url"}, {"op": "replied_in", "campaign_id": 1}],
        }
    }
    response = await client.get("/api/v1/exports", params={"filter": json.dumps(tree)})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in response.headers
    assert response.json()["detail"]
    assert not response.text.startswith("[")


async def test_a_static_list_exports_its_own_members(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """P1-27's "done when": the Export button on a list exports the list, not everyone."""
    with session_scope(_factory(running_app), write=True) as session:
        user = _local_user(session)
        inside = factories.make_contact(session, user, current_company="Acme Corp")
        factories.make_contact(session, user, current_company="Globex")
        row = create_list(session, user, "First 100", ListKind.STATIC)
        add_members(session, user, row.id, [inside.id])
        list_id = row.id
    tree = {"where": {"op": "list_member", "list_id": list_id}}
    response = await client.get("/api/v1/exports", params={"filter": json.dumps(tree)})
    assert response.status_code == 200
    body = response.json()
    assert [row["current_company"] for row in body] == ["Acme Corp"]


async def test_filter_query_param_is_applied(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        user = _local_user(session)
        factories.make_contact(session, user, current_company="Acme Corp")
        factories.make_contact(session, user, current_company="Globex")
    tree = {"where": {"op": "eq", "field": "current_company", "value": "Acme Corp"}}
    response = await client.get("/api/v1/exports", params={"filter": json.dumps(tree)})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["current_company"] == "Acme Corp"


@pytest.fixture
def two_users_with_distinct_contacts(running_app: FastAPI) -> tuple[int, int]:
    """User A and user B, each with one contact only they should ever see. Returns their ids."""
    with session_scope(_factory(running_app), write=True) as session:
        user_a = _local_user(session)
        user_b = factories.make_user(session, kind=UserKind.HOSTED)
        factories.make_contact(
            session,
            user_a,
            first_name="Aardvark",
            last_name="Anderson",
            emails=["aardvark.anderson@example.test"],
        )
        factories.make_contact(
            session,
            user_b,
            first_name="Bobcat",
            last_name="Baxter",
            emails=["bobcat.baxter@example.test"],
        )
        return user_a.id, user_b.id


async def _export_as(app: FastAPI, user_id: int, query: str) -> httpx.Response:
    previous = app.state.auth
    app.state.auth = FixedUser(user_id)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.get(f"/api/v1/exports?{query}")
    finally:
        app.state.auth = previous


async def test_export_run_by_one_user_never_contains_another_users_contacts(
    running_app: FastAPI, two_users_with_distinct_contacts: tuple[int, int]
) -> None:
    user_a_id, user_b_id = two_users_with_distinct_contacts

    as_b = await _export_as(running_app, user_b_id, "preset=full&format=json")
    assert as_b.status_code == 200
    assert "aardvark.anderson@example.test" not in as_b.text
    assert "Anderson" not in as_b.text
    rows = json.loads(as_b.text)
    assert len(rows) == 1
    assert rows[0]["first_name"] == "Bobcat"

    as_a = await _export_as(running_app, user_a_id, "preset=nine-column&format=csv")
    assert as_a.status_code == 200
    assert "bobcat.baxter@example.test" not in as_a.text
    assert "Baxter" not in as_a.text
    rows_csv = list(csv.DictReader(io.StringIO(as_a.text)))
    assert len(rows_csv) == 1
    assert rows_csv[0]["Email Address"] == "aardvark.anderson@example.test"
