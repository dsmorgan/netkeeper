"""LinkedIn reply and send detection (P4-02, #381).

The inbox poll's apply (:mod:`netkeeper.crm.inbox_apply`) hands its new messages to
:func:`netkeeper.services.campaign_replies.apply_linkedin_news` in its writer session.
These tests build deltas with :mod:`inbox_fakes` (``FakeInboxSource`` for the runner),
so nothing here touches LinkedIn. Every URN and message is invented.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import factories
import pytest
from campaign_fakes import NOW, SETTINGS, FakeSender, make_mailbox
from inbox_fakes import OWNER_URN, FakeInboxSource, conversation, delta, message, profile_urn
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import LinkedInSettings, Settings
from netkeeper.crm import inbox_apply
from netkeeper.crm.interactions import add_interaction
from netkeeper.crm.self_contact import ensure_self_contact
from netkeeper.db import session_scope
from netkeeper.linkedin.inbox import InboxConversation, InboxMessage
from netkeeper.linkedin.messaging import MessageOutcome, MessageOutcomeKind
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    Contact,
    ContactSource,
    DoNotSendAddress,
    DoNotSendReason,
    Enrollment,
    EnrollmentStatus,
    Interaction,
    InteractionKind,
    Message,
    MessageDirection,
    MessageStatus,
    StepCondition,
    StepMode,
    SyncRunKind,
    SyncRunStatus,
    TemplateChannel,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import campaign_engine as engine
from netkeeper.services import campaign_replies as replies
from netkeeper.services import linkedin_steps, runs
from netkeeper.services.campaign_guards import check_step
from netkeeper.services.inbox_poll import poll_inbox
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import record_session_evidence
from netkeeper.services.linkedin_steps import claim_prefill, record_prefill_outcome
from netkeeper.services.simulate_campaign import (
    CampaignShape,
    SimulatedLinkedIn,
    StepShape,
    render_schedule,
    simulate_campaign,
    simulate_schedule,
)

EMAIL = TemplateChannel.EMAIL
LINKEDIN = TemplateChannel.LINKEDIN
ADA = profile_urn("ada")
BEN = profile_urn("ben")
CONVERSATION = "urn:li:msg_conversation:INVENTEDONE"
#: LinkedIn's active hours on UTC, so they and the sending hours read the same clock.
LANE_SETTINGS = Settings(campaigns=SETTINGS.campaigns, linkedin=LinkedInSettings(timezone="UTC"))
_addresses = itertools.count(1)
_numbers = itertools.count(100)


@pytest.fixture(autouse=True)
def _message_send_has_a_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """``message_send`` has no runner until P4-03: stand in for it, as P4-09's tests do."""
    monkeypatch.setattr(runs, "RUNNABLE_KINDS", runs.RUNNABLE_KINDS | {SyncRunKind.MESSAGE_SEND})


@dataclass
class World:
    factory: sessionmaker[Session]
    user_id: int
    campaign_id: int
    settings: Settings = LANE_SETTINGS

    def write[T](self, fn: Callable[[Session, User], T]) -> T:
        with session_scope(self.factory, write=True) as session:
            user = session.get(User, self.user_id)
            assert user is not None
            return fn(session, user)

    def read[T](self, fn: Callable[[Session, User], T]) -> T:
        with session_scope(self.factory) as session:
            user = session.get(User, self.user_id)
            assert user is not None
            return fn(session, user)

    def enroll(self, urn: str | None = ADA, **overrides: Any) -> int:
        """A contact with ``urn`` and an email address, enrolled active and due at NOW."""

        def make(session: Session, user: User) -> int:
            contact = factories.make_contact(
                session, user, emails=[f"p{next(_addresses)}@example.test"], li_urn=urn
            )
            campaign = get_scoped(session, user, Campaign, self.campaign_id)
            assert campaign is not None
            fields: dict[str, Any] = {"next_action_at": NOW}
            fields.update(overrides)
            return factories.make_enrollment(session, campaign, contact, **fields).id

        return self.write(make)

    def sent(self, enrollment_id: int, at: datetime, position: int = 1) -> int:
        """Step ``position`` went out at ``at``."""

        def make(session: Session, user: User) -> int:
            enrollment = get_scoped(session, user, Enrollment, enrollment_id)
            assert enrollment is not None
            enrollment.current_step = max(enrollment.current_step or 0, position)
            return factories.make_message(session, enrollment, position=position, sent_at=at).id

        return self.write(make)

    def apply(
        self, *conversations: InboxConversation, polled_at: datetime = NOW
    ) -> inbox_apply.InboxCounts:
        return self.write(
            lambda s, u: inbox_apply.apply_delta(
                s, u, delta(*conversations), polled_at=polled_at, settings=self.settings
            )
        )

    def tick(self, now: datetime) -> engine.TickResult:
        [result] = [
            r
            for r in engine.run_tick(
                self.factory,
                settings=self.settings,
                sender=FakeSender(now=now),
                clock=lambda: now,
                rng=random.Random(1),
            )
            if r.user_id == self.user_id
        ]
        return result

    def prefill(self, enrollment_id: int, now: datetime = NOW) -> int:
        """Claim the due LinkedIn step and record it ``prefilled`` at ``now``; end the run."""

        def run(session: Session, user: User) -> int:
            claim = claim_prefill(session, user, enrollment_id, now=now, settings=self.settings)
            assert claim.claimed, claim.reasons
            assert claim.message_id is not None and claim.run_id is not None
            outcome = MessageOutcome(MessageOutcomeKind.PREFILLED, "fixed words", None, 12)
            assert record_prefill_outcome(
                session, user, claim.message_id, outcome, settings=self.settings, now=now
            )
            runs.finish_run(session, user, claim.run_id, status=SyncRunStatus.COMPLETED, now=now)
            return claim.message_id

        return self.write(run)

    def enrollment(self, enrollment_id: int) -> Enrollment:
        row = self.read(lambda s, u: get_scoped(s, u, Enrollment, enrollment_id))
        assert row is not None
        return row

    def messages(self, enrollment_id: int) -> list[Message]:
        return self.read(
            lambda s, u: list(
                s.scalars(
                    scoped(u, Message)
                    .where(Message.enrollment_id == enrollment_id)
                    .order_by(Message.id)
                )
            )
        )

    def inbound(self, enrollment_id: int) -> list[Message]:
        return [m for m in self.messages(enrollment_id) if m.direction is MessageDirection.IN]

    def message(self, message_id: int) -> Message:
        row = self.read(lambda s, u: get_scoped(s, u, Message, message_id))
        assert row is not None
        return row

    def interaction(self, external_id: str) -> Interaction:
        return self.read(
            lambda s, u: s.scalars(
                scoped(u, Interaction).where(Interaction.external_id == external_id)
            ).one()
        )


