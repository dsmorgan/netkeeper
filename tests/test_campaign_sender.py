"""netkeeper.services.campaign_sender (spec 11.5; item P3-07): send and draft modes,
follow-ups in thread, labels, the drafts poll, and reconciling by Message-ID.

Every test runs the real engine tick against :class:`FakeGmail`. Nothing here
reaches Gmail or Google.
"""

from __future__ import annotations

import contextlib
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any

import factories
import pytest
from campaign_fakes import NOW, SETTINGS, make_mailbox
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.compose import message_id_for
from netkeeper.campaigns.gmail import (
    Draft,
    GmailRateLimited,
    GmailRejected,
    GmailTransient,
    MessageRef,
)
from netkeeper.campaigns.gmail_fake import FakeGmail
from netkeeper.crm.contacts import merge_contacts
from netkeeper.db import session_scope
from netkeeper.models import (
    Campaign,
    Enrollment,
    EnrollmentStatus,
    Mailbox,
    Message,
    MessageStatus,
    StepMode,
    TemplateChannel,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import campaign_engine as engine_module
from netkeeper.services import campaign_sender as sender_module
from netkeeper.services.campaign_engine import (
    DRAFT_DISCARDED_REASON,
    DRAFT_MISSING,
    Firing,
    Reconciler,
    SendOutcome,
    SendResult,
    TickResult,
    remove_enrollment,
    run_tick,
)
from netkeeper.services.campaign_sender import GmailSender
from netkeeper.services.mailboxes import MailboxNotReady

EMAIL = TemplateChannel.EMAIL
WEEK = timedelta(days=7)
LATER = engine_module.RECONCILE_AFTER + timedelta(minutes=1)


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


class LostAnswers(FakeGmail):
    """A Gmail whose answer to the next ``lose`` writes never arrives, after it acted."""

    lose: int = 0

    def _lost(self) -> None:
        if self.lose:
            self.lose -= 1
            raise GmailTransient("reset", code="unavailable", outcome_unknown=True)

    def send(
        self, message: EmailMessage, *, thread_id: str | None = None, purpose: str
    ) -> MessageRef:
        ref = super().send(message, thread_id=thread_id, purpose=purpose)
        self._lost()
        return ref

    def create_draft(
        self, message: EmailMessage, *, thread_id: str | None = None, purpose: str
    ) -> Draft:
        draft = super().create_draft(message, thread_id=thread_id, purpose=purpose)
        self._lost()
        return draft


class Crash(BaseException):
    """The process dying: nothing in the tick catches it."""


@dataclass
class CrashAfterSend:
    """A sender that sends, then the process dies before the send is recorded."""

    inner: GmailSender

    def send(self, firing: Firing) -> SendResult:
        self.inner.send(firing)
        raise Crash


@dataclass
class Mail:
    factory: sessionmaker[Session]
    user: User
    mailbox: Mailbox
    campaign: Campaign
    gmail: LostAnswers
    clock: Clock
    sender: GmailSender

    @property
    def label(self) -> str:
        return f"netkeeper/{self.campaign.name}"

    def tick(self, at: datetime | None = None, *, sender: Any = None) -> TickResult:
        if at is not None:
            self.clock.now = at
        results = run_tick(
            self.factory,
            settings=SETTINGS,
            sender=sender or self.sender,
            clock=self.clock,
            rng=random.Random(1),
        )
        [result] = [r for r in results if r.user_id == self.user.id]
        return result

    def read[T](self, fn: Callable[[Session], T]) -> T:
        with session_scope(self.factory) as session:
            return fn(session)

    def write[T](self, fn: Callable[[Session], T]) -> T:
        with session_scope(self.factory, write=True) as session:
            return fn(session)

    def enroll(self, email: str = "ada@example.test") -> int:
        def make(session: Session) -> int:
            contact = factories.make_contact(session, self.user, emails=[email])
            campaign = get_scoped(session, self.user, Campaign, self.campaign.id)
            assert campaign is not None
            return factories.make_enrollment(
                session, campaign, contact, next_action_at=self.clock.now
            ).id

        return self.write(make)

    def messages(self, enrollment_id: int | None = None) -> list[Message]:
        def load(session: Session) -> list[Message]:
            statement = scoped(self.user, Message).order_by(Message.id)
            if enrollment_id is not None:
                statement = statement.where(Message.enrollment_id == enrollment_id)
            return list(session.scalars(statement))

        return self.read(load)

    def enrollment(self, enrollment_id: int) -> Enrollment:
        row = self.read(lambda s: get_scoped(s, self.user, Enrollment, enrollment_id))
        assert row is not None
        return row

    def set_message(self, message_id: int, **changes: Any) -> None:
        def change(session: Session) -> None:
            row = get_scoped(session, self.user, Message, message_id)
            assert row is not None
            for name, value in changes.items():
                setattr(row, name, value)

        self.write(change)

    def labelled(self) -> list[str]:
        """Gmail ids of the messages that carry the campaign's label."""
        [label] = [lb for lb in self.gmail.list_labels(purpose="test") if lb.name == self.label]
        return [m.id for m in self.gmail.sent() if label.id in m.label_ids]


def make_mail(
    factory: sessionmaker[Session],
    *,
    modes: Sequence[StepMode] = (StepMode.SEND, StepMode.SEND, StepMode.SEND),
    same_thread: Sequence[bool] = (False, True, True),
    opener: Callable[[int, int], Any] | None = None,
) -> Mail:
    clock = Clock()
    with session_scope(factory, write=True) as session:
        user = factories.make_user(session)
        mailbox = make_mailbox(session, user, email="me@example.com")
        campaign = factories.make_campaign(
            session, user, channels=(EMAIL,) * len(modes), mailbox_id=mailbox.id
        )
        for step, mode, threaded in zip(campaign.steps, modes, same_thread, strict=True):
            step.mode = mode
            step.same_thread = threaded
        session.flush()
    gmail = LostAnswers("me@example.com", mailbox_id=mailbox.id, clock=clock)
    sender = GmailSender(
        factory,
        opener=opener or (lambda user_id, mailbox_id: gmail),
        clock=clock,
        drafts_every=timedelta(0),
    )
    return Mail(factory, user, mailbox, campaign, gmail, clock, sender)


@pytest.fixture
def mail(session_factory: sessionmaker[Session]) -> Mail:
    return make_mail(session_factory)


@pytest.fixture
def drafts(session_factory: sessionmaker[Session]) -> Mail:
    return make_mail(session_factory, modes=(StepMode.DRAFT, StepMode.DRAFT, StepMode.DRAFT))


def expected_message_id(mail: Mail, message: Message) -> str:
    return message_id_for(
        user_id=mail.user.id,
        message_id=message.id,
        created_at=message.created_at,
        address="me@example.com",
    )


# --- constants -----------------------------------------------------------------------------


def test_the_sender_constants_are_pinned() -> None:
    """Safety constants against numbers written out here (CLAUDE.md)."""
    assert timedelta(minutes=10) == sender_module.DRAFTS_POLL_EVERY
    assert sender_module.FIND_MAX == 10
    assert timedelta(minutes=10) == engine_module.RECONCILE_AFTER
    assert engine_module.RECONCILE_BATCH == 10
    assert engine_module.STOP_WAIT_S == 45.0
    assert engine_module.DRAFT_DISCARDED_REASON == "draft_discarded"
    assert {s.value for s in engine_module.LIVE_STATUSES} == {"pending", "active", "paused"}


def test_the_gmail_sender_is_a_reconciler(mail: Mail) -> None:
    assert isinstance(mail.sender, Reconciler)


# --- send mode -----------------------------------------------------------------------------


def test_a_send_step_goes_out_with_its_message_id_and_label(mail: Mail) -> None:
    enrollment_id = mail.enroll("ada@example.test")
    [(_, outcome)] = mail.tick().fired
    assert outcome.outcome is SendOutcome.SENT

    [sent] = mail.gmail.sent()
    [message] = mail.messages(enrollment_id)
    assert (message.status, message.sent_at) == (MessageStatus.SENT, NOW)
    assert (message.gmail_message_id, message.gmail_thread_id) == (sent.id, sent.thread_id)
    assert sent.header("To") == "ada@example.test"
    assert sent.header("Subject") == "Hello"
    assert sent.header("Message-ID") == expected_message_id(mail, message)
    assert sent.header("In-Reply-To") is None and sent.header("References") is None
    assert sent.header("From") == "me@example.com"  # Gmail's own, never set by netkeeper
    assert mail.gmail.raw(sent.id).get_content().startswith("Hi First")
    assert mail.labelled() == [sent.id]
    assert mail.enrollment(enrollment_id).next_action_at == NOW + WEEK


def test_a_follow_up_has_the_headers_to_join_the_first_steps_thread(mail: Mail) -> None:
    """P3-07's "done when": the fake shows correct headers for a follow-up (#269, 8)."""
    enrollment_id = mail.enroll()
    mail.tick()
    mail.tick(NOW + WEEK)

    first, second = mail.gmail.sent()
    first_id = first.header("Message-ID")
    assert first_id is not None
    assert second.thread_id == first.thread_id  # the fake's threading rule accepted it
    assert second.header("In-Reply-To") == first_id
    assert second.header("References") == first_id
    assert second.header("Subject") == "Re: Hello"
    rows = mail.messages(enrollment_id)
    assert [m.status for m in rows] == [MessageStatus.SENT] * 2
    assert second.header("Message-ID") == expected_message_id(mail, rows[1])
    assert rows[1].gmail_thread_id == first.thread_id
    assert sorted(mail.labelled()) == sorted([first.id, second.id])
    assert ("threads.get", f"send step 2 for enrollment {enrollment_id}") in mail.gmail.calls


def test_a_third_step_cites_every_earlier_step_and_replies_to_the_newest(mail: Mail) -> None:
    mail.enroll()
    mail.tick()
    mail.tick(NOW + WEEK)
    mail.tick(NOW + 2 * WEEK)

    first, second, third = mail.gmail.sent()
    ids = [m.header("Message-ID") for m in (first, second)]
    assert third.thread_id == first.thread_id
    assert third.header("References") == f"{ids[0]} {ids[1]}"
    assert third.header("In-Reply-To") == ids[1]
    assert third.header("Subject") == "Re: Hello"  # never "Re: Re: Hello"


def test_a_follow_up_cites_the_message_id_gmail_holds(mail: Mail) -> None:
    """The citation comes from Gmail's copy of the thread, not from the database: a
    draft the person sent can carry a Message-ID of Gmail's making."""
    enrollment_id = mail.enroll()
    mail.tick()
    [first] = mail.gmail.sent()
    stored = mail.gmail.raw(first.id)
    del stored["Message-ID"]
    stored["Message-ID"] = "<gmail-made@mail.gmail.com>"

    mail.tick(NOW + WEEK)
    _, second = mail.gmail.sent()
    assert second.header("In-Reply-To") == "<gmail-made@mail.gmail.com>"
    assert second.thread_id == first.thread_id
    assert mail.messages(enrollment_id)[1].status is MessageStatus.SENT


def test_a_step_without_same_thread_starts_its_own_conversation(
    session_factory: sessionmaker[Session],
) -> None:
    mail = make_mail(session_factory, same_thread=(False, False, False))
    mail.enroll()
    mail.tick()
    mail.tick(NOW + WEEK)
    first, second = mail.gmail.sent()
    assert second.thread_id != first.thread_id
    assert second.header("Subject") == "Hello"
    assert second.header("In-Reply-To") is None
    assert "threads.get" not in [method for method, _ in mail.gmail.calls]


def test_a_follow_up_whose_thread_is_gone_is_not_sent(mail: Mail) -> None:
    enrollment_id = mail.enroll()
    mail.tick()
    [first] = mail.gmail.sent()
    mail.gmail.delete(MessageRef(first.id, first.thread_id))

    [(_, outcome)] = mail.tick(NOW + WEEK).fired
    assert outcome.outcome is SendOutcome.FAILED
    assert outcome.error is not None and "thread is gone" in outcome.error
    assert mail.gmail.sent() == []
    assert mail.messages(enrollment_id)[1].status is MessageStatus.FAILED


# --- draft mode ----------------------------------------------------------------------------


def test_a_draft_step_becomes_a_gmail_draft(drafts: Mail) -> None:
    enrollment_id = drafts.enroll()
    [(_, outcome)] = drafts.tick().fired
    assert outcome.outcome is SendOutcome.DRAFTED
    assert drafts.gmail.sent() == []
    [(draft_id, ref)] = drafts.gmail.drafts().items()
    [message] = drafts.messages(enrollment_id)
    assert (message.status, message.sent_at) == (MessageStatus.DRAFTED, None)
    assert (message.gmail_draft_id, message.gmail_message_id) == (draft_id, ref.id)
    assert drafts.gmail.raw(ref.id)["Message-ID"] == expected_message_id(drafts, message)
    assert drafts.enrollment(enrollment_id).next_action_at is None  # waits for the send


def test_a_draft_that_disappears_with_a_sent_message_in_its_thread_is_sent(drafts: Mail) -> None:
    """P3-07's "done when": the drafts poll sees the draft gone and a SENT message in its
    thread, records ``sent_at`` from Gmail, and schedules the next step from it."""
    enrollment_id = drafts.enroll()
    drafts.tick()
    [draft_id] = drafts.gmail.drafts()
    sent_at = NOW + timedelta(days=2, hours=3)
    sent_ref = drafts.gmail.send_draft(draft_id, at=sent_at)

    drafts.tick(NOW + timedelta(days=2, hours=4))
    [message] = drafts.messages(enrollment_id)
    assert (message.status, message.sent_at) == (MessageStatus.SENT, sent_at)
    assert message.gmail_message_id == sent_ref.id
    assert message.error is None
    enrollment = drafts.enrollment(enrollment_id)
    assert (enrollment.current_step, enrollment.next_action_at) == (1, sent_at + WEEK)
    assert drafts.labelled() == [sent_ref.id]


def test_a_follow_up_draft_goes_into_the_sent_drafts_thread(drafts: Mail) -> None:
    drafts.enroll()
    drafts.tick()
    [draft_id] = drafts.gmail.drafts()
    first = drafts.gmail.send_draft(draft_id, at=NOW + timedelta(hours=1))
    drafts.tick(NOW + timedelta(hours=2))  # the poll sees it sent
    drafts.tick(NOW + timedelta(hours=1) + WEEK)

    [(_, ref)] = drafts.gmail.drafts().items()
    follow_up = drafts.gmail.raw(ref.id)
    assert ref.thread_id == first.thread_id
    assert follow_up["In-Reply-To"] == drafts.gmail.raw(first.id)["Message-ID"]
    assert follow_up["Subject"] == "Re: Hello"


def test_a_draft_still_waiting_changes_nothing(drafts: Mail) -> None:
    enrollment_id = drafts.enroll()
    drafts.tick()

    def state() -> list[tuple[Any, ...]]:
        return [
            (m.status, m.error, m.sent_at, m.gmail_draft_id, m.gmail_message_id)
            for m in drafts.messages(enrollment_id)
        ]

    before = state()
    drafts.tick(NOW + timedelta(hours=5))
    assert state() == before
    assert before[0][0] is MessageStatus.DRAFTED


def test_a_deleted_draft_is_discarded_on_the_second_poll_and_the_enrollment_removed(
    drafts: Mail,
) -> None:
    """Spec 11.5: a draft deleted instead of sent. Gone once could be an undo send."""
    enrollment_id = drafts.enroll()
    drafts.tick()
    [draft_id] = drafts.gmail.drafts()
    drafts.gmail.discard_draft(draft_id)

    drafts.tick(NOW + timedelta(hours=1))
    [message] = drafts.messages(enrollment_id)
    assert (message.status, message.error) == (MessageStatus.DRAFTED, DRAFT_MISSING)
    assert drafts.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE

    drafts.tick(NOW + timedelta(hours=2))
    [message] = drafts.messages(enrollment_id)
    assert (message.status, message.gmail_draft_id) == (MessageStatus.DISCARDED, None)
    enrollment = drafts.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.exit_reason) == (
        EnrollmentStatus.REMOVED,
        DRAFT_DISCARDED_REASON,
    )


