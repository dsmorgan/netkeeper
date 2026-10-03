"""netkeeper.services.campaign_engine (spec 11.3, 11.4; item P3-06): the state machine and tick."""

from __future__ import annotations

import asyncio
import dataclasses
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import factories
import pytest
from campaign_fakes import LATENCY, NOW, SETTINGS, FakeSender, make_mailbox
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns import gmail, schedule
from netkeeper.campaigns.compose import message_id_for
from netkeeper.config import CampaignSettings, Settings
from netkeeper.crm.contacts import merge_contacts
from netkeeper.crm.interactions import add_interaction
from netkeeper.db import make_session_factory, session_scope
from netkeeper.models import (
    Campaign,
    CampaignMailboxLocked,
    CampaignStatus,
    Contact,
    EmailStatus,
    Enrollment,
    EnrollmentStatus,
    InteractionKind,
    Mailbox,
    MailboxStatus,
    Message,
    MessageDirection,
    MessageStatus,
    SettingKV,
    StepMode,
    TemplateChannel,
    User,
)
from netkeeper.scoping import get_scoped, install_scope_guard, scoped, scoped_delete, unscoped
from netkeeper.services import campaign_engine as engine_module
from netkeeper.services.campaign_engine import (
    REVIEW_GATE,
    CampaignEngine,
    CampaignEngineError,
    Firing,
    SendOutcome,
    SendResult,
    Skip,
    TickResult,
    activate,
    enroll,
    next_send_at,
    pause_campaign,
    pause_enrollment,
    remove_enrollment,
    resume_campaign,
    resume_enrollment,
    run_tick,
    schedule_next,
)

EMAIL = TemplateChannel.EMAIL
LINKEDIN = TemplateChannel.LINKEDIN


@dataclass
class World:
    factory: sessionmaker[Session]
    user: User
    mailbox: Mailbox
    campaign: Campaign
    sender: FakeSender

    def tick(
        self, now: datetime = NOW, *, settings: Settings = SETTINGS, seed: int = 1
    ) -> TickResult:
        self.sender.now = now
        [result] = [
            r
            for r in run_tick(
                self.factory,
                settings=settings,
                sender=self.sender,
                clock=lambda: now,
                rng=random.Random(seed),
            )
            if r.user_id == self.user.id
        ]
        return result

    def read[T](self, fn: Callable[[Session], T]) -> T:
        with session_scope(self.factory) as session:
            return fn(session)

    def write[T](self, fn: Callable[[Session], T]) -> T:
        with session_scope(self.factory, write=True) as session:
            return fn(session)

    def enrollment(self, enrollment_id: int) -> Enrollment:
        row = self.read(lambda s: get_scoped(s, self.user, Enrollment, enrollment_id))
        assert row is not None
        return row

    def messages(self, enrollment_id: int | None = None) -> list[Message]:
        def load(session: Session) -> list[Message]:
            statement = scoped(self.user, Message).order_by(Message.id)
            if enrollment_id is not None:
                statement = statement.where(Message.enrollment_id == enrollment_id)
            return list(session.scalars(statement))

        return self.read(load)

    def enroll_new(self, *, email: str | None = None, **overrides: Any) -> int:
        """A contact enrolled active in the campaign, its first step due at ``NOW``."""

        def make(session: Session) -> int:
            n = session.scalar(unscoped(select(Contact.id).order_by(Contact.id.desc()))) or 0
            contact = factories.make_contact(
                session, self.user, emails=[email or f"person{n + 1}@example.test"]
            )
            fields: dict[str, Any] = {"next_action_at": NOW}
            fields.update(overrides)
            campaign = get_scoped(session, self.user, Campaign, self.campaign.id)
            assert campaign is not None
            return factories.make_enrollment(session, campaign, contact, **fields).id

        return self.write(make)


def make_world(
    factory: sessionmaker[Session],
    *,
    channels: tuple[TemplateChannel, ...] = (EMAIL, EMAIL, EMAIL),
    **campaign: Any,
) -> World:
    with session_scope(factory, write=True) as session:
        user = factories.make_user(session)
        mailbox = make_mailbox(session, user)
        row = factories.make_campaign(
            session, user, channels=channels, mailbox_id=mailbox.id, **campaign
        )
        for step in row.steps:
            if step.channel is EMAIL:
                step.mode = StepMode.SEND
        session.flush()
    return World(factory, user, mailbox, row, FakeSender())


@pytest.fixture
def world(session_factory: sessionmaker[Session]) -> World:
    return make_world(session_factory)


def reasons_of(result: TickResult, enrollment_id: int) -> tuple[str, ...]:
    return result.skipped()[enrollment_id]


# --- constants ------------------------------------------------------------------------------


def test_the_engine_constants_are_pinned() -> None:
    """Safety constants against numbers and words written out here (CLAUDE.md)."""
    assert engine_module.BATCH_PER_TICK == 1
    assert engine_module.TICK_INTERVAL_S == 60.0
    assert timedelta(days=1) == engine_module.RECHECK_AFTER
    assert engine_module.SCAN_LIMIT == 500
    assert {r.value: s.value for r, s in engine_module.ENDING_REASONS.items()} == {
        "do_not_contact": "opted_out",
        "email_bounced": "bounced",
    }
    assert {s.value for s in engine_module.WAITING_STATUSES} == {
        "scheduled",
        "drafted",
        "prefilled",
    }
    assert {s.value for s in engine_module.ENROLLING_STATUSES} == {"draft", "reviewing"}


# --- a step fires ---------------------------------------------------------------------------


def test_without_a_sender_the_tick_does_nothing(world: World) -> None:
    enrollment_id = world.enroll_new()
    assert run_tick(world.factory, settings=SETTINGS, sender=None, clock=lambda: NOW) == []
    assert world.messages() == []
    assert world.enrollment(enrollment_id).next_action_at == NOW


def test_a_due_step_fires_and_the_next_is_due_after_the_actual_send(world: World) -> None:
    """Spec 11.3: step 2 is computed from step 1's actual ``sent_at``, not the fire time."""
    enrollment_id = world.enroll_new(email="ada@example.test")
    result = world.tick()

    [(firing, outcome)] = result.fired
    assert outcome.outcome is SendOutcome.SENT
    assert (firing.enrollment_id, firing.step_position, firing.channel) == (enrollment_id, 1, EMAIL)
    assert firing.to_address == "ada@example.test"
    assert firing.mode is StepMode.SEND
    assert firing.mailbox_id == world.mailbox.id
    assert firing.subject == "Hello"
    assert firing.body.startswith("Hi First")
    [message] = world.messages(enrollment_id)
    assert (message.status, message.direction) == (MessageStatus.SENT, MessageDirection.OUT)
    assert message.scheduled_at == NOW
    assert message.sent_at == NOW + LATENCY
    assert (message.subject, message.body_rendered) == (firing.subject, firing.body)
    assert message.gmail_message_id == f"gm-{message.id}"
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.current_step) == (EnrollmentStatus.ACTIVE, 1)
    assert enrollment.next_action_at == NOW + LATENCY + timedelta(days=7)


def test_a_follow_up_lands_in_the_next_suggested_slot_after_its_delay(
    session_factory: sessionmaker[Session],
) -> None:
    """#338: after its delay, a follow-up aims for the next suggested slot (Tuesday to
    Thursday, 09:00 to 16:30 local; the user here is on UTC)."""
    world = make_world(session_factory)
    enrollment_id = world.enroll_new()
    world.tick(settings=Settings())
    # Sent Tuesday 14:07; seven days on is Tuesday 14:07, inside a suggested slot.
    assert world.enrollment(enrollment_id).next_action_at == NOW + LATENCY + timedelta(days=7)
    world.sender.latency = timedelta(hours=3)  # the next one goes out at 18:00, after 16:30
    second = world.enroll_new()
    world.tick(NOW + timedelta(hours=1), settings=Settings())
    assert world.enrollment(second).next_action_at == datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


def test_an_explicit_step_time_is_honored(session_factory: sessionmaker[Session]) -> None:
    """#338: a step's own time of day, on the day its delay lands, at any hour."""
    world = make_world(session_factory)

    def at_ten_pm(session: Session) -> None:
        campaign = get_scoped(session, world.user, Campaign, world.campaign.id)
        assert campaign is not None
        campaign.steps[1].send_time = "22:00"
        campaign.steps[1].delay_days = 4

    world.write(at_ten_pm)
    enrollment_id = world.enroll_new()
    world.tick()
    due = datetime(2026, 10, 3, 22, 0, tzinfo=UTC)  # Saturday, outside every suggested slot
    assert world.enrollment(enrollment_id).next_action_at == due
    assert world.tick(due - timedelta(minutes=1)).fired == []
    [(firing, _)] = world.tick(due).fired
    assert firing.step_position == 2


def test_the_last_step_completes_the_enrollment(session_factory: sessionmaker[Session]) -> None:
    world = make_world(session_factory, channels=(EMAIL,))
    enrollment_id = world.enroll_new()
    world.tick()
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.current_step) == (EnrollmentStatus.COMPLETED, 1)
    assert (enrollment.next_action_at, enrollment.exit_reason) == (None, None)


def test_a_draft_waits_for_its_send_before_the_next_step_is_due(world: World) -> None:
    """Draft mode (spec 11.5): the next step counts from when the draft is seen sent."""
    world.sender.outcome = SendOutcome.DRAFTED
    enrollment_id = world.enroll_new()
    world.tick()
    [message] = world.messages(enrollment_id)
    assert (message.status, message.sent_at) == (MessageStatus.DRAFTED, None)
    assert message.gmail_draft_id == f"draft-{message.id}"
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.current_step, enrollment.next_action_at) == (1, None)

    # Still waiting: even with a due time set by hand, step 2 does not go.
    world.write(
        lambda s: setattr(
            get_scoped(s, world.user, Enrollment, enrollment_id), "next_action_at", NOW
        )
    )
    later = world.tick(NOW + timedelta(days=8))
    assert reasons_of(later, enrollment_id) == (Skip.WAITING_ON_UNSENT,)
    assert world.enrollment(enrollment_id).next_action_at is None

    sent_at = NOW + timedelta(days=2)

    def seen_sent(session: Session) -> None:
        row = get_scoped(session, world.user, Message, message.id)
        assert row is not None
        row.status, row.sent_at = MessageStatus.SENT, sent_at
        session.flush()
        schedule_next(session, world.user, enrollment_id, settings=SETTINGS, now=sent_at)

    world.write(seen_sent)
    assert world.enrollment(enrollment_id).next_action_at == sent_at + timedelta(days=7)


def test_a_later_draft_is_not_scheduled_from_an_earlier_send(world: World) -> None:
    """Step 1 went out, step 2 is a draft nobody sent: step 3 is due from nothing yet, not
    from step 1's send."""
    enrollment_id = world.enroll_new()
    world.tick()
    world.sender.outcome = SendOutcome.DRAFTED
    due = world.enrollment(enrollment_id).next_action_at
    assert due is not None
    world.tick(due)
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.current_step, enrollment.next_action_at) == (2, None)


# --- one at a time, spaced, capped -----------------------------------------------------------