def make_world(
    factory: sessionmaker[Session],
    channels: tuple[TemplateChannel, ...] = (EMAIL, EMAIL),
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
        ensure_account(session, user)
        record_session_evidence(
            session, user, logged_in=True, source="preflight", now=NOW - timedelta(hours=1)
        )
        session.flush()
        return World(factory, user.id, row.id)


@pytest.fixture
def world(session_factory: sessionmaker[Session]) -> World:
    return make_world(session_factory)


def said(
    urn: str, at: datetime, text: str = "Invented reply.", *, name: str = "one"
) -> InboxConversation:
    """One inbound message from ``urn`` in a one-to-one conversation."""
    return conversation(
        name, urn, [message(next(_numbers), sender=urn, at=at, text=text, tag=name)]
    )


def you_sent(
    urn: str, *times: datetime, name: str = "one"
) -> tuple[InboxConversation, list[InboxMessage]]:
    """Messages you sent ``urn`` at ``times``, in their one-to-one conversation."""
    messages = [
        message(next(_numbers), sender=urn, at=at, outbound=True, text="Edited.", tag=name)
        for at in times
    ]
    return conversation(name, urn, messages), messages


# --- what a reply is ---------------------------------------------------------------------


def test_a_linkedin_reply_ends_an_email_only_enrollment_before_its_follow_up(
    world: World,
) -> None:
    """Step 1 goes by email; the contact answers on LinkedIn; step 2 never fires."""
    enrollment_id = world.enroll()
    [(firing, _)] = world.tick(NOW).fired
    assert firing.enrollment_id == enrollment_id
    sent_at = world.messages(enrollment_id)[0].sent_at
    assert sent_at is not None
    due = world.enrollment(enrollment_id).next_action_at
    assert due is not None

    world.apply(said(ADA, sent_at + timedelta(days=1)), polled_at=sent_at + timedelta(days=1))

    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.exit_reason) == (EnrollmentStatus.REPLIED, "replied")
    assert enrollment.replied_at == sent_at + timedelta(days=1)
    [reply] = world.inbound(enrollment_id)
    assert (reply.channel, reply.status, reply.snippet) == (
        LINKEDIN,
        MessageStatus.RECEIVED,
        "Invented reply.",
    )
    assert reply.sent_at == sent_at + timedelta(days=1)
    assert reply.li_conversation_urn == CONVERSATION
    assert reply.li_message_urn is not None
    assert reply.step_id == world.messages(enrollment_id)[0].step_id
    assert world.interaction(reply.li_message_urn).message_id == reply.id
    assert not world.tick(due).fired
    assert [m.direction for m in world.messages(enrollment_id)] == [
        MessageDirection.OUT,
        MessageDirection.IN,
    ]


def test_a_reply_recorded_by_the_poll_stops_the_next_claim(world: World) -> None:
    """The race: the tick's claim reads the inbound message in its own writer session
    (as for Gmail, P3-06), even when the enrollment still reads ``active``."""
    enrollment_id = world.enroll()
    world.sent(enrollment_id, NOW - timedelta(days=8))
    world.apply(said(ADA, NOW - timedelta(minutes=5)))

    def revive(session: Session, user: User) -> None:
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        enrollment.status = EnrollmentStatus.ACTIVE
        enrollment.exit_reason = None
        enrollment.next_action_at = NOW - timedelta(minutes=1)

    world.write(revive)
    result = world.tick(NOW)
    assert not result.fired
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    assert (
        len([m for m in world.messages(enrollment_id) if m.direction is MessageDirection.OUT]) == 1
    )


def test_a_reply_before_the_first_send_is_not_a_reply(world: World) -> None:
    enrollment_id = world.enroll()
    world.sent(enrollment_id, NOW - timedelta(days=1))
    world.apply(said(ADA, NOW - timedelta(days=2)))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    assert world.inbound(enrollment_id) == []


def test_an_enrollment_with_nothing_sent_has_no_reply(world: World) -> None:
    enrollment_id = world.enroll()
    world.apply(said(ADA, NOW - timedelta(minutes=1)))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    assert world.inbound(enrollment_id) == []


def test_a_prefill_not_yet_seen_sent_is_no_first_send(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory, channels=(LINKEDIN, EMAIL))
    enrollment_id = world.enroll()
    world.prefill(enrollment_id)
    world.apply(said(ADA, NOW + timedelta(minutes=5)), polled_at=NOW + timedelta(minutes=6))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    assert world.inbound(enrollment_id) == []


