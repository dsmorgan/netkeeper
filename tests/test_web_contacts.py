"""The contacts API (spec 10.1, 14.1; item P1-05).

The list with its filter, sort, page and columns; one contact with children and
timeline; edits that stick and revert; bulk actions behind a count confirmation
token; archive. Plus the cross-user attack: user B must reach nothing of user
A's through any of it.

Every name, address and number here is invented.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from isolation.harness import acting_as
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm.interactions import add_interaction
from netkeeper.crm.provenance import record_synced_value
from netkeeper.db import session_scope
from netkeeper.models import ContactSnapshot, ContactSource, InteractionKind, User, UserKind
from netkeeper.models.contacts import Contact
from netkeeper.scoping import get_scoped

CSRF = {"X-Netkeeper-Client": "1"}
AT = datetime(2026, 2, 1, 9, 30, tzinfo=UTC)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def factory(running_app: FastAPI) -> sessionmaker[Session]:
    factories_: sessionmaker[Session] = running_app.state.session_factory
    return factories_


@pytest.fixture
def owner(factory: sessionmaker[Session]) -> User:
    """The local user the API acts as: the first one, as ``LocalSingleUser`` resolves it."""
    with session_scope(factory) as session:
        user = session.scalars(
            select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)
        ).first()
        assert user is not None
        return user


@pytest.fixture
def people(factory: sessionmaker[Session], owner: User) -> list[int]:
    """Three of the owner's contacts: an engineer, a designer, and an archived founder."""
    with session_scope(factory, write=True) as session:
        user = session.get(User, owner.id)
        assert user is not None
        ada = factories.make_contact(
            session,
            user,
            first_name="Ada",
            last_name="Quill",
            current_title="Staff Engineer",
            current_company="Blueleaf",
            headline="Staff Engineer at Blueleaf",
            location="Portland",
            connected_on=date(2024, 5, 1),
            emails=["ada.quill@example.test"],
            phones=["+15550001111"],
        )
        bo = factories.make_contact(
            session,
            user,
            first_name="Bo",
            last_name="Marsh",
            current_title="Design Lead",
            current_company="Tinwork",
            headline="Design Lead at Tinwork",
            location="Denver",
            connected_on=date(2023, 1, 9),
            emails=["bo.marsh@example.test"],
        )
        cy = factories.make_contact(
            session,
            user,
            first_name="Cy",
            last_name="Odell",
            current_title="Founder",
            current_company="Roan Labs",
            headline="Founder at Roan Labs",
            location="Portland",
            archived_at=AT,
        )
        return [ada.id, bo.id, cy.id]