def test_a_draft_seen_again_clears_its_missing_mark(drafts: Mail) -> None:
    enrollment_id = drafts.enroll()
    drafts.tick()
    [message] = drafts.messages(enrollment_id)
    drafts.set_message(message.id, error=DRAFT_MISSING)
    drafts.tick(NOW + timedelta(hours=1))
    [message] = drafts.messages(enrollment_id)
    assert (message.status, message.error) == (MessageStatus.DRAFTED, None)


def test_the_drafts_poll_waits_its_interval(session_factory: sessionmaker[Session]) -> None:
    mail = make_mail(session_factory, modes=(StepMode.DRAFT,), same_thread=(False,))
    mail.sender = GmailSender(
        session_factory, opener=lambda u, m: mail.gmail, clock=mail.clock
    )  # the default interval
    enrollment_id = mail.enroll()
    mail.tick()
    mail.tick(NOW + timedelta(minutes=1))  # the first poll
    [draft_id] = mail.gmail.drafts()
    mail.gmail.send_draft(draft_id, at=NOW + timedelta(minutes=2))

    def polls() -> int:
        return [m for m, _ in mail.gmail.calls].count("drafts.list")

    before = polls()
    mail.tick(NOW + timedelta(minutes=10))
    assert polls() == before
    assert mail.messages(enrollment_id)[0].status is MessageStatus.DRAFTED
    mail.tick(NOW + timedelta(minutes=11))
    assert polls() == before + 1
    assert mail.messages(enrollment_id)[0].status is MessageStatus.SENT