def test_a_repoll_changes_nothing(world: World) -> None:
    enrollment_id = world.enroll()
    world.sent(enrollment_id, NOW - timedelta(days=1))
    reply = said(ADA, NOW - timedelta(hours=1))
    world.apply(reply)
    before = world.enrollment(enrollment_id)
    again = world.apply(reply, polled_at=NOW + timedelta(hours=3))

    assert again.new_inbound == []
    assert len(world.inbound(enrollment_id)) == 1
    after = world.enrollment(enrollment_id)
    assert (after.status, after.replied_at, after.updated_at) == (
        before.status,
        before.replied_at,
        before.updated_at,
    )


def test_recording_is_idempotent_per_enrollment_and_message(world: World) -> None:
    """Called twice with the same message (a second handler, a retried apply)."""
    enrollment_id = world.enroll()
    world.sent(enrollment_id, NOW - timedelta(days=1))
    counts = world.apply(said(ADA, NOW - timedelta(hours=1)))
    [inbound] = counts.new_inbound
    assert not world.write(lambda s, u: replies.record_linkedin_reply(s, u, enrollment_id, inbound))
    assert len(world.inbound(enrollment_id)) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Please stop messaging me.",
        "STOP MESSAGING",
        "unsubscribe",
        "Remove me from this, thanks",
        "stop emailing me",
    ],
)
def test_an_unsubscribe_phrase_opts_the_contact_out(world: World, text: str) -> None:
    enrollment_id = world.enroll()
    world.sent(enrollment_id, NOW - timedelta(days=1))
    world.apply(said(ADA, NOW - timedelta(hours=1), text))

    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.exit_reason) == (
        EnrollmentStatus.OPTED_OUT,
        replies.UNSUBSCRIBE_REASON,
    )
    [reply] = world.inbound(enrollment_id)
    assert reply.asks_unsubscribe

    def check(session: Session, user: User) -> None:
        contact = get_scoped(session, user, Contact, enrollment.contact_id)
        assert contact is not None and contact.do_not_contact
        assert contact.do_not_contact_reason == (
            f"asked to unsubscribe in a reply to a campaign (message {reply.id})"
        )
        listed = session.scalars(scoped(user, DoNotSendAddress)).all()
        assert {(d.email, d.reason) for d in listed} == {
            (e.email, DoNotSendReason.OPTED_OUT) for e in contact.emails
        }

    world.read(check)


def test_a_phrase_inside_another_word_is_not_an_unsubscribe(world: World) -> None:
    enrollment_id = world.enroll()
    world.sent(enrollment_id, NOW - timedelta(days=1))
    world.apply(said(ADA, NOW - timedelta(hours=1), "I'll stop messagingly soon"))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED


def test_a_group_thread_message_from_the_contact_changes_nothing(world: World) -> None:
    enrollment_id = world.enroll()
    world.sent(enrollment_id, NOW - timedelta(days=1))
    world.write(lambda s, u: factories.make_contact(s, u, li_urn=BEN))
    group = conversation(
        "group",
        ADA,
        [
            message(1, sender=ADA, at=NOW - timedelta(hours=2), tag="group"),
            message(2, sender=BEN, at=NOW - timedelta(hours=1), tag="group"),
        ],
    )
    counts = world.apply(group)
    assert counts.skipped_group == 1
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    assert world.inbound(enrollment_id) == []


def test_the_self_contact_is_never_watched(world: World) -> None:
    """A thread with your own URN is ignored by the apply, and the self contact's
    enrollments are never watched for a reply (#342)."""

    def enroll_self(session: Session, user: User) -> int:
        me = ensure_self_contact(session, user)
        me.li_urn = OWNER_URN
        campaign = get_scoped(session, user, Campaign, world.campaign_id)
        assert campaign is not None
        enrollment = factories.make_enrollment(session, campaign, me)
        factories.make_message(session, enrollment, sent_at=NOW - timedelta(days=1))
        return me.id

    me_id = world.write(enroll_self)
    counts = world.apply(said(OWNER_URN, NOW - timedelta(hours=1)))
    assert counts.new_inbound == []
    assert world.read(lambda s, u: replies.linkedin_watches(s, u, me_id, now=NOW)) == []


def test_a_completed_enrollments_reply_is_recorded_and_it_stays_completed(
    world: World,
) -> None:
    enrollment_id = world.enroll(status=EnrollmentStatus.COMPLETED, next_action_at=None)
    world.sent(enrollment_id, NOW - timedelta(days=10))
    world.apply(said(ADA, NOW - timedelta(hours=1)))

    enrollment = world.enrollment(enrollment_id)
    assert enrollment.status is EnrollmentStatus.COMPLETED
    assert enrollment.replied_at == NOW - timedelta(hours=1)
    assert len(world.inbound(enrollment_id)) == 1


def test_a_completed_enrollment_is_watched_only_thirty_days_after_its_latest_send(
    world: World,
) -> None:
    enrollment_id = world.enroll(status=EnrollmentStatus.COMPLETED, next_action_at=None)
    world.sent(enrollment_id, NOW - replies.WATCH_AFTER_COMPLETED - timedelta(minutes=1))
    world.apply(said(ADA, NOW - timedelta(hours=1)))
    assert world.inbound(enrollment_id) == []
    assert world.enrollment(enrollment_id).replied_at is None


@pytest.mark.parametrize("status", [CampaignStatus.COMPLETED, CampaignStatus.ARCHIVED])
def test_an_ended_campaigns_live_enrollment_is_watched_like_a_completed_one(
    session_factory: sessionmaker[Session], status: CampaignStatus
) -> None:
    """#345, as the Gmail poll: within thirty days of its latest send the reply ends it
    ``replied``; after that it is not looked at."""
    world = make_world(session_factory, status=status)
    recent = world.enroll(urn=ADA)
    world.sent(recent, NOW - timedelta(days=29))
    old = world.enroll(urn=BEN)
    world.sent(old, NOW - timedelta(days=31))
    world.apply(
        said(ADA, NOW - timedelta(hours=1)), said(BEN, NOW - timedelta(hours=1), name="two")
    )

    assert world.enrollment(recent).status is EnrollmentStatus.REPLIED
    assert world.enrollment(old).status is EnrollmentStatus.ACTIVE
    assert world.inbound(old) == []


