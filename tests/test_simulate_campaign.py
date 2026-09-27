"""P3-06's "done when": 100 contacts over three weeks, nothing outside a window or over a cap,
and step 2's timing from the actual ``sent_at`` (spec 11.3, 11.4)."""

from __future__ import annotations

from collections import Counter
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
from netkeeper.services.campaign_engine import activate
from netkeeper.services.simulate_campaign import simulate_campaign

ZONE = ZoneInfo("America/New_York")
START = datetime(2026, 9, 28, 8, 0, tzinfo=ZONE).astimezone(UTC)  # a Monday morning
END = START + timedelta(weeks=3)
HOLIDAY = date(2026, 10, 7)  # the second Wednesday
# Week 1: the big campaign's cap and the mailbox's both bind while step 1 goes out
# (34 + 5 = 39). Week 2: step 2 has room, so it fires when it is due, and the holiday
# pushes a day's worth into the next, where the caps bind again.
MAILBOX_CAP = 39
BIG_CAP = 34
SMALL_CAP = 5
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
    activate(session, user, campaign.id, settings=SETTINGS, now=START)
    return campaign.id


@pytest.mark.slow
def test_a_hundred_contacts_over_three_weeks_keep_every_window_cap_and_cadence(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session, timezone="America/New_York")
        mailbox = make_mailbox(session, user, daily_cap=MAILBOX_CAP)
        big = _campaign(session, user, mailbox.id, 100, BIG_CAP)
        small = _campaign(session, user, mailbox.id, 10, SMALL_CAP)

    run = simulate_campaign(session_factory, settings=SETTINGS, start=START, end=END, seed=3)

    with session_scope(session_factory) as session:
        messages = list(session.scalars(scoped(user, Message).order_by(Message.scheduled_at)))
        enrollments = {e.id: e for e in session.scalars(scoped(user, Enrollment))}
        steps = {s.id: s for s in session.scalars(scoped(user, CampaignStep))}
        campaigns = {c.id: c for c in session.scalars(scoped(user, Campaign))}
    assert len(run.firings) == len(messages)
    assert all(m.status is MessageStatus.SENT for m in messages)
    assert run.ticks < 5_000

    # Nothing outside the window: Tuesday to Thursday, 09:00 to 16:30 New York, no holiday.
    for message in messages:
        assert message.scheduled_at is not None and message.sent_at is not None
        local = message.scheduled_at.astimezone(ZONE)
        assert local.strftime("%a") in ("Tue", "Wed", "Thu"), local
        assert time(9, 0) <= local.time() < time(16, 30), local
        assert local.date() != HOLIDAY, local

    # Nothing over a cap, per local day: each campaign's, and the mailbox's across both.
    per_campaign: Counter[tuple[int, date]] = Counter()
    per_mailbox: Counter[date] = Counter()
    for message in messages:
        assert message.scheduled_at is not None
        day = message.scheduled_at.astimezone(ZONE).date()
        per_campaign[enrollments[message.enrollment_id].campaign_id, day] += 1
        per_mailbox[day] += 1
    caps = {big: BIG_CAP, small: SMALL_CAP}
    assert max(n for (c, _), n in per_campaign.items() if c == big) == BIG_CAP
    assert all(n <= caps[c] for (c, _), n in per_campaign.items())
    assert all(n <= MAILBOX_CAP for n in per_mailbox.values())
    assert max(per_mailbox.values()) == MAILBOX_CAP  # the caps were reached, not just unused

    # One at a time, spaced: never two within the floor.
    fired = [m.scheduled_at for m in messages if m.scheduled_at is not None]
    assert all(b - a >= FLOOR for a, b in pairwise(fired))

    # Every contact got step 1, once; nobody got a step twice.
    per_step = Counter((m.enrollment_id, steps[m.step_id or 0].position) for m in messages)
    assert set(per_step.values()) == {1}
    assert {e for e, position in per_step if position == 1} == set(enrollments)

    # Step 2 derives from step 1's actual sent_at (a few minutes after it fired), pushed
    # into the window.
    first = {m.enrollment_id: m for m in messages if steps[m.step_id or 0].position == 1}
    seconds = [m for m in messages if steps[m.step_id or 0].position == 2]
    assert len(seconds) > 50
    for second in seconds:
        step_one = first[second.enrollment_id]
        assert step_one.sent_at is not None and second.scheduled_at is not None
        assert step_one.scheduled_at is not None
        assert step_one.sent_at > step_one.scheduled_at
        assert second.scheduled_at >= step_one.sent_at + timedelta(days=7)
    # And some fired as soon as they were due: the cadence is not only the caps' doing.
    lateness = []
    for second in seconds:
        sent_at = first[second.enrollment_id].sent_at
        assert sent_at is not None and second.scheduled_at is not None
        lateness.append(second.scheduled_at - (sent_at + timedelta(days=7)))
    assert min(lateness) < timedelta(minutes=2)

    # The ones that finished are completed; the rest still have a due step.
    for enrollment in enrollments.values():
        if enrollment.current_step == 2:
            assert enrollment.status is EnrollmentStatus.COMPLETED
        else:
            assert enrollment.status is EnrollmentStatus.ACTIVE
            assert enrollment.next_action_at is not None
    assert all(c.status is CampaignStatus.ACTIVE for c in campaigns.values())