def test_one_firing_per_tick_and_the_next_waits_for_the_spacing(world: World) -> None:
    """Spec 11.4: a batch of 1, then ``human_delay`` spacing with a 90 s floor."""
    ids = [world.enroll_new() for _ in range(3)]
    first = world.tick()
    assert [f.enrollment_id for f, _ in first.fired] == [ids[0]]
    stored = world.read(lambda s: next_send_at(s, world.user, world.mailbox.id))
    assert stored is not None
    assert stored >= NOW + LATENCY + timedelta(seconds=90)

    blocked = world.tick(NOW + timedelta(minutes=1))
    assert blocked.fired == []
    assert reasons_of(blocked, ids[1]) == (Skip.SPACING,)
    assert blocked.next_wake == stored

    released = world.tick(stored)
    assert [f.enrollment_id for f, _ in released.fired] == [ids[1]]


def test_the_spacing_floor_holds_without_the_stored_time(world: World) -> None:
    """The stored next send time lost: the floor after the last firing still holds."""
    ids = [world.enroll_new() for _ in range(2)]
    world.tick()
    world.write(lambda s: s.execute(scoped_delete(world.user, SettingKV)))
    assert reasons_of(world.tick(NOW + timedelta(seconds=89)), ids[1]) == (Skip.SPACING,)
    assert world.tick(NOW + timedelta(seconds=90)).fired != []


def test_the_spacing_survives_a_restart(world: World, engine: Engine) -> None:
    """Spec 11.4: persisted, so a new process with a new session factory keeps it."""
    ids = [world.enroll_new() for _ in range(2)]
    world.tick()
    fresh = make_session_factory(engine)
    install_scope_guard(fresh)
    stored = world.read(lambda s: next_send_at(s, world.user, world.mailbox.id))
    assert stored is not None
    world.factory = fresh
    assert reasons_of(world.tick(stored - timedelta(seconds=1)), ids[1]) == (Skip.SPACING,)
    assert world.tick(stored).fired != []


def test_the_campaign_cap_holds_for_the_local_day(session_factory: sessionmaker[Session]) -> None:
    world = make_world(session_factory, daily_cap=2)
    ids = [world.enroll_new() for _ in range(3)]
    world.tick()
    world.tick(NOW + timedelta(hours=1))
    capped = world.tick(NOW + timedelta(hours=2))
    assert reasons_of(capped, ids[2]) == (Skip.CAMPAIGN_AT_CAP,)
    assert capped.next_wake == datetime(2026, 9, 30, tzinfo=UTC)  # the local midnight
    # #338: what is left of the batch spills to the next day at its start time, 14:00,
    # never overnight.
    midnight = world.tick(datetime(2026, 9, 30, 0, 1, tzinfo=UTC))
    assert midnight.fired == [] and reasons_of(midnight, ids[2]) == (Skip.SPILLED,)
    resume = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)
    assert world.enrollment(ids[2]).next_action_at == resume
    assert world.tick(resume - timedelta(minutes=1)).fired == []
    assert world.tick(resume).fired != []


def test_a_campaign_with_no_cap_of_its_own_uses_the_configs(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory)
    settings = Settings(campaigns=CampaignSettings(mailbox_daily_cap=1))
    ids = [world.enroll_new() for _ in range(2)]
    world.tick(settings=settings)
    assert reasons_of(world.tick(NOW + timedelta(hours=1), settings=settings), ids[1]) == (
        Skip.CAMPAIGN_AT_CAP,
    )


def test_the_mailbox_cap_counts_every_campaign_on_it(world: World) -> None:
    """Spec 11.4: per mailbox across all campaigns."""
    world.write(
        lambda s: setattr(get_scoped(s, world.user, Mailbox, world.mailbox.id), "daily_cap", 1)
    )

    def second_campaign(session: Session) -> int:
        user = session.get(User, world.user.id)
        assert user is not None
        other = factories.make_campaign(session, user, mailbox_id=world.mailbox.id)
        contact = factories.make_contact(session, user, emails=["other@example.test"])
        return factories.make_enrollment(session, other, contact, next_action_at=NOW).id

    other_id = world.write(second_campaign)
    mine = world.enroll_new()
    fired = world.tick()
    assert len(fired.fired) == 1
    capped = world.tick(NOW + timedelta(hours=1))
    waiting = mine if fired.fired[0][0].enrollment_id == other_id else other_id
    assert reasons_of(capped, waiting) == ("mailbox_at_cap",)


def test_campaign_cap_is_never_over_the_mailbox_hard_max() -> None:
    assert engine_module.campaign_cap(SETTINGS, Campaign(daily_cap=10_000)) == 400
    assert engine_module.campaign_cap(SETTINGS, Campaign(daily_cap=None)) == 80
    assert engine_module.campaign_cap(SETTINGS, Campaign(daily_cap=3)) == 3


# --- the scheduled start, and no window (#338) -----------------------------------------------

SATURDAY_NIGHT = datetime(2026, 10, 3, 22, 0, tzinfo=UTC)
"""Outside every suggested slot, and a holiday in some tests below."""


def _start_at(world: World, starts_at: datetime | None) -> None:
    def move(session: Session) -> None:
        campaign = get_scoped(session, world.user, Campaign, world.campaign.id)
        assert campaign is not None
        campaign.starts_at = starts_at

    world.write(move)


def test_nothing_sends_before_the_scheduled_start(world: World) -> None:
    """The enrollment is due now; its campaign starts two hours later. Nothing goes out,
    to the second, and the tick wakes at the start."""
    start = NOW + timedelta(hours=2)
    _start_at(world, start)
    enrollment_id = world.enroll_new()
    for at in (NOW, NOW + timedelta(hours=1), start - timedelta(seconds=1)):
        result = world.tick(at)
        assert result.fired == [], at
        # Left out by the tick's query itself, not refused later (#338 review, S3a).
        assert enrollment_id not in result.skipped(), at
        assert result.next_wake == start
    assert world.sender.firings == [] and world.messages() == []
    assert world.enrollment(enrollment_id).next_action_at == NOW  # nothing changed
    [(firing, _)] = world.tick(start).fired
    assert firing.enrollment_id == enrollment_id


def test_an_active_campaign_with_no_start_sends_nothing(world: World) -> None:
    _start_at(world, None)
    world.enroll_new()
    for days in (0, 1, 30):
        assert world.tick(NOW + timedelta(days=days)).fired == []
    assert world.messages() == []