def test_a_paused_enrollment_ends_replied(world: World) -> None:
    enrollment_id = world.enroll(status=EnrollmentStatus.PAUSED)
    world.sent(enrollment_id, NOW - timedelta(days=1))
    world.apply(said(ADA, NOW - timedelta(hours=1)))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED


def test_every_watched_enrollment_of_the_contact_gets_the_reply(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory)
    first = world.enroll()

    def second_campaign(session: Session, user: User) -> int:
        campaign = factories.make_campaign(session, user)
        contact_id = get_scoped(session, user, Enrollment, first)
        assert contact_id is not None
        contact = get_scoped(session, user, Contact, contact_id.contact_id)
        assert contact is not None
        return factories.make_enrollment(session, campaign, contact).id

    second = world.write(second_campaign)
    world.sent(first, NOW - timedelta(days=2))
    world.sent(second, NOW - timedelta(days=1))
    counts = world.apply(said(ADA, NOW - timedelta(hours=1)))
    [inbound] = counts.new_inbound

    assert world.enrollment(first).status is EnrollmentStatus.REPLIED
    assert world.enrollment(second).status is EnrollmentStatus.REPLIED
    [one] = world.inbound(first)
    assert len(world.inbound(second)) == 1
    assert world.interaction(inbound.message_urn).message_id == one.id


def test_no_snippet_reaches_a_log(world: World, caplog: pytest.LogCaptureFixture) -> None:
    secret = "an invented sentence to stop messaging"
    enrollment_id = world.enroll()
    world.sent(enrollment_id, NOW - timedelta(days=1))
    with caplog.at_level(logging.DEBUG):
        world.apply(said(ADA, NOW - timedelta(hours=1), secret))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.OPTED_OUT
    assert secret not in caplog.text
    assert "invented sentence" not in caplog.text


# --- send confirmation -------------------------------------------------------------------


@pytest.fixture
def linkedin_first(session_factory: sessionmaker[Session]) -> World:
    """A LinkedIn step, then an email a week later."""
    return make_world(session_factory, channels=(LINKEDIN, EMAIL))


def test_a_prefilled_message_is_confirmed_sent_and_the_next_step_counts_from_it(
    linkedin_first: World,
) -> None:
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)
    assert world.enrollment(enrollment_id).next_action_at is None  # parked until it is sent
    sent_at = NOW + timedelta(hours=2, seconds=30, microseconds=250_000)
    thread, [sent] = you_sent(ADA, sent_at)
    world.apply(thread, polled_at=NOW + timedelta(hours=3))

    row = world.message(message_id)
    assert (row.status, row.sent_at) == (MessageStatus.SENT, sent_at)
    assert (row.li_conversation_urn, row.li_message_urn) == (CONVERSATION, sent.message_urn)
    assert world.interaction(sent.message_urn).message_id == message_id

    def due(session: Session, user: User) -> datetime:
        steps = engine._steps(session, user, world.campaign_id)
        return engine.follow_up_due(
            world.settings, user, steps[1], sent_at, engine.hours_for(session, user)
        )

    expected = world.read(due)
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.current_step) == (EnrollmentStatus.ACTIVE, 1)
    assert enrollment.next_action_at == expected
    assert expected >= sent_at + timedelta(days=7)
    # The enrollment's own polled li_out is its own step, not other contact (#388 review).
    verdict = world.read(
        lambda s, u: check_step(
            s,
            u,
            get_scoped(s, u, Enrollment, enrollment_id),  # type: ignore[arg-type]
            engine._steps(s, u, world.campaign_id)[1],
            now=expected,
        )
    )
    assert verdict.eligible, verdict.reasons
    [(firing, _)] = world.tick(expected).fired
    assert firing.enrollment_id == enrollment_id


def test_a_message_you_sent_before_the_prefill_confirms_nothing(linkedin_first: World) -> None:
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)
    thread, _ = you_sent(ADA, NOW - timedelta(minutes=1))
    world.apply(thread, polled_at=NOW + timedelta(hours=1))
    assert world.message(message_id).status is MessageStatus.PREFILLED
    assert world.enrollment(enrollment_id).next_action_at is None


def test_the_first_message_after_the_prefill_confirms_it(linkedin_first: World) -> None:
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)
    thread, [_, first, _] = you_sent(
        ADA,
        NOW - timedelta(minutes=1),
        NOW + timedelta(minutes=4),
        NOW + timedelta(minutes=9),
    )
    world.apply(thread, polled_at=NOW + timedelta(hours=1))
    row = world.message(message_id)
    assert (row.sent_at, row.li_message_urn) == (NOW + timedelta(minutes=4), first.message_urn)


def test_a_message_to_someone_else_confirms_nothing(linkedin_first: World) -> None:
    world = linkedin_first
    enrollment_id = world.enroll()
    world.write(lambda s, u: factories.make_contact(s, u, li_urn=BEN))
    message_id = world.prefill(enrollment_id)
    thread, _ = you_sent(BEN, NOW + timedelta(minutes=5), name="two")
    world.apply(thread, polled_at=NOW + timedelta(hours=1))
    assert world.message(message_id).status is MessageStatus.PREFILLED


