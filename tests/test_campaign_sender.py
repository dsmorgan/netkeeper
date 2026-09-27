"""netkeeper.services.campaign_sender (spec 11.5; item P3-07): send and draft modes,
follow-ups in thread, labels, the drafts poll, and reconciling by Message-ID.

Every test runs the real engine tick against :class:`FakeGmail`. Nothing here
reaches Gmail or Google.
"""

from __future__ import annotations

import contextlib
import itertools
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
    GmailAuthError,
    GmailError,
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
    """A Gmail whose answer to the next ``lose`` writes never arrives, after it acted.
    The next ``lag`` searches by Message-ID find nothing (Gmail's search lagging a send),
    and the next ``unlisted`` draft listings leave every draft out."""

    lose: int = 0
    lag: int = 0
    unlisted: int = 0

    def search(self, query: str, *, max_results: int = 100, purpose: str) -> list[MessageRef]:
        found = super().search(query, max_results=max_results, purpose=purpose)
        if self.lag and "rfc822msgid:" in query:
            self.lag -= 1
            return []
        return found

    def list_drafts(self, *, purpose: str) -> list[Draft]:
        listed = super().list_drafts(purpose=purpose)
        if self.unlisted:
            self.unlisted -= 1
            return []
        return listed

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

    def enroll(self, email: str = "ada@example.test", *, campaign_id: int | None = None) -> int:
        def make(session: Session) -> int:
            contact = factories.make_contact(session, self.user, emails=[email])
            campaign = get_scoped(session, self.user, Campaign, campaign_id or self.campaign.id)
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


def search_until_given_up(mail: Mail, first: datetime) -> datetime:
    """Tick at every search a leftover gets, from ``first`` until no message is left
    ``scheduled`` (the engine's give-up). The time of the last tick."""
    every = engine_module.RECONCILE_SEARCH_EVERY
    at = first
    while True:
        mail.tick(at)
        if all(m.status is not MessageStatus.SCHEDULED for m in mail.messages()):
            return at
        assert at - first <= engine_module.RECONCILE_GIVE_UP_AFTER + every, "never gave up"
        at += every


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


def test_a_mailbox_that_is_not_ready_gives_the_claim_back(
    session_factory: sessionmaker[Session],
) -> None:
    ready = False
    opened: list[Mail] = []

    def open_when_ready(user_id: int, mailbox_id: int) -> Any:
        if not ready:
            raise MailboxNotReady(mailbox_id, "reauth_required")
        return opened[0].gmail

    mail = make_mail(session_factory, opener=open_when_ready)
    opened.append(mail)
    enrollment_id = mail.enroll()
    [(_, outcome)] = mail.tick().fired
    assert outcome.outcome is SendOutcome.NOT_SENT
    assert outcome.error == "the mailbox is not ready (reauth_required); nothing was sent"
    assert mail.messages(enrollment_id) == []  # the step is free to fire again
    retry = NOW + engine_module.RETRY_AFTER
    assert mail.enrollment(enrollment_id).next_action_at == retry

    ready = True
    [(_, outcome)] = mail.tick(retry).fired
    assert outcome.outcome is SendOutcome.SENT
    assert [m.status for m in mail.messages(enrollment_id)] == [MessageStatus.SENT]


def test_a_send_gmail_refused_is_failed_and_never_retried(mail: Mail) -> None:
    enrollment_id = mail.enroll()
    mail.gmail.fail_next(
        "messages.send", GmailRejected("Invalid To header", code="invalidArgument")
    )
    [(_, outcome)] = mail.tick().fired
    assert outcome.outcome is SendOutcome.FAILED
    assert outcome.error is not None and "ada@" not in outcome.error
    [message] = mail.messages(enrollment_id)
    assert message.status is MessageStatus.FAILED
    mail.tick(NOW + timedelta(days=1))
    mail.tick(NOW + timedelta(days=1) + LATER)
    assert [m for m, _ in mail.gmail.calls].count("messages.send") == 1
    assert "messages.list" not in [m for m, _ in mail.gmail.calls]  # no search for a known outcome