async def _query(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/contacts/query", json=body, headers=CSRF)
    assert response.status_code == 200, response.text
    page: dict[str, Any] = response.json()
    return page


def _names(page: dict[str, Any]) -> list[str]:
    return [f"{row['first_name']} {row['last_name']}" for row in page["items"]]


# --- list: filter, sort, page, columns --------------------------------------


async def test_an_empty_query_lists_every_live_contact(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    page = await _query(client)
    assert page["total"] == 2  # the archived founder is out
    assert sorted(_names(page)) == ["Ada Quill", "Bo Marsh"]
    assert page["describe"]


async def test_include_archived_brings_the_archived_one_back(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    page = await _query(client, filter={"include_archived": True})
    assert page["total"] == 3
    assert "Cy Odell" in _names(page)


async def test_a_filter_tree_selects_and_describes(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    page = await _query(
        client,
        filter={
            "where": {
                "op": "and",
                "children": [
                    {"op": "eq", "field": "location", "value": "portland"},
                    {"op": "contains", "field": "current_title", "value": "engineer"},
                ],
            }
        },
    )
    assert _names(page) == ["Ada Quill"]
    assert "portland" in page["describe"].lower()


async def test_sort_and_pagination_walk_the_whole_list(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    sort = [{"field": "last_name", "direction": "desc"}]
    first = await _query(client, sort=sort, limit=1)
    second = await _query(client, sort=sort, limit=1, offset=1)
    assert _names(first) == ["Ada Quill"]  # Quill before Marsh, descending
    assert _names(second) == ["Bo Marsh"]
    assert first["total"] == second["total"] == 2


async def test_columns_pick_what_a_row_carries(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    page = await _query(client, columns=["last_name", "current_company"], limit=1)
    row = page["items"][0]
    assert set(row) == {"id", "last_name", "current_company", "primary_email", "primary_phone"}
    assert row["primary_email"]


async def test_a_row_carries_its_primary_email_and_phone(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    page = await _query(client, sort=[{"field": "last_name"}])
    by_name = {row["last_name"]: row for row in page["items"]}
    assert by_name["Quill"]["primary_email"] == "ada.quill@example.test"
    assert by_name["Quill"]["primary_phone"] == "+15550001111"
    assert by_name["Marsh"]["primary_phone"] is None


async def test_an_unknown_field_is_a_readable_422(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/contacts/query",
        json={"filter": {"where": {"op": "eq", "field": "favourite_colour", "value": "teal"}}},
        headers=CSRF,
    )
    assert response.status_code == 422
    assert "favourite_colour" in response.text


async def test_a_predicate_that_is_not_built_yet_says_which_item_delivers_it(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/contacts/query",
        json={"filter": {"where": {"op": "list_member", "list_id": 1}}},
        headers=CSRF,
    )
    assert response.status_code == 422
    assert "P1-08" in response.text


async def test_the_search_lists_by_substring(client: httpx.AsyncClient, people: list[int]) -> None:
    response = await client.get("/api/v1/contacts", params={"q": "blueleaf"})
    assert response.status_code == 200
    assert _names(response.json()) == ["Ada Quill"]

    by_email = await client.get("/api/v1/contacts", params={"q": "bo.marsh@"})
    assert _names(by_email.json()) == ["Bo Marsh"]


# --- get: children and timeline ---------------------------------------------


async def test_a_contact_comes_back_with_children_and_timeline(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User, people: list[int]
) -> None:
    with session_scope(factory, write=True) as session:
        user = session.get(User, owner.id)
        assert user is not None
        add_interaction(session, user, people[0], InteractionKind.EMAIL_OUT, AT, "sent a note")
        session.add(
            ContactSnapshot(
                user_id=user.id,
                contact_id=people[0],
                observed_at=datetime(2026, 1, 15, tzinfo=UTC),
                headline="Senior Engineer at Blueleaf",
                source=ContactSource.SYNC,
            )
        )

    response = await client.get(f"/api/v1/contacts/{people[0]}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["first_name"] == "Ada"
    assert [row["email"] for row in body["emails"]] == ["ada.quill@example.test"]
    assert [row["number_e164"] for row in body["phones"]] == ["+15550001111"]
    assert body["links"] == []
    assert isinstance(body["positions"], list)
    kinds = [entry["kind"] for entry in body["timeline"]]
    assert kinds == ["interaction", "snapshot"], body["timeline"]
    assert body["timeline"][0]["interaction"]["summary"] == "sent a note"
    assert body["timeline"][1]["snapshot"]["headline"] == "Senior Engineer at Blueleaf"


async def test_an_unknown_contact_is_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/contacts/424242")).status_code == 404


# --- patch and revert -------------------------------------------------------


async def test_an_edit_sticks_and_reverts_to_the_synced_value(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User, people: list[int]
) -> None:
    with session_scope(factory, write=True) as session:
        user = session.get(User, owner.id)
        assert user is not None
        contact = get_scoped(session, user, Contact, people[0])
        assert contact is not None
        record_synced_value(
            contact,
            "current_title",
            "Staff Engineer",
            source=ContactSource.SYNC,
            observed_at=AT,
        )

    edited = await client.patch(
        f"/api/v1/contacts/{people[0]}", json={"current_title": "Principal Engineer"}, headers=CSRF
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["current_title"] == "Principal Engineer"
    assert body["field_sources"]["current_title"] == "manual"
    assert body["overridden_fields"] == ["current_title"]
    assert body["synced_values"]["current_title"]["value"] == "Staff Engineer"

    # It sticks across a read, which is the whole point of the provenance rule.
    again = await client.get(f"/api/v1/contacts/{people[0]}")
    assert again.json()["current_title"] == "Principal Engineer"

    reverted = await client.post(
        f"/api/v1/contacts/{people[0]}/revert-field",
        json={"field": "current_title"},
        headers=CSRF,
    )
    assert reverted.status_code == 200, reverted.text
    back = reverted.json()
    assert back["current_title"] == "Staff Engineer"
    assert back["field_sources"]["current_title"] == "sync"
    assert back["overridden_fields"] == []


async def test_reverting_a_field_that_was_never_synced_is_a_readable_409(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    response = await client.post(
        f"/api/v1/contacts/{people[0]}/revert-field", json={"field": "location"}, headers=CSRF
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "location has no synced value to revert to"


async def test_reverting_a_field_that_carries_no_provenance_is_refused_by_the_schema(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    response = await client.post(
        f"/api/v1/contacts/{people[0]}/revert-field", json={"field": "notes"}, headers=CSRF
    )
    assert response.status_code == 422


async def test_clearing_a_field_by_hand_sticks_too(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    response = await client.patch(
        f"/api/v1/contacts/{people[0]}", json={"headline": None}, headers=CSRF
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["headline"] is None
    assert body["field_sources"]["headline"] == "manual"


async def test_a_slug_another_contact_holds_is_a_409(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User, people: list[int]
) -> None:
    with session_scope(factory) as session:
        user = session.get(User, owner.id)
        assert user is not None
        other = get_scoped(session, user, Contact, people[1])
        assert other is not None
        held = other.li_public_id
    response = await client.patch(
        f"/api/v1/contacts/{people[0]}", json={"li_public_id": held}, headers=CSRF
    )
    assert response.status_code == 409
    assert str(people[1]) in response.json()["detail"]


async def test_a_field_that_is_not_editable_is_refused(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    response = await client.patch(
        f"/api/v1/contacts/{people[0]}", json={"li_urn": "urn:li:fsd_profile/NOPE"}, headers=CSRF
    )
    assert response.status_code == 422


# --- archive ----------------------------------------------------------------


async def test_archive_is_soft_and_reversible(client: httpx.AsyncClient, people: list[int]) -> None:
    archived = await client.post(f"/api/v1/contacts/{people[0]}/archive", headers=CSRF)
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None

    assert "Ada Quill" not in _names(await _query(client))
    assert (await client.get(f"/api/v1/contacts/{people[0]}")).status_code == 200  # still there

    back = await client.post(f"/api/v1/contacts/{people[0]}/unarchive", headers=CSRF)
    assert back.json()["archived_at"] is None
    assert "Ada Quill" in _names(await _query(client))


# --- bulk with a count confirmation token -----------------------------------


async def _count(client: httpx.AsyncClient, selection: Any, action: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/contacts/bulk/count",
        json={"selection": selection, "action": action},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


EVERYONE: dict[str, Any] = {"filter": {"where": None}}


async def test_a_bulk_action_needs_the_token_from_the_count(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    counted = await _count(client, EVERYONE, "archive")
    assert counted["count"] == 2
    assert counted["describe"]
    assert counted["expires_at"]

    applied = await client.post(
        "/api/v1/contacts/bulk",
        json={"selection": EVERYONE, "action": "archive", "token": counted["token"]},
        headers=CSRF,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json() == {"affected": 2}
    assert (await _query(client))["total"] == 0


async def test_a_token_whose_selection_has_moved_is_refused(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    counted = await _count(client, EVERYONE, "archive")
    assert counted["count"] == 2

    # A third contact appears between the dialog and the click.
    await client.post(f"/api/v1/contacts/{people[2]}/unarchive", headers=CSRF)

    applied = await client.post(
        "/api/v1/contacts/bulk",
        json={"selection": EVERYONE, "action": "archive", "token": counted["token"]},
        headers=CSRF,
    )
    assert applied.status_code == 409, applied.text
    assert applied.json() == {"detail": "count mismatch", "expected_count": 2, "actual_count": 3}
    # Nothing was applied: the two that did match are still live.
    assert (await _query(client))["total"] == 3


async def test_a_token_cannot_execute_a_different_selection(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    counted = await _count(client, EVERYONE, "archive")
    others = {"filter": {"where": {"op": "eq", "field": "location", "value": "Denver"}}}
    response = await client.post(
        "/api/v1/contacts/bulk",
        json={"selection": others, "action": "archive", "token": counted["token"]},
        headers=CSRF,
    )
    assert response.status_code == 422, response.text
    assert response.json()["reason"] == "selection"


async def test_a_token_cannot_execute_a_different_action(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    counted = await _count(client, EVERYONE, "archive")
    response = await client.post(
        "/api/v1/contacts/bulk",
        json={
            "selection": EVERYONE,
            "action": "set_do_not_contact",
            "value": True,
            "token": counted["token"],
        },
        headers=CSRF,
    )
    assert response.status_code == 422
    assert response.json()["reason"] == "action"


async def test_an_expired_token_asks_for_the_count_again(
    client: httpx.AsyncClient, running_app: FastAPI, people: list[int]
) -> None:
    from datetime import timedelta

    from netkeeper.crm.confirmation import Signer

    running_app.state.confirmations = Signer(key=b"short-lived" * 4, ttl=timedelta(seconds=-1))
    counted = await _count(client, EVERYONE, "archive")
    response = await client.post(
        "/api/v1/contacts/bulk",
        json={"selection": EVERYONE, "action": "archive", "token": counted["token"]},
        headers=CSRF,
    )
    assert response.status_code == 409
    assert response.json()["reason"] == "expired"


async def test_a_forged_token_is_refused(client: httpx.AsyncClient, people: list[int]) -> None:
    response = await client.post(
        "/api/v1/contacts/bulk",
        json={"selection": EVERYONE, "action": "archive", "token": "not-a-token"},
        headers=CSRF,
    )
    assert response.status_code == 422
    assert response.json()["reason"] == "malformed"
    assert (await _query(client))["total"] == 2  # nothing happened


async def test_a_bulk_action_on_ids_ignores_the_order_they_are_sent_in(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    counted = await _count(client, {"ids": [people[0], people[1]]}, "set_met")
    assert counted["count"] == 2
    response = await client.post(
        "/api/v1/contacts/bulk",
        json={
            "selection": {"ids": [people[1], people[0]]},
            "action": "set_met",
            "value": "met",
            "token": counted["token"],
        },
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"affected": 2}
    page = await _query(client, columns=["met"])
    assert {row["met"] for row in page["items"]} == {"met"}


async def test_a_bulk_action_whose_value_does_not_fit_is_refused(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/contacts/bulk/count",
        json={"selection": EVERYONE, "action": "set_met"},
        headers=CSRF,
    )
    assert response.status_code == 200
    bad = await client.post(
        "/api/v1/contacts/bulk",
        json={"selection": EVERYONE, "action": "set_met", "token": response.json()["token"]},
        headers=CSRF,
    )
    assert bad.status_code == 422


async def test_a_selection_must_be_a_filter_or_ids_not_both(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/contacts/bulk/count",
        json={"selection": {"filter": {"where": None}, "ids": [1]}, "action": "archive"},
        headers=CSRF,
    )
    assert response.status_code == 422


# --- CSRF -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/contacts/query"),
        ("POST", "/api/v1/contacts/bulk"),
        ("POST", "/api/v1/contacts/bulk/count"),
        ("PATCH", "/api/v1/contacts/1"),
        ("POST", "/api/v1/contacts/1/archive"),
        ("POST", "/api/v1/contacts/1/revert-field"),
    ],
)
async def test_state_changing_routes_need_the_csrf_header(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path, json={})
    assert response.status_code == 403
    assert response.json()["rule"] == "client-header"


# --- the cross-user attack (ADR 0005) ---------------------------------------


@pytest.fixture
def intruder(factory: sessionmaker[Session]) -> User:
    """A second user, who owns nothing the fixtures above create."""
    with session_scope(factory, write=True) as session:
        return factories.make_user(session, kind=UserKind.LOCAL, display_name="Intruder")


async def test_another_user_reads_nothing_of_the_owners(
    client: httpx.AsyncClient, running_app: FastAPI, intruder: User, people: list[int]
) -> None:
    with acting_as(running_app, intruder.id):
        assert (await _query(client))["items"] == []
        assert (await client.get("/api/v1/contacts")).json()["items"] == []
        wide = await _query(client, filter={"include_archived": True})
        assert wide == {"items": [], "total": 0, "describe": wide["describe"]}
        for contact_id in people:
            assert (await client.get(f"/api/v1/contacts/{contact_id}")).status_code == 404


async def test_another_user_cannot_patch_archive_or_revert_the_owners_contacts(
    client: httpx.AsyncClient, running_app: FastAPI, intruder: User, people: list[int]
) -> None:
    target = people[0]
    with acting_as(running_app, intruder.id):
        attacks = [
            client.patch(f"/api/v1/contacts/{target}", json={"first_name": "Stolen"}, headers=CSRF),
            client.post(f"/api/v1/contacts/{target}/archive", headers=CSRF),
            client.post(f"/api/v1/contacts/{target}/unarchive", headers=CSRF),
            client.post(
                f"/api/v1/contacts/{target}/revert-field",
                json={"field": "current_title"},
                headers=CSRF,
            ),
            client.post(
                f"/api/v1/contacts/{target}/merge", json={"loser_id": people[1]}, headers=CSRF
            ),
            client.post(
                f"/api/v1/contacts/{target}/emails",
                json={"email": "intruder@example.test"},
                headers=CSRF,
            ),
        ]
        for attack in attacks:
            assert (await attack).status_code == 404

    # The owner's contact is untouched.
    owned = await client.get(f"/api/v1/contacts/{target}")
    body = owned.json()
    assert body["first_name"] == "Ada"
    assert body["archived_at"] is None
    assert [row["email"] for row in body["emails"]] == ["ada.quill@example.test"]


async def test_another_user_cannot_bulk_update_the_owners_contacts(
    client: httpx.AsyncClient, running_app: FastAPI, intruder: User, people: list[int]
) -> None:
    by_id = {"ids": people}
    with acting_as(running_app, intruder.id):
        counted = await _count(client, by_id, "archive")
        assert counted["count"] == 0, "another user's ids must not count"
        applied = await client.post(
            "/api/v1/contacts/bulk",
            json={"selection": by_id, "action": "archive", "token": counted["token"]},
            headers=CSRF,
        )
        assert applied.status_code == 200
        assert applied.json() == {"affected": 0}

        everyone = await _count(client, EVERYONE, "archive")
        assert everyone["count"] == 0
        swept = await client.post(
            "/api/v1/contacts/bulk",
            json={"selection": EVERYONE, "action": "archive", "token": everyone["token"]},
            headers=CSRF,
        )
        assert swept.json() == {"affected": 0}

    assert (await _query(client))["total"] == 2  # nothing of the owner's was archived


async def test_a_confirmation_token_does_not_travel_between_users(
    client: httpx.AsyncClient, running_app: FastAPI, intruder: User, people: list[int]
) -> None:
    """The owner's token, replayed by the intruder, is refused before anything is counted."""
    counted = await _count(client, EVERYONE, "archive")
    assert counted["count"] == 2
    with acting_as(running_app, intruder.id):
        response = await client.post(
            "/api/v1/contacts/bulk",
            json={"selection": EVERYONE, "action": "archive", "token": counted["token"]},
            headers=CSRF,
        )
    assert response.status_code == 422
    assert response.json()["reason"] == "user"
    assert (await _query(client))["total"] == 2
