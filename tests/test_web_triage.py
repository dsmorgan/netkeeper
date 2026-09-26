"""The triage routes (spec 10.2, 14.1): the keyboard flow, its cost, undo, isolation.

The headline test is :func:`test_a_triage_run_costs_one_request_and_a_fixed_number_of_queries`:
it counts HTTP requests and SQL statements rather than timing anything, so it
says the same thing on a loaded machine as on an idle one.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import factories
import httpx
import pytest
from fastapi import FastAPI
from isolation.harness import FixedUser
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import tags as tag_service
from netkeeper.crm import triage as triage_service
from netkeeper.crm.identity import merge
from netkeeper.crm.interactions import add_interaction
from netkeeper.db import session_scope
from netkeeper.models import (
    Contact,
    ContactMet,
    ContactSnapshot,
    ContactSource,
    InteractionKind,
    MetSource,
    TriageDecision,
    User,
    UserKind,
)
from netkeeper.scoping import get_scoped, scoped_update
from netkeeper.web.deps import LocalSingleUser

CSRF = {"X-Netkeeper-Client": "1"}
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(days=30)
LOCAL_USER_ID = 1
TRIAGE = "/api/v1/triage"

# A decide request carries the decision, the next contact, its whole evidence
# panel, and the progress counters: 14 statements as this is written. The budget
# leaves a little room; the test below also fails if the number stops being the
# same for every contact, which is what a query per message or per position
# would look like.
MAX_QUERIES_PER_CONTACT = 16

# How many contacts the run test triages. Enough that the steady state (a decide
# that also prefetches) happens several times over.
RUN_LENGTH = 6

# An invented fragment in the shape of what an InMail body can carry (#75). Nothing
# on the way out escapes it, and the test below is what says so out loud.
UNSAFE_MARKUP = '<p class="editor">hello</p><img src=x onerror=alert(1)>'


def _factory(app: FastAPI) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = app.state.session_factory
    return factory


def _local_user(session: Session) -> User:
    user = session.get(User, LOCAL_USER_ID)
    assert user is not None
    return user


def _seed(session: Session, user: User, *, messages: int, positions: int, tags: int) -> Contact:
    """A contact with a given amount of evidence, so a query per item would show up."""
    contact = factories.make_contact(
        session,
        user,
        current_company="Northwind Pottery",
        notes="a note about this person" if messages else None,
        positions=[
            {"title": f"Role {index}", "company": f"Company {index}", "is_current": index == 0}
            for index in range(positions)
        ],
    )
    for index in range(messages):
        add_interaction(
            session,
            user,
            contact.id,
            InteractionKind.LI_IN if index % 2 else InteractionKind.LI_OUT,
            EARLIER + timedelta(days=index),
            f"message {index}",
            source=ContactSource.ARCHIVE,
        )
    for index in range(tags):
        tag = tag_service.create_tag(session, user, f"seeded-{contact.id}-{index}")
        tag_service.tag_contact(session, user, contact.id, tag.id)
    if positions:
        session.add(
            ContactSnapshot(
                user_id=user.id,
                contact_id=contact.id,
                headline="An older headline",
                observed_at=EARLIER,
            )
        )
        session.flush()
    return contact


@pytest.fixture
def run_contacts(running_app: FastAPI) -> list[int]:
    """``RUN_LENGTH`` contacts of the local user carrying different amounts of evidence."""
    with session_scope(_factory(running_app), write=True) as session:
        user = _local_user(session)
        return [
            _seed(session, user, messages=index, positions=index % 3, tags=index % 2).id
            for index in range(RUN_LENGTH)
        ]


@pytest.fixture
def contact_id(running_app: FastAPI) -> int:
    """One contact of the local user, with a message and a note."""
    with session_scope(_factory(running_app), write=True) as session:
        return _seed(session, _local_user(session), messages=1, positions=1, tags=1).id


@pytest.fixture
def foreign(running_app: FastAPI) -> tuple[int, int]:
    """Another user and one of their contacts, with message history: ``(user_id, contact_id)``."""
    with session_scope(_factory(running_app), write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED)
        contact = _seed(session, other, messages=2, positions=1, tags=0)
        return other.id, contact.id


class _QueryLog:
    """Every SQL statement the app issues, so a test can count them per request.

    ``PRAGMA`` and ``BEGIN`` are left out: the pragmas run once when a pooled
    connection is opened, which would otherwise make the first request look
    dearer than the rest.
    """

    def __init__(self, engine: Engine) -> None:
        self.statements: list[str] = []
        event.listen(engine, "before_cursor_execute", self._record)

    def _record(self, _conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        if not statement.lstrip().upper().startswith(("PRAGMA", "BEGIN")):
            self.statements.append(statement)

    def mark(self) -> int:
        return len(self.statements)

    def since(self, mark: int) -> int:
        return len(self.statements) - mark


class _RequestCount:
    """How many HTTP requests the client has made."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.count = 0
        client.event_hooks["request"].append(self._count)

    async def _count(self, _request: httpx.Request) -> None:
        self.count += 1