def test_a_known_conversation_must_match(linkedin_first: World) -> None:
    """When the prefill learned its conversation, only a message in it confirms."""
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)

    def learn(session: Session, user: User) -> None:
        row = get_scoped(session, user, Message, message_id)
        assert row is not None
        row.li_conversation_urn = "urn:li:msg_conversation:INVENTEDOTHER"

    world.write(learn)
    thread, _ = you_sent(ADA, NOW + timedelta(minutes=5))
    world.apply(thread, polled_at=NOW + timedelta(hours=1))
    assert world.message(message_id).status is MessageStatus.PREFILLED


def test_a_stale_message_seen_sent_becomes_sent_and_the_enrollment_moves_on(
    linkedin_first: World,
) -> None:
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)
    world.tick(NOW + timedelta(days=3))
    assert world.message(message_id).status is MessageStatus.STALE
    assert world.enrollment(enrollment_id).next_action_at is None

    sent_at = NOW + timedelta(days=4)
    thread, _ = you_sent(ADA, sent_at)
    world.apply(thread, polled_at=sent_at + timedelta(hours=1))
    row = world.message(message_id)
    assert (row.status, row.sent_at) == (MessageStatus.SENT, sent_at)
    due = world.enrollment(enrollment_id).next_action_at
    assert due is not None and due >= sent_at + timedelta(days=7)


def test_confirming_the_last_step_completes_the_enrollment(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory, channels=(LINKEDIN,))
    enrollment_id = world.enroll()
    world.prefill(enrollment_id)
    thread, _ = you_sent(ADA, NOW + timedelta(minutes=5))
    world.apply(thread, polled_at=NOW + timedelta(hours=1))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.COMPLETED


def test_a_send_then_a_reply_in_one_poll_ends_the_enrollment_replied(
    linkedin_first: World,
) -> None:
    """The send is confirmed first, so the reply after it counts."""
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)
    thread = conversation(
        "one",
        ADA,
        [
            message(1, sender=ADA, at=NOW + timedelta(minutes=5), outbound=True, tag="one"),
            message(2, sender=ADA, at=NOW + timedelta(minutes=30), tag="one"),
        ],
    )
    world.apply(thread, polled_at=NOW + timedelta(hours=1))
    assert world.message(message_id).status is MessageStatus.SENT
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.next_action_at) == (EnrollmentStatus.REPLIED, None)


def test_a_reply_polled_before_the_send_was_seen_still_ends_the_enrollment(
    linkedin_first: World,
) -> None:
    """The poll that first saw the answer did not load the sent message (a thread not
    opened), so nothing was sent yet. The poll that then confirms the send records the
    earlier answer as the reply before the next step can fire."""
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)
    world.apply(
        said(ADA, NOW + timedelta(minutes=30), "stop messaging please"),
        polled_at=NOW + timedelta(hours=1),
    )
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE

    thread, _ = you_sent(ADA, NOW + timedelta(minutes=5))
    world.apply(thread, polled_at=NOW + timedelta(hours=4))
    assert world.message(message_id).status is MessageStatus.SENT
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.next_action_at) == (EnrollmentStatus.OPTED_OUT, None)
    [reply] = world.inbound(enrollment_id)
    assert (reply.snippet, reply.asks_unsubscribe) == ("stop messaging please", True)
    assert reply.li_conversation_urn == CONVERSATION
    assert world.interaction(reply.li_message_urn or "").message_id == reply.id


def test_catching_up_ignores_what_came_before_the_first_send(linkedin_first: World) -> None:
    world = linkedin_first
    enrollment_id = world.enroll()
    world.apply(said(ADA, NOW - timedelta(days=1)), polled_at=NOW - timedelta(hours=20))
    world.prefill(enrollment_id)
    thread, _ = you_sent(ADA, NOW + timedelta(minutes=5))
    world.apply(thread, polled_at=NOW + timedelta(hours=1))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    assert world.inbound(enrollment_id) == []


def test_an_archived_sent_message_the_poll_adopts_confirms_the_prefill(
    linkedin_first: World,
) -> None:
    """#388 review: the adopted archive row takes the message id too, so the guard reads
    it as this enrollment's own step."""
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)
    sent_at = NOW + timedelta(minutes=5)

    def archived(session: Session, user: User) -> int:
        contact_id = get_scoped(session, user, Enrollment, enrollment_id)
        assert contact_id is not None
        return add_interaction(
            session,
            user,
            contact_id.contact_id,
            InteractionKind.LI_OUT,
            sent_at,
            "Invented opener.",
            source=ContactSource.ARCHIVE,
        ).id

    archive_id = world.write(archived)
    thread, [sent] = you_sent(ADA, sent_at)
    counts = world.apply(thread, polled_at=NOW + timedelta(hours=1))
    assert counts.messages_new == 0
    row = world.message(message_id)
    assert (row.status, row.li_message_urn) == (MessageStatus.SENT, sent.message_urn)
    adopted = world.interaction(sent.message_urn)
    assert (adopted.id, adopted.message_id) == (archive_id, message_id)


def test_one_sent_message_confirms_one_prefill(session_factory: sessionmaker[Session]) -> None:
    """Two campaigns' waiting messages to the same contact: one send confirms the older."""
    world = make_world(session_factory, channels=(LINKEDIN, EMAIL))
    first = world.enroll()
    older = world.prefill(first)

    def second(session: Session, user: User) -> int:
        campaign = factories.make_campaign(session, user, channels=(LINKEDIN, EMAIL))
        enrollment = get_scoped(session, user, Enrollment, first)
        assert enrollment is not None
        contact = get_scoped(session, user, Contact, enrollment.contact_id)
        assert contact is not None
        other = factories.make_enrollment(session, campaign, contact, current_step=1)
        return factories.make_message(
            session,
            other,
            status=MessageStatus.STALE,
            sent_at=None,
            prefilled_at=NOW + timedelta(minutes=1),
        ).id

    newer = world.write(second)
    thread, _ = you_sent(ADA, NOW + timedelta(minutes=5))
    world.apply(thread, polled_at=NOW + timedelta(hours=1))
    assert world.message(older).status is MessageStatus.SENT
    assert world.message(newer).status is MessageStatus.STALE
    # A later poll: the send already belongs to the older prefill, so not to this one too.
    world.apply(polled_at=NOW + timedelta(hours=2))
    assert world.message(newer).status is MessageStatus.STALE


