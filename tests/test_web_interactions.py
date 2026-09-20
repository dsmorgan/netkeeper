"""The interactions, timeline, and notes routes (spec 8.1, 10.1, 14.1, 14.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.models import Contact, ContactSnapshot, Interaction, User, UserKind
from netkeeper.scoping import get_scoped, unscoped

CSRF = {"X-Netkeeper-Client": "1"}
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
EARLIER = NOW - timedelta(days=1)
LOCAL_USER_ID = 1


def _factory(app: FastAPI) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = app.state.session_factory
    return factory


def _local_user(session: Session) -> User:
    user = session.get(User, LOCAL_USER_ID)
    assert user is not None
    return user


@pytest.fixture
def contact_id(running_app: FastAPI) -> int:
    """A contact of the local user."""
    with session_scope(_factory(running_app), write=True) as session:
        return factories.make_contact(session, _local_user(session)).id


@pytest.fixture
def foreign(running_app: FastAPI) -> tuple[int, int]:
    """Another user's contact and one interaction on it: ``(contact_id, interaction_id)``."""
    with session_scope(_factory(running_app), write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED)
        contact = factories.make_contact(session, other)
        row = Interaction(
            user_id=other.id, contact_id=contact.id, kind="note", at=NOW, summary="theirs"
        )
        session.add(row)
        session.flush()
        return contact.id, row.id


def _contact(app: FastAPI, contact_id: int) -> Contact:
    with session_scope(_factory(app)) as session:
        contact = get_scoped(session, _local_user(session), Contact, contact_id)
        assert contact is not None
        return contact


def _interaction_count(app: FastAPI) -> int:
    with session_scope(_factory(app)) as session:
        return len(session.scalars(unscoped(select(Interaction))).all())


async def _create(
    client: httpx.AsyncClient, contact_id: int, kind: str, at: datetime, **fields: Any
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/contacts/{contact_id}/interactions",
        json={"kind": kind, "at": at.isoformat(), **fields},
        headers=CSRF,
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# --- create -----------------------------------------------------------------


async def test_create_returns_the_row_and_lists_it(
    client: httpx.AsyncClient, contact_id: int
) -> None:
    body = await _create(client, contact_id, "note", NOW, summary="met at a meetup", message_id=5)
    assert body["contact_id"] == contact_id
    assert body["kind"] == "note"
    assert body["summary"] == "met at a meetup"
    assert body["message_id"] == 5
    assert body["source"] == "manual"
    assert datetime.fromisoformat(body["at"]) == NOW
    assert body["created_at"] and body["updated_at"]

    listed = await client.get(f"/api/v1/contacts/{contact_id}/interactions")
    assert listed.status_code == 200
    assert listed.json() == {"items": [body], "total": 1}


async def test_create_with_the_minimum_body(client: httpx.AsyncClient, contact_id: int) -> None:
    body = await _create(client, contact_id, "li_view", NOW)
    assert body["summary"] is None and body["message_id"] is None


async def test_an_outbound_interaction_moves_last_contacted(
    client: httpx.AsyncClient, running_app: FastAPI, contact_id: int
) -> None:
    assert _contact(running_app, contact_id).last_contacted_at is None
    await _create(client, contact_id, "email_out", NOW)
    assert _contact(running_app, contact_id).last_contacted_at == NOW
    await _create(client, contact_id, "call", EARLIER)
    assert _contact(running_app, contact_id).last_contacted_at == NOW
    await _create(client, contact_id, "li_in", LATER)
    assert _contact(running_app, contact_id).last_contacted_at == NOW


async def test_create_rejects_a_naive_time_and_an_unknown_kind(
    client: httpx.AsyncClient, running_app: FastAPI, contact_id: int
) -> None:
    naive = await client.post(
        f"/api/v1/contacts/{contact_id}/interactions",
        json={"kind": "note", "at": NOW.replace(tzinfo=None).isoformat()},
        headers=CSRF,
    )
    assert naive.status_code == 422
    unknown = await client.post(
        f"/api/v1/contacts/{contact_id}/interactions",
        json={"kind": "fax", "at": NOW.isoformat()},
        headers=CSRF,
    )
    assert unknown.status_code == 422
    assert _interaction_count(running_app) == 0


async def test_create_on_another_users_or_a_missing_contact_is_404(
    client: httpx.AsyncClient, running_app: FastAPI, foreign: tuple[int, int]
) -> None:
    theirs, _ = foreign
    for target in (theirs, theirs + 1000):
        response = await client.post(
            f"/api/v1/contacts/{target}/interactions",
            json={"kind": "note", "at": NOW.isoformat()},
            headers=CSRF,
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "no such contact"}
    assert _interaction_count(running_app) == 1  # theirs, untouched


# --- list -------------------------------------------------------------------


async def test_list_pages_newest_first(client: httpx.AsyncClient, contact_id: int) -> None:
    oldest = await _create(client, contact_id, "note", EARLIER)
    newest = await _create(client, contact_id, "call", LATER)
    middle = await _create(client, contact_id, "note", NOW)

    page = await client.get(f"/api/v1/contacts/{contact_id}/interactions", params={"limit": 2})
    assert page.json() == {"items": [newest, middle], "total": 3}
    page = await client.get(
        f"/api/v1/contacts/{contact_id}/interactions", params={"limit": 2, "offset": 2}
    )
    assert page.json() == {"items": [oldest], "total": 3}


async def test_list_validates_its_page_parameters(
    client: httpx.AsyncClient, contact_id: int
) -> None:
    for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}):
        response = await client.get(f"/api/v1/contacts/{contact_id}/interactions", params=params)
        assert response.status_code == 422, params