@pytest.fixture
def as_user(running_app: FastAPI) -> Iterator[Any]:
    """Run requests as another user, the way the isolation harness does."""
    original = running_app.state.auth

    def switch(user_id: int) -> None:
        running_app.state.auth = FixedUser(user_id)

    yield switch
    running_app.state.auth = original


async def _get(client: httpx.AsyncClient, path: str, **params: Any) -> dict[str, Any]:
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _decide(
    client: httpx.AsyncClient, contact_id: int, met: str, **extra: Any
) -> httpx.Response:
    return await client.post(
        f"{TRIAGE}/decisions", json={"contact_id": contact_id, "met": met, **extra}, headers=CSRF
    )


async def _undo(client: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await client.post(f"{TRIAGE}/undo", json=body or None, headers=CSRF)


def _restorable(card: dict[str, Any]) -> dict[str, Any]:
    """A card without the row's own modification time, which a write is meant to move."""
    contact = {key: value for key, value in card["contact"].items() if key != "updated_at"}
    return {**card, "contact": contact}


def _contact_row(app: FastAPI, user_id: int, contact_id: int) -> Contact:
    with session_scope(_factory(app)) as session:
        user = session.get(User, user_id)
        assert user is not None
        contact = get_scoped(session, user, Contact, contact_id)
        assert contact is not None
        return contact


# --- the keyboard flow ------------------------------------------------------


async def test_a_triage_run_costs_one_request_and_a_fixed_number_of_queries(
    running_app: FastAPI, client: httpx.AsyncClient, run_contacts: list[int]
) -> None:
    """One request per contact, evidence included, and no query that grows with it.

    The contacts carry different amounts of history on purpose: an evidence panel
    that loaded one row at a time would make the counts differ between them.
    """
    queries = _QueryLog(running_app.state.engine)
    requests = _RequestCount(client)
    start = await _get(client, f"{TRIAGE}/next", prefetch=True)
    card, upcoming = start["card"], start["next"]
    assert card is not None and upcoming is not None
    decided: list[int] = []
    cost: list[tuple[int, bool]] = []
    while card is not None:
        body: dict[str, Any] = {"contact_id": card["contact"]["id"], "met": "met"}
        if upcoming is not None:
            body["prefetch_after_id"] = upcoming["contact"]["id"]
        mark = queries.mark()
        response = await client.post(f"{TRIAGE}/decisions", json=body, headers=CSRF)
        assert response.status_code == 201, response.text
        following = response.json()["next"]
        cost.append((queries.since(mark), following is not None))
        decided.append(card["contact"]["id"])
        card, upcoming = upcoming, following
    assert decided == run_contacts, "the run walked the queue in order and triaged everyone"
    assert requests.count == len(run_contacts) + 1, (
        "one request to start the run, then exactly one per contact"
    )
    steady = [count for count, prefetched in cost if prefetched]
    assert len(steady) >= 3, cost
    assert len(set(steady)) == 1, f"the query cost per contact is not constant: {cost}"
    assert steady[0] <= MAX_QUERIES_PER_CONTACT, f"{steady[0]} queries per contact"
    tail = [count for count, prefetched in cost if not prefetched]
    assert all(count <= steady[0] for count in tail), cost


async def test_the_card_carries_its_evidence_in_the_same_response(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int
) -> None:
    body = await _get(client, f"{TRIAGE}/next")
    card = body["card"]
    assert card["contact"]["id"] == contact_id
    assert card["contact"]["notes"] == "a note about this person"
    assert len(card["contact"]["tags"]) == 1
    evidence = card["evidence"]
    assert evidence["messages"]["total"] == 1
    assert evidence["messages"]["recent"][0]["summary"] == "message 0"
    assert [entry["kind"] for entry in evidence["timeline"]] == ["interaction", "snapshot"]
    assert evidence["shared_companies"][0]["company"] == "Northwind Pottery"
    assert body["progress"] == {
        "total": 1,
        "triaged": 0,
        "remaining": 1,
        "by_state": {"unknown": 1, "met": 0, "not_met": 0, "skip": 0},
        "automatic": 0,
    }


async def test_the_queue_ends_with_two_empty_cards(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int
) -> None:
    body = await _get(client, f"{TRIAGE}/next")
    assert body["card"] is not None and body["next"] is None
    assert (await _decide(client, contact_id, "not_met")).status_code == 201
    done = await _get(client, f"{TRIAGE}/next")
    assert done["card"] is None and done["next"] is None
    assert done["progress"]["remaining"] == 0


async def test_the_arrow_key_moves_on_without_deciding(
    running_app: FastAPI, client: httpx.AsyncClient, run_contacts: list[int]
) -> None:
    body = await _get(client, f"{TRIAGE}/next", after_id=run_contacts[0])
    assert body["card"]["contact"]["id"] == run_contacts[1]
    assert _contact_row(running_app, LOCAL_USER_ID, run_contacts[0]).met is ContactMet.UNKNOWN


async def test_the_skipped_can_be_revisited(
    running_app: FastAPI, client: httpx.AsyncClient, run_contacts: list[int]
) -> None:
    await _decide(client, run_contacts[0], "skip")
    await _decide(client, run_contacts[1], "met")
    body = await _get(client, f"{TRIAGE}/next", states=["skip"])
    assert body["card"]["contact"]["id"] == run_contacts[0]
    assert body["progress"]["remaining"] == 1


async def test_an_unknown_decision_is_refused(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int
) -> None:
    response = await _decide(client, contact_id, "unknown")
    assert response.status_code == 422, response.text
    assert _contact_row(running_app, LOCAL_USER_ID, contact_id).met is ContactMet.UNKNOWN


async def test_deciding_a_contact_that_is_not_yours_is_not_found(
    running_app: FastAPI, client: httpx.AsyncClient, foreign: tuple[int, int]
) -> None:
    _owner, theirs = foreign
    response = await _decide(client, theirs, "met")
    assert response.status_code == 404
    assert response.json()["detail"] == "no such contact"


@pytest.fixture
def gone(running_app: FastAPI) -> tuple[int, int, int]:
    """``(archived, merged_away, survivor)``: two contacts the queue never serves (#83)."""
    with session_scope(_factory(running_app), write=True) as session:
        user = _local_user(session)
        archived = factories.make_contact(session, user, archived_at=NOW)
        survivor = factories.make_contact(session, user)
        loser = factories.make_contact(session, user)
        merge(session, user, survivor.id, loser.id)
        return archived.id, loser.id, survivor.id


async def test_deciding_a_contact_the_queue_never_serves_is_a_conflict(
    running_app: FastAPI, client: httpx.AsyncClient, gone: tuple[int, int, int]
) -> None:
    """A hand-written request, since the queue excludes both; nothing is written (#83)."""
    archived, loser, survivor = gone
    response = await _decide(client, archived, "met")
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "archived"}
    response = await _decide(client, loser, "met")
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "merged", "merged_into_id": survivor}
    for contact_id in (archived, loser, survivor):
        assert _contact_row(running_app, LOCAL_USER_ID, contact_id).met is ContactMet.UNKNOWN
    assert (await _undo(client)).status_code == 404, "nothing reached the undo stack"