@pytest.mark.parametrize(
    ("method", "error"),
    [
        ("messages.send", GmailRateLimited("slow down", code="rateLimitExceeded")),
        ("messages.send", GmailTransient("no route", code="unavailable", outcome_unknown=False)),
        ("messages.send", GmailAuthError("token revoked", code="invalid_grant")),
        ("threads.get", GmailTransient("no route", code="unavailable")),
        ("threads.get", GmailRateLimited("slow down", code="rateLimitExceeded")),
    ],
    ids=["send-rate-limited", "send-unavailable", "send-auth", "thread-unavailable", "thread-rate"],
)
def test_a_send_that_certainly_sent_nothing_is_tried_again_later(
    mail: Mail, method: str, error: GmailError
) -> None:
    """#273 review, fix 4: nothing sent is never ``failed`` for a reason that may pass.
    The claim is given back, and the mailbox waits :data:`RETRY_AFTER` before its next."""
    enrollment_id = mail.enroll()
    if method == "threads.get":  # the follow-up's read of the first step's thread
        mail.tick()
        mail.clock.now = NOW + WEEK
    mail.gmail.fail_next(method, error)
    [(firing, outcome)] = mail.tick().fired
    assert outcome.outcome is SendOutcome.NOT_SENT
    attempt = mail.clock.now
    assert len(mail.messages(enrollment_id)) == firing.step_position - 1  # the claim is gone
    retry = attempt + engine_module.RETRY_AFTER
    assert mail.enrollment(enrollment_id).next_action_at == retry
    next_send = mail.read(lambda s: engine_module.next_send_at(s, mail.user, mail.mailbox.id))
    assert next_send is not None and next_send >= retry

    assert mail.tick(retry - timedelta(minutes=1)).fired == []
    [(again, outcome)] = mail.tick(retry).fired
    assert (again.step_position, outcome.outcome) == (firing.step_position, SendOutcome.SENT)
    statuses = [m.status for m in mail.messages(enrollment_id)]
    assert statuses == [MessageStatus.SENT] * firing.step_position
    assert len(mail.gmail.sent()) == firing.step_position  # never twice


def test_an_hour_long_outage_fails_no_step(session_factory: sessionmaker[Session]) -> None:
    """#273 review, fix 4: 8 due steps and an hour with Gmail down. Every one goes out
    once Gmail is back, once each, and none is ``failed``."""

    class Down(LostAnswers):
        until = NOW + timedelta(hours=1)

        def send(
            self, message: EmailMessage, *, thread_id: str | None = None, purpose: str
        ) -> MessageRef:
            if self.clock() < self.until:
                self._call("messages.send", purpose)
                raise GmailTransient("backend error", code="unavailable")
            return super().send(message, thread_id=thread_id, purpose=purpose)

    mail = make_mail(session_factory, modes=(StepMode.SEND,), same_thread=(False,))
    mail.gmail.__class__ = Down
    enrolled = [mail.enroll(f"p{n}@example.test") for n in range(8)]
    at = NOW
    while at < NOW + timedelta(hours=6):
        mail.tick(at)
        at += timedelta(minutes=5)
    statuses = [m.status for m in mail.messages()]
    assert statuses == [MessageStatus.SENT] * 8
    assert sorted(m.enrollment_id for m in mail.messages()) == sorted(enrolled)
    assert len(mail.gmail.sent()) == 8


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

    mail.tick(NOW + LATER)  # one empty search rules nothing out: Gmail's search can lag
    assert mail.messages(enrollment_id)[0].status is MessageStatus.SCHEDULED
    last = search_until_given_up(mail, NOW + LATER + engine_module.RECONCILE_SEARCH_EVERY)
    [message] = mail.messages(enrollment_id)
    assert message.status is MessageStatus.FAILED
    assert message.reconcile_first_miss_at == NOW + LATER
    assert message.reconcile_last_miss_at == last
    assert last - (NOW + LATER) >= engine_module.RECONCILE_GIVE_UP_AFTER
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
    [message] = mail.messages(enrollment_id)
    assert (message.status, message.reconcile_misses) == (MessageStatus.SCHEDULED, 0)
    mail.tick(NOW + LATER + timedelta(minutes=1))  # a search that ran: one miss, no more
    [message] = mail.messages(enrollment_id)
    assert (message.status, message.reconcile_misses) == (MessageStatus.SCHEDULED, 1)


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

    if delivered:
        mail.tick(NOW + LATER)
    else:
        search_until_given_up(mail, NOW + LATER)
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


