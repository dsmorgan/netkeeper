"""``/inbox``: detected replies, unsubscribes and bounces, and marking them handled (#300,
P3-11b). The two-user isolation of the list is the registry's (``tests/isolation``)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import factories
import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.models import (
    EnrollmentStatus,
    Message,
    MessageDirection,
    MessageStatus,
    User,
    UserKind,
)
from netkeeper.scoping import unscoped

CSRF = {"X-Netkeeper-Client": "1"}


def _at(day: int) -> datetime:
    return datetime(2030, 6, day, 12, 0, tzinfo=UTC)


def _reply(session: Session, enrollment: Any, day: int, **overrides: Any) -> Message:
    fields: dict[str, Any] = {
        "direction": MessageDirection.IN,
        "status": MessageStatus.RECEIVED,
        "subject": "Re: Catching up",
        "snippet": "Good to hear <b>from</b> you",
        "sent_at": _at(day),
    }
    fields.update(overrides)
    return factories.make_message(session, enrollment, **fields)


def _seed(app: FastAPI) -> dict[str, int]:
    """For the local user: a reply (day 3), an unsubscribe (day 5), a bounce (day 4) and
    a sent message; for another user, a reply."""
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        campaign = factories.make_campaign(session, user, name="Spring hello")
        contact = factories.make_contact(session, user, first_name="Rosalind", last_name="Q")
        replied = factories.make_enrollment(
            session, campaign, contact, status=EnrollmentStatus.REPLIED
        )
        bounced = factories.make_enrollment(
            session,
            campaign,
            factories.make_contact(session, user),
            status=EnrollmentStatus.BOUNCED,
        )
        sent = factories.make_message(session, replied)
        reply = _reply(session, replied, 3)
        unsubscribe = _reply(
            session, replied, 5, snippet="Please remove me from this", asks_unsubscribe=True
        )
        bounce = factories.make_message(
            session, bounced, status=MessageStatus.BOUNCED, bounced_at=_at(4)
        )
        other = factories.make_user(session, UserKind.HOSTED)
        theirs = factories.make_enrollment(
            session,
            factories.make_campaign(session, other),
            factories.make_contact(session, other),
        )
        foreign = _reply(session, theirs, 6)
        return {
            "sent": sent.id,
            "reply": reply.id,
            "unsubscribe": unsubscribe.id,
            "bounce": bounce.id,
            "foreign": foreign.id,
            "campaign": campaign.id,
            "contact": contact.id,
            "replied": replied.id,
        }


async def test_lists_every_kind_newest_first(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)

    page = (await client.get("/api/v1/inbox")).json()

    assert (page["total"], page["unhandled"]) == (3, 3)
    assert [(i["id"], i["kind"]) for i in page["items"]] == [
        (seed["unsubscribe"], "unsubscribe"),
        (seed["bounce"], "bounce"),
        (seed["reply"], "reply"),
    ]
    reply = page["items"][2]
    assert reply == {
        "id": seed["reply"],
        "kind": "reply",
        "contact_id": seed["contact"],
        "contact_name": "Rosalind Q",
        "campaign_id": seed["campaign"],
        "campaign_name": "Spring hello",
        "enrollment_id": seed["replied"],
        "enrollment_status": "replied",
        "subject": "Re: Catching up",
        "snippet": "Good to hear <b>from</b> you",  # as stored: the page shows it as text
        "received_at": "2030-06-03T12:00:00Z",
        "handled_at": None,
    }
    bounce = page["items"][1]
    assert (bounce["snippet"], bounce["enrollment_status"]) == (None, "bounced")
    assert bounce["received_at"] == "2030-06-04T12:00:00Z"


async def test_filters_by_kind_handled_and_enrollment(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)

    async def ids(**params: Any) -> list[int]:
        response = await client.get("/api/v1/inbox", params=params)
        assert response.status_code == 200, response.text
        return [item["id"] for item in response.json()["items"]]

    assert await ids(kind="reply") == [seed["reply"]]
    assert await ids(kind="unsubscribe") == [seed["unsubscribe"]]
    assert await ids(kind="bounce") == [seed["bounce"]]
    assert await ids(enrollment_id=seed["replied"]) == [seed["unsubscribe"], seed["reply"]]
    assert await ids(handled="true") == []
    assert len(await ids(handled="false")) == 3
    assert (await client.get("/api/v1/inbox", params={"kind": "sent"})).status_code == 422


async def test_paging_is_bounded(running_app: FastAPI, client: httpx.AsyncClient) -> None:
    seed = _seed(running_app)

    page = (await client.get("/api/v1/inbox", params={"limit": 1, "offset": 1})).json()

    assert (page["total"], [i["id"] for i in page["items"]]) == (3, [seed["bounce"]])
    for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}):
        assert (await client.get("/api/v1/inbox", params=params)).status_code == 422
    assert (await client.get("/api/v1/inbox", params={"limit": 200})).status_code == 200


async def test_mark_handled_is_idempotent_and_reversible(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    url = f"/api/v1/inbox/{seed['reply']}/handled"

    first = await client.put(url, json={"handled": True}, headers=CSRF)
    again = await client.put(url, json={"handled": True}, headers=CSRF)

    assert first.status_code == again.status_code == 200
    assert first.json()["handled_at"] is not None
    assert again.json()["handled_at"] == first.json()["handled_at"]
    page = (await client.get("/api/v1/inbox", params={"handled": "false"})).json()
    assert seed["reply"] not in [i["id"] for i in page["items"]]
    assert page["unhandled"] == 2
    handled = (await client.get("/api/v1/inbox", params={"handled": "true"})).json()
    assert [i["id"] for i in handled["items"]] == [seed["reply"]]

    undone = await client.put(url, json={"handled": False}, headers=CSRF)
    assert undone.json()["handled_at"] is None
    assert (await client.get("/api/v1/inbox")).json()["unhandled"] == 3


async def test_mark_handled_refuses_what_is_not_an_inbox_item_of_the_users(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)

    for message_id in (seed["sent"], seed["foreign"], 999_999):
        response = await client.put(
            f"/api/v1/inbox/{message_id}/handled", json={"handled": True}, headers=CSRF
        )
        assert response.status_code == 404, message_id
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory) as session:
        foreign = session.scalars(
            unscoped(select(Message).where(Message.id == seed["foreign"]))
        ).one()
        assert foreign.handled_at is None