# --- the runner and the simulation -------------------------------------------------------


def test_the_inbox_poll_confirms_a_prefill_with_the_campaign_settings(
    linkedin_first: World,
) -> None:
    """Through ``poll_inbox`` and ``FakeInboxSource``: the hook runs in the apply."""
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)
    sent_at = NOW + timedelta(minutes=5)
    thread, _ = you_sent(ADA, sent_at)
    report = asyncio.run(
        poll_inbox(
            world.factory,
            world.user_id,
            FakeInboxSource(delta(thread)),
            settings=world.settings.linkedin,
            clock=lambda: NOW + timedelta(hours=1),
            campaign_settings=world.settings,
        )
    )
    assert report.stop_reason == "inbox_read"
    assert world.message(message_id).status is MessageStatus.SENT
    assert world.enrollment(enrollment_id).next_action_at is not None


def _statuses(factory: sessionmaker[Session], user_id: int) -> dict[int, EnrollmentStatus]:
    with session_scope(factory) as session:
        user = session.get_one(User, user_id)
        return {e.id: e.status for e in session.scalars(scoped(user, Enrollment))}


def _outbound(
    factory: sessionmaker[Session], user_id: int
) -> dict[int, list[tuple[TemplateChannel, MessageStatus]]]:
    with session_scope(factory) as session:
        user = session.get_one(User, user_id)
        found: dict[int, list[tuple[TemplateChannel, MessageStatus]]] = {}
        for row in session.scalars(
            scoped(user, Message)
            .where(Message.direction == MessageDirection.OUT)
            .order_by(Message.id)
        ):
            found.setdefault(row.enrollment_id, []).append((row.channel, row.status))
        return found


def _simulated(
    factory: sessionmaker[Session], channels: tuple[TemplateChannel, ...]
) -> tuple[World, int, int]:
    world = make_world(factory, channels=channels, contacted_within_days_guard=0)
    return world, world.enroll(urn=ADA), world.enroll(urn=BEN)


def test_in_simulate_a_linkedin_reply_suppresses_a_pending_email_step(
    session_factory: sessionmaker[Session],
) -> None:
    """The "done when": Ada answers on LinkedIn a day after step 1's email, so her step 2
    never goes; Ben gets both."""
    world, ada, ben = _simulated(session_factory, (EMAIL, EMAIL))
    linkedin = SimulatedLinkedIn(random.Random(3), replies={ADA: timedelta(days=1)})
    simulate_campaign(
        session_factory,
        settings=world.settings,
        start=NOW,
        end=NOW + timedelta(weeks=3),
        linkedin=linkedin,
    )
    statuses = _statuses(session_factory, world.user_id)
    assert (statuses[ada], statuses[ben]) == (EnrollmentStatus.REPLIED, EnrollmentStatus.COMPLETED)
    sent = _outbound(session_factory, world.user_id)
    assert len(sent[ada]) == 1 and len(sent[ben]) == 2
    [reply] = world.inbound(ada)
    assert reply.channel is LINKEDIN


def test_in_simulate_a_linkedin_step_is_prefilled_then_sent_and_the_next_follows(
    session_factory: sessionmaker[Session],
) -> None:
    world, ada, ben = _simulated(session_factory, (LINKEDIN, EMAIL))
    linkedin = SimulatedLinkedIn(random.Random(3))
    simulate_campaign(
        session_factory,
        settings=world.settings,
        start=NOW,
        end=NOW + timedelta(weeks=3),
        linkedin=linkedin,
    )
    assert len(linkedin.prefilled) == 2
    sent = _outbound(session_factory, world.user_id)
    for enrollment_id in (ada, ben):
        assert sent[enrollment_id] == [
            (LINKEDIN, MessageStatus.SENT),
            (EMAIL, MessageStatus.SENT),
        ]
        linkedin_row, email_row = [
            m for m in world.messages(enrollment_id) if m.direction is MessageDirection.OUT
        ]
        assert linkedin_row.prefilled_at is not None and linkedin_row.sent_at is not None
        assert linkedin_row.sent_at > linkedin_row.prefilled_at
        assert email_row.scheduled_at is not None
        assert email_row.scheduled_at >= linkedin_row.sent_at + timedelta(days=7)
    statuses = _statuses(session_factory, world.user_id)
    assert set(statuses.values()) == {EnrollmentStatus.COMPLETED}


def test_in_simulate_a_linkedin_reply_after_a_prefilled_step_stops_the_email(
    session_factory: sessionmaker[Session],
) -> None:
    world, ada, ben = _simulated(session_factory, (LINKEDIN, EMAIL))
    linkedin = SimulatedLinkedIn(random.Random(3), replies={ADA: timedelta(days=2)})
    simulate_campaign(
        session_factory,
        settings=world.settings,
        start=NOW,
        end=NOW + timedelta(weeks=3),
        linkedin=linkedin,
    )
    sent = _outbound(session_factory, world.user_id)
    assert sent[ada] == [(LINKEDIN, MessageStatus.SENT)]
    assert len(sent[ben]) == 2
    assert _statuses(session_factory, world.user_id)[ada] is EnrollmentStatus.REPLIED