# --- discarded drafts (#269, requirement 3; #273, question 2) -----------------------------


def test_a_merge_discarded_drafts_gmail_draft_stays_for_the_person(drafts: Mail) -> None:
    """netkeeper deletes nothing in Gmail (ADR 0003): the message records ``discarded``,
    and its draft stays where it is, untouched, poll after poll."""
    older = drafts.enroll("ada@example.test")
    newer = drafts.enroll("ada.other@example.test")
    drafts.tick()
    drafts.tick(NOW + timedelta(hours=1))
    before = drafts.gmail.drafts()
    assert len(before) == 2
    survivor, loser = (drafts.enrollment(i).contact_id for i in (newer, older))
    drafts.write(lambda s: merge_contacts(s, drafts.user, survivor, loser))
    [discarded] = [m for m in drafts.messages() if m.status is MessageStatus.DISCARDED]
    assert discarded.gmail_draft_id in before

    drafts.tick(NOW + timedelta(hours=2))
    drafts.tick(NOW + timedelta(hours=3))
    assert drafts.gmail.drafts() == before
    [after] = [m for m in drafts.messages() if m.id == discarded.id]
    assert (after.status, after.gmail_draft_id) == (
        MessageStatus.DISCARDED,
        discarded.gmail_draft_id,
    )
    methods = {m for m, _ in drafts.gmail.calls}
    assert methods <= {
        "drafts.create",
        "drafts.list",
        "labels.list",
        "labels.create",
        "threads.get",
        "messages.get",
        "messages.list",
    }


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


# --- #273 review -----------------------------------------------------------------------------


def person_writes(mail: Mail, thread_of: MessageRef, subject: str, at: datetime) -> MessageRef:
    """The person sending their own note in a thread, from Gmail: SENT, not netkeeper's."""
    first = mail.gmail.raw(thread_of.id)
    note = EmailMessage()
    note["To"] = "ada@example.test"
    note["Subject"] = subject
    note["In-Reply-To"] = first["Message-ID"]
    note["References"] = first["Message-ID"]
    note.set_content("One more thing.")
    saved, mail.clock.now = mail.clock.now, at
    try:
        return mail.gmail.send(note, thread_id=thread_of.thread_id, purpose="the person")
    finally:
        mail.clock.now = saved


@pytest.fixture
def mixed(session_factory: sessionmaker[Session]) -> Mail:
    """Step 1 sent, step 2 a draft in its thread, step 3 sent in the thread."""
    return make_mail(
        session_factory,
        modes=(StepMode.SEND, StepMode.DRAFT, StepMode.SEND),
        same_thread=(False, True, True),
    )


@pytest.mark.parametrize("snapshot", [True, False], ids=["snapshot", "made-before-0021"])
def test_a_deleted_draft_never_reads_as_sent_from_an_older_note_in_its_thread(
    mixed: Mail, snapshot: bool
) -> None:
    """#273 review, fix 1: step 1 is sent; on day 2 the person sends their own note in
    the thread; on day 7 step 2 is drafted; the person deletes the draft. Step 2 was
    never sent, so step 3 never goes out. A draft made before migration 0021 has no
    thread snapshot, and the note's date alone rules it out."""
    enrollment_id = mixed.enroll()
    mixed.tick()
    [first] = mixed.gmail.sent()
    person_writes(
        mixed, MessageRef(first.id, first.thread_id), "Re: Hello", NOW + timedelta(days=2)
    )
    mixed.tick(NOW + WEEK)
    [(draft_id, draft)] = mixed.gmail.drafts().items()
    assert draft.thread_id == first.thread_id
    if not snapshot:
        mixed.set_message(mixed.messages(enrollment_id)[1].id, thread_known_json=None)
    mixed.gmail.discard_draft(draft_id)

    mixed.tick(NOW + WEEK + timedelta(hours=1))
    mixed.tick(NOW + WEEK + timedelta(hours=2))
    step_two = mixed.messages(enrollment_id)[1]
    assert (step_two.status, step_two.sent_at) == (MessageStatus.DISCARDED, None)
    enrollment = mixed.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.current_step) == (EnrollmentStatus.REMOVED, 2)
    mixed.tick(NOW + timedelta(days=9))
    mixed.tick(NOW + timedelta(days=30))
    assert len(mixed.gmail.sent()) == 2  # step 1 and the person's note: no step 3


