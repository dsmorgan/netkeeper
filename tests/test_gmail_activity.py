"""``GET /gmail/activity``: today's sends against the cap, and recent email (#449).

Two-user isolation is in ``tests/isolation``. Nothing here reaches Gmail: it is a read of
what the campaign engine stored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import factories
import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.models import (
    Mailbox,
    MessageDirection,
    MessageStatus,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.models.base import utcnow
from netkeeper.services import gmail_activity as service
from netkeeper.services import mailboxes as mailbox_service
from netkeeper.services.mailboxes import MAILBOX_HARD_MAX_PER_DAY


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
    # How many count as today's depends on the clock; the counting is tested at the
    # service level with a fixed time.
    assert (sends["email"], sends["daily_cap"], sends["lower_campaign_caps"]) == (
        "me@example.test",
        5,
        [],
    )
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


NOW = datetime(2026, 9, 16, 15, 0, tzinfo=UTC)


def _box(session: Session, user: User, **overrides: object) -> Mailbox:
    box = mailbox_service.connect(session, user, "me@example.test", "rt-fixed", daily_cap=80)
    for name, value in overrides.items():
        setattr(box, name, value)
    session.flush()
    return box


def test_the_day_is_the_users_local_day(session: Session) -> None:
    """02:00 UTC on the 16th is still the 15th in Los Angeles: it is not today's send."""
    user = factories.make_user(session, timezone="America/Los_Angeles")
    box = _box(session, user)
    campaign = factories.make_campaign(session, user, mailbox_id=box.id)
    enrollment = factories.make_enrollment(session, campaign, factories.make_contact(session, user))
    factories.make_message(session, enrollment, sent_at=datetime(2026, 9, 16, 2, 0, tzinfo=UTC))
    factories.make_message(session, enrollment, sent_at=datetime(2026, 9, 16, 8, 0, tzinfo=UTC))

    result = service.gmail_activity(session, user, now=NOW, settings=Settings())

    assert result.timezone == "America/Los_Angeles"
    assert result.day_start == datetime(2026, 9, 16, 7, 0, tzinfo=UTC)
    assert [box.sent_today for box in result.mailboxes] == [1]


def test_an_unreadable_time_zone_counts_utc_days(session: Session) -> None:
    user = factories.make_user(session, timezone="Not/AZone")
    _box(session, user)

    result = service.gmail_activity(session, user, now=NOW, settings=Settings())

    assert (result.timezone, result.day_start) == ("UTC", datetime(2026, 9, 16, tzinfo=UTC))


def test_the_cap_is_never_over_the_hard_maximum(session: Session) -> None:
    user = factories.make_user(session)
    _box(session, user, daily_cap=MAILBOX_HARD_MAX_PER_DAY + 500)

    [sends] = service.gmail_activity(session, user, now=NOW, settings=Settings()).mailboxes

    assert sends.daily_cap == 400 == MAILBOX_HARD_MAX_PER_DAY


def test_a_campaigns_lower_cap_is_named(session: Session) -> None:
    user = factories.make_user(session)
    box = _box(session, user)
    lower = factories.make_campaign(
        session, user, name="Small batch", mailbox_id=box.id, daily_cap=3
    )
    factories.make_campaign(session, user, name="Roomy", mailbox_id=box.id, daily_cap=80)
    enrollment = factories.make_enrollment(session, lower, factories.make_contact(session, user))
    factories.make_message(session, enrollment, sent_at=NOW)

    [sends] = service.gmail_activity(session, user, now=NOW, settings=Settings()).mailboxes

    [cap] = sends.lower_campaign_caps
    assert (cap.name, cap.sent_today, cap.daily_cap) == ("Small batch", 1, 3)


def test_the_backend_report_keys_the_four_gmail_posture_rows(session: Session) -> None:
    from netkeeper.models.base import utcnow as now_fn
    from netkeeper.services import posture
    from netkeeper.services.linkedin_accounts import account_id_for

    user = factories.make_user(session)
    report = posture.posture(
        session, user, account_id_for(session, user), now=now_fn(), settings=Settings(), probe=None
    )

    keys = {row.key for row in report.protections}
    assert {
        "gmail_reply_poll",
        "sending_hours",
        "next_campaign_send",
        "campaign_templates",
    } <= keys