def test_a_drafts_poll_failure_changes_nothing(drafts: Mail) -> None:
    enrollment_id = drafts.enroll()
    drafts.tick()
    [draft_id] = drafts.gmail.drafts()
    drafts.gmail.discard_draft(draft_id)
    drafts.gmail.fail_next("drafts.list", GmailRateLimited("slow down", code="rateLimitExceeded"))
    drafts.tick(NOW + timedelta(hours=1))
    [message] = drafts.messages(enrollment_id)
    assert (message.status, message.error) == (MessageStatus.DRAFTED, None)


# --- failures that sent nothing ------------------------------------------------------------


def test_a_mailbox_that_is_not_ready_sends_nothing(session_factory: sessionmaker[Session]) -> None:
    def refuse(user_id: int, mailbox_id: int) -> Any:
        raise MailboxNotReady(mailbox_id, "reauth_required")

    mail = make_mail(session_factory, opener=refuse)
    enrollment_id = mail.enroll()
    [(_, outcome)] = mail.tick().fired
    assert outcome.outcome is SendOutcome.FAILED
    assert outcome.error == "the mailbox is not ready (reauth_required); nothing was sent"
    assert mail.messages(enrollment_id)[0].status is MessageStatus.FAILED


@pytest.mark.parametrize(
    "error",
    [
        GmailRejected("Invalid To header", code="invalidArgument"),
        GmailRateLimited("slow down", code="rateLimitExceeded"),
        GmailTransient("no route", code="unavailable", outcome_unknown=False),
    ],
)
def test_a_send_gmail_refused_is_failed_and_never_retried(mail: Mail, error: Exception) -> None:
    enrollment_id = mail.enroll()
    mail.gmail.fail_next("messages.send", error)  # type: ignore[arg-type]
    [(_, outcome)] = mail.tick().fired
    assert outcome.outcome is SendOutcome.FAILED
    assert outcome.error is not None and "ada@" not in outcome.error
    [message] = mail.messages(enrollment_id)
    assert message.status is MessageStatus.FAILED
    mail.tick(NOW + timedelta(days=1))
    mail.tick(NOW + timedelta(days=1) + LATER)
    assert [m for m, _ in mail.gmail.calls].count("messages.send") == 1
    assert "messages.list" not in [m for m, _ in mail.gmail.calls]  # no search for a known outcome