def test_a_note_the_thread_held_when_the_draft_was_made_never_reads_as_the_draft(
    mixed: Mail,
) -> None:
    """Gmail's clock ahead of netkeeper's: a note dated after the claim, but already in the
    thread when the draft was made. The draft's thread snapshot rules it out."""
    enrollment_id = mixed.enroll()
    mixed.tick()
    [first] = mixed.gmail.sent()
    note = person_writes(
        mixed, MessageRef(first.id, first.thread_id), "Re: Hello", NOW + WEEK + timedelta(minutes=5)
    )
    mixed.tick(NOW + WEEK)
    step_two = mixed.messages(enrollment_id)[1]
    assert step_two.thread_known_json is not None and note.id in step_two.thread_known_json
    [draft_id] = mixed.gmail.drafts()
    mixed.gmail.discard_draft(draft_id)

    mixed.tick(NOW + WEEK + timedelta(hours=1))
    mixed.tick(NOW + WEEK + timedelta(hours=2))
    assert mixed.messages(enrollment_id)[1].status is MessageStatus.DISCARDED


@pytest.mark.parametrize("keep_id", [False, True], ids=["new-id", "kept-id"])
def test_a_sent_draft_is_found_whether_gmail_keeps_its_id_or_not(
    drafts: Mail, keep_id: bool
) -> None:
    """E4: Gmail has been seen giving a sent draft a new message id, and keeping it."""
    enrollment_id = drafts.enroll()
    drafts.tick()
    [(draft_id, draft)] = drafts.gmail.drafts().items()
    sent_at = NOW + timedelta(hours=3)
    sent = drafts.gmail.send_draft(draft_id, at=sent_at, keep_id=keep_id)
    assert (sent.id == draft.id) is keep_id

    drafts.tick(NOW + timedelta(hours=4))
    [message] = drafts.messages(enrollment_id)
    assert (message.status, message.sent_at, message.gmail_message_id) == (
        MessageStatus.SENT,
        sent_at,
        sent.id,
    )
    assert drafts.enrollment(enrollment_id).next_action_at == sent_at + WEEK
    assert drafts.labelled() == [sent.id]


def test_a_draft_sent_keeping_its_id_counts_even_with_gmails_clock_behind(drafts: Mail) -> None:
    """A sent copy that kept the draft's own message id is the draft, whatever its date."""
    enrollment_id = drafts.enroll()
    drafts.tick()
    [draft_id] = drafts.gmail.drafts()
    sent_at = NOW - timedelta(minutes=5)  # Gmail's clock behind netkeeper's
    sent = drafts.gmail.send_draft(draft_id, at=sent_at, keep_id=True)
    drafts.tick(NOW + timedelta(hours=1))
    [message] = drafts.messages(enrollment_id)
    assert (message.status, message.sent_at, message.gmail_message_id) == (
        MessageStatus.SENT,
        sent_at,
        sent.id,
    )


@pytest.mark.parametrize("lag", [2, 5])
def test_gmail_search_lag_never_turns_a_sent_message_into_failed(mail: Mail, lag: int) -> None:
    """#273 review, fix 2: the send went out, its answer was lost, and the search by
    Message-ID finds nothing ``lag`` times (the first right after the send)."""
    enrollment_id = mail.enroll()
    mail.gmail.lose = 1
    mail.gmail.lag = lag
    [(_, outcome)] = mail.tick().fired
    assert outcome.outcome is SendOutcome.UNKNOWN
    at = NOW + LATER
    while mail.messages(enrollment_id)[0].status is MessageStatus.SCHEDULED:
        assert at < NOW + timedelta(days=1), "never settled"
        mail.tick(at)
        at += engine_module.RECONCILE_SEARCH_EVERY
    [message] = mail.messages(enrollment_id)
    [sent] = mail.gmail.sent()
    assert (message.status, message.sent_at, message.gmail_message_id) == (
        MessageStatus.SENT,
        NOW,
        sent.id,
    )
    assert mail.enrollment(enrollment_id).next_action_at == NOW + WEEK
    assert [m for m, _ in mail.gmail.calls].count("messages.send") == 1