def test_the_start_is_checked_again_where_the_claim_is_decided(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: if the query ever selected a campaign that has not started,
    the claim is still refused."""
    monkeypatch.setattr(
        engine_module,
        "_selected",
        lambda user, now: engine_module._selectable(user).where(Enrollment.next_action_at <= now),
    )
    start = NOW + timedelta(hours=2)
    _start_at(world, start)
    enrollment_id = world.enroll_new()
    result = world.tick()
    assert result.fired == [] and reasons_of(result, enrollment_id) == (Skip.NOT_STARTED,)
    assert result.next_wake == start
    _start_at(world, None)
    result = world.tick()
    assert result.fired == [] and reasons_of(result, enrollment_id) == (Skip.NOT_STARTED,)
    assert world.messages() == []


def test_an_explicit_start_at_ten_pm_on_a_saturday_holiday_sends(world: World) -> None:
    """No window gates a send: an explicitly scheduled 22:00 goes out at 22:00, on a
    weekend, on a holiday."""
    _start_at(world, SATURDAY_NIGHT)
    enrollment_id = world.enroll_new(next_action_at=SATURDAY_NIGHT)
    settings = Settings(campaigns=CampaignSettings(holidays=("2026-10-03",)))
    assert world.tick(SATURDAY_NIGHT - timedelta(minutes=1), settings=settings).fired == []
    [(firing, _)] = world.tick(SATURDAY_NIGHT, settings=settings).fired
    assert firing.enrollment_id == enrollment_id


def test_without_a_window_the_spacing_still_holds(world: World) -> None:
    _start_at(world, SATURDAY_NIGHT)
    first = world.enroll_new(next_action_at=SATURDAY_NIGHT)
    second = world.enroll_new(next_action_at=SATURDAY_NIGHT)
    [(firing, _)] = world.tick(SATURDAY_NIGHT).fired
    assert firing.enrollment_id == first
    later = world.tick(SATURDAY_NIGHT + timedelta(seconds=60))
    assert later.fired == [] and reasons_of(later, second) == (Skip.SPACING,)


def test_without_a_window_the_daily_caps_still_hold(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory, daily_cap=1)
    _start_at(world, SATURDAY_NIGHT)
    ids = [world.enroll_new(next_action_at=SATURDAY_NIGHT) for _ in range(2)]
    assert world.tick(SATURDAY_NIGHT).fired != []
    capped = world.tick(SATURDAY_NIGHT + timedelta(hours=1))
    assert capped.fired == [] and reasons_of(capped, ids[1]) == (Skip.CAMPAIGN_AT_CAP,)


def test_without_a_window_do_not_contact_still_holds(world: World) -> None:
    _start_at(world, SATURDAY_NIGHT)
    enrollment_id = world.enroll_new(next_action_at=SATURDAY_NIGHT)
    _set_contact(world, enrollment_id, do_not_contact=True)
    result = world.tick(SATURDAY_NIGHT)
    assert result.fired == [] and world.messages() == []
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.OPTED_OUT


@pytest.mark.parametrize("status", [CampaignStatus.DRAFT, CampaignStatus.REVIEWING])
def test_without_a_window_a_campaign_short_of_the_review_gate_sends_nothing(
    session_factory: sessionmaker[Session], status: CampaignStatus
) -> None:
    """A start in the past never stands in for activation: only an active campaign fires."""
    world = make_world(session_factory, status=status, starts_at=NOW - timedelta(days=1))
    world.enroll_new()
    assert world.tick(SATURDAY_NIGHT).fired == [] and world.messages() == []


def test_a_moved_start_is_refused_once_a_message_is_scheduled_even_if_not_sent(
    world: World,
) -> None:
    """#338 review, S3b: a claimed message with no ``sent_at`` may be in Gmail's hands."""
    _start_at(world, NOW + timedelta(days=1))
    enrollment_id = world.enroll_new(next_action_at=NOW + timedelta(days=1))

    def claimed(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        factories.make_message(
            session, enrollment, status=MessageStatus.SCHEDULED, sent_at=None, scheduled_at=NOW
        )

    world.write(claimed)
    with pytest.raises(CampaignEngineError, match="already sent"):
        world.write(
            lambda s: engine_module.set_start(
                s, world.user, world.campaign.id, settings=Settings(), now=NOW, starts_at=NOW
            )
        )


# --- no send-time constraint: a leftover goes as soon as it can (#338, #354) ----------------

TUESDAY = datetime(2026, 9, 29, tzinfo=UTC)  # the user is on UTC


def _at(day: datetime, hours: float) -> datetime:
    return day + timedelta(hours=hours)


def test_a_step_due_at_nine_reached_at_ten_pm_fires_at_ten_pm(world: World) -> None:
    """Deliberate (#338, maintainer's decision): netkeeper does not restrict send times.
    A step due today at 09:00 that ``serve`` only reaches at 22:00 goes then. An
    optional, user-chosen constraint is #354."""
    enrollment_id = world.enroll_new(next_action_at=_at(TUESDAY, 9))
    [(firing, _)] = world.tick(_at(TUESDAY, 22)).fired
    assert firing.enrollment_id == enrollment_id


def test_an_upgraded_campaigns_overdue_rows_fire_on_the_next_tick_whatever_the_hour(
    session_factory: sessionmaker[Session],
) -> None:
    """0031 sets ``starts_at = approved_at`` on a campaign already running. A row overdue
    from earlier today goes at the first tick after the upgrade, even at 23:30; one from
    an earlier day whose time of day has passed today goes too (the spill only waits
    for a time of day still ahead)."""
    approved = _at(TUESDAY, -10 * 24 + 10)
    world = make_world(session_factory, approved_at=approved, starts_at=approved)
    today = world.enroll_new(next_action_at=_at(TUESDAY, 10))
    [(first, _)] = world.tick(_at(TUESDAY, 23.5)).fired
    assert first.enrollment_id == today
    sunday = world.enroll_new(next_action_at=_at(TUESDAY, -2 * 24 + 11))
    spaced = world.read(lambda s: next_send_at(s, world.user, world.mailbox.id))
    assert spaced is not None
    [(second, _)] = world.tick(max(spaced, _at(TUESDAY, 23.75))).fired
    assert second.enrollment_id == sunday


def test_an_explicit_step_time_before_the_raw_delay_is_not_pushed_back(
    session_factory: sessionmaker[Session],
) -> None:
    """#338 review, S2: step 1 went at Monday 23:30; step 2 is "1 day, at 09:00", so it
    is due Tuesday 09:00, not Tuesday 23:30 (the raw delay)."""
    world = make_world(session_factory)

    def at_nine(session: Session) -> None:
        campaign = get_scoped(session, world.user, Campaign, world.campaign.id)
        assert campaign is not None
        campaign.steps[1].delay_days, campaign.steps[1].send_time = 1, "09:00"

    world.write(at_nine)
    tuesday_nine = _at(TUESDAY, 9)
    enrollment_id = world.enroll_new(current_step=1, next_action_at=tuesday_nine)

    def step_one(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        factories.make_message(session, enrollment, position=1, sent_at=_at(TUESDAY, -0.5))

    world.write(step_one)
    [(firing, _)] = world.tick(tuesday_nine).fired
    assert firing.step_position == 2


def test_a_time_zone_that_cannot_be_read_sends_nothing(world: World) -> None:
    def broken(session: Session) -> None:
        user = session.get(User, world.user.id)
        assert user is not None
        user.timezone = "Mars/Olympus_Mons"

    world.write(broken)
    enrollment_id = world.enroll_new()
    result = world.tick()
    assert result.fired == [] and reasons_of(result, enrollment_id) == (Skip.BAD_SCHEDULE,)
    assert world.enrollment(enrollment_id).next_action_at == NOW  # nothing changed


def test_a_holiday_list_that_cannot_be_read_sends_nothing(world: World) -> None:
    enrollment_id = world.enroll_new()
    settings = Settings(campaigns=CampaignSettings(holidays=("2026-13-01",)))
    result = world.tick(settings=settings)
    assert result.fired == [] and reasons_of(result, enrollment_id) == (Skip.BAD_SCHEDULE,)


def test_a_holiday_never_blocks_a_due_step(world: World) -> None:
    """Holidays are a suggestion now: a step due on one still goes."""
    world.enroll_new()
    settings = Settings(campaigns=CampaignSettings(holidays=("2026-09-29",)))
    assert world.tick(settings=settings).fired != []


# --- #261 requirement 4: status selects, not the due time --------------------------------


@pytest.mark.parametrize(
    "status", [EnrollmentStatus.PENDING, EnrollmentStatus.PAUSED, EnrollmentStatus.COMPLETED]
)
def test_an_enrollment_that_is_not_active_never_fires_whatever_its_due_time(
    world: World, status: EnrollmentStatus
) -> None:
    """A held pause keeps ``next_action_at`` (#242 review): the due time alone means nothing."""
    enrollment_id = world.enroll_new(status=status)
    result = world.tick(NOW + timedelta(days=30))
    assert result.fired == [] and enrollment_id not in result.skipped()


@pytest.mark.parametrize("status", [CampaignStatus.PAUSED, CampaignStatus.REVIEWING])
def test_a_campaign_that_is_not_active_fires_nothing(
    session_factory: sessionmaker[Session], status: CampaignStatus
) -> None:
    world = make_world(session_factory, status=status)
    enrollment_id = world.enroll_new()
    result = world.tick(NOW + timedelta(days=30))
    assert result.fired == [] and enrollment_id not in result.skipped()  # never even selected


def test_pause_and_resume_a_campaign(world: World) -> None:
    enrollment_id = world.enroll_new()
    world.write(lambda s: pause_campaign(s, world.user, world.campaign.id))
    assert world.tick().fired == []
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.next_action_at) == (EnrollmentStatus.ACTIVE, NOW)
    with pytest.raises(CampaignEngineError, match="not active"):
        world.write(lambda s: pause_campaign(s, world.user, world.campaign.id))
    world.write(lambda s: resume_campaign(s, world.user, world.campaign.id))
    assert [f.enrollment_id for f, _ in world.tick(NOW + timedelta(days=3)).fired] == [
        enrollment_id
    ]
    with pytest.raises(CampaignEngineError, match="not paused"):
        world.write(lambda s: resume_campaign(s, world.user, world.campaign.id))


def test_a_campaign_resume_keeps_an_enrollments_own_pause(world: World) -> None:
    """Why the campaign pause holds on the campaign: a resume must not undo a person's pause."""
    paused_by_hand = world.enroll_new()
    world.write(lambda s: pause_enrollment(s, world.user, paused_by_hand))
    world.write(lambda s: pause_campaign(s, world.user, world.campaign.id))
    world.write(lambda s: resume_campaign(s, world.user, world.campaign.id))
    assert world.tick().fired == []
    enrollment = world.enrollment(paused_by_hand)
    assert (enrollment.status, enrollment.next_action_at) == (EnrollmentStatus.PAUSED, NOW)
    world.write(lambda s: resume_enrollment(s, world.user, paused_by_hand))
    assert world.tick().fired != []


def test_enrollment_moves_refuse_the_wrong_state(world: World) -> None:
    enrollment_id = world.enroll_new(status=EnrollmentStatus.COMPLETED)
    for move, match in (
        (pause_enrollment, "not active"),
        (resume_enrollment, "not paused"),
        (remove_enrollment, "already completed"),
    ):

        def attempt(
            session: Session, move: Callable[[Session, User, int], Enrollment] = move
        ) -> None:
            move(session, world.user, enrollment_id)

        with pytest.raises(CampaignEngineError, match=match):
            world.write(attempt)


def test_removal_ends_the_enrollment_and_leaves_a_scheduled_message_for_reconcile(
    world: World,
) -> None:
    """#269: a ``scheduled`` message may be in the sender's hands, or out already. Only
    reconcile, after a search by Message-ID, says whether it went; never a blind discard."""
    enrollment_id = world.enroll_new()

    def with_waiting(session: Session) -> int:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        return factories.make_message(
            session, enrollment, status=MessageStatus.SCHEDULED, sent_at=None
        ).id

    waiting = world.write(with_waiting)
    world.write(lambda s: remove_enrollment(s, world.user, enrollment_id))
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.exit_reason) == (EnrollmentStatus.REMOVED, "removed")
    assert enrollment.next_action_at is None
    assert [m.status for m in world.messages() if m.id == waiting] == [MessageStatus.SCHEDULED]


# --- #261 requirement 2: never fire a step twice -------------------------------------------


def test_a_step_with_an_outbound_message_already_is_refused(world: World) -> None:
    """The step's message is on the enrollment, whatever ``current_step`` says (#242)."""
    enrollment_id = world.enroll_new()

    def already(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        factories.make_message(session, enrollment, position=1, sent_at=NOW - timedelta(days=60))

    world.write(already)
    result = world.tick()
    assert result.fired == [] and reasons_of(result, enrollment_id) == (Skip.STEP_ALREADY_SENT,)
    assert len(world.messages(enrollment_id)) == 1
    assert world.enrollment(enrollment_id).next_action_at is None  # parked, not retried


def test_a_discarded_message_of_the_step_counts_too(world: World) -> None:
    """Review of #264: a merge discards the outranked side's ``scheduled`` message, which
    may be in the sender's hands. If the process stops before the send is recorded, the
    discarded row is all that says the step fired: it must refuse the step."""
    enrollment_id = world.enroll_new()

    def discarded(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        factories.make_message(
            session, enrollment, position=1, status=MessageStatus.DISCARDED, sent_at=None
        )

    world.write(discarded)
    result = world.tick()
    assert result.fired == [] and reasons_of(result, enrollment_id) == (Skip.STEP_ALREADY_SENT,)


def test_a_crash_after_the_claim_never_sends_the_step_again(world: World) -> None:
    """Restart safety (spec 11.4): the claim is committed before the send."""

    class Crash(BaseException):
        pass

    enrollment_id = world.enroll_new()
    world.sender.raises = Crash()
    with pytest.raises(Crash):
        world.tick()
    [claimed] = world.messages(enrollment_id)
    assert claimed.status is MessageStatus.SCHEDULED
    assert world.enrollment(enrollment_id).next_action_at is None

    world.sender.raises = None
    assert world.tick(NOW + timedelta(hours=1)).fired == []
    # Even with its due time put back, the step is refused.
    world.write(
        lambda s: setattr(
            get_scoped(s, world.user, Enrollment, enrollment_id), "next_action_at", NOW
        )
    )
    result = world.tick(NOW + timedelta(hours=2))
    assert result.fired == [] and reasons_of(result, enrollment_id) == (Skip.STEP_ALREADY_SENT,)
    assert len(world.sender.firings) == 1


def test_a_sender_failure_is_the_messages_and_never_retried(world: World) -> None:
    enrollment_id = world.enroll_new()
    world.sender.outcome = SendOutcome.FAILED
    [(_, outcome)] = world.tick().fired
    assert outcome.outcome is SendOutcome.FAILED
    [message] = world.messages(enrollment_id)
    assert message.status is MessageStatus.FAILED
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.current_step, enrollment.next_action_at) == (None, None)
    world.sender.outcome = SendOutcome.SENT
    assert world.tick(NOW + timedelta(days=1)).fired == []


def test_a_sender_that_raises_leaves_the_outcome_unknown_never_failed(world: World) -> None:
    """#269: a sender may raise after Gmail took the message. The message stays
    ``scheduled`` (the "did it go out?" signal) and is never sent again."""
    enrollment_id = world.enroll_new()
    world.sender.raises = RuntimeError("connection reset, token abc123")
    result = world.tick()
    [(_, outcome)] = result.fired
    assert outcome.outcome is SendOutcome.UNKNOWN
    [message] = world.messages(enrollment_id)
    assert message.status is MessageStatus.SCHEDULED
    assert message.error is not None
    assert "sender raised RuntimeError" in message.error
    assert "abc123" not in message.error  # never the exception's text
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.current_step, enrollment.next_action_at) == (None, None)
    world.sender.raises = None
    assert world.tick(NOW + timedelta(days=1)).fired == []


def test_a_merge_does_not_send_a_step_twice(world: World) -> None:
    """#242's case end to end: step 1 went to L, S was enrolled too, then they merge."""
    to_loser = world.enroll_new(email="l@example.test")
    to_survivor = world.enroll_new(email="s@example.test")
    world.tick()  # step 1 to the loser's enrollment (the older)
    loser_id, survivor_id = (world.enrollment(i).contact_id for i in (to_loser, to_survivor))
    world.write(lambda s: merge_contacts(s, world.user, survivor_id, loser_id))
    kept = world.enrollment(to_survivor)
    assert kept.current_step == 1
    # An hour past the time of day it was due, so the day-old due time does not spill.
    world.tick(NOW + timedelta(days=30, hours=1))
    steps = [m.step_id for m in world.messages()]
    assert len(steps) == len(set(steps)) == 2  # step 1 once, then step 2


# --- #261 requirement 3: cadence from the latest outbound message ---------------------------


def test_the_next_step_counts_from_the_latest_outbound_message(world: World) -> None:
    """#242 review: the survivor sent step 2 fourteen days ago and the loser's step 1 went
    out yesterday. After the merge, step 3 is not due today."""
    enrollment_id = world.enroll_new(current_step=2)

    def history(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        factories.make_message(session, enrollment, position=1, sent_at=NOW - timedelta(days=21))
        factories.make_message(session, enrollment, position=2, sent_at=NOW - timedelta(days=14))
        # What the merge brought in: a newer step-1 message from the other contact.
        factories.make_message(session, enrollment, position=1, sent_at=NOW - timedelta(days=1))

    world.write(history)
    result = world.tick()
    assert result.fired == [] and reasons_of(result, enrollment_id) == (Skip.NOT_DUE,)
    # A week after Monday 14:00 is Monday: the next suggested slot is Tuesday 09:00 (#338).
    due = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    assert world.enrollment(enrollment_id).next_action_at == due
    assert world.tick(due).fired != []


# --- #261 requirement 5: mailbox health ---------------------------------------------------


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"status": MailboxStatus.REAUTH_REQUIRED}, "mailbox_unhealthy"),
        ({"status": MailboxStatus.DISABLED}, "mailbox_unhealthy"),
    ],
)
def test_an_unhealthy_mailbox_pauses_email_steps(
    world: World, change: dict[str, Any], reason: str
) -> None:
    ids = [world.enroll_new() for _ in range(2)]

    def mark(session: Session) -> None:
        mailbox = get_scoped(session, world.user, Mailbox, world.mailbox.id)
        assert mailbox is not None
        for name, value in change.items():
            setattr(mailbox, name, value)

    world.write(mark)
    result = world.tick()
    assert result.fired == [] and world.messages() == []
    assert reasons_of(result, ids[0]) == (reason,)
    assert reasons_of(result, ids[1]) == (Skip.CAMPAIGN_BLOCKED, reason)
    assert world.enrollment(ids[0]).next_action_at == NOW  # nothing marked, nothing moved
    world.write(
        lambda s: setattr(
            get_scoped(s, world.user, Mailbox, world.mailbox.id), "status", MailboxStatus.OK
        )
    )
    assert world.tick().fired != []