def test_a_label_failure_never_fails_a_send(mail: Mail) -> None:
    enrollment_id = mail.enroll()
    mail.gmail.fail_next("labels.list", GmailTransient("down", code="unavailable"))
    [(_, outcome)] = mail.tick().fired
    assert outcome.outcome is SendOutcome.SENT
    assert mail.messages(enrollment_id)[0].status is MessageStatus.SENT
    assert [lb.name for lb in mail.gmail.list_labels(purpose="test") if lb.type == "user"] == []


def test_the_label_is_looked_up_once_per_mailbox(mail: Mail) -> None:
    mail.enroll("ada@example.test")
    mail.enroll("bob@example.test")
    mail.tick()
    mail.tick(NOW + timedelta(hours=1))
    methods = [m for m, _ in mail.gmail.calls]
    assert (methods.count("labels.list"), methods.count("labels.create")) == (1, 1)
    assert len(mail.labelled()) == 2


# --- an answer that never came (#269, requirement 1) ---------------------------------------


def test_a_send_whose_answer_was_lost_is_found_by_its_message_id_at_once(mail: Mail) -> None:
    enrollment_id = mail.enroll()
    mail.gmail.lose = 1
    [(_, outcome)] = mail.tick().fired
    assert outcome.outcome is SendOutcome.SENT
    [sent] = mail.gmail.sent()
    [message] = mail.messages(enrollment_id)
    assert (message.status, message.gmail_message_id) == (MessageStatus.SENT, sent.id)
    methods = [m for m, _ in mail.gmail.calls]
    assert methods.index("messages.list") > methods.index("messages.send")
    assert methods.count("messages.send") == 1
    assert mail.labelled() == [sent.id]