def add_mailbox(mail: Mail, modes: Sequence[StepMode]) -> tuple[int, int, LostAnswers]:
    """A second mailbox of the same user with its own campaign and Gmail: the mailbox's
    id, the campaign's, and the Gmail. The sender opens each mailbox's own."""

    def make(session: Session) -> tuple[int, int]:
        user = session.get(User, mail.user.id)
        assert user is not None
        mailbox = make_mailbox(session, user, email="two@example.com")
        campaign = factories.make_campaign(
            session, user, channels=(EMAIL,) * len(modes), mailbox_id=mailbox.id
        )
        for step, mode in zip(campaign.steps, modes, strict=True):
            step.mode = mode
        session.flush()
        return mailbox.id, campaign.id

    mailbox_id, campaign_id = mail.write(make)
    gmail = LostAnswers("two@example.com", mailbox_id=mailbox_id, clock=mail.clock)
    # Ids of its own, as two real mailboxes have: the fakes' counters start alike.
    gmail._ids = itertools.count(0x28F0000000000000)
    gmail._drafts_made = itertools.count(1001)
    boxes = {mail.mailbox.id: mail.gmail, mailbox_id: gmail}
    mail.sender = GmailSender(
        mail.factory,
        opener=lambda user_id, box: boxes[box],
        clock=mail.clock,
        drafts_every=timedelta(0),
    )
    return mailbox_id, campaign_id, gmail


def test_a_mailbox_that_cannot_be_searched_never_stalls_another(mail: Mail) -> None:
    """#273 review, fix 5: 10 old leftovers on a disconnected mailbox 1, and mailbox 2's
    leftover is still searched."""
    _, campaign_id, gmail = add_mailbox(mail, (StepMode.SEND,))
    opener = mail.sender._open

    def first_disconnected(user_id: int, mailbox_id: int) -> Any:
        if mailbox_id == mail.mailbox.id:
            raise MailboxNotReady(mailbox_id, "reauth_required")
        return opener(user_id, mailbox_id)

    mail.sender._open = first_disconnected

    def leftovers(session: Session) -> int:
        made: list[int] = []
        for n, campaign in enumerate(
            [mail.campaign.id] * engine_module.RECONCILE_BATCH + [campaign_id]
        ):
            row = get_scoped(session, mail.user, Campaign, campaign)
            assert row is not None
            contact = factories.make_contact(session, mail.user, emails=[f"l{n}@example.test"])
            enrollment = factories.make_enrollment(session, row, contact, next_action_at=None)
            made.append(
                factories.make_message(
                    session,
                    enrollment,
                    status=MessageStatus.SCHEDULED,
                    sent_at=None,
                    scheduled_at=NOW,
                ).id
            )
        return made[-1]

    theirs = mail.write(leftovers)
    mail.tick(NOW + LATER)
    [message] = [m for m in mail.messages() if m.id == theirs]
    assert message.reconcile_misses == 1  # searched in mailbox 2's Gmail
    assert "messages.list" in [m for m, _ in gmail.calls]


def test_the_drafts_poll_checks_each_draft_against_its_own_mailbox(drafts: Mail) -> None:
    """S21: a draft waiting in mailbox 2 is never looked for in mailbox 1's drafts, nor
    one in mailbox 1 in mailbox 2's."""
    _, campaign_id, gmail = add_mailbox(drafts, (StepMode.DRAFT,))
    theirs = drafts.enroll("bob@example.test", campaign_id=campaign_id)
    ours = drafts.enroll("ada@example.test")
    drafts.tick()
    drafts.tick(NOW + timedelta(hours=1))
    assert (len(gmail.drafts()), len(drafts.gmail.drafts())) == (1, 1)
    drafts.tick(NOW + timedelta(hours=2))
    drafts.tick(NOW + timedelta(hours=3))
    for enrollment_id in (theirs, ours):
        [message] = drafts.messages(enrollment_id)
        assert (message.status, message.error) == (MessageStatus.DRAFTED, None)
        assert drafts.enrollment(enrollment_id).status is not EnrollmentStatus.REMOVED


