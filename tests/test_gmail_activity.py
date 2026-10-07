"""``GET /gmail/activity``: today's sends against the cap, and recent email (#449).

Two-user isolation is in ``tests/isolation``. Nothing here reaches Gmail: it is a read of
what the campaign engine stored.
"""

from __future__ import annotations

from datetime import timedelta

import factories
import httpx
from fastapi import FastAPI
from sqlalchemy import select

from netkeeper.db import session_scope
from netkeeper.models import (
    MessageDirection,
    MessageStatus,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.models.base import utcnow
from netkeeper.services import mailboxes as mailbox_service


async def test_a_user_who_never_connected_sees_nothing(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/gmail/activity")

    assert response.status_code == 200
    body = response.json()
    assert body["mailboxes"] == []
    assert body["recent"] == []
    assert body["timezone"]


async def test_sends_count_against_the_cap_and_recent_lists_newest_first(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    now = utcnow()
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        box = mailbox_service.connect(session, user, "me@example.test", "rt-activity", daily_cap=5)
        campaign = factories.make_campaign(session, user, name="Autumn hello", mailbox_id=box.id)
        contact = factories.make_contact(session, user, first_name="Fictional", last_name="Person")
        enrollment = factories.make_enrollment(session, campaign, contact)
        # Sent today (a failed one counts too), sent long ago, and a reply.
        factories.make_message(session, enrollment, sent_at=now - timedelta(seconds=30))
        factories.make_message(
            session,
            enrollment,
            status=MessageStatus.FAILED,
            error="Gmail refused",
            sent_at=now - timedelta(seconds=20),
        )
        factories.make_message(session, enrollment, sent_at=now - timedelta(days=3))
        reply = factories.make_message(
            session,
            enrollment,
            direction=MessageDirection.IN,
            status=MessageStatus.RECEIVED,
            sent_at=now - timedelta(seconds=10),
        )
        reply_id = reply.id
        # A LinkedIn message is not Gmail's.
        other = factories.make_campaign(
            session, user, channels=(TemplateChannel.LINKEDIN,), mailbox_id=None
        )
        factories.make_message(
            session, factories.make_enrollment(session, other, contact), sent_at=now
        )

    body = (await client.get("/api/v1/gmail/activity")).json()

    [sends] = body["mailboxes"]
    assert (sends["email"], sends["sent_today"], sends["daily_cap"]) == ("me@example.test", 2, 5)
    assert [row["direction"] for row in body["recent"]] == ["in", "out", "out", "out"]
    assert body["recent"][0]["id"] == reply_id
    assert body["recent"][0]["contact_name"] == "Fictional Person"
    assert body["recent"][0]["campaign_name"] == "Autumn hello"
    assert body["recent"][1]["error"] == "Gmail refused"
    assert body["recent"][1]["status"] == "failed"


async def test_a_disconnected_mailbox_is_not_listed(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        box = mailbox_service.connect(session, user, "old@example.test", "rt-old", daily_cap=80)
        mailbox_service.disconnect(session, user, box)

    assert (await client.get("/api/v1/gmail/activity")).json()["mailboxes"] == []
