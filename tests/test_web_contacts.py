"""The contacts API (spec 10.1, 14.1; item P1-05).

The list with its filter, sort, page and columns; one contact with children and
timeline; edits that stick and revert; bulk actions behind a count confirmation
token; archive. Plus the cross-user attack: user B must reach nothing of user
A's through any of it.

Every name, address and number here is invented.
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from isolation.harness import acting_as
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import contacts as service
from netkeeper.crm.filters import parse_filter
from netkeeper.crm.interactions import add_interaction
from netkeeper.crm.provenance import record_synced_value
from netkeeper.crm.tags import create_tag, tag_contact
from netkeeper.db import session_scope
from netkeeper.models import (
    ContactMet,
    ContactSnapshot,
    ContactSource,
    InteractionKind,
    TagSource,
    User,
    UserKind,
)
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


# --- stats (P1-25, #90) -------------------------------------------------------


async def test_stats_counts_the_live_set_the_same_way_the_service_does(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    """``people`` is two live contacts (Ada, Bo) and one archived (Cy); Ada carries both
    an email and a phone, Bo an email only, and neither has been triaged.
    """
    response = await client.get("/api/v1/contacts/stats")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["total"] == 2
    assert body["untriaged"] == 2
    assert body["met"] == body["not_met"] == body["skipped"] == 0
    assert body["archived"] == 1
    assert body["merged_away"] == 0
    assert body["with_email"] == 2
    assert body["with_phone"] == 1


async def test_stats_matches_a_direct_call_to_the_service(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User, people: list[int]
) -> None:
    with session_scope(factory) as session:
        user = session.get(User, owner.id)
        assert user is not None
        expected = service.contact_stats(session, user)

    response = await client.get("/api/v1/contacts/stats")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "total": expected.total,
        "met": expected.met,
        "not_met": expected.not_met,
        "skipped": expected.skipped,
        "untriaged": expected.untriaged,
        "archived": expected.archived,
        "merged_away": expected.merged_away,
        "with_email": expected.with_email,
        "with_phone": expected.with_phone,
        "tagged": expected.tagged,
        "tagged_by_rule": expected.tagged_by_rule,
    }


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


async def _count(
    client: httpx.AsyncClient, selection: Any, action: str, **written: Any
) -> dict[str, Any]:
    """Ask for the count and the token. ``written`` is the ``value`` and ``reason``."""
    response = await client.post(
        "/api/v1/contacts/bulk/count",
        json={"selection": selection, "action": action, **written},
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
    counted = await _count(client, {"ids": [people[0], people[1]]}, "set_met", value="met")
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


@pytest.mark.parametrize(
    ("path", "extra"),
    [("/api/v1/contacts/bulk/count", {}), ("/api/v1/contacts/bulk", {"token": "unused"})],
)
async def test_a_bulk_action_whose_value_does_not_fit_is_refused(
    client: httpx.AsyncClient, path: str, extra: dict[str, Any]
) -> None:
    """The count validates the value too, so a bad one fails before anything is counted."""
    response = await client.post(
        path,
        json={"selection": EVERYONE, "action": "set_met", **extra},
        headers=CSRF,
    )
    assert response.status_code == 422
    assert "set_met needs value" in response.text


async def test_a_token_cannot_execute_a_different_value(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    """ "Mark 2 people do-not-contact" and "clear it on 2 people" are two confirmations."""
    counted = await _count(client, EVERYONE, "set_do_not_contact", value=True, reason="asked me to")
    assert counted["count"] == 2

    flipped = await client.post(
        "/api/v1/contacts/bulk",
        json={
            "selection": EVERYONE,
            "action": "set_do_not_contact",
            "value": False,
            "token": counted["token"],
        },
        headers=CSRF,
    )
    assert flipped.status_code == 422, flipped.text
    assert flipped.json()["reason"] == "selection"

    # The reason is bound too: same value, different sentence.
    reworded = await client.post(
        "/api/v1/contacts/bulk",
        json={
            "selection": EVERYONE,
            "action": "set_do_not_contact",
            "value": True,
            "reason": "no longer at the company",
            "token": counted["token"],
        },
        headers=CSRF,
    )
    assert reworded.status_code == 422
    assert reworded.json()["reason"] == "selection"

    # What was confirmed still executes.
    applied = await client.post(
        "/api/v1/contacts/bulk",
        json={
            "selection": EVERYONE,
            "action": "set_do_not_contact",
            "value": True,
            "reason": "asked me to",
            "token": counted["token"],
        },
        headers=CSRF,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json() == {"affected": 2}


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


async def test_stats_does_not_count_another_users_contacts(
    client: httpx.AsyncClient, running_app: FastAPI, intruder: User, people: list[int]
) -> None:
    """The intruder gets two contacts -- one carrying a manual tag, one carrying a
    rule-sourced tag, plus an email and a phone -- numbers that differ from the owner's
    ``people`` fixture and from each other, so a leak in either the user filter or the
    tag-source filter shows up as a wrong count, not just a nonzero one.
    """
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        theirs_user = session.get(User, intruder.id)
        assert theirs_user is not None
        by_hand = factories.make_contact(
            session, theirs_user, emails=["intruder@example.test"], phones=["+15550009999"]
        )
        by_rule = factories.make_contact(session, theirs_user)
        manual_tag = create_tag(session, theirs_user, "seeded-manual")
        rule_tag = create_tag(session, theirs_user, "seeded-rule")
        tag_contact(session, theirs_user, by_hand.id, manual_tag.id, source=TagSource.MANUAL)
        tag_contact(session, theirs_user, by_rule.id, rule_tag.id, source=TagSource.RULE)

    with acting_as(running_app, intruder.id):
        response = await client.get("/api/v1/contacts/stats")
        assert response.status_code == 200
        body = response.json()
    assert body["total"] == 2
    assert body["with_email"] == 1
    assert body["with_phone"] == 1
    assert body["tagged"] == 2  # both contacts carry some tag
    assert body["tagged_by_rule"] == 1  # only the rule-sourced one
    assert body["archived"] == 0
    assert body["merged_away"] == 0

    # The owner's own numbers are exactly `people`'s, unaffected by the intruder's data.
    owned = await client.get("/api/v1/contacts/stats")
    owned_body = owned.json()
    assert owned_body["total"] == 2
    assert owned_body["with_email"] == 2
    assert owned_body["with_phone"] == 1
    assert owned_body["tagged"] == 0
    assert owned_body["tagged_by_rule"] == 0
    assert owned_body["archived"] == 1


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


# --- merge, and the merged-away convention (spec 8.2) -----------------------


@pytest.fixture
def merged(client: httpx.AsyncClient, people: list[int]) -> tuple[int, int]:
    """``(survivor, merged_away)``: the designer folded into the engineer."""
    return people[0], people[1]


async def _merge(client: httpx.AsyncClient, survivor: int, loser: int) -> httpx.Response:
    return await client.post(
        f"/api/v1/contacts/{survivor}/merge", json={"loser_id": loser}, headers=CSRF
    )


async def test_a_merge_folds_the_loser_into_the_survivor(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    survivor_id, loser_id, _ = people
    response = await _merge(client, survivor_id, loser_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == survivor_id
    assert body["first_name"] == "Ada"  # the survivor's own fields win
    assert body["merged_into_id"] is None
    assert body["resolved_from"] is None
    # The loser's children came across.
    assert sorted(row["email"] for row in body["emails"]) == [
        "ada.quill@example.test",
        "bo.marsh@example.test",
    ]
    assert sum(row["is_primary"] for row in body["emails"]) == 1
    # And the loser is gone from the table, without being deleted.
    assert _names(await _query(client)) == ["Ada Quill"]


async def test_a_merged_away_id_reads_as_its_survivor(
    client: httpx.AsyncClient, merged: tuple[int, int]
) -> None:
    survivor_id, loser_id = merged
    assert (await _merge(client, survivor_id, loser_id)).status_code == 200

    response = await client.get(f"/api/v1/contacts/{loser_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == survivor_id
    assert body["first_name"] == "Ada"
    assert body["resolved_from"] == loser_id, "a stale link still lands on the person"


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [
        ("PATCH", "", {"first_name": "Renamed"}),
        ("POST", "/archive", None),
        ("POST", "/unarchive", None),
        ("POST", "/revert-field", {"field": "current_title"}),
        ("POST", "/emails", {"email": "new@example.test"}),
        ("POST", "/phones", {"raw": "+15550009999"}),
        ("POST", "/links", {"url": "https://example.test/bo"}),
    ],
)
async def test_every_write_to_a_merged_away_id_is_a_409_naming_the_survivor(
    client: httpx.AsyncClient,
    merged: tuple[int, int],
    method: str,
    suffix: str,
    payload: dict[str, Any] | None,
) -> None:
    """The convention the module docstring states, on every route that follows it."""
    survivor_id, loser_id = merged
    assert (await _merge(client, survivor_id, loser_id)).status_code == 200

    response = await client.request(
        method, f"/api/v1/contacts/{loser_id}{suffix}", json=payload, headers=CSRF
    )
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "merged", "merged_into_id": survivor_id}


async def test_merging_into_a_merged_away_id_is_refused(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    """Not silently redirected to the survivor: the caller asked for a row nothing will show."""
    survivor_id, loser_id, third_id = people
    assert (await _merge(client, survivor_id, loser_id)).status_code == 200

    response = await _merge(client, loser_id, third_id)
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "merged", "merged_into_id": survivor_id}


async def test_a_contact_cannot_be_merged_into_itself(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    response = await _merge(client, people[0], people[0])
    assert response.status_code == 409
    assert "itself" in response.json()["detail"]


async def test_a_loser_already_merged_elsewhere_is_refused(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    survivor_id, loser_id, other_id = people
    assert (await _merge(client, survivor_id, loser_id)).status_code == 200

    response = await _merge(client, other_id, loser_id)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert str(survivor_id) in detail and str(other_id) in detail


async def test_merging_an_unknown_loser_is_a_404(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    response = await _merge(client, people[0], 424242)
    assert response.status_code == 404


async def test_merging_the_same_pair_twice_is_a_no_op(
    client: httpx.AsyncClient, merged: tuple[int, int]
) -> None:
    survivor_id, loser_id = merged
    first = await _merge(client, survivor_id, loser_id)
    second = await _merge(client, survivor_id, loser_id)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"] == survivor_id


# --- bulk archive keeps the stamp a row already carries ---------------------


async def test_bulk_archive_does_not_restamp_an_already_archived_contact(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    """``archived_at`` records when the contact left the table; it left once.

    Reachable two ways, and both are here: an ``ids`` selection names archived
    rows outright, and a filter with ``include_archived`` sweeps them in.
    """
    already, fresh = people[0], people[1]
    first = await client.post(f"/api/v1/contacts/{already}/archive", headers=CSRF)
    assert first.status_code == 200
    stamped = first.json()["archived_at"]
    assert stamped is not None

    for selection in ({"ids": [already, fresh]}, {"filter": {"include_archived": True}}):
        counted = await _count(client, selection, "archive")
        applied = await client.post(
            "/api/v1/contacts/bulk",
            json={"selection": selection, "action": "archive", "token": counted["token"]},
            headers=CSRF,
        )
        assert applied.status_code == 200, applied.text
        assert applied.json() == {"affected": counted["count"]}, (
            "every row the person confirmed is written, archived or not"
        )

        body = (await client.get(f"/api/v1/contacts/{already}")).json()
        assert body["archived_at"] == stamped, f"{selection} re-stamped an archived contact"

    # The one that was live did get a stamp, and not the other one's.
    newly = (await client.get(f"/api/v1/contacts/{fresh}")).json()["archived_at"]
    assert newly is not None and newly != stamped


async def test_bulk_unarchive_then_archive_stamps_afresh(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    """Leaving the table again is a new fact, so it gets a new time."""
    target = people[0]
    first = (await client.post(f"/api/v1/contacts/{target}/archive", headers=CSRF)).json()
    await client.post(f"/api/v1/contacts/{target}/unarchive", headers=CSRF)

    selection = {"ids": [target]}
    counted = await _count(client, selection, "archive")
    applied = await client.post(
        "/api/v1/contacts/bulk",
        json={"selection": selection, "action": "archive", "token": counted["token"]},
        headers=CSRF,
    )
    assert applied.status_code == 200, applied.text
    again = (await client.get(f"/api/v1/contacts/{target}")).json()["archived_at"]
    assert again is not None and again != first["archived_at"]


# --- the service's clock ----------------------------------------------------


def test_a_bulk_action_resolves_now_once(
    factory: sessionmaker[Session], owner: User, people: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count and the UPDATE must compile against one instant, not two.

    A relative window compiles its own clock every time ``compile_where`` runs,
    so leaving ``now`` unset in two places resolves it twice: the count can see
    a contact the write then misses, which is the drift the confirmation token
    exists to prevent, arriving through the back door. Pinned with a clock that
    jumps a day per call, so the bug is a failure and not a race -- resolving
    twice puts the count and the write a day apart and the contact seeded here
    falls out of the window between them.
    """
    ticks = itertools.count()
    start = datetime(2026, 4, 1, tzinfo=UTC)
    filter_calls = 0

    def service_clock() -> datetime:
        return start + timedelta(days=next(ticks))

    def filter_clock() -> datetime:
        nonlocal filter_calls
        filter_calls += 1
        return service_clock()

    monkeypatch.setattr("netkeeper.crm.contacts.utcnow", service_clock)
    monkeypatch.setattr("netkeeper.crm.filters.utcnow", filter_clock)

    with session_scope(factory, write=True) as session:
        user = session.get(User, owner.id)
        assert user is not None
        contact = get_scoped(session, user, Contact, people[0])
        assert contact is not None
        contact.last_contacted_at = start - timedelta(hours=12)  # inside a one-day window at start
        session.flush()

        selection = service.Selection(
            tree=parse_filter({"where": {"op": "last_contacted", "within_days": 1}})
        )
        # What the confirmation dialog counted, at the instant the token was minted.
        confirmed = service.count_selection(session, user, selection, now=start)
        assert confirmed == 1
        # And the action, with no instant of its own: the service picks one and
        # both statements must use it. Resolving twice raises CountMismatch here.
        affected = service.bulk_update(
            session,
            user,
            selection,
            "set_met",
            value=ContactMet.MET,
            expected_count=confirmed,
        )

    assert affected == confirmed
    assert filter_calls == 0, "the filter resolved its own clock instead of the one passed in"
