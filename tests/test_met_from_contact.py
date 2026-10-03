"""Set Met from a contact's page, and jump to a contact in Triage (#322)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.models import (
    Contact,
    ContactMet,
    MetSource,
    TriageDecision,
    TriageDecisionKind,
    User,
)
from netkeeper.scoping import get_scoped, scoped

CSRF = {"X-Netkeeper-Client": "1"}
CONTACTS = "/api/v1/contacts"
TRIAGE = "/api/v1/triage"
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _factory(app: FastAPI) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = app.state.session_factory
    return factory


@pytest.fixture
def people(running_app: FastAPI) -> list[int]:
    """Four live contacts of the local user, all untriaged."""
    with session_scope(_factory(running_app), write=True) as session:
        user = session.get(User, 1)
        assert user is not None
        return [factories.make_contact(session, user).id for _ in range(4)]


def _row(app: FastAPI, contact_id: int) -> tuple[Contact, list[TriageDecision]]:
    with session_scope(_factory(app)) as session:
        user = session.get(User, 1)
        assert user is not None
        contact = get_scoped(session, user, Contact, contact_id)
        assert contact is not None
        decisions = list(
            session.scalars(scoped(user, TriageDecision).order_by(TriageDecision.id)).all()
        )
        return contact, decisions


async def _patch_met(client: httpx.AsyncClient, contact_id: int, met: str) -> dict[str, Any]:
    response = await client.patch(f"{CONTACTS}/{contact_id}", json={"met": met}, headers=CSRF)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- Met from the contact page ----------------------------------------------


async def test_met_from_the_page_records_what_a_triage_decision_records(
    running_app: FastAPI, client: httpx.AsyncClient, people: list[int]
) -> None:
    via_page, via_triage = people[0], people[1]
    page = await _patch_met(client, via_page, "met")
    decided = await client.post(
        f"{TRIAGE}/decisions", json={"contact_id": via_triage, "met": "met"}, headers=CSRF
    )
    assert decided.status_code == 201, decided.text

    first, first_log = _row(running_app, via_page)
    second, second_log = _row(running_app, via_triage)
    assert (page["met"], page["met_source"]) == ("met", "manual")
    assert (first.met, first.met_source) == (second.met, second.met_source)
    assert (first.met, first.met_source) == (ContactMet.MET, MetSource.MANUAL)
    assert first.triaged_at is not None and second.triaged_at is not None
    # Both left an undoable decision row of the same shape.
    page_row = next(row for row in first_log if row.contact_id == via_page)
    triage_row = next(row for row in second_log if row.contact_id == via_triage)
    assert page_row.kind is triage_row.kind is TriageDecisionKind.DECIDE
    assert page_row.before_state.keys() == triage_row.before_state.keys()
    assert page_row.after_state["met"] == triage_row.after_state["met"] == "met"
    assert page_row.after_state["met_source"] == triage_row.after_state["met_source"] == "manual"


async def test_not_met_from_the_page_takes_over_a_batch_decision(
    running_app: FastAPI, client: httpx.AsyncClient, people: list[int]
) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        user = session.get(User, 1)
        assert user is not None
        contact = get_scoped(session, user, Contact, people[0])
        assert contact is not None
        contact.met = ContactMet.MET
        contact.met_source = MetSource.AUTOMATIC
        contact.triaged_at = NOW
    body = await _patch_met(client, people[0], "not_met")
    assert (body["met"], body["met_source"]) == ("not_met", "manual")
    assert body["triaged_at"] is not None


async def test_clearing_met_returns_the_contact_to_the_untriaged_queue(
    running_app: FastAPI, client: httpx.AsyncClient, people: list[int]
) -> None:
    await _patch_met(client, people[0], "met")
    cleared = await _patch_met(client, people[0], "unknown")
    assert cleared["met"] == "unknown"
    assert cleared["triaged_at"] is None
    assert cleared["met_source"] == "manual"
    queue = await client.get(f"{TRIAGE}/next")
    assert queue.json()["card"]["contact"]["id"] == people[0]
    _contact, decisions = _row(running_app, people[0])
    assert len(decisions) == 2


async def test_a_met_set_from_the_page_can_be_undone_from_triage(
    running_app: FastAPI, client: httpx.AsyncClient, people: list[int]
) -> None:
    await _patch_met(client, people[0], "met")
    undone = await client.post(f"{TRIAGE}/undo", headers=CSRF)
    assert undone.status_code == 200, undone.text
    contact, _ = _row(running_app, people[0])
    assert (contact.met, contact.triaged_at) == (ContactMet.UNKNOWN, None)


async def test_a_contact_marked_met_from_the_page_is_met_everywhere_met_matters(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    await _patch_met(client, people[2], "met")

    stats = (await client.get(f"{CONTACTS}/stats")).json()
    assert (stats["met"], stats["untriaged"]) == (1, 3)

    # The campaign and test-send pickers filter on met = "met".
    queried = await client.post(
        f"{CONTACTS}/query",
        json={
            "filter": {
                "include_archived": False,
                "where": {"op": "eq", "field": "met", "value": "met"},
            },
            "sort": [],
            "limit": 50,
            "offset": 0,
            "columns": ["met"],
        },
        headers=CSRF,
    )
    assert queried.status_code == 200, queried.text
    assert [row["id"] for row in queried.json()["items"]] == [people[2]]

    # The built-in Validated list is that same filter.
    lists = (await client.get("/api/v1/lists")).json()
    built_in = next(item for item in lists if item["builtin"] and item["name"] == "Validated")
    members = (await client.get(f"/api/v1/lists/{built_in['id']}/members")).json()
    assert [item["id"] for item in members["items"]] == [people[2]]


# --- Jumping to a contact in Triage -----------------------------------------


async def test_the_jump_serves_a_named_contact_without_deciding_the_ones_before(
    running_app: FastAPI, client: httpx.AsyncClient, people: list[int]
) -> None:
    response = await client.get(f"{TRIAGE}/contacts/{people[3]}")
    assert response.status_code == 200, response.text
    assert response.json()["contact"]["id"] == people[3]
    assert "evidence" in response.json()
    for earlier in people[:3]:
        contact, decisions = _row(running_app, earlier)
        assert contact.met is ContactMet.UNKNOWN
        assert decisions == []
    # The normal order is untouched.
    queue = await client.get(f"{TRIAGE}/next")
    assert queue.json()["card"]["contact"]["id"] == people[0]


async def test_the_jump_respects_which_contacts_the_queue_holds(
    client: httpx.AsyncClient, people: list[int]
) -> None:
    await client.post(
        f"{TRIAGE}/decisions", json={"contact_id": people[0], "met": "met"}, headers=CSRF
    )
    # Decided: not in the default (untriaged) queue, but in the one that holds `met`.
    assert (await client.get(f"{TRIAGE}/contacts/{people[0]}")).status_code == 404
    holds = await client.get(f"{TRIAGE}/contacts/{people[0]}", params={"states": ["met"]})
    assert holds.status_code == 200
    # Decided by hand, so not waiting for review.
    review = await client.get(f"{TRIAGE}/contacts/{people[0]}", params={"decided_by": "automatic"})
    assert review.status_code == 404


async def test_the_jump_refuses_archived_merged_and_missing_contacts(
    running_app: FastAPI, client: httpx.AsyncClient, people: list[int]
) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        user = session.get(User, 1)
        assert user is not None
        contact = get_scoped(session, user, Contact, people[0])
        assert contact is not None
        contact.archived_at = NOW
    assert (await client.get(f"{TRIAGE}/contacts/{people[0]}")).status_code == 404
    assert (await client.get(f"{TRIAGE}/contacts/999999")).status_code == 404


async def test_an_unchanged_met_logs_no_decision(
    running_app: FastAPI, client: httpx.AsyncClient, people: list[int]
) -> None:
    await _patch_met(client, people[0], "met")
    await _patch_met(client, people[0], "met")
    _contact, decisions = _row(running_app, people[0])
    assert len(decisions) == 1


async def test_met_on_an_archived_contact_is_refused_and_undo_still_works(
    running_app: FastAPI, client: httpx.AsyncClient, people: list[int]
) -> None:
    a, b = people[0], people[1]
    decided = await client.post(
        f"{TRIAGE}/decisions", json={"contact_id": a, "met": "met"}, headers=CSRF
    )
    assert decided.status_code == 201, decided.text
    archived = await client.post(f"{CONTACTS}/{b}/archive", headers=CSRF)
    assert archived.status_code == 200, archived.text

    refused = await client.patch(f"{CONTACTS}/{b}", json={"met": "met"}, headers=CSRF)
    assert refused.status_code == 409, refused.text
    contact_b, decisions = _row(running_app, b)
    assert contact_b.met is ContactMet.UNKNOWN
    assert [row.contact_id for row in decisions] == [a]

    undone = await client.post(f"{TRIAGE}/undo", headers=CSRF)
    assert undone.status_code == 200, undone.text
    contact_a, _ = _row(running_app, a)
    assert contact_a.met is ContactMet.UNKNOWN


async def test_bulk_clear_met_clears_the_triage_stamp(
    running_app: FastAPI, client: httpx.AsyncClient, people: list[int]
) -> None:
    await _patch_met(client, people[0], "met")
    counted = await client.post(
        f"{CONTACTS}/bulk/count",
        json={"selection": {"ids": [people[0]]}, "action": "set_met", "value": "unknown"},
        headers=CSRF,
    )
    assert counted.status_code == 200, counted.text
    applied = await client.post(
        f"{CONTACTS}/bulk",
        json={
            "selection": {"ids": [people[0]]},
            "action": "set_met",
            "value": "unknown",
            "token": counted.json()["token"],
        },
        headers=CSRF,
    )
    assert applied.status_code == 200, applied.text
    contact, _ = _row(running_app, people[0])
    assert (contact.met, contact.triaged_at) == (ContactMet.UNKNOWN, None)
