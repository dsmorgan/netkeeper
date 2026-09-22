"""The lists and saved views API (spec 10.1, 10.4; item P1-08): CRUD, membership, CSRF."""

from __future__ import annotations

from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm.filters import MAX_LIST_EXPANSIONS
from netkeeper.db import session_scope
from netkeeper.models import ContactList, ContactMet, User
from netkeeper.scoping import get_scoped

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


# --- a write may not take the lists page down (#126 review) -----------------


async def _create_list(client: httpx.AsyncClient, name: str, **body: Any) -> httpx.Response:
    return await client.post("/api/v1/lists", json={"name": name, **body}, headers=CSRF)


def _smart(where: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "smart", "filter": {"where": where}}


async def test_a_filter_may_not_name_a_list_that_does_not_exist_yet(
    client: httpx.AsyncClient,
) -> None:
    """The two-POST cycle from the review, route one.

    Ids are handed out in order, so a filter naming the id it is *about* to be
    given used to be accepted — a list that matches nobody at validation time
    and itself a moment later. The second POST then closed the ring and every
    page that counts a list answered 422.
    """
    before = await _lists_by_name(client)
    taken = max(row["id"] for row in before.values())

    first = await _create_list(client, "A", **_smart({"op": "list_member", "list_id": taken + 2}))
    assert first.status_code == 422, first.text
    assert f"there is no list {taken + 2}" in first.text

    # ...and nothing was stored: the page is intact and A is not on it.
    after = await _lists_by_name(client)
    assert "A" not in after
    assert set(after) == set(before)


async def test_a_list_may_not_name_the_id_a_deleted_one_left_behind(
    client: httpx.AsyncClient,
) -> None:
    """The one-POST self-cycle: delete a list to free its id, then name that id.

    SQLite hands the id of the highest deleted row straight back, so the new
    list was created *as* the list its own filter named.
    """
    made = await _create_list(client, "Temporary", kind="static")
    assert made.status_code == 201
    freed = made.json()["id"]
    assert (await client.delete(f"/api/v1/lists/{freed}", headers=CSRF)).status_code == 204

    refused = await _create_list(
        client, "Ouroboros", **_smart({"op": "list_member", "list_id": freed})
    )
    assert refused.status_code == 422, refused.text
    assert f"there is no list {freed}" in refused.text
    assert (await client.get("/api/v1/lists")).status_code == 200


async def test_a_new_list_may_not_take_over_a_dangling_reference_and_close_a_cycle(
    client: httpx.AsyncClient,
) -> None:
    """The same route with a real dangling reference, and no id prediction at all.

    A filter that named a list when it was written outlives that list, and
    SQLite hands the id of the highest deleted row to the next insert. So the
    new list can *become* the list an existing filter names — and if it names
    that filter's list back, the ring closes with ordinary requests. Only
    walking up from the write catches this one.
    """
    holder = await _create_list(client, "Holder", **_smart({"op": "has_email"}))
    holder_id = holder.json()["id"]
    target = await _create_list(client, "Target", kind="static")
    target_id = target.json()["id"]
    assert target_id > holder_id, "Target has to be the highest row for its id to come back"
    patched = await client.patch(
        f"/api/v1/lists/{holder_id}",
        json={"filter": {"where": {"op": "list_member", "list_id": target_id}}},
        headers=CSRF,
    )
    assert patched.status_code == 200, patched.text
    assert (await client.delete(f"/api/v1/lists/{target_id}", headers=CSRF)).status_code == 204

    # Holder's reference now dangles and matches nobody; the page still works.
    lists = await _lists_by_name(client)
    assert lists["Holder"]["member_count"] == 0
    assert lists["Holder"]["broken"] is None

    # A new list naming Holder, handed Target's id back, would close Holder -> it -> Holder.
    closing = await _create_list(
        client, "Closing", **_smart({"op": "list_member", "list_id": holder_id})
    )
    assert closing.status_code == 422, closing.text
    assert "would leave list" in closing.text and "defined in terms of itself" in closing.text
    page = await client.get("/api/v1/lists")
    assert page.status_code == 200
    assert "Closing" not in {row["name"] for row in page.json()}


async def test_an_edit_below_the_expansion_cap_may_not_break_a_list_above_it(
    client: httpx.AsyncClient,
) -> None:
    """The PATCH route from the review: a save that costs one inline, and breaks a list at 32.

    The guard that looks down from the tree being written sees a single
    ``list_member``. Only walking up — recompiling the user's lists after the
    write — sees that the list naming the top of the chain now pulls in 33.
    """
    previous: int | None = None
    for index in range(MAX_LIST_EXPANSIONS):
        where = None if previous is None else {"op": "list_member", "list_id": previous}
        body = {"kind": "smart", "filter": {"where": where}}
        made = await _create_list(client, f"C{index}", **body)
        assert made.status_code == 201, made.text
        previous = made.json()["id"]
    assert previous is not None
    dependent = await _create_list(
        client, "Dependent", **_smart({"op": "list_member", "list_id": previous})
    )
    assert dependent.status_code == 201, "a filter at the cap is allowed"
    assert (await client.get("/api/v1/lists")).status_code == 200

    # Smart, because only a smart list is inlined: a static one is an EXISTS and
    # costs the cap nothing.
    leaf = await _create_list(client, "Leaf", **_smart({"op": "has_email"}))
    leaf_id = leaf.json()["id"]
    bottom = (await _lists_by_name(client))["C0"]["id"]
    patched = await client.patch(
        f"/api/v1/lists/{bottom}",
        json={"filter": {"where": {"op": "list_member", "list_id": leaf_id}}},
        headers=CSRF,
    )
    assert patched.status_code == 422, patched.text
    assert "would leave list" in patched.text and "more than" in patched.text

    page = await client.get("/api/v1/lists")
    assert page.status_code == 200
    assert all(row["broken"] is None for row in page.json())


async def test_one_list_that_cannot_compile_does_not_hide_the_others(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """The containment, reached the only way left: by writing the row behind the API's back.

    Nothing the API offers can produce this state any more. If something else
    does, the page that lists every list still lists them — with the one that
    cannot be counted saying so — because that is the page someone would use to
    delete it.
    """
    good = await _create_list(client, "Good", kind="static")
    assert good.status_code == 201
    bad = await _create_list(client, "Self", **_smart({"op": "has_email"}))
    bad_id = bad.json()["id"]
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User)).one()
        row = get_scoped(session, user, ContactList, bad_id)
        assert row is not None
        row.filter_json = {"where": {"op": "list_member", "list_id": bad_id}}

    page = await client.get("/api/v1/lists")
    assert page.status_code == 200
    rows = {row["name"]: row for row in page.json()}
    assert set(rows) == {"Validated", "Good", "Self"}
    assert rows["Good"]["broken"] is None
    assert rows["Self"]["broken"] is not None
    assert "defined in terms of itself" in rows["Self"]["broken"]
    assert rows["Self"]["member_count"] == 0

    # Asking about that one list still says what is wrong, and it can be deleted.
    members = await client.get(f"/api/v1/lists/{bad_id}/members")
    assert members.status_code == 422
    assert (await client.delete(f"/api/v1/lists/{bad_id}", headers=CSRF)).status_code == 204
    assert (await client.get("/api/v1/lists")).status_code == 200
