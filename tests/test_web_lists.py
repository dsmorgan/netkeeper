"""The lists and saved views API (spec 10.1, 10.4; item P1-08): CRUD, membership, CSRF."""

from __future__ import annotations

from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.models import ContactMet, User

CSRF = {"X-Netkeeper-Client": "1"}


@pytest.fixture
def contact_ids(running_app: FastAPI) -> dict[str, int]:
    """Three contacts of the local user: two met, one not."""
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User)).one()
        met_a = factories.make_contact(session, user, met=ContactMet.MET)
        met_b = factories.make_contact(session, user, met=ContactMet.MET)
        not_met = factories.make_contact(session, user, met=ContactMet.NOT_MET)
        return {"met_a": met_a.id, "met_b": met_b.id, "not_met": not_met.id}


async def _lists_by_name(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    response = await client.get("/api/v1/lists")
    assert response.status_code == 200
    return {row["name"]: row for row in response.json()}


async def test_validated_list_is_seeded_at_startup(
    client: httpx.AsyncClient, contact_ids: dict[str, int]
) -> None:
    lists = await _lists_by_name(client)
    assert "Validated" in lists
    validated = lists["Validated"]
    assert validated["kind"] == "smart"
    assert validated["filter"] == {
        "where": {"op": "eq", "field": "met", "value": "met"},
        "include_archived": False,
    }
    assert validated["member_count"] == 2  # met_a, met_b

    members = await client.get(f"/api/v1/lists/{validated['id']}/members")
    assert members.status_code == 200
    ids = {item["id"] for item in members.json()["items"]}
    assert ids == {contact_ids["met_a"], contact_ids["met_b"]}


async def test_state_changing_routes_need_the_csrf_header(client: httpx.AsyncClient) -> None:
    for method, path in [
        ("POST", "/api/v1/lists"),
        ("PATCH", "/api/v1/lists/1"),
        ("DELETE", "/api/v1/lists/1"),
        ("POST", "/api/v1/lists/1/members"),
        ("DELETE", "/api/v1/lists/1/members/1"),
        ("POST", "/api/v1/views"),
        ("PATCH", "/api/v1/views/1"),
        ("DELETE", "/api/v1/views/1"),
    ]:
        response = await client.request(method, path, json={})
        assert response.status_code == 403, (method, path)


async def test_list_crud(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/api/v1/lists", json={"name": "First 100", "kind": "static"}, headers=CSRF
    )
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["kind"] == "static" and row["filter"] is None and row["member_count"] == 0

    dup = await client.post(
        "/api/v1/lists", json={"name": "First 100", "kind": "static"}, headers=CSRF
    )
    assert dup.status_code == 409

    bad_kind = await client.post("/api/v1/lists", json={"name": "x", "kind": "bogus"}, headers=CSRF)
    assert bad_kind.status_code == 422

    static_with_filter = await client.post(
        "/api/v1/lists",
        json={"name": "y", "kind": "static", "filter": {"where": {"op": "has_email"}}},
        headers=CSRF,
    )
    assert static_with_filter.status_code == 422

    smart_without_filter = await client.post(
        "/api/v1/lists", json={"name": "z", "kind": "smart"}, headers=CSRF
    )
    assert smart_without_filter.status_code == 422

    url = f"/api/v1/lists/{row['id']}"
    renamed = await client.patch(url, json={"name": "First 200"}, headers=CSRF)
    assert renamed.status_code == 200 and renamed.json()["name"] == "First 200"

    assert (await client.delete(url, headers=CSRF)).status_code == 204
    assert (await client.delete(url, headers=CSRF)).status_code == 404
    assert (await client.patch(url, json={"name": "x"}, headers=CSRF)).status_code == 404


async def test_smart_list_filter_is_validated_and_replaceable(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/api/v1/lists",
        json={"name": "Met", "kind": "smart", "filter": {"where": {"op": "has_email"}}},
        headers=CSRF,
    )
    assert created.status_code == 201, created.text
    row = created.json()
    url = f"/api/v1/lists/{row['id']}"

    unsupported = await client.patch(
        url, json={"filter": {"where": {"op": "enrolled_in", "campaign_id": 1}}}, headers=CSRF
    )
    assert unsupported.status_code == 422

    replaced = await client.patch(
        url, json={"filter": {"where": {"op": "has_phone"}}}, headers=CSRF
    )
    assert replaced.status_code == 200
    assert replaced.json()["filter"]["where"]["op"] == "has_phone"

    left_alone = await client.patch(url, json={"name": "Met (renamed)"}, headers=CSRF)
    assert left_alone.status_code == 200
    assert left_alone.json()["filter"]["where"]["op"] == "has_phone"


async def test_static_list_membership(
    client: httpx.AsyncClient, contact_ids: dict[str, int]
) -> None:
    created = await client.post(
        "/api/v1/lists", json={"name": "First 100", "kind": "static"}, headers=CSRF
    )
    list_id = created.json()["id"]

    empty = await client.get(f"/api/v1/lists/{list_id}/members")
    assert empty.status_code == 200 and empty.json() == {"items": [], "total": 0}

    added = await client.post(
        f"/api/v1/lists/{list_id}/members",
        json={"contact_ids": [contact_ids["met_a"], contact_ids["not_met"], contact_ids["met_a"]]},
        headers=CSRF,
    )
    assert added.status_code == 201 and added.json() == {"added": 2}

    added_again = await client.post(
        f"/api/v1/lists/{list_id}/members",
        json={"contact_ids": [contact_ids["met_a"]]},
        headers=CSRF,
    )
    assert added_again.json() == {"added": 0}

    members = await client.get(f"/api/v1/lists/{list_id}/members")
    ids = {item["id"] for item in members.json()["items"]}
    assert ids == {contact_ids["met_a"], contact_ids["not_met"]}
    assert (await _lists_by_name(client))["First 100"]["member_count"] == 2

    missing = await client.post(
        f"/api/v1/lists/{list_id}/members", json={"contact_ids": [999999]}, headers=CSRF
    )
    assert missing.status_code == 404

    empty_body = await client.post(
        f"/api/v1/lists/{list_id}/members", json={"contact_ids": []}, headers=CSRF
    )
    assert empty_body.status_code == 422  # min_length=1

    removed = await client.delete(
        f"/api/v1/lists/{list_id}/members/{contact_ids['met_a']}", headers=CSRF
    )
    assert removed.status_code == 204
    removed_again = await client.delete(
        f"/api/v1/lists/{list_id}/members/{contact_ids['met_a']}", headers=CSRF
    )
    assert removed_again.status_code == 404


async def test_membership_endpoints_refuse_a_smart_list(
    client: httpx.AsyncClient, contact_ids: dict[str, int]
) -> None:
    created = await client.post(
        "/api/v1/lists",
        json={"name": "Smart", "kind": "smart", "filter": {"where": {"op": "has_email"}}},
        headers=CSRF,
    )
    list_id = created.json()["id"]
    add = await client.post(
        f"/api/v1/lists/{list_id}/members",
        json={"contact_ids": [contact_ids["met_a"]]},
        headers=CSRF,
    )
    assert add.status_code == 422
    remove = await client.delete(
        f"/api/v1/lists/{list_id}/members/{contact_ids['met_a']}", headers=CSRF
    )
    assert remove.status_code == 422


async def test_smart_list_members_equal_a_direct_filter_run(
    client: httpx.AsyncClient, contact_ids: dict[str, int]
) -> None:
    """The API-level version of P1-08's "done when": what the list returns must match what the
    filter alone would find, contact for contact."""
    created = await client.post(
        "/api/v1/lists",
        json={
            "name": "Met",
            "kind": "smart",
            "filter": {"where": {"op": "eq", "field": "met", "value": "met"}},
        },
        headers=CSRF,
    )
    list_id = created.json()["id"]
    members = await client.get(f"/api/v1/lists/{list_id}/members")
    assert members.status_code == 200
    ids = {item["id"] for item in members.json()["items"]}
    assert ids == {contact_ids["met_a"], contact_ids["met_b"]}
    assert members.json()["total"] == 2


async def test_view_crud(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/api/v1/views",
        json={"name": "Default", "columns": ["first_name", "last_name"]},
        headers=CSRF,
    )
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["columns"] == ["first_name", "last_name"]
    assert row["sort"] == [] and row["filter"] is None

    dup = await client.post(
        "/api/v1/views", json={"name": "Default", "columns": ["first_name"]}, headers=CSRF
    )
    assert dup.status_code == 409

    empty_columns = await client.post(
        "/api/v1/views", json={"name": "x", "columns": []}, headers=CSRF
    )
    assert empty_columns.status_code == 422

    url = f"/api/v1/views/{row['id']}"
    with_filter = await client.patch(
        url, json={"filter": {"where": {"op": "has_email"}}}, headers=CSRF
    )
    assert with_filter.status_code == 200
    assert with_filter.json()["filter"]["where"]["op"] == "has_email"

    cleared = await client.patch(url, json={"filter": None}, headers=CSRF)
    assert cleared.status_code == 200 and cleared.json()["filter"] is None

    left_alone = await client.patch(url, json={"name": "Default 2"}, headers=CSRF)
    assert left_alone.status_code == 200 and left_alone.json()["filter"] is None

    with_sort = await client.patch(
        url, json={"sort": [{"field": "last_name", "direction": "desc"}]}, headers=CSRF
    )
    assert with_sort.status_code == 200
    assert with_sort.json()["sort"] == [{"field": "last_name", "direction": "desc"}]

    assert (await client.delete(url, headers=CSRF)).status_code == 204
    assert (await client.delete(url, headers=CSRF)).status_code == 404
    listed = await client.get("/api/v1/views")
    assert listed.json() == []