def test_a_draft_whose_answer_was_lost_is_found_by_its_message_id(drafts: Mail) -> None:
    enrollment_id = drafts.enroll()
    drafts.gmail.lose = 1
    [(_, outcome)] = drafts.tick().fired
    assert outcome.outcome is SendOutcome.DRAFTED
    [(draft_id, _)] = drafts.gmail.drafts().items()
    [message] = drafts.messages(enrollment_id)
    assert (message.status, message.gmail_draft_id) == (MessageStatus.DRAFTED, draft_id)


def test_an_unknown_outcome_not_found_stays_scheduled_then_fails_never_resent(
    mail: Mail,
) -> None:
    enrollment_id = mail.enroll()
    mail.gmail.fail_next(
        "messages.send", GmailTransient("reset", code="unavailable", outcome_unknown=True)
    )
    [(_, outcome)] = mail.tick().fired
    assert outcome.outcome is SendOutcome.UNKNOWN
    [message] = mail.messages(enrollment_id)
    assert message.status is MessageStatus.SCHEDULED  # never "failed" while it may be out
    assert message.error is not None and "unknown" in message.error

    mail.tick(NOW + timedelta(minutes=5))  # too soon to rule it out
    assert mail.messages(enrollment_id)[0].status is MessageStatus.SCHEDULED

    mail.tick(NOW + LATER)
    [message] = mail.messages(enrollment_id)
    assert message.status is MessageStatus.FAILED
    assert mail.enrollment(enrollment_id).next_action_at is None  # parked for a person
    mail.tick(NOW + timedelta(days=30))
    assert [m for m, _ in mail.gmail.calls].count("messages.send") == 1
    assert mail.gmail.sent() == []


