"""The dashboard's own reads (P3-12): next campaign fires, changed jobs, inbound this week.

Everything else the dashboard shows comes from an endpoint with its own tests
(``/linkedin/*``, ``/mailboxes``). The two-user isolation of these three is in
``tests/isolation``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import factories
import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.templates import contact_fields
from netkeeper.crm.interactions import add_interaction
from netkeeper.db import session_scope
from netkeeper.models import (
    CampaignStatus,
    EnrollmentStatus,
    InteractionKind,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.models.base import utcnow
from netkeeper.services import campaign_engine
from netkeeper.services import dashboard as service

NOW = datetime(2026, 9, 16, 15, 0, tzinfo=UTC)
TODAY = date(2026, 9, 16)


def test_the_windows_are_pinned() -> None:
    """Spec 9.8 names the changed-jobs window; "this week" is the last seven days."""
    assert service.CHANGED_JOBS_DAYS == 30
    assert timedelta(days=7) == service.INBOUND_WINDOW


# --- next fires ------------------------------------------------------------------------


def _due(session: Session, user: User, at: datetime, **campaign: Any) -> int:
    row = factories.make_campaign(session, user, **campaign)
    contact = factories.make_contact(session, user)
    return factories.make_enrollment(session, row, contact, next_action_at=at).id


def test_next_fires_are_soonest_first_and_a_due_one_leads(session: Session) -> None:
    user = factories.make_user(session)
    later = _due(session, user, NOW + timedelta(days=2))
    soon = _due(session, user, NOW + timedelta(hours=1))
    overdue = _due(session, user, NOW - timedelta(minutes=5))

    fires, total = campaign_engine.upcoming(session, user, limit=10)

    assert [fire.enrollment.id for fire in fires] == [overdue, soon, later]
    assert total == 3


def test_next_fires_list_only_what_the_tick_would_fire(session: Session) -> None:
    """Status selects, never the due time alone (#242 review); LinkedIn steps wait for P4."""
    user = factories.make_user(session)
    listed = _due(session, user, NOW)
    _due(session, user, NOW, status=CampaignStatus.PAUSED)
    _due(session, user, NOW, channels=(TemplateChannel.LINKEDIN,))
    campaign = factories.make_campaign(session, user)
    for status in (EnrollmentStatus.PAUSED, EnrollmentStatus.PENDING, EnrollmentStatus.REPLIED):
        factories.make_enrollment(
            session,
            campaign,
            factories.make_contact(session, user),
            status=status,
            next_action_at=NOW,
        )
    factories.make_enrollment(session, campaign, factories.make_contact(session, user))

    fires, total = campaign_engine.upcoming(session, user, limit=10)

    assert [fire.enrollment.id for fire in fires] == [listed]
    assert total == 1


def test_next_fires_name_the_next_step(session: Session) -> None:
    user = factories.make_user(session)
    campaign = factories.make_campaign(
        session, user, channels=(TemplateChannel.EMAIL, TemplateChannel.EMAIL)
    )
    contact = factories.make_contact(session, user)
    factories.make_enrollment(session, campaign, contact, current_step=1, next_action_at=NOW)

    [fire], _ = campaign_engine.upcoming(session, user, limit=10)

    assert fire.step is not None
    assert fire.step.position == 2
    assert fire.due == NOW


def test_next_fires_agree_with_the_ticks_own_next_due(session: Session) -> None:
    """The soonest future row is the one the engine's own ``_next_due`` names."""
    user = factories.make_user(session)
    _due(session, user, NOW + timedelta(hours=3))
    _due(session, user, NOW + timedelta(hours=1))
    _due(session, user, NOW + timedelta(hours=1), channels=(TemplateChannel.LINKEDIN,))

    fires, _ = campaign_engine.upcoming(session, user, limit=1)

    assert fires[0].due == campaign_engine._next_due(session, user, NOW)


def test_next_fires_limit_keeps_the_total(session: Session) -> None:
    user = factories.make_user(session)
    for hours in range(4):
        _due(session, user, NOW + timedelta(hours=hours))

    fires, total = campaign_engine.upcoming(session, user, limit=2)

    assert len(fires) == 2
    assert total == 4


# --- changed jobs ----------------------------------------------------------------------


def _changed(session: Session, user: User) -> list[int]:
    rows, _ = service.changed_jobs(session, user, today=TODAY, limit=50)
    return [row.contact.id for row in rows]


def _with(session: Session, user: User, *positions: dict[str, Any], **overrides: Any) -> int:
    return factories.make_contact(session, user, positions=positions, **overrides).id


def test_a_start_or_an_end_in_the_last_30_days_is_a_changed_job(session: Session) -> None:
    user = factories.make_user(session)
    started = _with(session, user, {"started_on": TODAY - timedelta(days=3)})
    left = _with(
        session,
        user,
        {"started_on": date(2020, 1, 1), "ended_on": TODAY - timedelta(days=10)},
    )
    edge = _with(session, user, {"started_on": TODAY - timedelta(days=30)})
    _with(session, user, {"started_on": TODAY - timedelta(days=31)})
    _with(session, user, {"started_on": None})
    _with(session, user)

    assert _changed(session, user) == [started, left, edge]


def test_an_announced_future_start_is_not_a_change_yet(session: Session) -> None:
    """#255: a position starting after today has not happened."""
    user = factories.make_user(session)
    _with(session, user, {"started_on": TODAY + timedelta(days=1)})
    _with(session, user, {"started_on": date(2020, 1, 1), "ended_on": TODAY + timedelta(days=5)})

    assert _changed(session, user) == []


def test_each_contact_is_listed_once_at_its_latest_change(session: Session) -> None:
    user = factories.make_user(session)
    contact = factories.make_contact(
        session,
        user,
        positions=[
            {"started_on": TODAY - timedelta(days=2)},
            {"started_on": date(2021, 1, 1), "ended_on": TODAY - timedelta(days=20)},
            {"started_on": TODAY + timedelta(days=9)},
        ],
    )

    [row], total = service.changed_jobs(session, user, today=TODAY, limit=10)

    assert row.contact.id == contact.id
    assert total == 1
    # The same date the merge field gives (#232, #255): one meaning of the phrase.
    assert row.changed_on == contact_fields(contact, TODAY)["last_position_change"]


def test_archived_merged_and_do_not_contact_are_not_prompts(session: Session) -> None:
    user = factories.make_user(session)
    recent = {"started_on": TODAY - timedelta(days=1)}
    live = _with(session, user, recent)
    _with(session, user, recent, archived_at=NOW)
    _with(session, user, recent, merged_into_id=live)
    _with(session, user, recent, do_not_contact=True)

    assert _changed(session, user) == [live]


def test_changed_jobs_limit_keeps_the_total(session: Session) -> None:
    user = factories.make_user(session)
    for days in range(3):
        _with(session, user, {"started_on": TODAY - timedelta(days=days)})

    rows, total = service.changed_jobs(session, user, today=TODAY, limit=2)

    assert len(rows) == 2
    assert total == 3


def test_today_is_the_users_own_date(session: Session) -> None:
    late_evening_in_la = datetime(2026, 9, 17, 3, 0, tzinfo=UTC)
    west = factories.make_user(session, timezone="America/Los_Angeles")
    unknown = factories.make_user(session, timezone="Nowhere/Special")

    assert service.local_today(west, late_evening_in_la) == date(2026, 9, 16)
    assert service.local_today(unknown, late_evening_in_la) == date(2026, 9, 17)


# --- inbound this week -----------------------------------------------------------------


def test_inbound_counts_email_and_linkedin_in_over_the_last_seven_days(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        _inbound(session)


def _inbound(session: Session) -> None:
    user = factories.make_user(session)
    contact = factories.make_contact(session, user)

    def add(kind: InteractionKind, at: datetime) -> None:
        add_interaction(session, user, contact.id, kind, at, "fake")

    add(InteractionKind.EMAIL_IN, NOW - timedelta(days=1))
    add(InteractionKind.LI_IN, NOW - timedelta(days=6, hours=23))
    add(InteractionKind.EMAIL_IN, NOW - timedelta(days=8))
    add(InteractionKind.EMAIL_IN, NOW + timedelta(days=1))
    add(InteractionKind.EMAIL_OUT, NOW - timedelta(days=1))
    add(InteractionKind.LI_OUT, NOW - timedelta(days=1))
    add(InteractionKind.NOTE, NOW - timedelta(days=1))

    count, since = service.inbound_since(session, user, now=NOW)

    assert count == 2
    assert since == NOW - timedelta(days=7)


# --- the endpoints ---------------------------------------------------------------------


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


async def test_next_fires_endpoint_names_the_contact_and_nothing_more(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    due = utcnow() + timedelta(hours=2)
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        campaign = factories.make_campaign(session, user, name="Autumn hello")
        contact = factories.make_contact(
            session,
            user,
            first_name="Fictional",
            preferred_name="Fic",
            last_name="Person",
            emails=["fic@example.test"],
        )
        enrollment = factories.make_enrollment(session, campaign, contact, next_action_at=due)
        ids = (enrollment.id, campaign.id, contact.id)

    response = await client.get("/api/v1/dashboard/next-fires")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    [item] = body["items"]
    assert item == {
        "enrollment_id": ids[0],
        "due": item["due"],
        "campaign_id": ids[1],
        "campaign_name": "Autumn hello",
        "step_position": 1,
        "channel": "email",
        "contact_id": ids[2],
        "contact_name": "Fic Person",
    }
    assert datetime.fromisoformat(item["due"]) == due
    assert "fic@example.test" not in response.text


async def test_next_fires_endpoint_bounds_its_limit(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/dashboard/next-fires?limit=0")).status_code == 422
    assert (await client.get("/api/v1/dashboard/next-fires?limit=51")).status_code == 422
    empty = (await client.get("/api/v1/dashboard/next-fires")).json()
    assert empty == {"items": [], "total": 0}


async def test_changed_jobs_endpoint(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        today = service.local_today(user, utcnow())
        contact = factories.make_contact(
            session,
            user,
            first_name="Fictional",
            last_name="Mover",
            current_title="Head of Tea",
            current_company="Kettle Ltd",
            positions=[{"started_on": today - timedelta(days=4)}],
        )
        contact_id = contact.id

    body = (await client.get("/api/v1/dashboard/changed-jobs")).json()

    assert body == {
        "items": [
            {
                "contact_id": contact_id,
                "contact_name": "Fictional Mover",
                "current_title": "Head of Tea",
                "current_company": "Kettle Ltd",
                "changed_on": (today - timedelta(days=4)).isoformat(),
            }
        ],
        "total": 1,
        "days": 30,
    }


async def test_inbound_endpoint_says_replies_are_not_detected_yet(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        contact = factories.make_contact(session, user)
        add_interaction(
            session, user, contact.id, InteractionKind.LI_IN, utcnow() - timedelta(hours=1), "hi"
        )

    body = (await client.get("/api/v1/dashboard/inbound")).json()

    assert body["count"] == 1
    assert body["reply_detection"] is False
    assert datetime.fromisoformat(body["since"]) < utcnow() - timedelta(days=6)