def test_a_thread_with_no_sent_message_left_fails_the_follow_up(mail: Mail) -> None:
    """S13: the first step is gone from its thread, which only holds the reply. The
    follow-up is ``failed``, never sent as a new conversation, and never retried."""
    enrollment_id = mail.enroll()
    mail.tick()
    [first] = mail.gmail.sent()
    ref = MessageRef(first.id, first.thread_id)
    mail.gmail.reply(ref, sender="ada@example.test", at=NOW + timedelta(days=1))
    mail.gmail.delete(ref)

    [(_, outcome)] = mail.tick(NOW + WEEK).fired
    assert outcome.outcome is SendOutcome.FAILED
    assert outcome.error is not None and "not in its Gmail thread" in outcome.error
    assert mail.gmail.sent() == []
    assert mail.messages(enrollment_id)[1].status is MessageStatus.FAILED


def test_a_follow_up_takes_its_subject_and_first_citation_from_the_earliest_sent(
    mail: Mail,
) -> None:
    """S16 and S9: the person forwarded step 1 in its thread. The follow-up's subject is
    step 1's with one ``Re:``, ``References`` starts at step 1, and ``In-Reply-To`` is
    the newest."""
    mail.enroll()
    mail.tick()
    [first] = mail.gmail.sent()
    ref = MessageRef(first.id, first.thread_id)
    forwarded = person_writes(mail, ref, "Fwd: Hello", NOW + timedelta(days=1))
    mail.tick(NOW + WEEK)
    follow_up = mail.gmail.raw(mail.gmail.sent()[-1].id)
    first_id = mail.gmail.raw(first.id)["Message-ID"]
    forwarded_id = mail.gmail.raw(forwarded.id)["Message-ID"]
    assert follow_up["Subject"] == "Re: Hello"
    assert follow_up["References"].split() == [first_id, forwarded_id]
    assert follow_up["In-Reply-To"] == forwarded_id


def test_the_find_returns_the_earliest_sent_copy() -> None:
    """S9: two sent messages carry the Message-ID (a resend by hand): the first to go out
    is the one the step's time comes from."""
    clock = Clock()
    gmail = FakeGmail("me@example.com", clock=clock)

    def send_at(at: datetime) -> MessageRef:
        message = EmailMessage()
        message["To"] = "ada@example.test"
        message["Subject"] = "Hello"
        message["Message-ID"] = "<same@example.com>"
        message.set_content("Hi")
        clock.now = at
        return gmail.send(message, purpose="test")

    send_at(NOW + timedelta(hours=1))
    earlier = send_at(NOW)
    found = sender_module.find_by_message_id(gmail, "<same@example.com>", purpose="reconcile")
    assert found is not None and found.sent
    assert (found.message.id, found.message.internal_date) == (earlier.id, NOW)


def test_a_leftover_found_as_a_draft_not_listed_yet_waits(drafts: Mail) -> None:
    """S23: the search finds the draft's message, but ``drafts.list`` does not name it yet.
    It stays ``scheduled``, with no miss counted, and is drafted once it is listed."""
    enrollment_id = drafts.enroll()
    with pytest.raises(Crash):
        drafts.tick(sender=CrashAfterSend(drafts.sender))
    drafts.gmail.unlisted = 1
    drafts.tick(NOW + LATER)
    [message] = drafts.messages(enrollment_id)
    assert (message.status, message.reconcile_misses) == (MessageStatus.SCHEDULED, 0)
    drafts.tick(NOW + LATER + timedelta(minutes=1))
    [message] = drafts.messages(enrollment_id)
    [draft_id] = drafts.gmail.drafts()
    assert (message.status, message.gmail_draft_id) == (MessageStatus.DRAFTED, draft_id)


def test_a_lost_draft_answer_with_the_draft_not_listed_yet_is_unknown(drafts: Mail) -> None:
    """S25: the answer to ``drafts.create`` was lost, and the draft is not listed yet: the
    outcome is unknown, never failed, and reconcile drafts it later."""
    enrollment_id = drafts.enroll()
    drafts.gmail.lose = 1
    drafts.gmail.unlisted = 1
    [(_, outcome)] = drafts.tick().fired
    assert outcome.outcome is SendOutcome.UNKNOWN
    assert drafts.messages(enrollment_id)[0].status is MessageStatus.SCHEDULED
    drafts.tick(NOW + LATER)
    [message] = drafts.messages(enrollment_id)
    assert message.status is MessageStatus.DRAFTED
    assert [m for m, _ in drafts.gmail.calls].count("drafts.create") == 1