def test_an_unknown_outcome_found_later_is_sent_and_the_next_step_scheduled(mail: Mail) -> None:
    enrollment_id = mail.enroll()
    mail.gmail.lose = 1
    mail.gmail.fail_next("messages.list", GmailTransient("down", code="unavailable"))
    [(_, outcome)] = mail.tick().fired
    assert outcome.outcome is SendOutcome.UNKNOWN
    assert mail.messages(enrollment_id)[0].status is MessageStatus.SCHEDULED

    mail.tick(NOW + LATER)
    [message] = mail.messages(enrollment_id)
    [sent] = mail.gmail.sent()
    assert (message.status, message.sent_at) == (MessageStatus.SENT, NOW)
    assert message.gmail_message_id == sent.id
    enrollment = mail.enrollment(enrollment_id)
    assert (enrollment.current_step, enrollment.next_action_at) == (1, NOW + WEEK)
    assert mail.labelled() == [sent.id]


def test_a_reconcile_that_cannot_reach_gmail_leaves_the_message(mail: Mail) -> None:
    enrollment_id = mail.enroll()
    mail.gmail.fail_next(
        "messages.send", GmailTransient("reset", code="unavailable", outcome_unknown=True)
    )
    mail.tick()
    mail.gmail.fail_next("messages.list", GmailRateLimited("slow", code="rateLimitExceeded"))
    mail.tick(NOW + LATER)
    assert mail.messages(enrollment_id)[0].status is MessageStatus.SCHEDULED
    mail.tick(NOW + LATER + timedelta(minutes=1))
    assert mail.messages(enrollment_id)[0].status is MessageStatus.FAILED


# --- crash leftovers (#269, requirement 2) -------------------------------------------------


def test_a_crash_after_the_send_is_reconciled_as_sent_never_sent_again(mail: Mail) -> None:
    enrollment_id = mail.enroll()
    with pytest.raises(Crash):
        mail.tick(sender=CrashAfterSend(mail.sender))
    assert mail.messages(enrollment_id)[0].status is MessageStatus.SCHEDULED

    mail.tick(NOW + LATER)
    [message] = mail.messages(enrollment_id)
    [sent] = mail.gmail.sent()
    assert (message.status, message.gmail_message_id) == (MessageStatus.SENT, sent.id)
    assert mail.enrollment(enrollment_id).next_action_at == NOW + WEEK
    assert [m for m, _ in mail.gmail.calls].count("messages.send") == 1