@pytest.mark.parametrize("mailbox", ["none", "another_users"])
def test_an_unknown_mailbox_pauses_email_steps(
    session_factory: sessionmaker[Session], mailbox: str
) -> None:
    world = make_world(session_factory)

    def point(session: Session) -> None:
        campaign = get_scoped(session, world.user, Campaign, world.campaign.id)
        assert campaign is not None
        campaign.status = CampaignStatus.DRAFT  # the mailbox is locked outside draft (#269)
        if mailbox == "none":
            campaign.mailbox_id = None
        else:
            campaign.mailbox_id = make_mailbox(session, factories.make_user(session)).id
        campaign.status = CampaignStatus.ACTIVE

    world.write(point)
    enrollment_id = world.enroll_new()
    result = world.tick()
    assert result.fired == [] and reasons_of(result, enrollment_id) == ("mailbox_unknown",)


# --- guards at the fire --------------------------------------------------------------------


def _set_contact(world: World, enrollment_id: int, **changes: Any) -> None:
    def change(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        contact = get_scoped(session, world.user, Contact, enrollment.contact_id)
        assert contact is not None
        for name, value in changes.items():
            setattr(contact, name, value)

    world.write(change)


def test_do_not_contact_at_the_fire_opts_the_enrollment_out(world: World) -> None:
    """Spec 11.3: ``active -> opted_out`` when do_not_contact is set."""
    enrollment_id = world.enroll_new()
    _set_contact(world, enrollment_id, do_not_contact=True)
    result = world.tick()
    assert result.fired == [] and reasons_of(result, enrollment_id)[0] == Skip.ENDED
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.exit_reason) == (
        EnrollmentStatus.OPTED_OUT,
        "do_not_contact",
    )
    assert enrollment.next_action_at is None


def test_a_bounced_address_at_the_fire_ends_the_enrollment_bounced(world: World) -> None:
    enrollment_id = world.enroll_new()

    def bounce(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        contact = get_scoped(session, world.user, Contact, enrollment.contact_id)
        assert contact is not None
        contact.emails[0].status = EmailStatus.BOUNCED

    world.write(bounce)
    world.tick()
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.exit_reason) == (
        EnrollmentStatus.BOUNCED,
        "email_bounced",
    )


def test_any_other_exclusion_is_checked_again_a_day_later(world: World) -> None:
    enrollment_id = world.enroll_new()

    def called(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        add_interaction(
            session,
            world.user,
            enrollment.contact_id,
            InteractionKind.CALL,
            NOW - timedelta(days=2),
        )

    world.write(called)
    result = world.tick()
    assert reasons_of(result, enrollment_id) == (Skip.GUARD_EXCLUDED, "contacted_recently")
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.next_action_at) == (
        EnrollmentStatus.ACTIVE,
        NOW + timedelta(days=1),
    )


def test_a_duplicate_address_at_the_fire_holds_the_newer_enrollment(world: World) -> None:
    """#238 at the fire: the older proceeds, the newer is held."""
    older = world.enroll_new(email="same@example.test")
    newer = world.enroll_new(email="same@example.test")
    first = world.tick()
    assert [f.enrollment_id for f, _ in first.fired] == [older]
    second = world.tick(NOW + timedelta(hours=1))
    assert reasons_of(second, newer) == (Skip.GUARD_EXCLUDED, "duplicate_address")


def test_a_reply_on_the_enrollment_ends_it_replied(world: World) -> None:
    """Spec 11.3: ``active -> replied``. P3-08 detects; the tick refuses to fire past one."""
    enrollment_id = world.enroll_new(current_step=1)
    replied_at = NOW - timedelta(days=2)

    def history(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        factories.make_message(session, enrollment, position=1, sent_at=NOW - timedelta(days=8))
        factories.make_message(
            session,
            enrollment,
            position=1,
            direction=MessageDirection.IN,
            status=MessageStatus.RECEIVED,
            sent_at=replied_at,
        )

    world.write(history)
    result = world.tick()
    assert result.fired == [] and reasons_of(result, enrollment_id) == (Skip.ENDED, Skip.REPLIED)
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.replied_at) == (EnrollmentStatus.REPLIED, replied_at)


def test_a_template_with_lint_errors_is_parked(world: World) -> None:
    enrollment_id = world.enroll_new()

    def break_it(session: Session) -> None:
        campaign = get_scoped(session, world.user, Campaign, world.campaign.id)
        assert campaign is not None
        campaign.steps[0].template.body = "Hello everyone"  # no per-contact field

    world.write(break_it)
    result = world.tick()
    assert result.fired == [] and reasons_of(result, enrollment_id) == (Skip.TEMPLATE_ERRORS,)
    assert world.messages() == []


# --- LinkedIn steps are P4 ----------------------------------------------------------------


def test_a_linkedin_step_is_left_unfired_and_blocks_nobody(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory, channels=(LINKEDIN, EMAIL))
    linkedin = world.enroll_new()  # due first
    world.write(
        lambda s: setattr(
            get_scoped(s, world.user, Enrollment, linkedin),
            "next_action_at",
            NOW - timedelta(hours=1),
        )
    )
    email_step = world.enroll_new(current_step=1)

    def sent_step_one(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, email_step)
        assert enrollment is not None
        factories.make_message(session, enrollment, position=1, sent_at=NOW - timedelta(days=8))

    world.write(sent_step_one)
    result = world.tick()
    assert [f.enrollment_id for f, _ in result.fired] == [email_step]
    assert reasons_of(result, linkedin) == (Skip.LINKEDIN_STEP,)
    assert world.enrollment(linkedin).next_action_at == NOW - timedelta(hours=1)


# --- enrollment and activation ------------------------------------------------------------


def test_enroll_makes_pending_enrollments_of_those_the_guards_pass(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory, status=CampaignStatus.REVIEWING)

    def run(session: Session) -> Any:
        user = session.get(User, world.user.id)
        assert user is not None
        a = factories.make_contact(session, user, emails=["a@example.test"])
        b = factories.make_contact(session, user, emails=["a@example.test"])  # a's address
        c = factories.make_contact(session, user, emails=["c@example.test"], do_not_contact=True)
        first = enroll(session, user, world.campaign.id, [c.id, b.id, a.id], now=NOW)
        again = enroll(session, user, world.campaign.id, [a.id], now=NOW)
        return a.id, first, again

    a_id, first, again = world.write(run)
    assert first.enrolled == (a_id,)
    assert {
        v.contact_id: [r.value for r in v.reasons] for v in first.verdicts if not v.eligible
    } == {
        a_id + 1: ["duplicate_address"],
        a_id + 2: ["do_not_contact"],
    }
    assert (again.enrolled, again.already) == ((), (a_id,))
    [row] = world.read(lambda s: list(s.scalars(scoped(world.user, Enrollment))))
    assert (row.status, row.next_action_at) == (EnrollmentStatus.PENDING, None)


@pytest.mark.parametrize(
    "status", [CampaignStatus.ACTIVE, CampaignStatus.PAUSED, CampaignStatus.COMPLETED]
)
def test_enroll_is_refused_after_review(
    session_factory: sessionmaker[Session], status: CampaignStatus
) -> None:
    world = make_world(session_factory, status=status)
    with pytest.raises(CampaignEngineError, match="nobody can join"):
        world.write(lambda s: enroll(s, world.user, world.campaign.id, [1], now=NOW))


def test_enroll_needs_a_writer(session_factory: sessionmaker[Session]) -> None:
    world = make_world(session_factory, status=CampaignStatus.REVIEWING)
    with pytest.raises(RuntimeError, match="writer session"):
        world.read(lambda s: enroll(s, world.user, world.campaign.id, [1], now=NOW))


def _activate_at(world: World, starts_at: datetime, now: datetime = NOW) -> Campaign:
    return world.write(
        lambda s: activate(
            s,
            world.user,
            world.campaign.id,
            settings=Settings(),
            now=now,
            starts_at=starts_at,
            gate=REVIEW_GATE,
        )
    )


