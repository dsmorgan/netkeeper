"""``/do-not-send``: list, add by hand, and remove (#238, Part B). The two-user isolation
of the list is the registry's (``tests/isolation``)."""

from __future__ import annotations

import factories
import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import do_not_send
from netkeeper.db import session_scope
from netkeeper.models import DoNotSendReason, User, UserKind

CSRF = {"X-Netkeeper-Client": "1"}


def _seed(app: FastAPI) -> dict[str, int]:
    """For the local user, a bounced address found on a contact; for another user, one entry."""
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        contact = factories.make_contact(session, user, emails=["ada@example.test"])
        bounced = do_not_send.add(
            session, user, "ada@example.test", DoNotSendReason.BOUNCED, contact_id=contact.id
        )
        other = factories.make_user(session, UserKind.HOSTED)
        theirs = do_not_send.add_by_hand(session, other, "theirs@example.test")
        return {"contact": contact.id, "bounced": bounced.id, "theirs": theirs.id}


async def test_list_add_and_remove(running_app: FastAPI, client: httpx.AsyncClient) -> None:
    seed = _seed(running_app)

    added = await client.post(
        "/api/v1/do-not-send", json={"email": " Name+NK1@Example.test "}, headers=CSRF
    )
    assert added.status_code == 201, added.text
    assert added.json()["email"] == "name+nk1@example.test"  # the +tag is kept

    listed = (await client.get("/api/v1/do-not-send")).json()
    assert [(e["email"], e["reason"], e["contact_id"]) for e in listed] == [
        ("name+nk1@example.test", "manual", None),
        ("ada@example.test", "bounced", seed["contact"]),
    ]

    removed = await client.delete(f"/api/v1/do-not-send/{seed['bounced']}", headers=CSRF)
    assert removed.status_code == 204
    listed = (await client.get("/api/v1/do-not-send")).json()
    assert [e["email"] for e in listed] == ["name+nk1@example.test"]


async def test_adding_an_address_already_there_keeps_its_reason(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    added = await client.post(
        "/api/v1/do-not-send", json={"email": "ADA@example.test"}, headers=CSRF
    )
    assert added.status_code == 201
    assert (added.json()["id"], added.json()["reason"]) == (seed["bounced"], "bounced")


async def test_a_bad_address_is_refused(running_app: FastAPI, client: httpx.AsyncClient) -> None:
    for bad in ("", "a@x.test, b@y.test", "Eve <eve@example.test>"):
        response = await client.post("/api/v1/do-not-send", json={"email": bad}, headers=CSRF)
        assert response.status_code == 422, bad
    assert (await client.get("/api/v1/do-not-send")).json() == []


async def test_another_users_entry_and_a_missing_one_are_not_found(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    for entry_id in (seed["theirs"], 999_999):
        response = await client.delete(f"/api/v1/do-not-send/{entry_id}", headers=CSRF)
        assert response.status_code == 404
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory) as session:
        other = session.scalars(select(User).where(User.kind == UserKind.HOSTED)).one()
        assert [e.id for e in do_not_send.entries(session, other)] == [seed["theirs"]]