async def test_naming_a_contact_the_queue_never_serves_is_a_conflict(
    running_app: FastAPI, client: httpx.AsyncClient, gone: tuple[int, int, int]
) -> None:
    archived, loser, survivor = gone
    for contact_id, body in (
        (archived, {"detail": "archived"}),
        (loser, {"detail": "merged", "merged_into_id": survivor}),
    ):
        response = await client.put(
            f"{TRIAGE}/contacts/{contact_id}/preferred-name",
            json={"preferred_name": "Bob"},
            headers=CSRF,
        )
        assert response.status_code == 409, response.text
        assert response.json() == body
        assert _contact_row(running_app, LOCAL_USER_ID, contact_id).preferred_name != "Bob"


async def test_message_summaries_and_notes_come_back_verbatim(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    """The API escapes nothing, on any of the three paths that carry someone else's words.

    The archive importer cleans a LinkedIn message body to plain text at
    import time (#75); this test is about a different layer, that the API
    itself never escapes or strips a stored `summary` or `notes` value on the
    way out, whatever it contains -- an interaction added by hand, as this one
    is, can carry anything a person typed. The contract: if the API ever
    starts cleaning, or ever stops, a renderer built on the other assumption
    has to be told.
    """
    with session_scope(_factory(running_app), write=True) as session:
        user = _local_user(session)
        contact = factories.make_contact(session, user, notes=UNSAFE_MARKUP)
        add_interaction(
            session,
            user,
            contact.id,
            InteractionKind.LI_IN,
            EARLIER,
            UNSAFE_MARKUP,
            source=ContactSource.ARCHIVE,
        )
        contact_id = contact.id
    card = (await _get(client, f"{TRIAGE}/next"))["card"]
    assert card["contact"]["id"] == contact_id
    assert card["contact"]["notes"] == UNSAFE_MARKUP
    assert card["evidence"]["messages"]["recent"][0]["summary"] == UNSAFE_MARKUP
    timeline = card["evidence"]["timeline"][0]
    assert timeline["kind"] == "interaction"
    assert timeline["interaction"]["summary"] == UNSAFE_MARKUP


# --- undo -------------------------------------------------------------------


async def test_undo_restores_the_previous_state_and_the_queue_position(
    running_app: FastAPI, client: httpx.AsyncClient, run_contacts: list[int]
) -> None:
    before = await _get(client, f"{TRIAGE}/next")
    await _decide(client, run_contacts[0], "not_met")
    await _decide(client, run_contacts[0], "met")
    response = await _undo(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decisions"] == 1
    assert body["card"]["contact"]["id"] == run_contacts[0]
    assert body["card"]["contact"]["met"] == "not_met"
    assert body["forced"] == []
    again = await _undo(client)
    assert again.json()["card"]["contact"]["met"] == "unknown"
    after = await _get(client, f"{TRIAGE}/next")
    assert _restorable(after["card"]) == _restorable(before["card"]), (
        "the contact is back exactly where it was, at the head of the queue"
    )


async def test_undo_reports_an_empty_stack(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int
) -> None:
    response = await _undo(client)
    assert response.status_code == 404
    assert response.json()["detail"] == "nothing to undo"


async def test_undo_refuses_to_overwrite_a_change_that_arrived_after_the_decision(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int
) -> None:
    await _decide(client, contact_id, "met")
    with session_scope(_factory(running_app), write=True) as session:
        contact = get_scoped(session, _local_user(session), Contact, contact_id)
        assert contact is not None
        contact.met = ContactMet.NOT_MET
    response = await _undo(client)
    assert response.status_code == 409, response.text
    assert "not_met" in response.json()["detail"]
    assert _contact_row(running_app, LOCAL_USER_ID, contact_id).met is ContactMet.NOT_MET
    forced = await _undo(client, force=True)
    assert forced.status_code == 200, forced.text
    assert forced.json()["forced"] == [contact_id]
    assert _contact_row(running_app, LOCAL_USER_ID, contact_id).met is ContactMet.UNKNOWN


async def test_an_undo_that_lost_a_race_is_a_409_that_offers_no_force(
    running_app: FastAPI,
    client: httpx.AsyncClient,
    contact_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its own shape, not the conflict's: a client that answers a conflict with
    ``force`` would take back the decision before the one it meant (#222). A 409
    and not a 500: ``translate_errors`` has to know the error at all."""
    await _decide(client, contact_id, "met")
    real = triage_service._contacts_by_id

    def racing(session: Session, owner: User, ids: Any) -> dict[int, Contact]:
        # Another undo committed first: its spend is what this one then counts.
        session.execute(
            scoped_update(owner, TriageDecision)
            .where(TriageDecision.undone_at.is_(None))
            .values(undone_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        return real(session, owner, ids)

    monkeypatch.setattr(triage_service, "_contacts_by_id", racing)
    response = await _undo(client)
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["reason"] == "raced"
    assert isinstance(body["decision_id"], int)
    assert "undone by another request first" in body["detail"]
    monkeypatch.undo()
    # Nothing was restored by the loser, and the request rolled the spend back.
    assert _contact_row(running_app, LOCAL_USER_ID, contact_id).met is ContactMet.MET
    assert (await _undo(client)).status_code == 200


async def test_an_undo_conflict_is_not_marked_as_a_race(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int
) -> None:
    await _decide(client, contact_id, "met")
    with session_scope(_factory(running_app), write=True) as session:
        contact = get_scoped(session, _local_user(session), Contact, contact_id)
        assert contact is not None
        contact.met = ContactMet.NOT_MET
    response = await _undo(client)
    assert response.status_code == 409, response.text
    assert "reason" not in response.json()


async def test_undo_refuses_a_contact_that_left_the_queue_and_says_so(
    running_app: FastAPI, client: httpx.AsyncClient, run_contacts: list[int]
) -> None:
    """Archived after the decision: `409`, not a card for a contact the queue never serves."""
    before = await _get(client, f"{TRIAGE}/next")
    await _decide(client, run_contacts[0], "met")
    with session_scope(_factory(running_app), write=True) as session:
        contact = get_scoped(session, _local_user(session), Contact, run_contacts[0])
        assert contact is not None
        contact.archived_at = NOW
    response = await _undo(client)
    assert response.status_code == 409, response.text
    assert "archived" in response.json()["detail"]
    forced = await _undo(client, force=True)
    assert forced.status_code == 200, forced.text
    assert forced.json()["forced"] == [run_contacts[0]]
    after = await _get(client, f"{TRIAGE}/next")
    assert after["card"]["contact"]["id"] == run_contacts[1], (
        "the archived contact stays out of the queue, whatever undo did to its met"
    )
    assert before["card"]["contact"]["id"] == run_contacts[0]


async def test_the_card_says_whether_the_contact_is_still_in_the_queue(
    running_app: FastAPI, client: httpx.AsyncClient, run_contacts: list[int]
) -> None:
    """`archived_at` and `merged_into_id` on the card, so no client infers them (#91).

    `TriageUndoOut.forced` was the only signal a client had, and it does not
    mean this: the service appends to it for *any* divergence a force
    overrode, an ordinary field edit included. So both halves are asserted
    here — a live card carries `null` for each, and the card a forced undo
    hands back for a contact that has left the queue says which way it left,
    while `forced` alone cannot tell the two apart.
    """
    live = await _get(client, f"{TRIAGE}/next")
    assert live["card"]["contact"]["archived_at"] is None
    assert live["card"]["contact"]["merged_into_id"] is None

    await _decide(client, run_contacts[0], "met")
    with session_scope(_factory(running_app), write=True) as session:
        contact = get_scoped(session, _local_user(session), Contact, run_contacts[0])
        assert contact is not None
        contact.archived_at = NOW
    forced = await _undo(client, force=True)
    assert forced.status_code == 200, forced.text
    body = forced.json()
    assert body["forced"] == [run_contacts[0]]
    assert body["card"]["contact"]["id"] == run_contacts[0]
    assert body["card"]["contact"]["archived_at"] is not None
    assert body["card"]["contact"]["merged_into_id"] is None


async def test_a_forced_undo_over_an_ordinary_edit_hands_back_a_card_still_in_the_queue(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int
) -> None:
    """The case `forced` cannot tell from the one above, and the fields can (#91)."""
    await _decide(client, contact_id, "met")
    with session_scope(_factory(running_app), write=True) as session:
        contact = get_scoped(session, _local_user(session), Contact, contact_id)
        assert contact is not None
        contact.met = ContactMet.NOT_MET
    forced = await _undo(client, force=True)
    assert forced.status_code == 200, forced.text
    body = forced.json()
    assert body["forced"] == [contact_id], "forced says the same thing in both cases"
    assert body["card"]["contact"]["archived_at"] is None
    assert body["card"]["contact"]["merged_into_id"] is None


# --- the preferred-name edit ------------------------------------------------


async def test_the_preferred_name_edit_is_a_manual_override_and_is_undoable(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int
) -> None:
    original = _contact_row(running_app, LOCAL_USER_ID, contact_id).preferred_name
    response = await client.put(
        f"{TRIAGE}/contacts/{contact_id}/preferred-name",
        json={"preferred_name": "Bobbie"},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    assert response.json()["preferred_name"] == "Bobbie"
    assert _contact_row(running_app, LOCAL_USER_ID, contact_id).preferred_name == "Bobbie"
    undone = await _undo(client)
    assert undone.json()["kind"] == "preferred_name"
    assert _contact_row(running_app, LOCAL_USER_ID, contact_id).preferred_name == original


async def test_an_empty_preferred_name_falls_back_to_the_first_name(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int
) -> None:
    first_name = _contact_row(running_app, LOCAL_USER_ID, contact_id).first_name
    response = await client.put(
        f"{TRIAGE}/contacts/{contact_id}/preferred-name",
        json={"preferred_name": ""},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    assert response.json()["preferred_name"] == first_name


async def test_renaming_a_contact_that_is_not_yours_is_not_found(
    running_app: FastAPI, client: httpx.AsyncClient, foreign: tuple[int, int]
) -> None:
    _owner, theirs = foreign
    response = await client.put(
        f"{TRIAGE}/contacts/{theirs}/preferred-name",
        json={"preferred_name": "Bobbie"},
        headers=CSRF,
    )
    assert response.status_code == 404


# --- the bulk suggestion ----------------------------------------------------


async def test_the_suggestion_is_a_preview_until_it_is_applied(
    running_app: FastAPI, client: httpx.AsyncClient, run_contacts: list[int]
) -> None:
    response = await client.get(f"{TRIAGE}/suggestions")
    assert response.status_code == 200, response.text
    (suggestion,) = response.json()
    assert suggestion["key"] == "met_with_messages"
    assert suggestion["count"] == RUN_LENGTH - 1  # the first contact has no messages
    assert str(suggestion["count"]) in suggestion["description"]
    assert all(
        _contact_row(running_app, LOCAL_USER_ID, cid).met is ContactMet.UNKNOWN
        for cid in run_contacts
    ), "looking at the suggestion changed nothing"


async def test_applying_the_suggestion_is_one_undoable_batch(
    running_app: FastAPI, client: httpx.AsyncClient, run_contacts: list[int]
) -> None:
    response = await client.post(
        f"{TRIAGE}/suggestions/met_with_messages/apply",
        json={"expected_count": RUN_LENGTH - 1},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == RUN_LENGTH - 1
    assert body["progress"]["remaining"] == 1
    undone = await _undo(client)
    assert undone.status_code == 200, undone.text
    assert undone.json()["decisions"] == RUN_LENGTH - 1
    assert undone.json()["card"] is None
    assert undone.json()["batch_id"] == body["batch_id"]
    assert all(
        _contact_row(running_app, LOCAL_USER_ID, cid).met is ContactMet.UNKNOWN
        for cid in run_contacts
    )


async def test_a_stale_count_stops_the_apply(
    running_app: FastAPI, client: httpx.AsyncClient, run_contacts: list[int]
) -> None:
    response = await client.post(
        f"{TRIAGE}/suggestions/met_with_messages/apply",
        json={"expected_count": 1},
        headers=CSRF,
    )
    assert response.status_code == 409, response.text
    assert all(
        _contact_row(running_app, LOCAL_USER_ID, cid).met is ContactMet.UNKNOWN
        for cid in run_contacts
    )


async def test_an_unknown_suggestion_key_is_not_found(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int
) -> None:
    response = await client.post(
        f"{TRIAGE}/suggestions/no-such-key/apply", json={"expected_count": 0}, headers=CSRF
    )
    assert response.status_code == 404, response.text


async def test_applying_without_the_count_the_banner_showed_is_refused(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int
) -> None:
    """The guard is not opt-in: there is no form of the request that skips it."""
    response = await client.post(
        f"{TRIAGE}/suggestions/met_with_messages/apply", json={}, headers=CSRF
    )
    assert response.status_code == 422, response.text
    assert _contact_row(running_app, LOCAL_USER_ID, contact_id).met is ContactMet.UNKNOWN


async def test_a_batch_refuses_to_reach_a_decision_somebody_made(
    running_app: FastAPI, client: httpx.AsyncClient, run_contacts: list[int]
) -> None:
    """`states` cannot point a batch at an answer the person gave (`met_source` laundering)."""
    assert (await _decide(client, run_contacts[1], "not_met")).status_code == 201
    decided = _contact_row(running_app, LOCAL_USER_ID, run_contacts[1])
    assert (decided.met, decided.met_source) == (ContactMet.NOT_MET, MetSource.MANUAL)

    for path, method in (
        (f"{TRIAGE}/suggestions", "GET"),
        (f"{TRIAGE}/suggestions/met_with_messages/contacts", "GET"),
    ):
        response = await client.request(method, path, params={"states": ["unknown", "not_met"]})
        assert response.status_code == 422, (path, response.text)
    applied = await client.post(
        f"{TRIAGE}/suggestions/met_with_messages/apply",
        json={"expected_count": RUN_LENGTH - 1},
        params={"states": ["unknown", "not_met"]},
        headers=CSRF,
    )
    assert applied.status_code == 422, applied.text

    after = _contact_row(running_app, LOCAL_USER_ID, run_contacts[1])
    assert (after.met, after.met_source) == (ContactMet.NOT_MET, MetSource.MANUAL)
    review = await _get(client, f"{TRIAGE}/next", decided_by="automatic")
    assert review["card"] is None, "their own answer did not become a batch's"


# --- the preview, and reviewing what was decided ----------------------------


async def test_a_suggestion_can_be_read_before_it_is_taken(
    running_app: FastAPI, client: httpx.AsyncClient, run_contacts: list[int]
) -> None:
    page = await _get(client, f"{TRIAGE}/suggestions/met_with_messages/contacts", limit=2)
    assert page["total"] == RUN_LENGTH - 1  # the first contact has no messages
    assert [item["id"] for item in page["items"]] == run_contacts[1:3]
    assert page["items"][0]["met"] == "unknown"
    assert page["items"][0]["met_source"] == "manual"
    assert (page["limit"], page["offset"]) == (2, 0)
    rest = await _get(client, f"{TRIAGE}/suggestions/met_with_messages/contacts", offset=2)
    assert [item["id"] for item in rest["items"]] == run_contacts[3:]
    still = await _get(client, f"{TRIAGE}/next")
    assert still["progress"]["triaged"] == 0, "reading the preview decided nobody"


async def test_previewing_an_unknown_suggestion_is_not_found(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int
) -> None:
    response = await client.get(f"{TRIAGE}/suggestions/no-such-key/contacts")
    assert response.status_code == 404, response.text


async def test_the_review_queue_walks_what_the_batch_decided(
    running_app: FastAPI, client: httpx.AsyncClient, run_contacts: list[int]
) -> None:
    """After a batch, `/triage/next?decided_by=automatic` is the pass over its work."""
    applied = await client.post(
        f"{TRIAGE}/suggestions/met_with_messages/apply",
        json={"expected_count": RUN_LENGTH - 1},
        headers=CSRF,
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert (body["applied"], body["met"]) == (RUN_LENGTH - 1, "met")
    assert body["progress"]["automatic"] == RUN_LENGTH - 1

    review = await _get(client, f"{TRIAGE}/next", decided_by="automatic")
    assert review["card"]["contact"]["id"] == run_contacts[1], "the first contact was not decided"
    assert review["card"]["contact"]["met_source"] == "automatic"
    assert review["next"]["contact"]["id"] == run_contacts[2]
    assert review["progress"]["remaining"] == RUN_LENGTH - 1

    # The review pass decides with ``decided_by`` too, so the prefetch is the
    # next contact still waiting to be reviewed rather than the next untriaged.
    corrected = await client.post(
        f"{TRIAGE}/decisions",
        json={"contact_id": run_contacts[1], "met": "not_met"},
        params={"decided_by": "automatic"},
        headers=CSRF,
    )
    assert corrected.status_code == 201, corrected.text
    assert corrected.json()["next"]["contact"]["id"] == run_contacts[2]
    after = await _get(client, f"{TRIAGE}/next", decided_by="automatic")
    assert after["card"]["contact"]["id"] == run_contacts[2], "the corrected one has left the queue"
    assert after["progress"]["automatic"] == RUN_LENGTH - 2
    assert _contact_row(running_app, LOCAL_USER_ID, run_contacts[1]).met is ContactMet.NOT_MET


async def test_undo_in_the_review_pass_counts_the_queue_it_was_pressed_in(
    client: httpx.AsyncClient, run_contacts: list[int]
) -> None:
    """The counters an undo hands back describe the queue the caller is looking at.

    All three routes take ``decided_by`` for the same reason: the number under
    the card is the one that queue is being served with. Without it here, the
    undo answered with every live ``met``/``not_met`` contact — every decision
    the person had ever made by hand included — so the count jumped on the
    press and settled again on the next read.
    """
    applied = await client.post(
        f"{TRIAGE}/suggestions/met_with_messages/apply",
        json={"expected_count": RUN_LENGTH - 1},
        headers=CSRF,
    )
    assert applied.status_code == 200, applied.text
    # A contact the batch never touched, answered by hand: it belongs to the
    # same states the review queue serves and must never be counted in it.
    mine = await client.post(
        f"{TRIAGE}/decisions", json={"contact_id": run_contacts[0], "met": "met"}, headers=CSRF
    )
    assert mine.status_code == 201, mine.text
    # One of the batch's, answered by hand, which is what takes it out.
    corrected = await client.post(
        f"{TRIAGE}/decisions",
        json={"contact_id": run_contacts[1], "met": "not_met"},
        params={"decided_by": "automatic"},
        headers=CSRF,
    )
    assert corrected.status_code == 201, corrected.text
    assert corrected.json()["progress"]["remaining"] == RUN_LENGTH - 2

    undone = await client.post(
        f"{TRIAGE}/undo",
        json={},
        params={"states": ["met", "not_met"], "decided_by": "automatic"},
        headers=CSRF,
    )
    assert undone.status_code == 200, undone.text
    # The undo put that contact back in the review queue, and the count says so.
    assert undone.json()["progress"]["remaining"] == RUN_LENGTH - 1
    assert undone.json()["progress"]["automatic"] == RUN_LENGTH - 1
    following = await _get(
        client, f"{TRIAGE}/next", states=["met", "not_met"], decided_by="automatic"
    )
    assert following["progress"]["remaining"] == undone.json()["progress"]["remaining"]


async def test_a_decision_records_the_batch_it_came_from(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int
) -> None:
    applied = await client.post(
        f"{TRIAGE}/suggestions/met_with_messages/apply", json={"expected_count": 1}, headers=CSRF
    )
    assert applied.status_code == 200, applied.text
    undone = await _undo(client)
    assert undone.status_code == 200, undone.text
    assert undone.json()["batch_id"] == applied.json()["batch_id"]
    assert _contact_row(running_app, LOCAL_USER_ID, contact_id).met is ContactMet.UNKNOWN


async def test_a_tag_the_user_gave_a_meaning_becomes_a_batch(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int
) -> None:
    # The default rule set ships a ``recruiter`` tag; giving it a meaning is
    # exactly the move this batch exists for.
    tags = {tag["name"]: tag for tag in (await client.get("/api/v1/tags")).json()}
    assert tags["recruiter"]["met_signal"] is None
    tag_id = tags["recruiter"]["id"]
    created = await client.patch(
        f"/api/v1/tags/{tag_id}", json={"met_signal": "not_met"}, headers=CSRF
    )
    assert created.status_code == 200, created.text
    assert created.json()["met_signal"] == "not_met"
    assert (
        await client.post(
            f"/api/v1/contacts/{contact_id}/tags", json={"tag_id": tag_id}, headers=CSRF
        )
    ).status_code == 201
    offers = {offer["key"]: offer for offer in (await client.get(f"{TRIAGE}/suggestions")).json()}
    key = f"tag:{tag_id}"
    assert offers[key]["count"] == 1 and offers[key]["met"] == "not_met"
    assert offers[key]["tag_id"] == tag_id
    # One contact, and the sentence about it is English. The count and the noun
    # agree through ``{people}``; the verb cannot, so the sentence is built not
    # to need it to. A tag batch with exactly one untriaged contact is the
    # ordinary case now that these are the only ``not_met`` batches there are.
    assert offers[key]["description"] == (
        "The tag recruiter is on 1 untriaged person, "
        "which you have said means you have not met them."
    )
    # The path the browser actually sends. `openapi-fetch` percent-encodes a
    # path parameter, so a tag batch's colon goes over the wire as `%3A`, and
    # every committed test until now used the unencoded form — which the router
    # also accepts, so neither the client nor the server was pinned to the same
    # string.
    encoded = f"tag%3A{tag_id}"
    assert quote(key, safe="") == encoded
    preview = await _get(client, f"{TRIAGE}/suggestions/{encoded}/contacts")
    assert [item["id"] for item in preview["items"]] == [contact_id]
    applied = await client.post(
        f"{TRIAGE}/suggestions/{encoded}/apply", json={"expected_count": 1}, headers=CSRF
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["key"] == key, "the service answers with the key, not the encoding"
    assert _contact_row(running_app, LOCAL_USER_ID, contact_id).met is ContactMet.NOT_MET


# --- two users --------------------------------------------------------------


async def test_another_user_is_never_served_or_allowed_to_act_on_these_contacts(
    running_app: FastAPI, client: httpx.AsyncClient, contact_id: int, as_user: Any
) -> None:
    """User B: not served A's contacts, and cannot decide, undo, or rename one."""
    with session_scope(_factory(running_app), write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED)
        other_id = other.id
    assert (await _decide(client, contact_id, "met")).status_code == 201

    as_user(other_id)
    served = await _get(client, f"{TRIAGE}/next")
    assert served["card"] is None, "B is served nothing of A's"
    assert served["progress"]["total"] == 0
    assert (await _decide(client, contact_id, "not_met")).status_code == 404
    assert (await _undo(client)).status_code == 404, "A's decision is not on B's undo stack"
    rename = await client.put(
        f"{TRIAGE}/contacts/{contact_id}/preferred-name",
        json={"preferred_name": "Mine now"},
        headers=CSRF,
    )
    assert rename.status_code == 404
    suggestions = await client.get(f"{TRIAGE}/suggestions")
    assert suggestions.json() == [], "A's message history suggests nothing to B"
    apply_response = await client.post(
        f"{TRIAGE}/suggestions/met_with_messages/apply", json={"expected_count": 0}, headers=CSRF
    )
    assert apply_response.json()["applied"] == 0
    preview = await client.get(f"{TRIAGE}/suggestions/met_with_messages/contacts")
    assert preview.json()["items"] == [], "B previews none of A's contacts"

    as_user(LOCAL_USER_ID)
    contact = _contact_row(running_app, LOCAL_USER_ID, contact_id)
    assert contact.met is ContactMet.MET, "A's decision survived every one of B's attempts"
    assert contact.preferred_name != "Mine now"
    assert isinstance(running_app.state.auth, FixedUser | LocalSingleUser)