@pytest.mark.parametrize("delivered", [True, False])
def test_a_removed_enrollments_leftover_is_reconciled_not_discarded_blindly(
    mail: Mail, delivered: bool
) -> None:
    """``_end`` leaves a ``scheduled`` message alone: only the search says what it is."""
    enrollment_id = mail.enroll()
    sender: Any = CrashAfterSend(mail.sender)
    if not delivered:
        mail.gmail.fail_next(
            "messages.send", GmailTransient("reset", code="unavailable", outcome_unknown=True)
        )
        mail.gmail.fail_next("messages.list", GmailTransient("down", code="unavailable"))
        sender = mail.sender
    with contextlib.suppress(Crash):
        mail.tick(sender=sender)
    mail.write(lambda s: remove_enrollment(s, mail.user, enrollment_id))
    assert mail.messages(enrollment_id)[0].status is MessageStatus.SCHEDULED

    mail.tick(NOW + LATER)
    [message] = mail.messages(enrollment_id)
    expected = MessageStatus.SENT if delivered else MessageStatus.DISCARDED
    assert message.status is expected
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.REMOVED


def test_a_leftover_found_as_a_draft_is_drafted(drafts: Mail) -> None:
    enrollment_id = drafts.enroll()
    with pytest.raises(Crash):
        drafts.tick(sender=CrashAfterSend(drafts.sender))
    drafts.tick(NOW + LATER)
    [message] = drafts.messages(enrollment_id)
    [draft_id] = drafts.gmail.drafts()
    assert (message.status, message.gmail_draft_id) == (MessageStatus.DRAFTED, draft_id)
    assert drafts.enrollment(enrollment_id).current_step == 1


# --- discarded drafts (#269, requirement 3) ------------------------------------------------


def test_a_merge_discarded_drafts_gmail_draft_is_deleted(drafts: Mail) -> None:
    older = drafts.enroll("ada@example.test")
    newer = drafts.enroll("ada.other@example.test")
    drafts.tick()
    drafts.tick(NOW + timedelta(hours=1))
    assert len(drafts.gmail.drafts()) == 2
    survivor, loser = (drafts.enrollment(i).contact_id for i in (newer, older))
    drafts.write(lambda s: merge_contacts(s, drafts.user, survivor, loser))
    rows = drafts.messages()
    [discarded] = [m for m in rows if m.status is MessageStatus.DISCARDED]
    [kept] = [m for m in rows if m.status is MessageStatus.DRAFTED]
    assert discarded.gmail_draft_id is not None

    drafts.tick(NOW + timedelta(hours=2))
    assert list(drafts.gmail.drafts()) == [kept.gmail_draft_id]
    [after] = [m for m in drafts.messages() if m.id == discarded.id]
    assert (after.status, after.gmail_draft_id) == (MessageStatus.DISCARDED, None)
    assert (
        "drafts.delete",
        f"delete the discarded draft of message {discarded.id} "
        f"for enrollment {after.enrollment_id}",
    ) in drafts.gmail.calls


def test_a_discarded_draft_the_person_sent_anyway_is_sent(drafts: Mail) -> None:
    enrollment_id = drafts.enroll()
    drafts.tick()
    [message] = drafts.messages(enrollment_id)
    assert message.gmail_draft_id is not None
    drafts.set_message(message.id, status=MessageStatus.DISCARDED)
    sent_ref = drafts.gmail.send_draft(message.gmail_draft_id, at=NOW + timedelta(minutes=30))

    drafts.tick(NOW + timedelta(hours=1))
    [message] = drafts.messages(enrollment_id)
    assert (message.status, message.sent_at) == (MessageStatus.SENT, NOW + timedelta(minutes=30))
    assert message.gmail_message_id == sent_ref.id
    assert "drafts.delete" not in [m for m, _ in drafts.gmail.calls]


def test_a_discarded_draft_already_gone_only_forgets_its_id(drafts: Mail) -> None:
    enrollment_id = drafts.enroll()
    drafts.tick()
    [message] = drafts.messages(enrollment_id)
    assert message.gmail_draft_id is not None
    drafts.set_message(message.id, status=MessageStatus.DISCARDED)
    drafts.gmail.discard_draft(message.gmail_draft_id)

    drafts.tick(NOW + timedelta(hours=1))
    [message] = drafts.messages(enrollment_id)
    assert (message.status, message.gmail_draft_id) == (MessageStatus.DISCARDED, None)


def test_a_discarded_draft_sent_between_the_list_and_the_delete_is_sent(drafts: Mail) -> None:
    enrollment_id = drafts.enroll()
    drafts.tick()
    [message] = drafts.messages(enrollment_id)
    draft_id = message.gmail_draft_id
    assert draft_id is not None
    drafts.set_message(message.id, status=MessageStatus.DISCARDED)
    sent_at = NOW + timedelta(minutes=45)

    real_delete = drafts.gmail.delete_draft

    def raced(draft: str, *, purpose: str) -> None:
        drafts.gmail.send_draft(draft, at=sent_at)  # the person, a moment before
        real_delete(draft, purpose=purpose)

    drafts.gmail.delete_draft = raced  # type: ignore[method-assign,assignment]
    drafts.tick(NOW + timedelta(hours=1))
    [message] = drafts.messages(enrollment_id)
    assert (message.status, message.sent_at) == (MessageStatus.SENT, sent_at)