def test_activate_makes_pending_active_with_the_first_step_at_the_start(
    session_factory: sessionmaker[Session],
) -> None:
    """#338: the start is stored on the campaign, and step 1 is due exactly then."""
    world = make_world(
        session_factory, status=CampaignStatus.REVIEWING, approved_at=NOW, starts_at=None
    )
    enrollment_id = world.enroll_new(status=EnrollmentStatus.PENDING, next_action_at=None)
    start = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    _activate_at(world, start)
    enrollment = world.enrollment(enrollment_id)
    assert enrollment.status is EnrollmentStatus.ACTIVE
    assert enrollment.next_action_at == start
    campaign = world.read(lambda s: get_scoped(s, world.user, Campaign, world.campaign.id))
    assert campaign is not None and campaign.status is CampaignStatus.ACTIVE
    assert campaign.starts_at == start
    assert world.tick(start - timedelta(seconds=1)).fired == []
    assert world.tick(start).fired != []


def test_a_start_already_past_starts_now(session_factory: sessionmaker[Session]) -> None:
    world = make_world(
        session_factory, status=CampaignStatus.REVIEWING, approved_at=NOW, starts_at=None
    )
    enrollment_id = world.enroll_new(status=EnrollmentStatus.PENDING, next_action_at=None)
    campaign = _activate_at(world, NOW - timedelta(days=3))
    assert campaign.starts_at == NOW
    assert world.enrollment(enrollment_id).next_action_at == NOW


def test_a_first_step_with_a_delay_aims_for_a_suggested_slot(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(
        session_factory, status=CampaignStatus.REVIEWING, approved_at=NOW, starts_at=None
    )

    def delayed(session: Session) -> None:
        campaign = get_scoped(session, world.user, Campaign, world.campaign.id)
        assert campaign is not None
        campaign.steps[0].delay_days = 2

    world.write(delayed)
    enrollment_id = world.enroll_new(status=EnrollmentStatus.PENDING, next_action_at=None)
    _activate_at(world, NOW)  # Tuesday 14:00, so Thursday 14:00, inside the slot
    assert world.enrollment(enrollment_id).next_action_at == NOW + timedelta(days=2)


def test_activate_refuses_a_naive_start(session_factory: sessionmaker[Session]) -> None:
    world = make_world(session_factory, status=CampaignStatus.REVIEWING, approved_at=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        _activate_at(world, datetime(2026, 10, 6, 9, 0))


# --- moving the start, and a step's timing (#338) ---------------------------------------------


def test_the_start_moves_until_the_first_send_and_takes_waiting_enrollments_with_it(
    world: World,
) -> None:
    later = NOW + timedelta(days=1)
    _start_at(world, later)
    waiting = world.enroll_new(next_action_at=later)
    parked = world.enroll_new(next_action_at=None)
    sooner = NOW + timedelta(hours=1)
    moved = world.write(
        lambda s: engine_module.set_start(
            s, world.user, world.campaign.id, settings=Settings(), now=NOW, starts_at=sooner
        )
    )
    assert moved.starts_at == sooner
    assert world.enrollment(waiting).next_action_at == sooner
    assert world.enrollment(parked).next_action_at is None
    assert world.tick(sooner).fired != []
    with pytest.raises(CampaignEngineError, match="already sent"):
        world.write(
            lambda s: engine_module.set_start(
                s, world.user, world.campaign.id, settings=Settings(), now=NOW, starts_at=later
            )
        )


@pytest.mark.parametrize("status", [CampaignStatus.REVIEWING, CampaignStatus.COMPLETED])
def test_the_start_moves_only_on_an_active_or_paused_campaign(
    session_factory: sessionmaker[Session], status: CampaignStatus
) -> None:
    world = make_world(session_factory, status=status)
    with pytest.raises(CampaignEngineError, match="only an active or paused"):
        world.write(
            lambda s: engine_module.set_start(
                s, world.user, world.campaign.id, settings=Settings(), now=NOW, starts_at=NOW
            )
        )


def test_a_changed_step_time_moves_the_enrollments_waiting_for_it(world: World) -> None:
    enrollment_id = world.enroll_new()
    world.tick()  # step 1 sent at 14:07; step 2 due a week on, 14:07
    other = world.enroll_new(next_action_at=NOW + timedelta(days=1))  # still on step 1

    def change(session: Session) -> int:
        campaign = get_scoped(session, world.user, Campaign, world.campaign.id)
        assert campaign is not None
        step = campaign.steps[1]
        step.delay_days, step.send_time = 3, "22:00"
        return engine_module.reschedule_step(session, world.user, step, settings=Settings())

    assert world.write(change) == 1
    assert world.enrollment(enrollment_id).next_action_at == datetime(
        2026, 10, 2, 22, 0, tzinfo=UTC
    )
    assert world.enrollment(other).next_action_at == NOW + timedelta(days=1)


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"status": CampaignStatus.DRAFT}, "not reviewing"),
        ({"approved_at": None}, "not passed review"),
        ({"mailbox_id": None}, "no mailbox"),
    ],
)
def test_activate_is_refused_until_the_campaign_is_ready(
    session_factory: sessionmaker[Session], changes: dict[str, Any], match: str
) -> None:
    fields: dict[str, Any] = {"status": CampaignStatus.REVIEWING, "approved_at": NOW}
    world = make_world(session_factory, **fields)

    def change(session: Session) -> None:
        campaign = get_scoped(session, world.user, Campaign, world.campaign.id)
        assert campaign is not None
        status = changes.get("status", campaign.status)
        campaign.status = CampaignStatus.DRAFT  # the mailbox is locked outside draft (#269)
        for name, value in changes.items():
            setattr(campaign, name, value)
        campaign.status = status

    world.write(change)
    with pytest.raises(CampaignEngineError, match=match):
        world.write(
            lambda s: activate(
                s,
                world.user,
                world.campaign.id,
                settings=SETTINGS,
                now=NOW,
                starts_at=NOW,
                gate=REVIEW_GATE,
            )
        )


def test_activate_is_refused_outside_the_review_gate(
    session_factory: sessionmaker[Session],
) -> None:
    """Even with ``approved_at`` set, only the gate's token activates (#288)."""
    world = make_world(session_factory, status=CampaignStatus.REVIEWING, approved_at=NOW)
    with pytest.raises(CampaignEngineError, match="review gate"):
        world.write(
            lambda s: activate(
                s, world.user, world.campaign.id, settings=SETTINGS, now=NOW, starts_at=NOW
            )
        )
    campaign = world.read(lambda s: get_scoped(s, world.user, Campaign, world.campaign.id))
    assert campaign is not None and campaign.status is CampaignStatus.REVIEWING


def test_activate_is_refused_over_a_template_with_lint_errors(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory, status=CampaignStatus.REVIEWING, approved_at=NOW)

    def break_it(session: Session) -> None:
        campaign = get_scoped(session, world.user, Campaign, world.campaign.id)
        assert campaign is not None
        campaign.steps[1].template.subject = None

    world.write(break_it)
    with pytest.raises(CampaignEngineError, match="step 2's template"):
        world.write(
            lambda s: activate(
                s,
                world.user,
                world.campaign.id,
                settings=SETTINGS,
                now=NOW,
                starts_at=NOW,
                gate=REVIEW_GATE,
            )
        )


# --- users ---------------------------------------------------------------------------------


def test_each_user_ticks_their_own_campaigns(session_factory: sessionmaker[Session]) -> None:
    alice = make_world(session_factory)
    bob = make_world(session_factory)
    mine = alice.enroll_new()
    theirs = bob.enroll_new()
    results = run_tick(
        session_factory,
        settings=SETTINGS,
        sender=alice.sender,
        clock=lambda: NOW,
        rng=random.Random(1),
    )
    fired = {r.user_id: [f.enrollment_id for f, _ in r.fired] for r in results}
    assert fired == {alice.user.id: [mine], bob.user.id: [theirs]}
    assert {m.contact_id for m in bob.messages()} == {bob.enrollment(theirs).contact_id}


# --- the minute loop runs off the event loop (#259) ---------------------------------------


async def test_the_engine_ticks_in_a_worker_thread(world: World) -> None:
    seen: list[int] = []

    class Recording(FakeSender):
        def send(self, firing: Firing) -> SendResult:
            seen.append(threading.get_ident())
            return super().send(firing)

    world.enroll_new()
    engine = CampaignEngine(world.factory, SETTINGS, Recording(), clock=lambda: NOW)
    [result] = [r for r in await engine.tick_once() if r.user_id == world.user.id]
    assert len(result.fired) == 1
    assert seen and seen[0] != threading.get_ident()


def test_the_engine_refuses_no_interval(world: World) -> None:
    with pytest.raises(ValueError, match="positive"):
        CampaignEngine(world.factory, SETTINGS, None, interval_s=0)


# --- review of #264 ------------------------------------------------------------------------


