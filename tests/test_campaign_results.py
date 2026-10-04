"""A campaign's results (#350): sends per local day, replies, bounces and opt-outs per step.

The service is read straight; ``GET /campaigns/{id}/results`` once for its shape and
once for another user's campaign. ``netkeeper campaigns status`` has its own tests in
``test_cli_campaigns.py``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.models import (
    Campaign,
    Enrollment,
    Message,
    MessageDirection,
    MessageStatus,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.services import campaign_results
from netkeeper.services.campaigns import CampaignNotFound

EMAIL3 = (TemplateChannel.EMAIL, TemplateChannel.EMAIL, TemplateChannel.EMAIL)
T0 = datetime(2030, 6, 3, 14, 0, tzinfo=UTC)  # a Monday, 10:00 in New York
NOW = datetime(2030, 6, 20, 16, 0, tzinfo=UTC)


def _campaign(session: Session, user: User) -> Campaign:
    return factories.make_campaign(session, user, channels=EMAIL3)


def _enroll(session: Session, campaign: Campaign, **overrides: object) -> Enrollment:
    contact = factories.make_contact(session, campaign_user(session, campaign))
    return factories.make_enrollment(session, campaign, contact, **overrides)


def campaign_user(session: Session, campaign: Campaign) -> User:
    return session.get_one(User, campaign.user_id)


def _send(
    session: Session,
    enrollment: Enrollment,
    position: int,
    at: datetime | None,
    **overrides: object,
) -> Message:
    return factories.make_message(session, enrollment, position=position, sent_at=at, **overrides)


def _reply(
    session: Session, enrollment: Enrollment, at: datetime, *, unsubscribe: bool = False
) -> Message:
    message = Message(
        user_id=enrollment.user_id,
        enrollment_id=enrollment.id,
        contact_id=enrollment.contact_id,
        channel=TemplateChannel.EMAIL,
        direction=MessageDirection.IN,
        status=MessageStatus.RECEIVED,
        asks_unsubscribe=unsubscribe,
        sent_at=at,
    )
    session.add(message)
    enrollment.replied_at = enrollment.replied_at or at
    session.flush()
    return message


def _results(session: Session, user: User, campaign: Campaign) -> campaign_results.CampaignResults:
    return campaign_results.campaign_results(session, user, campaign.id, now=NOW)


def _per_step(results: campaign_results.CampaignResults, field: str) -> list[int]:
    return [getattr(s, field) for s in results.steps]


def test_a_reply_counts_against_the_last_step_sent_before_it(session: Session) -> None:
    user = factories.make_user(session)
    campaign = _campaign(session, user)
    # Replied after step 2.
    after_two = _enroll(session, campaign)
    _send(session, after_two, 1, T0)
    _send(session, after_two, 2, T0 + timedelta(days=7))
    _reply(session, after_two, T0 + timedelta(days=8))
    # Replied while step 3 was scheduled but not yet sent: still step 2's.
    waiting = _enroll(session, campaign)
    _send(session, waiting, 1, T0)
    _send(session, waiting, 2, T0 + timedelta(days=7))
    _send(
        session,
        waiting,
        3,
        None,
        status=MessageStatus.SCHEDULED,
        scheduled_at=T0 + timedelta(days=14),
    )
    _reply(session, waiting, T0 + timedelta(days=14, minutes=5))
    # Replied after step 1 only.
    after_one = _enroll(session, campaign)
    _send(session, after_one, 1, T0)
    _reply(session, after_one, T0 + timedelta(hours=3))
    # Never replied.
    _send(session, _enroll(session, campaign), 1, T0)

    results = _results(session, user, campaign)

    assert _per_step(results, "replied") == [1, 2, 0]
    assert _per_step(results, "sent") == [4, 2, 0]
    assert results.totals.replied == 3
    assert results.totals.contacted == 4
    assert results.totals.reply_rate == pytest.approx(0.75)


def test_a_reply_at_the_same_moment_as_a_send_counts_against_that_send(session: Session) -> None:
    user = factories.make_user(session)
    campaign = _campaign(session, user)
    enrollment = _enroll(session, campaign)
    _send(session, enrollment, 1, T0)
    _send(session, enrollment, 2, T0 + timedelta(days=7))
    _reply(session, enrollment, T0 + timedelta(days=7))

    assert _per_step(_results(session, user, campaign), "replied") == [0, 1, 0]


def test_bounces_and_opt_outs_count_against_the_step_by_the_time_of_the_event(
    session: Session,
) -> None:
    user = factories.make_user(session)
    campaign = _campaign(session, user)
    bounced = _enroll(session, campaign)
    _send(session, bounced, 1, T0)
    _send(
        session,
        bounced,
        2,
        T0 + timedelta(days=7),
        status=MessageStatus.BOUNCED,
        bounced_at=T0 + timedelta(days=7, minutes=2),
    )
    # A bounce found before its own send time (clocks disagree): its own step's.
    early = _enroll(session, campaign)
    _send(
        session,
        early,
        1,
        T0 + timedelta(minutes=10),
        status=MessageStatus.BOUNCED,
        bounced_at=T0 + timedelta(minutes=9),
    )
    opted = _enroll(session, campaign)
    _send(session, opted, 1, T0)
    _reply(session, opted, T0 + timedelta(days=1), unsubscribe=True)
    _reply(session, opted, T0 + timedelta(days=2), unsubscribe=True)  # counted once

    results = _results(session, user, campaign)

    assert _per_step(results, "bounced") == [1, 1, 0]
    assert _per_step(results, "opted_out") == [1, 0, 0]
    assert _per_step(results, "replied") == [1, 0, 0]  # an opt-out is a reply too
    # A bounced message went out, so it counts as sent.
    assert _per_step(results, "sent") == [3, 1, 0]
    assert (results.totals.bounced, results.totals.opted_out) == (2, 1)


def test_only_messages_that_went_out_count_as_sent(session: Session) -> None:
    user = factories.make_user(session)
    campaign = _campaign(session, user)
    for status in (
        MessageStatus.SCHEDULED,
        MessageStatus.DRAFTED,
        MessageStatus.PREFILLED,
        MessageStatus.STALE,
        MessageStatus.DISCARDED,
        MessageStatus.FAILED,
    ):
        # A sent_at on each, so only the status keeps it out.
        _send(session, _enroll(session, campaign), 1, T0, status=status)
    _send(session, _enroll(session, campaign), 1, T0)

    results = _results(session, user, campaign)

    assert results.totals.sent == 1
    assert results.totals.contacted == 1
    assert [d.sent for d in results.sends_per_day if d.sent] == [1]


def test_sends_per_day_are_local_days_with_zeros_up_to_today(session: Session) -> None:
    user = factories.make_user(session, timezone="America/New_York")
    campaign = _campaign(session, user)
    # 23:30 on June 3 in New York is 03:30 on June 4 in UTC.
    late = datetime(2030, 6, 4, 3, 30, tzinfo=UTC)
    _send(session, _enroll(session, campaign), 1, late)
    _send(session, _enroll(session, campaign), 1, late + timedelta(minutes=1))
    _send(session, _enroll(session, campaign), 1, datetime(2030, 6, 6, 15, 0, tzinfo=UTC))
    now = datetime(2030, 6, 9, 2, 0, tzinfo=UTC)  # still June 8 in New York

    results = campaign_results.campaign_results(session, user, campaign.id, now=now)

    assert results.timezone == "America/New_York"
    assert [(d.day, d.sent) for d in results.sends_per_day] == [
        (date(2030, 6, 3), 2),
        (date(2030, 6, 4), 0),
        (date(2030, 6, 5), 0),
        (date(2030, 6, 6), 1),
        (date(2030, 6, 7), 0),
        (date(2030, 6, 8), 0),
    ]


def test_a_campaign_that_sent_nothing_has_no_days_and_no_rate(session: Session) -> None:
    user = factories.make_user(session)
    campaign = _campaign(session, user)
    _enroll(session, campaign)

    results = _results(session, user, campaign)

    assert results.sends_per_day == ()
    assert _per_step(results, "sent") == [0, 0, 0]
    assert results.totals == campaign_results.ResultTotals(
        sent=0, contacted=0, replied=0, reply_rate=None, bounced=0, opted_out=0
    )


def test_an_unreadable_time_zone_counts_utc_days(session: Session) -> None:
    user = factories.make_user(session, timezone="Not/A_Zone")
    campaign = _campaign(session, user)
    _send(session, _enroll(session, campaign), 1, datetime(2030, 6, 4, 3, 30, tzinfo=UTC))

    results = campaign_results.campaign_results(
        session, user, campaign.id, now=datetime(2030, 6, 4, 12, 0, tzinfo=UTC)
    )

    assert results.timezone == "UTC"
    assert [(d.day, d.sent) for d in results.sends_per_day] == [(date(2030, 6, 4), 1)]


def test_another_users_campaign_is_not_found(session: Session) -> None:
    mine = factories.make_user(session)
    theirs = factories.make_user(session, UserKind.HOSTED)
    campaign = _campaign(session, theirs)
    _send(session, _enroll(session, campaign), 1, T0)

    with pytest.raises(CampaignNotFound):
        _results(session, mine, campaign)


def test_only_this_campaigns_messages_count(session: Session) -> None:
    user = factories.make_user(session)
    campaign, other = _campaign(session, user), _campaign(session, user)
    _send(session, _enroll(session, campaign), 1, T0)
    _send(session, _enroll(session, other), 1, T0)
    _send(session, _enroll(session, other), 1, T0)

    assert _results(session, user, campaign).totals.sent == 1


# --- the API ------------------------------------------------------------------------


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


async def test_results_endpoint_answers_the_numbers(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        campaign = _campaign(session, user)
        enrollment = _enroll(session, campaign)
        _send(session, enrollment, 1, T0)
        _send(
            session,
            enrollment,
            2,
            T0 + timedelta(days=7),
            status=MessageStatus.BOUNCED,
            bounced_at=T0 + timedelta(days=7, minutes=1),
        )
        _send(session, _enroll(session, campaign), 1, T0)
        replied = _enroll(session, campaign)
        _send(session, replied, 1, T0)
        _reply(session, replied, T0 + timedelta(days=1))
        campaign_id, step_ids = campaign.id, [s.id for s in campaign.steps]

    response = await client.get(f"/api/v1/campaigns/{campaign_id}/results")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign_id"] == campaign_id
    assert body["sends_per_day"][0] == {"date": T0.date().isoformat(), "sent": 3}
    assert body["steps"] == [
        {
            "step_id": step_ids[0],
            "position": 1,
            "sent": 3,
            "replied": 1,
            "bounced": 0,
            "opted_out": 0,
        },
        {
            "step_id": step_ids[1],
            "position": 2,
            "sent": 1,
            "replied": 0,
            "bounced": 1,
            "opted_out": 0,
        },
        {
            "step_id": step_ids[2],
            "position": 3,
            "sent": 0,
            "replied": 0,
            "bounced": 0,
            "opted_out": 0,
        },
    ]
    assert body["totals"] == {
        "sent": 4,
        "contacted": 3,
        "replied": 1,
        "reply_rate": pytest.approx(1 / 3),
        "bounced": 1,
        "opted_out": 0,
    }
    # The detail's own sent count agrees: a bounced message went out.
    detail = (await client.get(f"/api/v1/campaigns/{campaign_id}")).json()
    assert [s["sent"] for s in detail["steps"]] == [3, 1, 0]


async def test_another_users_campaign_answers_404_and_no_counts(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        other = factories.make_user(session, UserKind.HOSTED)
        campaign = _campaign(session, other)
        replied = _enroll(session, campaign)
        _send(session, replied, 1, T0)
        _reply(session, replied, T0 + timedelta(days=1))
        theirs = campaign.id

    for campaign_id in (theirs, 999_999):
        response = await client.get(f"/api/v1/campaigns/{campaign_id}/results")
        assert response.status_code == 404, response.text
        assert set(response.json()) == {"detail"}
        assert "sent" not in response.text