# --- nothing reaches Gmail but the fake ----------------------------------------------------


def test_every_gmail_call_names_a_purpose_without_an_address(mail: Mail) -> None:
    mail.enroll("ada@example.test")
    mail.gmail.lose = 1
    mail.tick()
    mail.tick(NOW + WEEK)
    assert mail.gmail.calls
    assert all("@" not in purpose for _, purpose in mail.gmail.calls)


def test_the_find_refuses_something_that_is_not_a_message_id() -> None:
    with pytest.raises(ValueError, match="not a Message-ID"):
        sender_module.find_by_message_id(FakeGmail(), "in:anywhere", purpose="test")


def test_a_message_found_in_trash_still_counts_as_sent() -> None:
    gmail = FakeGmail("me@example.com")
    message = EmailMessage()
    message["To"] = "ada@example.test"
    message["Subject"] = "Hello"
    message["Message-ID"] = "<abc@example.com>"
    message.set_content("Hi")
    ref = gmail.send(message, purpose="test")
    gmail.modify_labels(ref.id, add=("TRASH",), purpose="test")
    found = sender_module.find_by_message_id(gmail, "<abc@example.com>", purpose="test")
    assert found is not None and found.sent and found.message.id == ref.id


def test_a_user_with_nothing_to_reconcile_makes_no_gmail_call(mail: Mail) -> None:
    mail.tick()
    assert mail.gmail.calls == []


def test_an_unknown_mailbox_during_reconcile_waits(session_factory: sessionmaker[Session]) -> None:
    ready = {"yes": True}
    holder: dict[str, Mail] = {}

    def opener(user_id: int, mailbox_id: int) -> Any:
        if not ready["yes"]:
            raise MailboxNotReady(mailbox_id, "reauth_required")
        return holder["mail"].gmail

    mail = make_mail(session_factory, opener=opener)
    holder["mail"] = mail
    enrollment_id = mail.enroll()
    with pytest.raises(Crash):
        mail.tick(sender=CrashAfterSend(mail.sender))
    ready["yes"] = False
    mail.tick(NOW + LATER)
    assert mail.messages(enrollment_id)[0].status is MessageStatus.SCHEDULED
    ready["yes"] = True
    mail.tick(NOW + LATER + timedelta(minutes=1))
    assert mail.messages(enrollment_id)[0].status is MessageStatus.SENT


def test_an_earlier_step_in_the_thread_never_reads_as_the_draft_sent(drafts: Mail) -> None:
    """Step 2's draft sits in step 1's thread. Deleted, it must read as missing: step 1's
    sent message in the same thread is not the draft going out."""
    enrollment_id = drafts.enroll()
    drafts.tick()
    [first_draft] = drafts.gmail.drafts()
    drafts.gmail.send_draft(first_draft, at=NOW + timedelta(hours=1))
    drafts.tick(NOW + timedelta(hours=2))
    drafts.tick(NOW + timedelta(hours=1) + WEEK)
    [second_draft] = drafts.gmail.drafts()
    drafts.gmail.discard_draft(second_draft)

    drafts.tick(NOW + timedelta(hours=2) + WEEK)
    first, second = drafts.messages(enrollment_id)
    assert first.status is MessageStatus.SENT
    assert (second.status, second.error) == (MessageStatus.DRAFTED, DRAFT_MISSING)


def test_a_label_that_fails_to_apply_is_looked_up_again(mail: Mail) -> None:
    """The person may have deleted the label in Gmail: the cached id is dropped."""
    mail.enroll("ada@example.test")
    mail.enroll("bob@example.test")
    mail.gmail.fail_next("messages.modify", GmailRejected("Invalid label", code="invalidArgument"))
    [(_, outcome)] = mail.tick().fired
    assert outcome.outcome is SendOutcome.SENT
    mail.tick(NOW + timedelta(hours=1))
    methods = [m for m, _ in mail.gmail.calls]
    assert methods.count("labels.list") == 2
    assert len(mail.labelled()) == 1  # the second send is labelled
