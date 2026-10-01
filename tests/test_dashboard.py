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

from netkeeper.crm.interactions import add_interaction
from netkeeper.db import session_scope
from netkeeper.models import (
    CampaignStatus,
    Contact,
    ContactSnapshot,
    ContactSource,
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


def test_next_fires_due_now_are_exactly_what_the_tick_selects(session: Session) -> None:
    """#286: ``upcoming`` and the tick share one selection, so they cannot drift.

    Every row the tick's own query (``_due``) would pick up now is listed, and
    every listed row due by now is one the tick would pick up.
    """
    user = factories.make_user(session)
    _due(session, user, NOW - timedelta(hours=1))
    _due(session, user, NOW)
    _due(session, user, NOW + timedelta(hours=1))
    _due(session, user, NOW - timedelta(hours=1), status=CampaignStatus.PAUSED)
    _due(session, user, NOW - timedelta(hours=1), channels=(TemplateChannel.LINKEDIN,))
    campaign = factories.make_campaign(session, user)
    for status in (EnrollmentStatus.PAUSED, EnrollmentStatus.PENDING, EnrollmentStatus.ACTIVE):
        factories.make_enrollment(
            session,
            campaign,
            factories.make_contact(session, user),
            status=status,
            next_action_at=NOW - timedelta(minutes=1),
        )
    factories.make_enrollment(session, campaign, factories.make_contact(session, user))

    fires, _ = campaign_engine.upcoming(session, user, limit=50)
    tick = campaign_engine._due(session, user, NOW, seen=(), campaigns=(), mailboxes=())

    listed_due = [fire.enrollment.id for fire in fires if fire.due <= NOW]
    assert listed_due == [enrollment.id for enrollment, _ in tick]
    assert len(listed_due) == 3


def test_a_step_due_exactly_now_is_the_ticks_not_the_next_due(session: Session) -> None:
    """``_next_due`` is the soonest *after* now: a row due at now is this tick's to take."""
    user = factories.make_user(session)
    _due(session, user, NOW)
    later = NOW + timedelta(minutes=5)
    _due(session, user, later)

    assert campaign_engine._next_due(session, user, NOW) == later
    [first, *_], _ = campaign_engine.upcoming(session, user, limit=10)
    assert first.due == NOW
    assert [
        e.id
        for e, _ in campaign_engine._due(session, user, NOW, seen=(), campaigns=(), mailboxes=())
    ] == [first.enrollment.id]


def test_next_fires_limit_keeps_the_total(session: Session) -> None:
    user = factories.make_user(session)
    for hours in range(4):
        _due(session, user, NOW + timedelta(hours=hours))

    fires, total = campaign_engine.upcoming(session, user, limit=2)

    assert len(fires) == 2
    assert total == 4


# --- changed jobs ----------------------------------------------------------------------


def _changed(session: Session, user: User) -> list[int]:
    rows, _ = service.changed_jobs(session, user, now=NOW, limit=50)
    return [row.contact.id for row in rows]


def _noticed(
    session: Session,
    user: User,
    at: datetime,
    *,
    contact_id: int | None = None,
    position_changed: bool = True,
    source: ContactSource = ContactSource.SYNC,
    **overrides: Any,
) -> int:
    """A contact (or ``contact_id``) with a snapshot netkeeper wrote at ``at``."""
    contact = (
        factories.make_contact(session, user, **overrides)
        if contact_id is None
        else session.get_one(Contact, contact_id)
    )
    contact.snapshots.append(
        ContactSnapshot(
            user_id=user.id,
            source=source,
            observed_at=at,
            current_title="Before",
            current_company="Old Co",
            position_changed=position_changed,
        )
    )
    session.flush()
    return contact.id


def test_a_position_change_noticed_in_the_last_30_days_is_a_changed_job(
    session: Session,
) -> None:
    user = factories.make_user(session)
    recent = _noticed(session, user, NOW - timedelta(days=3))
    older = _noticed(session, user, NOW - timedelta(days=10))
    edge = _noticed(session, user, NOW - timedelta(days=30))
    _noticed(session, user, NOW - timedelta(days=30, seconds=1))
    _noticed(session, user, NOW + timedelta(minutes=1))  # a clock skew is not "noticed yet"

    assert _changed(session, user) == [recent, older, edge]


def test_a_profile_start_date_alone_is_not_a_changed_job(session: Session) -> None:
    """#286: the card counts what netkeeper noticed, not the dates on the profile."""
    user = factories.make_user(session)
    factories.make_contact(session, user, positions=[{"started_on": TODAY - timedelta(days=3)}])
    factories.make_contact(
        session, user, positions=[{"started_on": date(2020, 1, 1), "ended_on": TODAY}]
    )

    assert _changed(session, user) == []


def test_a_new_headline_or_an_imported_job_is_not_a_changed_job(session: Session) -> None:
    """Only a sync's snapshot marked ``position_changed`` counts: an import notices nothing."""
    user = factories.make_user(session)
    _noticed(session, user, NOW - timedelta(days=1), position_changed=False)
    _noticed(session, user, NOW - timedelta(days=1), source=ContactSource.CSV)
    _noticed(session, user, NOW - timedelta(days=1), source=ContactSource.ARCHIVE)
    _noticed(session, user, NOW - timedelta(days=1), source=ContactSource.MANUAL)

    assert _changed(session, user) == []


def test_each_contact_is_listed_once_at_its_latest_notice(session: Session) -> None:
    user = factories.make_user(session)
    contact = _noticed(session, user, NOW - timedelta(days=20))
    _noticed(session, user, NOW - timedelta(days=2), contact_id=contact)
    _noticed(session, user, NOW - timedelta(days=1), contact_id=contact, position_changed=False)

    [row], total = service.changed_jobs(session, user, now=NOW, limit=10)

    assert row.contact.id == contact
    assert total == 1
    assert row.noticed_at == NOW - timedelta(days=2)


def test_archived_merged_and_do_not_contact_are_not_prompts(session: Session) -> None:
    user = factories.make_user(session)
    at = NOW - timedelta(days=1)
    live = _noticed(session, user, at)
    _noticed(session, user, at, archived_at=NOW)
    _noticed(session, user, at, merged_into_id=live)
    _noticed(session, user, at, do_not_contact=True)

    assert _changed(session, user) == [live]


def test_changed_jobs_limit_keeps_the_total(session: Session) -> None:
    user = factories.make_user(session)
    for days in range(3):
        _noticed(session, user, NOW - timedelta(days=days))

    rows, total = service.changed_jobs(session, user, now=NOW, limit=2)

    assert len(rows) == 2
    assert total == 3


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
    noticed = utcnow() - timedelta(days=4)
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        contact_id = _noticed(
            session,
            user,
            noticed,
            first_name="Fictional",
            last_name="Mover",
            current_title="Head of Tea",
            current_company="Kettle Ltd",
            # Started long ago by the profile's dates: the notice is what counts (#286).
            positions=[{"started_on": date(2025, 1, 1)}],
        )

    body = (await client.get("/api/v1/dashboard/changed-jobs")).json()

    [item] = body["items"]
    assert item == {
        "contact_id": contact_id,
        "contact_name": "Fictional Mover",
        "current_title": "Head of Tea",
        "current_company": "Kettle Ltd",
        "noticed_at": item["noticed_at"],
    }
    assert datetime.fromisoformat(item["noticed_at"]) == noticed
    assert (body["total"], body["days"]) == (1, 30)


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