async def test_list_of_another_users_contact_is_404(
    client: httpx.AsyncClient, foreign: tuple[int, int]
) -> None:
    theirs, _ = foreign
    response = await client.get(f"/api/v1/contacts/{theirs}/interactions")
    assert response.status_code == 404
    assert response.json() == {"detail": "no such contact"}


# --- update -----------------------------------------------------------------


async def test_patch_changes_only_what_is_sent(
    client: httpx.AsyncClient, running_app: FastAPI, contact_id: int
) -> None:
    created = await _create(client, contact_id, "call", LATER, summary="first", message_id=3)
    assert _contact(running_app, contact_id).last_contacted_at == LATER

    response = await client.patch(
        f"/api/v1/interactions/{created['id']}", json={"summary": "second"}, headers=CSRF
    )
    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == "second"
    assert body["message_id"] == 3 and body["kind"] == "call"

    response = await client.patch(
        f"/api/v1/interactions/{created['id']}",
        json={"summary": None, "message_id": None, "kind": None, "at": None},
        headers=CSRF,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["summary"] is None and body["message_id"] is None
    assert body["kind"] == "call" and datetime.fromisoformat(body["at"]) == LATER

    response = await client.patch(
        f"/api/v1/interactions/{created['id']}",
        json={"kind": "note", "at": EARLIER.isoformat()},
        headers=CSRF,
    )
    assert response.status_code == 200
    assert response.json()["kind"] == "note"
    assert _contact(running_app, contact_id).last_contacted_at is None


async def test_patch_rejects_a_naive_time(client: httpx.AsyncClient, contact_id: int) -> None:
    created = await _create(client, contact_id, "note", NOW)
    response = await client.patch(
        f"/api/v1/interactions/{created['id']}",
        json={"at": NOW.replace(tzinfo=None).isoformat()},
        headers=CSRF,
    )
    assert response.status_code == 422


async def test_patch_of_another_users_interaction_is_404(
    client: httpx.AsyncClient, running_app: FastAPI, foreign: tuple[int, int]
) -> None:
    _, theirs = foreign
    for target in (theirs, theirs + 1000):
        response = await client.patch(
            f"/api/v1/interactions/{target}", json={"summary": "mine now"}, headers=CSRF
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "no such interaction"}
    with session_scope(_factory(running_app)) as session:
        row = session.scalars(unscoped(select(Interaction))).one()
        assert row.summary == "theirs"


# --- delete -----------------------------------------------------------------


async def test_delete_removes_the_row_and_recomputes(
    client: httpx.AsyncClient, running_app: FastAPI, contact_id: int
) -> None:
    older = await _create(client, contact_id, "email_out", NOW)
    newer = await _create(client, contact_id, "meeting", LATER)
    assert _contact(running_app, contact_id).last_contacted_at == LATER

    response = await client.delete(f"/api/v1/interactions/{newer['id']}", headers=CSRF)
    assert response.status_code == 204
    assert response.content == b""
    assert _contact(running_app, contact_id).last_contacted_at == NOW
    listed = await client.get(f"/api/v1/contacts/{contact_id}/interactions")
    assert listed.json() == {"items": [older], "total": 1}

    again = await client.delete(f"/api/v1/interactions/{newer['id']}", headers=CSRF)
    assert again.status_code == 404


async def test_delete_of_another_users_interaction_is_404(
    client: httpx.AsyncClient, running_app: FastAPI, foreign: tuple[int, int]
) -> None:
    _, theirs = foreign
    response = await client.delete(f"/api/v1/interactions/{theirs}", headers=CSRF)
    assert response.status_code == 404
    assert response.json() == {"detail": "no such interaction"}
    assert _interaction_count(running_app) == 1


# --- timeline ---------------------------------------------------------------


def _snapshot(app: FastAPI, contact_id: int, observed_at: datetime, headline: str) -> int:
    with session_scope(_factory(app), write=True) as session:
        row = ContactSnapshot(
            user_id=LOCAL_USER_ID, contact_id=contact_id, observed_at=observed_at, headline=headline
        )
        session.add(row)
        session.flush()
        return row.id


async def test_timeline_interleaves_and_pages_by_cursor(
    client: httpx.AsyncClient, running_app: FastAPI, contact_id: int
) -> None:
    note = await _create(client, contact_id, "note", NOW, summary="met")
    call = await _create(client, contact_id, "call", EARLIER)
    new_job = _snapshot(running_app, contact_id, LATER, "New job")
    old_job = _snapshot(running_app, contact_id, EARLIER - timedelta(days=1), "Old job")

    response = await client.get(f"/api/v1/contacts/{contact_id}/timeline")
    assert response.status_code == 200
    body = response.json()
    assert body["next_before"] is None
    assert [(entry["kind"], entry["at"]) for entry in body["items"]] == [
        ("snapshot", LATER.isoformat().replace("+00:00", "Z")),
        ("interaction", NOW.isoformat().replace("+00:00", "Z")),
        ("interaction", EARLIER.isoformat().replace("+00:00", "Z")),
        ("snapshot", (EARLIER - timedelta(days=1)).isoformat().replace("+00:00", "Z")),
    ]
    assert body["items"][0]["snapshot"]["id"] == new_job
    assert body["items"][0]["snapshot"]["headline"] == "New job"
    assert "interaction" not in body["items"][0]
    assert body["items"][1]["interaction"] == note
    assert body["items"][2]["interaction"] == call
    assert body["items"][3]["snapshot"]["id"] == old_job

    seen: list[str] = []
    before: str | None = None
    for _ in range(10):
        params: dict[str, str | int] = {"limit": 2}
        if before is not None:
            params["before"] = before
        page = (await client.get(f"/api/v1/contacts/{contact_id}/timeline", params=params)).json()
        seen += [entry["at"] for entry in page["items"]]
        before = page["next_before"]
        if before is None:
            break
    assert seen == [entry["at"] for entry in body["items"]]


async def test_timeline_validates_its_cursor(client: httpx.AsyncClient, contact_id: int) -> None:
    naive = await client.get(
        f"/api/v1/contacts/{contact_id}/timeline",
        params={"before": NOW.replace(tzinfo=None).isoformat()},
    )
    assert naive.status_code == 422
    garbage = await client.get(
        f"/api/v1/contacts/{contact_id}/timeline", params={"before": "yesterday"}
    )
    assert garbage.status_code == 422
    empty = await client.get(f"/api/v1/contacts/{contact_id}/timeline", params={"limit": 0})
    assert empty.status_code == 422


async def test_timeline_of_another_users_contact_is_404(
    client: httpx.AsyncClient, foreign: tuple[int, int]
) -> None:
    theirs, _ = foreign
    response = await client.get(f"/api/v1/contacts/{theirs}/timeline")
    assert response.status_code == 404
    assert response.json() == {"detail": "no such contact"}


# --- notes ------------------------------------------------------------------


async def test_put_notes_replaces_them_whole(
    client: httpx.AsyncClient, running_app: FastAPI, contact_id: int
) -> None:
    text = "# Met at PyCon\n\n- likes **Rust**  \n"
    response = await client.put(
        f"/api/v1/contacts/{contact_id}/notes", json={"notes": text}, headers=CSRF
    )
    assert response.status_code == 200
    body = response.json()
    assert body["contact_id"] == contact_id
    assert body["notes"] == text
    assert body["updated_at"]
    assert _contact(running_app, contact_id).notes == text

    response = await client.put(
        f"/api/v1/contacts/{contact_id}/notes", json={"notes": None}, headers=CSRF
    )
    assert response.status_code == 200
    assert response.json()["notes"] is None
    assert _contact(running_app, contact_id).notes is None

    response = await client.put(f"/api/v1/contacts/{contact_id}/notes", json={}, headers=CSRF)
    assert response.status_code == 422


async def test_put_notes_on_another_users_contact_is_404(
    client: httpx.AsyncClient, running_app: FastAPI, foreign: tuple[int, int]
) -> None:
    theirs, _ = foreign
    response = await client.put(
        f"/api/v1/contacts/{theirs}/notes", json={"notes": "mine now"}, headers=CSRF
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "no such contact"}
    with session_scope(_factory(running_app)) as session:
        row = session.scalars(unscoped(select(Contact).where(Contact.id == theirs))).one()
        assert row.notes is None


# --- CSRF -------------------------------------------------------------------


async def test_state_changing_routes_need_the_client_header(
    client: httpx.AsyncClient, contact_id: int
) -> None:
    created = await _create(client, contact_id, "note", NOW)
    interaction = created["id"]
    attempts = [
        client.post(
            f"/api/v1/contacts/{contact_id}/interactions",
            json={"kind": "note", "at": NOW.isoformat()},
        ),
        client.patch(f"/api/v1/interactions/{interaction}", json={"summary": "x"}),
        client.delete(f"/api/v1/interactions/{interaction}"),
        client.put(f"/api/v1/contacts/{contact_id}/notes", json={"notes": "x"}),
    ]
    for attempt in attempts:
        response = await attempt
        assert response.status_code == 403, response.text
        assert response.json()["rule"] == "client-header"
    listed = await client.get(f"/api/v1/contacts/{contact_id}/interactions")
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["summary"] is None