def test_blocked_rows_never_starve_another_campaign(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows of a capped campaign keep their old due time. However many there are, they are
    left out of the scan, so another campaign on another mailbox still fires."""
    monkeypatch.setattr(engine_module, "SCAN_LIMIT", 2)
    monkeypatch.setattr(engine_module, "PAGE_SIZE", 1)
    capped = make_world(session_factory, daily_cap=0)
    ahead = [capped.enroll_new(next_action_at=NOW - timedelta(days=1)) for _ in range(3)]

    def other_campaign(session: Session) -> int:
        user = session.get(User, capped.user.id)
        assert user is not None
        mailbox = make_mailbox(session, user, email="other-box@example.test")
        campaign = factories.make_campaign(session, user, mailbox_id=mailbox.id)
        contact = factories.make_contact(session, user, emails=["behind@example.test"])
        return factories.make_enrollment(session, campaign, contact, next_action_at=NOW).id

    behind = capped.write(other_campaign)
    result = capped.tick()
    assert [f.enrollment_id for f, _ in result.fired] == [behind]
    assert reasons_of(result, ahead[0]) == (Skip.CAMPAIGN_AT_CAP,)


def test_linkedin_rows_never_starve_an_email_step(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "SCAN_LIMIT", 2)
    monkeypatch.setattr(engine_module, "PAGE_SIZE", 1)
    world = make_world(session_factory, channels=(LINKEDIN, EMAIL))
    linkedin = [world.enroll_new(next_action_at=NOW - timedelta(days=1)) for _ in range(3)]
    email_step = world.enroll_new(current_step=1)

    def sent_step_one(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, email_step)
        assert enrollment is not None
        factories.make_message(session, enrollment, position=1, sent_at=NOW - timedelta(days=8))

    world.write(sent_step_one)
    result = world.tick()
    assert [f.enrollment_id for f, _ in result.fired] == [email_step]
    assert reasons_of(result, linkedin[0]) == (Skip.LINKEDIN_STEP,)  # reported a page at most


@pytest.mark.parametrize(("median", "floor"), [(240, 0), (0, 90), (240, -5)])
def test_a_spacing_that_is_not_one_holds_every_send(world: World, median: int, floor: int) -> None:
    """Review of #264: refused before anything is claimed, never after the sender sent."""
    world.enroll_new()
    settings = Settings(
        campaigns=CampaignSettings(send_spacing_median_s=median, send_spacing_floor_s=floor)
    )
    results = run_tick(world.factory, settings=settings, sender=world.sender, clock=lambda: NOW)
    assert results == [] and world.sender.firings == [] and world.messages() == []


def test_one_users_failure_does_not_stop_the_next(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = make_world(session_factory)
    bob = make_world(session_factory)
    alice.enroll_new()
    theirs = bob.enroll_new()
    real = engine_module.tick_user

    def failing_for_alice(factory: Any, user_id: int, **kwargs: Any) -> TickResult:
        if user_id == alice.user.id:
            raise RuntimeError("boom")
        return real(factory, user_id, **kwargs)

    monkeypatch.setattr(engine_module, "tick_user", failing_for_alice)
    results = run_tick(session_factory, settings=SETTINGS, sender=bob.sender, clock=lambda: NOW)
    assert [(r.user_id, [f.enrollment_id for f, _ in r.fired]) for r in results] == [
        (bob.user.id, [theirs])
    ]


def test_a_gap_in_step_positions_fires_the_next_position_up(
    session_factory: sessionmaker[Session],
) -> None:
    """Review of #264: steps at 1, 2 and 4. After step 2, step 4 fires; nothing ends early."""
    world = make_world(session_factory)

    def renumber(session: Session) -> None:
        campaign = get_scoped(session, world.user, Campaign, world.campaign.id)
        assert campaign is not None
        campaign.steps[2].position = 4

    world.write(renumber)
    enrollment_id = world.enroll_new(current_step=2)

    def history(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        factories.make_message(session, enrollment, position=1, sent_at=NOW - timedelta(days=20))
        factories.make_message(session, enrollment, position=2, sent_at=NOW - timedelta(days=10))

    world.write(history)
    [(firing, _)] = world.tick().fired
    assert firing.step_position == 4
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.current_step) == (EnrollmentStatus.COMPLETED, 4)


# --- #264 review: rules that correct code had, and no test pinned -----------------------


def test_a_long_batch_does_not_send_overnight(session_factory: sessionmaker[Session]) -> None:
    """#338: a batch started at 22:00 sends until the local midnight, then the rest spills
    to 22:00 the next day, not on through the night."""
    world = make_world(session_factory)
    _start_at(world, SATURDAY_NIGHT)
    ids = [world.enroll_new(next_action_at=SATURDAY_NIGHT) for _ in range(40)]
    now = SATURDAY_NIGHT
    fired: list[datetime] = []
    while now < SATURDAY_NIGHT + timedelta(hours=26):
        result = world.tick(now, seed=len(fired))
        if result.fired:
            fired.append(now)
            now += timedelta(minutes=1)
            continue
        wakes = [w for w in (result.next_wake,) if w is not None]
        now = max(min(wakes), now + timedelta(minutes=1)) if wakes else now + timedelta(hours=1)
    midnight = datetime(2026, 10, 4, tzinfo=UTC)
    assert [at for at in fired if at < midnight]  # some went Saturday night
    # Nothing between the local midnight and the next 22:00.
    assert not [
        at
        for at in fired
        if datetime(2026, 10, 4, tzinfo=UTC) <= at < datetime(2026, 10, 4, 22, tzinfo=UTC)
    ]
    assert [at for at in fired if at >= datetime(2026, 10, 4, 22, tzinfo=UTC)]
    waiting = [
        world.enrollment(i).next_action_at for i in ids if world.enrollment(i).current_step is None
    ]
    assert all(at is None or at >= datetime(2026, 10, 4, 22, tzinfo=UTC) for at in waiting)


def test_the_time_is_read_once_the_write_lock_is_held(
    session_factory: sessionmaker[Session],
) -> None:
    """A tick that started at 23:59:30 and got the write lock at midnight must not claim a
    step due yesterday: at midnight it spills to tomorrow's start time (#338)."""
    world = make_world(session_factory)
    world.enroll_new()
    times = iter([datetime(2026, 9, 29, 23, 59, 30, tzinfo=UTC)])
    after = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
    results = run_tick(
        world.factory,
        settings=Settings(),
        sender=world.sender,
        clock=lambda: next(times, after),
        rng=random.Random(1),
    )
    assert [r.fired for r in results] == [[]]
    assert world.messages() == []


@pytest.mark.parametrize("delay", ["step_delay", "deferred"])
def test_a_first_step_due_later_does_not_fire_now(
    session_factory: sessionmaker[Session], delay: str
) -> None:
    """Nothing but the due time holds step 1 back: selection must require it."""
    world = make_world(session_factory)
    if delay == "step_delay":

        def three_days(session: Session) -> None:
            campaign = get_scoped(session, world.user, Campaign, world.campaign.id)
            assert campaign is not None
            campaign.steps[0].delay_days = 3

        world.write(three_days)
    enrollment_id = world.enroll_new(next_action_at=NOW + timedelta(days=3))
    result = world.tick()
    assert result.fired == [] and enrollment_id not in result.skipped()
    assert world.tick(NOW + timedelta(days=3)).fired != []


def _seed_fired_today(world: World, campaign_id: int, statuses: list[MessageStatus]) -> None:
    """Messages fired today on enrollments with no due step, in each of ``statuses``."""

    def seed(session: Session) -> None:
        user = session.get(User, world.user.id)
        assert user is not None
        campaign = get_scoped(session, user, Campaign, campaign_id)
        assert campaign is not None
        for status in statuses:
            contact = factories.make_contact(session, user)
            enrollment = factories.make_enrollment(session, campaign, contact)
            factories.make_message(
                session,
                enrollment,
                status=status,
                sent_at=None,
                scheduled_at=NOW - timedelta(hours=1),
            )

    world.write(seed)


UNSENT_TODAY = [MessageStatus.FAILED, MessageStatus.DRAFTED, MessageStatus.SCHEDULED]


def test_the_campaign_cap_counts_every_status_fired_today(
    session_factory: sessionmaker[Session],
) -> None:
    """A failed send may have gone out, and a draft goes out when the person sends it."""
    world = make_world(session_factory, daily_cap=3)
    _seed_fired_today(world, world.campaign.id, UNSENT_TODAY)
    enrollment_id = world.enroll_new()
    result = world.tick()
    assert result.fired == [] and reasons_of(result, enrollment_id) == (Skip.CAMPAIGN_AT_CAP,)


def test_the_mailbox_cap_counts_every_status_fired_today(world: World) -> None:
    world.write(
        lambda s: setattr(get_scoped(s, world.user, Mailbox, world.mailbox.id), "daily_cap", 3)
    )

    def other_campaign(session: Session) -> int:
        user = session.get(User, world.user.id)
        assert user is not None
        return factories.make_campaign(session, user, mailbox_id=world.mailbox.id).id

    _seed_fired_today(world, world.write(other_campaign), UNSENT_TODAY)
    enrollment_id = world.enroll_new()
    result = world.tick()
    assert result.fired == [] and reasons_of(result, enrollment_id) == ("mailbox_at_cap",)


def test_the_mailbox_cap_is_never_over_400(world: World) -> None:
    """Spec 11.4's hard max holds even over a ``daily_cap`` stored higher."""
    world.write(
        lambda s: setattr(get_scoped(s, world.user, Mailbox, world.mailbox.id), "daily_cap", 1000)
    )

    def four_hundred(session: Session) -> None:
        user = session.get(User, world.user.id)
        assert user is not None
        other = factories.make_campaign(session, user, mailbox_id=world.mailbox.id)
        contact = factories.make_contact(session, user)
        enrollment = factories.make_enrollment(session, other, contact)
        step_id = other.steps[0].id
        session.add_all(
            Message(
                user_id=user.id,
                enrollment_id=enrollment.id,
                step_id=step_id,
                contact_id=contact.id,
                channel=EMAIL,
                direction=MessageDirection.OUT,
                status=MessageStatus.SENT,
                scheduled_at=NOW - timedelta(hours=1),
                sent_at=NOW - timedelta(hours=1),
            )
            for _ in range(400)
        )
        session.flush()

    world.write(four_hundred)
    enrollment_id = world.enroll_new()
    result = world.tick()
    assert result.fired == [] and reasons_of(result, enrollment_id) == ("mailbox_at_cap",)


async def test_the_minute_loop_survives_a_failed_tick(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def flaky(*_: Any, **__: Any) -> list[TickResult]:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("the database went away")
        return []

    monkeypatch.setattr(engine_module, "run_tick", flaky)
    engine = CampaignEngine(world.factory, SETTINGS, world.sender, interval_s=0.01)
    engine.start()
    try:
        for _ in range(200):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await engine.stop()
    assert len(calls) >= 2


# --- P3-07 (#269): what the engine does with a sender's answer -----------------------------


def test_an_unknown_outcome_stays_scheduled_and_parked(world: World) -> None:
    """#269, requirement 1: Gmail's ``outcome_unknown`` is never recorded as ``failed``."""
    enrollment_id = world.enroll_new()
    world.sender.outcome = SendOutcome.UNKNOWN
    [(_, outcome)] = world.tick().fired
    assert outcome.outcome is SendOutcome.UNKNOWN
    [message] = world.messages(enrollment_id)
    assert (message.status, message.sent_at) == (MessageStatus.SCHEDULED, None)
    assert message.error is not None and "reconciling by Message-ID" in message.error
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.current_step, enrollment.next_action_at) == (None, None)
    assert world.read(lambda s: next_send_at(s, world.user, world.mailbox.id)) is not None
    world.sender.outcome = SendOutcome.SENT
    assert world.tick(NOW + timedelta(days=1)).fired == []  # never sent a second time


def test_a_naive_time_from_the_sender_is_refused_before_the_record(world: World) -> None:
    """#269, requirement 5: a naive ``at`` would make the record raise after the send.
    The record uses its own time instead."""

    class Naive(FakeSender):
        def send(self, firing: Firing) -> SendResult:
            self.firings.append(firing)
            return SendResult(
                SendOutcome.SENT,
                at=datetime(2026, 9, 29, 14, 7),  # no zone
                gmail_message_id="gm-1",
                gmail_thread_id="thread-1",
            )

    world.sender = Naive()
    enrollment_id = world.enroll_new()
    [(_, outcome)] = world.tick().fired
    assert outcome.at is None
    [message] = world.messages(enrollment_id)
    assert (message.status, message.sent_at) == (MessageStatus.SENT, NOW)
    assert world.enrollment(enrollment_id).next_action_at == NOW + timedelta(days=7)


def test_a_mailbox_address_with_no_domain_parks_the_step_before_the_claim(
    session_factory: sessionmaker[Session],
) -> None:
    """The Message-ID must be known before the send: no domain, no claim."""
    world = make_world(session_factory)
    world.write(
        lambda s: setattr(get_scoped(s, world.user, Mailbox, world.mailbox.id), "email", "me")
    )
    enrollment_id = world.enroll_new()
    result = world.tick()
    assert result.fired == []
    assert reasons_of(result, enrollment_id) == (Skip.MAILBOX_ADDRESS,)
    assert world.messages(enrollment_id) == []


def test_a_firing_carries_its_message_id_and_label(world: World) -> None:
    enrollment_id = world.enroll_new()
    [(firing, _)] = world.tick().fired
    [message] = world.messages(enrollment_id)
    assert firing.rfc822_message_id is not None
    assert firing.rfc822_message_id.endswith("@example.test>")
    assert firing.rfc822_message_id == message_id_for(
        user_id=world.user.id,
        message_id=message.id,
        created_at=message.created_at,
        address=world.mailbox.email,
    )
    assert firing.label == f"netkeeper/{world.campaign.name}"


def test_nothing_is_claimed_once_stopping(world: World) -> None:
    enrollment_id = world.enroll_new()
    results = run_tick(
        world.factory,
        settings=SETTINGS,
        sender=world.sender,
        clock=lambda: NOW,
        stopping=lambda: True,
    )
    assert results == []
    assert world.messages(enrollment_id) == []
    assert world.enrollment(enrollment_id).next_action_at == NOW


class _Held(FakeSender):
    """A send that blocks until the test lets it go."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def send(self, firing: Firing) -> SendResult:
        self.started.set()
        self.release.wait(5)
        return super().send(firing)


async def test_stop_waits_for_a_send_in_flight_so_it_is_recorded(world: World) -> None:
    """#269, requirement 4: the send in a worker thread is recorded before stop returns,
    so it is never written after the database is disposed."""
    sender = _Held()
    enrollment_id = world.enroll_new()
    engine = CampaignEngine(world.factory, SETTINGS, sender, clock=lambda: NOW, interval_s=0.01)
    engine.start()
    assert await asyncio.to_thread(sender.started.wait, 5)
    stopping = asyncio.ensure_future(engine.stop())
    await asyncio.sleep(0.05)
    assert not stopping.done()  # waiting for the send
    sender.release.set()
    assert await stopping is True
    [message] = world.messages(enrollment_id)
    assert message.status is MessageStatus.SENT
    assert len(sender.firings) == 1


async def test_stop_gives_up_after_its_bound_and_leaves_the_message_for_reconcile(
    world: World,
) -> None:
    sender = _Held()
    enrollment_id = world.enroll_new()
    engine = CampaignEngine(
        world.factory, SETTINGS, sender, clock=lambda: NOW, interval_s=0.01, stop_wait_s=0.05
    )
    engine.start()
    assert await asyncio.to_thread(sender.started.wait, 5)
    assert await engine.stop() is False
    [message] = world.messages(enrollment_id)
    assert message.status is MessageStatus.SCHEDULED  # the "did it go out?" signal
    sender.release.set()
    for _ in range(500):  # let the orphaned tick finish before the test's database goes
        if world.messages(enrollment_id)[0].status is not MessageStatus.SCHEDULED:
            break
        await asyncio.sleep(0.01)
    assert len(sender.firings) == 1


def test_the_stop_wait_cannot_be_negative(world: World) -> None:
    with pytest.raises(ValueError, match="negative"):
        CampaignEngine(world.factory, SETTINGS, None, stop_wait_s=-1)


def _leftovers(world: World, count: int, *, at: datetime) -> list[int]:
    def make(session: Session) -> list[int]:
        made: list[int] = []
        for n in range(count):
            contact = factories.make_contact(session, world.user, emails=[f"l{n}@example.test"])
            campaign = get_scoped(session, world.user, Campaign, world.campaign.id)
            assert campaign is not None
            enrollment = factories.make_enrollment(session, campaign, contact)
            made.append(
                factories.make_message(
                    session,
                    enrollment,
                    status=MessageStatus.SCHEDULED,
                    sent_at=None,
                    scheduled_at=at,
                ).id
            )
        return made

    return world.write(make)


def test_reconcile_lists_a_scheduled_message_only_once_it_is_a_leftover(world: World) -> None:
    [message_id] = _leftovers(world, 1, at=NOW)

    def listed(now: datetime) -> list[int]:
        work = world.read(lambda s: engine_module.reconcile_work(s, world.user, now=now))
        return [t.message_id for t in work.leftovers]

    assert listed(NOW + timedelta(minutes=9)) == []
    assert listed(NOW + timedelta(minutes=10)) == [message_id]


def test_reconcile_takes_a_bounded_batch_of_leftovers(world: World) -> None:
    made = _leftovers(world, 12, at=NOW)
    work = world.read(
        lambda s: engine_module.reconcile_work(s, world.user, now=NOW + timedelta(hours=1))
    )
    assert [t.message_id for t in work.leftovers] == made[:10]


def _settle_not_sent(
    world: World, message_id: int, session: Session, *, now: datetime = NOW
) -> bool:
    return engine_module.settle_not_sent(session, world.user, SETTINGS, message_id, now=now)


GIVE_UP_EVERY = engine_module.RECONCILE_GIVE_UP_AFTER / (engine_module.RECONCILE_GIVE_UP_MISSES - 1)
"""Misses this far apart rule a leftover out on the last one the give-up needs."""


def _rule_out(world: World, message_id: int) -> list[bool]:
    """Every empty search the give-up needs, spread over its span: what each returned."""
    return [
        world.write(partial(_settle_not_sent, world, message_id, now=NOW + GIVE_UP_EVERY * n))
        for n in range(engine_module.RECONCILE_GIVE_UP_MISSES)
    ]


def test_a_leftover_not_in_gmail_with_a_twin_is_discarded_the_other_failed(world: World) -> None:
    """A merge can leave two ``scheduled`` messages of one step on one enrollment. Neither is
    sent again: the first is ``discarded`` beside its twin, the last ``failed`` for a person."""
    enrollment_id = world.enroll_new(next_action_at=None)

    def two(session: Session) -> list[int]:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        fields: dict[str, Any] = {
            "status": MessageStatus.SCHEDULED,
            "sent_at": None,
            "scheduled_at": NOW,
        }
        return [factories.make_message(session, enrollment, **fields).id for _ in range(2)]

    first, second = world.write(two)
    for message_id in (first, second):
        assert _rule_out(world, message_id)[-1] is True
    statuses = {m.id: m.status for m in world.messages(enrollment_id)}
    assert statuses == {first: MessageStatus.DISCARDED, second: MessageStatus.FAILED}
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE


def test_settling_a_message_that_moved_on_changes_nothing(world: World) -> None:
    enrollment_id = world.enroll_new()
    world.tick()
    [message] = world.messages(enrollment_id)
    assert message.status is MessageStatus.SENT
    changed = world.write(
        lambda s: engine_module.settle_not_sent(s, world.user, SETTINGS, message.id, now=NOW)
    )
    assert changed is False
    assert world.messages(enrollment_id)[0].status is MessageStatus.SENT


def test_settle_sent_refuses_a_naive_time(world: World) -> None:
    [message_id] = _leftovers(world, 1, at=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        world.write(
            lambda s: engine_module.settle_sent(
                s,
                world.user,
                SETTINGS,
                message_id,
                expect=(MessageStatus.SCHEDULED,),
                at=datetime(2026, 9, 29, 14, 0),
                gmail_message_id="gm",
                gmail_thread_id="th",
            )
        )


# --- #269, requirement 6: the mailbox is locked once the campaign leaves draft --------------


@pytest.mark.parametrize(
    "status", [s for s in CampaignStatus if s is not CampaignStatus.DRAFT], ids=str
)
def test_a_campaign_past_draft_refuses_a_mailbox_change(
    session_factory: sessionmaker[Session], status: CampaignStatus
) -> None:
    world = make_world(session_factory, status=status)

    def change(session: Session, *, to: int | None) -> None:
        campaign = get_scoped(session, world.user, Campaign, world.campaign.id)
        assert campaign is not None
        campaign.mailbox_id = to

    other = world.write(lambda s: make_mailbox(s, world.user, email="other@example.test").id)
    for to in (other, None):
        with pytest.raises(CampaignMailboxLocked, match="cannot change once it leaves draft"):
            world.write(partial(change, to=to))
    world.write(partial(change, to=world.mailbox.id))  # the same one is no change
    kept = world.read(lambda s: get_scoped(s, world.user, Campaign, world.campaign.id))
    assert kept is not None and kept.mailbox_id == world.mailbox.id


def test_a_draft_campaign_may_change_its_mailbox(session_factory: sessionmaker[Session]) -> None:
    world = make_world(session_factory, status=CampaignStatus.DRAFT)
    other = world.write(lambda s: make_mailbox(s, world.user, email="other@example.test").id)

    def change(session: Session) -> None:
        campaign = get_scoped(session, world.user, Campaign, world.campaign.id)
        assert campaign is not None
        campaign.mailbox_id = other

    world.write(change)
    kept = world.read(lambda s: get_scoped(s, world.user, Campaign, world.campaign.id))
    assert kept is not None and kept.mailbox_id == other


def test_a_new_campaign_takes_any_mailbox_whatever_its_status(
    session_factory: sessionmaker[Session],
) -> None:
    """The lock is on a stored campaign: one being created has sent nothing yet."""
    world = make_world(session_factory, status=CampaignStatus.ACTIVE)
    assert world.campaign.mailbox_id == world.mailbox.id


# --- #273 review: search lag, a fair batch, not sent, and pinned choices ------------------


def test_the_reconcile_constants_are_pinned() -> None:
    """Safety constants against numbers written out here (CLAUDE.md)."""
    assert timedelta(minutes=20) == engine_module.RECONCILE_SEARCH_EVERY
    assert engine_module.RECONCILE_GIVE_UP_MISSES == 4
    assert timedelta(hours=2) == engine_module.RECONCILE_GIVE_UP_AFTER
    assert timedelta(minutes=15) == engine_module.RETRY_AFTER


def test_one_empty_search_never_rules_a_leftover_out(world: World) -> None:
    """Gmail's search can lag a send: misses are counted, and only enough of them, over
    enough time, rule it out."""
    [message_id] = _leftovers(world, 1, at=NOW)
    assert _rule_out(world, message_id) == [False, False, False, True]
    [message] = world.messages()
    assert message.status is MessageStatus.FAILED
    assert (message.reconcile_misses, message.reconcile_first_miss_at) == (4, NOW)


@pytest.mark.parametrize(
    "gaps",
    [
        # Many misses, close together: a lagging search, not an absent message.
        [timedelta(minutes=1)] * 20,
        # Few misses, far apart.
        [timedelta(hours=5)] * (4 - 2),
    ],
    ids=["many-close", "few-far"],
)
def test_misses_rule_nothing_out_until_both_count_and_time_are_met(
    world: World, gaps: list[timedelta]
) -> None:
    [message_id] = _leftovers(world, 1, at=NOW)
    at = NOW
    for gap in [timedelta(0), *gaps]:
        assert world.write(partial(_settle_not_sent, world, message_id, now=at)) is False
        at += gap
    [message] = world.messages()
    assert message.status is MessageStatus.SCHEDULED
    assert message.error is not None and "not found in Gmail yet" in message.error


def test_a_leftover_searched_recently_waits_its_turn(world: World) -> None:
    [message_id] = _leftovers(world, 1, at=NOW)
    first = NOW + timedelta(minutes=10)
    world.write(partial(_settle_not_sent, world, message_id, now=first))

    def listed(now: datetime) -> list[int]:
        work = world.read(lambda s: engine_module.reconcile_work(s, world.user, now=now))
        return [t.message_id for t in work.leftovers]

    assert listed(first + timedelta(minutes=19)) == []
    assert listed(first + timedelta(minutes=20)) == [message_id]


def test_settle_not_sent_refuses_a_naive_time(world: World) -> None:
    [message_id] = _leftovers(world, 1, at=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        world.write(partial(_settle_not_sent, world, message_id, now=datetime(2026, 9, 29)))


def test_reconcile_takes_a_batch_per_mailbox(world: World) -> None:
    """A mailbox whose leftovers cannot be searched never holds another mailbox's slots."""
    first = _leftovers(world, engine_module.RECONCILE_BATCH + 2, at=NOW)

    def elsewhere(session: Session) -> int:
        user = session.get(User, world.user.id)
        assert user is not None
        mailbox = make_mailbox(session, user, email="other-box@example.test")
        campaign = factories.make_campaign(session, user, mailbox_id=mailbox.id)
        contact = factories.make_contact(session, user, emails=["other@example.test"])
        enrollment = factories.make_enrollment(session, campaign, contact)
        return factories.make_message(
            session, enrollment, status=MessageStatus.SCHEDULED, sent_at=None, scheduled_at=NOW
        ).id

    other = world.write(elsewhere)
    work = world.read(
        lambda s: engine_module.reconcile_work(s, world.user, now=NOW + timedelta(hours=1))
    )
    assert [t.message_id for t in work.leftovers] == [
        *first[: engine_module.RECONCILE_BATCH],
        other,
    ]


def test_settle_sent_never_lowers_the_current_step(world: World) -> None:
    """A late find of step 1 on an enrollment already past step 2 leaves it where it is."""
    enrollment_id = world.enroll_new(current_step=2, next_action_at=None)

    def leftover(session: Session) -> int:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        return factories.make_message(
            session, enrollment, position=1, status=MessageStatus.SCHEDULED, sent_at=None
        ).id

    message_id = world.write(leftover)
    world.write(
        lambda s: engine_module.settle_sent(
            s,
            world.user,
            SETTINGS,
            message_id,
            expect=(MessageStatus.SCHEDULED,),
            at=NOW,
            gmail_message_id="gm",
            gmail_thread_id="th",
        )
    )
    assert world.enrollment(enrollment_id).current_step == 2


def test_a_follow_up_joins_the_thread_of_the_first_step_actually_sent(world: World) -> None:
    """``_thread_id``: a failed or discarded message is no thread to reply in, and the
    first sent is the earliest by ``sent_at``, not by row id."""
    enrollment_id = world.enroll_new(next_action_at=None)

    def history(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        for status, thread, sent_at in [
            (MessageStatus.FAILED, "failed-thread", NOW - timedelta(days=9)),
            (MessageStatus.DISCARDED, "discarded-thread", NOW - timedelta(days=8)),
            (MessageStatus.SENT, "later-thread", NOW - timedelta(days=1)),
            (MessageStatus.SENT, "earlier-thread", NOW - timedelta(days=5)),
        ]:
            factories.make_message(
                session, enrollment, status=status, sent_at=sent_at, gmail_thread_id=thread
            )

    world.write(history)
    found = world.read(lambda s: engine_module._thread_id(s, world.user, enrollment_id))
    assert found == "earlier-thread"


async def test_stop_holds_every_claim_and_start_lifts_it(world: World) -> None:
    enrollment_id = world.enroll_new()
    engine = CampaignEngine(world.factory, SETTINGS, world.sender, clock=lambda: NOW)
    await engine.stop()
    await engine.tick_once()
    assert world.messages(enrollment_id) == []  # stopping: nothing is claimed
    engine.start()
    try:
        await engine.tick_once()
    finally:
        await engine.stop()
    assert len(world.messages(enrollment_id)) == 1


# --- #280: spacing after an unknown outcome is taken from Gmail's send time ----------------


def _settle_sent_at(
    world: World,
    message_id: int,
    at: datetime,
    *,
    expect: tuple[MessageStatus, ...] = (MessageStatus.SCHEDULED,),
    seed: int = 3,
) -> bool:
    return world.write(
        lambda s: engine_module.settle_sent(
            s,
            world.user,
            SETTINGS,
            message_id,
            expect=expect,
            at=at,
            gmail_message_id="gm-late",
            gmail_thread_id="th-late",
            rng=random.Random(seed),
        )
    )


def _next_send(world: World) -> datetime | None:
    return world.read(lambda s: engine_module.next_send_at(s, world.user, world.mailbox.id))


def test_an_unknown_outcome_settled_sent_spaces_the_mailbox_from_gmails_time(
    world: World,
) -> None:
    """#280: the record of an unknown outcome spaced the mailbox from the claim. Gmail's
    real send time, found later, can be after that, and the next send is spaced from it."""
    enrollment_id = world.enroll_new()
    world.sender.outcome = SendOutcome.UNKNOWN
    world.tick()
    [message] = world.messages(enrollment_id)
    assert message.status is MessageStatus.SCHEDULED
    spaced_from_claim = _next_send(world)
    assert spaced_from_claim is not None

    gmail_sent = spaced_from_claim + timedelta(minutes=5)  # later than the claim's spacing
    assert _settle_sent_at(world, message.id, gmail_sent) is True
    gap = schedule.spacing_delay(
        random.Random(3),
        median_s=SETTINGS.campaigns.send_spacing_median_s,
        floor_s=SETTINGS.campaigns.send_spacing_floor_s,
    )
    assert _next_send(world) == gmail_sent + gap
    assert gap >= timedelta(seconds=SETTINGS.campaigns.send_spacing_floor_s)


def test_settling_sent_never_moves_the_mailbox_spacing_earlier(world: World) -> None:
    enrollment_id = world.enroll_new()
    world.sender.outcome = SendOutcome.UNKNOWN
    world.tick()
    [message] = world.messages(enrollment_id)
    before = _next_send(world)
    assert _settle_sent_at(world, message.id, NOW - timedelta(hours=1)) is True
    assert _next_send(world) == before


def test_a_draft_seen_sent_leaves_the_mailbox_spacing_as_it_is(world: World) -> None:
    """Only a leftover's settle re-spaces: a draft the person sent is not the engine's send."""
    enrollment_id = world.enroll_new()
    world.sender.outcome = SendOutcome.DRAFTED
    world.tick()
    [message] = world.messages(enrollment_id)
    assert message.status is MessageStatus.DRAFTED
    before = _next_send(world)
    later = NOW + timedelta(days=1)
    assert _settle_sent_at(world, message.id, later, expect=(MessageStatus.DRAFTED,)) is True
    assert _next_send(world) == before


# --- #280: tests for mutants that survived the P3-07 verification --------------------------


def test_settle_drafted_stores_the_thread_snapshot(world: World) -> None:
    """T11: the Gmail ids the draft's thread held when it was found are kept, so the drafts
    poll never reads one of them as the draft sent."""
    [message_id] = _leftovers(world, 1, at=NOW)
    settled = world.write(
        lambda s: engine_module.settle_drafted(
            s,
            world.user,
            SETTINGS,
            message_id,
            gmail_message_id="gm-draft",
            gmail_thread_id="th-1",
            gmail_draft_id="d-1",
            thread_known=("gm-note", "gm-earlier"),
        )
    )
    assert settled is True
    [message] = world.messages()
    assert message.status is MessageStatus.DRAFTED
    assert message.thread_known_json == ["gm-earlier", "gm-note"]
    work = world.read(lambda s: engine_module.reconcile_work(s, world.user, now=NOW))
    [tracked] = work.drafts
    assert tracked.thread_known == {"gm-earlier", "gm-note"}


def test_the_unknown_send_end_is_pinned_and_covers_the_gmail_timeout() -> None:
    """Pinned against a number written out here (CLAUDE.md), and never shorter than the
    Gmail client's request timeout, the latest a lost answer's send can still happen."""
    assert timedelta(seconds=30) == engine_module.UNKNOWN_SEND_END_MAX
    assert timedelta(seconds=gmail.DEFAULT_TIMEOUT_S) <= engine_module.UNKNOWN_SEND_END_MAX


def test_the_claim_after_an_unknown_outcome_keeps_the_floor_from_its_worst_case_end(
    world: World,
) -> None:
    """#280 review: reconcile settles an unknown outcome at least ``RECONCILE_AFTER`` after
    the claim, later than any spacing gap, so the next claim came before it could re-space.
    The record itself now spaces from the latest the send could have gone out."""
    # A median under the floor: every gap is the floor itself, the tightest spacing.
    tight = Settings(campaigns=dataclasses.replace(SETTINGS.campaigns, send_spacing_median_s=1))
    first = world.enroll_new()
    second = world.enroll_new()
    world.sender.outcome = SendOutcome.UNKNOWN
    world.tick(settings=tight)
    [unknown] = world.messages(first)
    assert unknown.status is MessageStatus.SCHEDULED
    world.sender.outcome = SendOutcome.SENT

    worst_end = NOW + engine_module.UNKNOWN_SEND_END_MAX  # recorded at NOW
    floor = timedelta(seconds=tight.campaigns.send_spacing_floor_s)
    at = NOW
    while not world.messages(second):
        at += timedelta(seconds=10)
        assert at < NOW + engine_module.RECONCILE_AFTER, "the next claim never came"
        world.tick(at, settings=tight)
    [claimed] = world.messages(second)
    assert claimed.scheduled_at == at
    assert at == worst_end + floor  # the first moment the floor from the worst case allows


@dataclass
class SlowUnknown:
    """A send that takes ``takes`` on the engine's own clock, then loses its answer."""

    clock: list[datetime]
    takes: timedelta
    firings: list[Firing] = dataclasses.field(default_factory=list)

    def send(self, firing: Firing) -> SendResult:
        self.firings.append(firing)
        self.clock[0] += self.takes
        return SendResult(SendOutcome.UNKNOWN, error="timed out")


def test_an_unknown_send_that_took_time_spaces_from_its_record_not_its_claim(
    world: World,
) -> None:
    """#277 (from the verification of #284): with a frozen clock, spacing from the claim
    time and from the record time are the same instant, so a mutant spacing from the
    claim survived. Here the send takes 25 s: the next claim waits for the floor after
    the worst-case end of a send recorded 25 s after its claim."""
    tight = Settings(campaigns=dataclasses.replace(SETTINGS.campaigns, send_spacing_median_s=1))
    world.enroll_new()
    second = world.enroll_new()
    clock = [NOW]
    slow = SlowUnknown(clock, timedelta(seconds=25))
    run_tick(
        world.factory, settings=tight, sender=slow, clock=lambda: clock[0], rng=random.Random(1)
    )
    assert len(slow.firings) == 1

    floor = timedelta(seconds=tight.campaigns.send_spacing_floor_s)
    allowed = NOW + timedelta(seconds=25) + engine_module.UNKNOWN_SEND_END_MAX + floor
    world.tick(allowed - timedelta(seconds=1), settings=tight)
    assert world.messages(second) == []  # spaced from the claim, it would be claimed here
    world.tick(allowed, settings=tight)
    [claimed] = world.messages(second)
    assert claimed.scheduled_at == allowed
