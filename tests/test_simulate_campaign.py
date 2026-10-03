"""P3-06's "done when": 100 contacts over three weeks, nothing before the scheduled start,
overnight or over a cap, and step 2's timing from the actual ``sent_at`` (spec 11.3, 11.4,
#338)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import factories
import pytest
from campaign_fakes import make_mailbox
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import CampaignSettings, Settings
from netkeeper.db import session_scope
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Enrollment,
    EnrollmentStatus,
    Message,
    MessageStatus,
    StepMode,
    TemplateChannel,
    User,
)
from netkeeper.scoping import scoped
from netkeeper.services.campaign_engine import REVIEW_GATE, activate
from netkeeper.services.simulate_campaign import simulate_campaign

ZONE = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 28, 8, 0, tzinfo=ZONE).astimezone(UTC)  # a Monday morning
START = datetime(2026, 9, 29, 9, 0, tzinfo=ZONE).astimezone(UTC)  # the default start, Tuesday
END = START + timedelta(weeks=3)
HOLIDAY = date(2026, 10, 7)  # the second Wednesday
# Two replays, each with its own mailbox, so each has headroom under the slow-test limit.
# The big one: 100 contacts, and the mailbox's cap (30) binds before the campaign's (34).
# The small one: 10 contacts, and the campaign's cap (5) binds under the mailbox's (39).
# Either way step 1 takes days, and step 2 fires when it is due where there is room.
BIG = (100, 34, 30)
SMALL = (10, 5, 39)
SETTINGS = Settings(campaigns=CampaignSettings(holidays=(HOLIDAY.isoformat(),)))
FLOOR = timedelta(seconds=SETTINGS.campaigns.send_spacing_floor_s)


def _campaign(session: Session, user: User, mailbox_id: int, contacts: int, cap: int) -> int:
    campaign = factories.make_campaign(
        session,
        user,
        channels=(TemplateChannel.EMAIL, TemplateChannel.EMAIL),
        status=CampaignStatus.REVIEWING,
        approved_at=START,
        mailbox_id=mailbox_id,
        daily_cap=cap,
    )
    for step in campaign.steps:
        step.mode = StepMode.SEND
    for _ in range(contacts):
        contact = factories.make_contact(session, user)
        contact.emails.append(
            type(contact).emails.property.mapper.class_(
                user_id=user.id, email=f"c{contact.id}@example.test", is_primary=True
            )
        )
        factories.make_enrollment(session, campaign, contact, status=EnrollmentStatus.PENDING)
    session.flush()
    activate(
        session, user, campaign.id, settings=SETTINGS, now=NOW, starts_at=START, gate=REVIEW_GATE
    )
    return campaign.id


@dataclass(frozen=True)
class Replay:
    messages: list[Message]
    enrollments: dict[int, Enrollment]
    steps: dict[int, CampaignStep]
    campaign: Campaign
    firings: int
    ticks: int


def _replay(
    session_factory: sessionmaker[Session], contacts: int, cap: int, mailbox_cap: int
) -> Replay:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session, timezone="America/New_York")
        mailbox = make_mailbox(session, user, daily_cap=mailbox_cap)
        _campaign(session, user, mailbox.id, contacts, cap)

    run = simulate_campaign(session_factory, settings=SETTINGS, start=NOW, end=END, seed=3)

    with session_scope(session_factory) as session:
        messages = list(session.scalars(scoped(user, Message).order_by(Message.scheduled_at)))
        enrollments = {e.id: e for e in session.scalars(scoped(user, Enrollment))}
        steps = {s.id: s for s in session.scalars(scoped(user, CampaignStep))}
        [campaign] = session.scalars(scoped(user, Campaign))
    return Replay(messages, enrollments, steps, campaign, len(run.firings), run.ticks)


def _check(replay: Replay, *, cap: int, mailbox_cap: int) -> None:
    """Every rule the schedule must keep, for one replay."""
    messages, enrollments, steps = replay.messages, replay.enrollments, replay.steps
    assert replay.firings == len(messages)
    assert all(m.status is MessageStatus.SENT for m in messages)
    assert replay.ticks < 5_000

    # Nothing before the start, and nothing overnight: step 1 goes from the start and a
    # follow-up in a suggested slot (09:00 to 16:30 New York), and what a cap holds back
    # spills to the next day at the same time (#338). A suggested day is never a holiday.
    days: Counter[str] = Counter()
    for message in messages:
        assert message.scheduled_at is not None and message.sent_at is not None
        assert message.scheduled_at >= START
        local = message.scheduled_at.astimezone(ZONE)
        assert time(9, 0) <= local.time() < time(16, 30), local
        days[local.strftime("%a")] += 1
    assert days["Sat"] == days["Sun"] == days["Mon"] == 0, days

    # Nothing over a cap, per local day, and the tighter one was reached.
    per_day: Counter[date] = Counter()
    for message in messages:
        assert message.scheduled_at is not None
        per_day[message.scheduled_at.astimezone(ZONE).date()] += 1
    assert max(per_day.values()) == min(cap, mailbox_cap)

    # One at a time, spaced: never two within the floor.
    fired = [m.scheduled_at for m in messages if m.scheduled_at is not None]
    assert all(b - a >= FLOOR for a, b in pairwise(fired))

    # Every contact got step 1, once; nobody got a step twice.
    per_step = Counter((m.enrollment_id, steps[m.step_id or 0].position) for m in messages)
    assert set(per_step.values()) == {1}
    assert {e for e, position in per_step if position == 1} == set(enrollments)

    # Step 2 derives from step 1's actual sent_at (a few minutes after it fired), in the
    # next suggested slot, and some fired as soon as they were due.
    first = {m.enrollment_id: m for m in messages if steps[m.step_id or 0].position == 1}
    seconds = [m for m in messages if steps[m.step_id or 0].position == 2]
    assert seconds
    lateness = []
    for second in seconds:
        step_one = first[second.enrollment_id]
        assert step_one.sent_at is not None and second.scheduled_at is not None
        assert step_one.scheduled_at is not None
        assert step_one.sent_at > step_one.scheduled_at
        assert second.scheduled_at >= step_one.sent_at + timedelta(days=7)
        lateness.append(second.scheduled_at - (step_one.sent_at + timedelta(days=7)))
    assert min(lateness) < timedelta(minutes=2)

    # The ones that finished are completed; the rest still have a due step.
    for enrollment in enrollments.values():
        if enrollment.current_step == 2:
            assert enrollment.status is EnrollmentStatus.COMPLETED
        else:
            assert enrollment.status is EnrollmentStatus.ACTIVE
            assert enrollment.next_action_at is not None
    assert replay.campaign.status is CampaignStatus.ACTIVE


@pytest.mark.slow
def test_a_hundred_contacts_over_three_weeks_keep_every_start_cap_and_cadence(
    session_factory: sessionmaker[Session],
) -> None:
    contacts, cap, mailbox_cap = BIG
    replay = _replay(session_factory, contacts, cap, mailbox_cap)
    _check(replay, cap=cap, mailbox_cap=mailbox_cap)
    assert len([m for m in replay.messages if replay.steps[m.step_id or 0].position == 2]) > 50


@pytest.mark.slow
def test_a_small_campaign_under_its_own_cap_keeps_every_start_cap_and_cadence(
    session_factory: sessionmaker[Session],
) -> None:
    contacts, cap, mailbox_cap = SMALL
    _check(_replay(session_factory, contacts, cap, mailbox_cap), cap=cap, mailbox_cap=mailbox_cap)