def test_simulate_campaign_replays_a_linkedin_step_in_its_schedule() -> None:
    """``netkeeper simulate --campaign`` (P3-13): a LinkedIn step fires now, one prefill
    at a time, and the email after it counts from each simulated send."""
    shape = CampaignShape(
        campaign_id=1,
        name="Invented",
        status=CampaignStatus.DRAFT,
        steps=(
            StepShape(1, LINKEDIN, StepMode.PREFILL, 0, StepCondition.ALWAYS, False),
            StepShape(2, EMAIL, StepMode.SEND, 7, StepCondition.ALWAYS, True),
        ),
        audience=3,
        audience_from="enrollments",
        daily_cap=None,
        mailbox_daily_cap=80,
        timezone="UTC",
    )
    report = simulate_schedule(shape, settings=LANE_SETTINGS, start=NOW, days=14, seed=1)

    assert report.per_step == (3, 3)
    assert report.finished == 3
    output = render_schedule(report)
    assert "LinkedIn steps are prefilled one at a time" in output
    assert "do not fire yet" not in output


# --- after the safety review (#416) ------------------------------------------------------


def _discard(world: World, message_id: int, at: datetime) -> None:
    world.write(
        lambda s, u: linkedin_steps.discard(s, u, message_id, settings=world.settings, now=at)
    )


def test_the_confirmation_constants_are_pinned() -> None:
    assert timedelta(seconds=30) == replies.CONFIRM_SKEW
    assert frozenset({"prefilled", "stale", "discarded"}) == {
        str(status) for status in replies.CONFIRMABLE
    }


def test_a_prefill_discarded_then_sent_is_sent_and_a_reply_stops_the_next_step(
    linkedin_first: World,
) -> None:
    """B1: discard, then the person sends it anyway, then the contact answers. The send is
    recorded, nothing advances twice, and the email step never fires."""
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)
    _discard(world, message_id, NOW + timedelta(minutes=1))
    discarded_due = world.enrollment(enrollment_id).next_action_at
    assert discarded_due is not None

    sent_at = NOW + timedelta(minutes=5)
    thread, [sent] = you_sent(ADA, sent_at)
    world.apply(thread, polled_at=NOW + timedelta(hours=1))
    row = world.message(message_id)
    assert (row.status, row.sent_at, row.li_message_urn) == (
        MessageStatus.SENT,
        sent_at,
        sent.message_urn,
    )
    assert world.interaction(sent.message_urn).message_id == message_id
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.current_step) == (EnrollmentStatus.ACTIVE, 1)
    assert enrollment.next_action_at is not None
    # Step 2's delay now counts from the send, after the discard: later, never twice.
    assert enrollment.next_action_at >= discarded_due
    assert enrollment.next_action_at >= sent_at + timedelta(days=7)

    world.apply(said(ADA, NOW + timedelta(hours=2)), polled_at=NOW + timedelta(hours=3))
    enrollment = world.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.next_action_at) == (EnrollmentStatus.REPLIED, None)
    assert not world.tick(discarded_due + timedelta(days=1)).fired
    assert [m.channel for m in world.messages(enrollment_id)] == [LINKEDIN, LINKEDIN]


def test_a_discarded_prefill_with_no_send_stays_discarded(linkedin_first: World) -> None:
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)
    _discard(world, message_id, NOW + timedelta(minutes=1))
    world.apply(said(ADA, NOW + timedelta(hours=2)), polled_at=NOW + timedelta(hours=3))
    assert world.message(message_id).status is MessageStatus.DISCARDED
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE


def test_a_reply_at_the_same_instant_as_the_send_counts(linkedin_first: World) -> None:
    """S1, the main path: the send is known, then a poll reads an answer dated the same."""
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)
    at = NOW + timedelta(minutes=5)
    thread, _ = you_sent(ADA, at)
    world.apply(thread, polled_at=NOW + timedelta(hours=1))
    assert world.message(message_id).status is MessageStatus.SENT
    world.apply(said(ADA, at), polled_at=NOW + timedelta(hours=2))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED


def test_a_caught_up_reply_at_the_same_second_as_the_send_counts(linkedin_first: World) -> None:
    """S1, the catch-up: the answer was polled first, dated the send's own second."""
    world = linkedin_first
    enrollment_id = world.enroll()
    world.prefill(enrollment_id)
    at = NOW + timedelta(minutes=5)
    world.apply(said(ADA, at), polled_at=NOW + timedelta(hours=1))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    thread, _ = you_sent(ADA, at)
    world.apply(thread, polled_at=NOW + timedelta(hours=2))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED


def _claim_only(world: World, enrollment_id: int) -> tuple[int, int]:
    def run(session: Session, user: User) -> tuple[int, int]:
        claim = claim_prefill(session, user, enrollment_id, now=NOW, settings=world.settings)
        assert claim.claimed and claim.message_id is not None and claim.run_id is not None
        return claim.message_id, claim.run_id

    return world.write(run)


def _record_prefilled(world: World, message_id: int, run_id: int, at: datetime) -> None:
    def run(session: Session, user: User) -> None:
        outcome = MessageOutcome(MessageOutcomeKind.PREFILLED, "fixed words", None, 12)
        assert record_prefill_outcome(
            session, user, message_id, outcome, settings=world.settings, now=at
        )
        runs.finish_run(session, user, run_id, status=SyncRunStatus.COMPLETED, now=at)

    world.write(run)


@pytest.mark.parametrize(("before", "confirmed"), [(30, True), (31, False)])
def test_a_send_a_poll_recorded_before_prefilled_at_confirms_it_on_a_later_poll(
    linkedin_first: World, before: int, confirmed: bool
) -> None:
    """S2: the poll recorded the send while the claim was still being recorded, so
    ``prefilled_at`` came after it. A later poll with nothing new (a "check now")
    confirms it, within ``CONFIRM_SKEW`` and no further."""
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id, run_id = _claim_only(world, enrollment_id)
    prefilled_at = NOW + timedelta(minutes=2)
    sent_at = prefilled_at - timedelta(seconds=before)
    thread, [sent] = you_sent(ADA, sent_at)
    world.apply(thread, polled_at=NOW + timedelta(minutes=1, seconds=40))
    assert world.message(message_id).status is MessageStatus.SCHEDULED
    _record_prefilled(world, message_id, run_id, prefilled_at)

    world.apply(polled_at=NOW + timedelta(hours=1))  # nothing new
    row = world.message(message_id)
    if confirmed:
        assert (row.status, row.sent_at, row.li_message_urn) == (
            MessageStatus.SENT,
            sent_at,
            sent.message_urn,
        )
        assert row.li_conversation_urn == CONVERSATION  # the contact's one conversation
        assert world.enrollment(enrollment_id).next_action_at is not None
    else:
        assert row.status is MessageStatus.PREFILLED
        assert world.interaction(sent.message_urn).message_id is None


def test_a_caught_up_reply_with_no_known_conversation_stores_none(
    linkedin_first: World,
) -> None:
    """S4: with two conversations for the contact, the reply's is unknown, never a guess."""
    world = linkedin_first
    enrollment_id = world.enroll()
    world.prefill(enrollment_id)
    world.apply(said(ADA, NOW + timedelta(minutes=20)), polled_at=NOW + timedelta(hours=1))
    other, _ = you_sent(ADA, NOW - timedelta(days=30), name="two")
    world.apply(other, polled_at=NOW + timedelta(hours=2))
    thread, _ = you_sent(ADA, NOW + timedelta(minutes=5))
    world.apply(thread, polled_at=NOW + timedelta(hours=3))

    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    [reply] = world.inbound(enrollment_id)
    assert reply.li_conversation_urn is None


def test_a_linkedin_claim_refuses_after_a_reply_even_if_revived(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory, channels=(EMAIL, LINKEDIN))
    enrollment_id = world.enroll()
    world.sent(enrollment_id, NOW - timedelta(days=8))
    world.apply(said(ADA, NOW - timedelta(minutes=5)))
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED

    def revive(session: Session, user: User) -> None:
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        enrollment.status = EnrollmentStatus.ACTIVE
        enrollment.exit_reason = None
        enrollment.next_action_at = NOW - timedelta(minutes=1)

    world.write(revive)
    claim = world.write(
        lambda s, u: claim_prefill(s, u, enrollment_id, now=NOW, settings=world.settings)
    )
    assert not claim.claimed
    assert world.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED


# --- after the re-review (#416) ----------------------------------------------------------


def test_a_message_sent_by_hand_just_before_the_claim_confirms_nothing(
    linkedin_first: World,
) -> None:
    """Within CONFIRM_SKEW of ``prefilled_at``, but before the claim: not the prefill."""
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)  # claimed and prefilled at NOW
    manual, _ = you_sent(ADA, NOW - timedelta(seconds=10))
    world.apply(manual, polled_at=NOW + timedelta(hours=1))
    assert world.message(message_id).status is MessageStatus.PREFILLED
    assert world.enrollment(enrollment_id).next_action_at is None


@pytest.mark.parametrize(
    ("after", "confirmed"),
    [
        (timedelta(days=3, seconds=-1), True),
        (timedelta(days=3), False),
        (timedelta(days=20), False),
    ],
)
def test_a_discarded_prefill_is_confirmed_only_within_three_days(
    linkedin_first: World, after: timedelta, confirmed: bool
) -> None:
    """A discard means "I won't send it": a message much later is not the prefill."""
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)
    _discard(world, message_id, NOW + timedelta(minutes=1))
    due_before = world.enrollment(enrollment_id).next_action_at
    thread, _ = you_sent(ADA, NOW + after)
    world.apply(thread, polled_at=NOW + after + timedelta(hours=1))
    status = world.message(message_id).status
    if confirmed:
        assert status is MessageStatus.SENT
    else:
        assert status is MessageStatus.DISCARDED
        assert world.enrollment(enrollment_id).next_action_at == due_before


def test_a_stale_prefill_has_no_such_bound(linkedin_first: World) -> None:
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id = world.prefill(enrollment_id)
    world.tick(NOW + timedelta(days=3))
    thread, _ = you_sent(ADA, NOW + timedelta(days=20))
    world.apply(thread, polled_at=NOW + timedelta(days=20, hours=1))
    assert world.message(message_id).status is MessageStatus.SENT


def test_a_stored_send_with_two_known_conversations_does_not_confirm_a_known_one(
    linkedin_first: World,
) -> None:
    """The prefill learned its conversation; a send an earlier poll stored, for a contact
    with two conversations, is in an unknown one, so it confirms nothing."""
    world = linkedin_first
    enrollment_id = world.enroll()
    message_id, run_id = _claim_only(world, enrollment_id)
    old, _ = you_sent(ADA, NOW - timedelta(days=30), name="two")
    world.apply(old, polled_at=NOW - timedelta(days=29))
    prefilled_at = NOW + timedelta(minutes=2)
    thread, _ = you_sent(ADA, prefilled_at - timedelta(seconds=10))
    world.apply(thread, polled_at=NOW + timedelta(minutes=1, seconds=55))

    def run(session: Session, user: User) -> None:
        outcome = MessageOutcome(MessageOutcomeKind.PREFILLED, "fixed words", CONVERSATION, 12)
        assert record_prefill_outcome(
            session, user, message_id, outcome, settings=world.settings, now=prefilled_at
        )
        runs.finish_run(session, user, run_id, status=SyncRunStatus.COMPLETED, now=prefilled_at)

    world.write(run)
    world.apply(polled_at=NOW + timedelta(hours=1))
    assert world.message(message_id).status is MessageStatus.PREFILLED
