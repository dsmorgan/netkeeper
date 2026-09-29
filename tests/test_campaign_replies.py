"""netkeeper.services.campaign_replies (spec 11.5 "Bounces", 11.7; item P3-08): replies in
and out of the thread, bounces, unsubscribe phrases, history expiry, idempotency, and the
claim-time race.

Every test runs the real engine tick and the Gmail sender against :class:`FakeGmail`.
Nothing here reaches Gmail or Google.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage

import factories
import pytest
from campaign_fakes import ARMED_FOR_SEND, NOW, SETTINGS, make_mailbox
from sqlalchemy.orm import Session, sessionmaker
from test_campaign_sender import WEEK, Mail, make_mail

from netkeeper.campaigns.gmail import GmailTransient, MessageRef
from netkeeper.campaigns.gmail_fake import FakeGmail
from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.models import (
    CampaignStatus,
    Contact,
    ContactEmail,
    EmailStatus,
    Enrollment,
    EnrollmentStatus,
    Interaction,
    InteractionKind,
    Mailbox,
    MailboxArm,
    MessageDirection,
    MessageStatus,
    StepMode,
    TemplateChannel,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import campaign_replies as replies
from netkeeper.services.campaign_engine import (
    REVIEW_GATE,
    Firing,
    SendOutcome,
    SendResult,
    activate,
)
from netkeeper.services.campaign_sender import GmailSender
from netkeeper.services.simulate_campaign import simulate_campaign

ADA = "ada@example.test"


@pytest.fixture
def mail(session_factory: sessionmaker[Session]) -> Mail:
    """Two ``send`` steps a week apart, the second in the first's thread; every tick polls."""
    mail = make_mail(session_factory, modes=(StepMode.SEND,) * 2, same_thread=(False, True))
    mail.sender = poller(mail)
    return mail


def poller(mail: Mail, every: timedelta = timedelta(0)) -> GmailSender:
    return GmailSender(
        mail.factory,
        opener=lambda user_id, mailbox_id: mail.gmail,
        clock=mail.clock,
        drafts_every=timedelta(0),
        replies_every=every,
    )


def send_first(mail: Mail) -> None:
    """Step 1 goes out; the next tick's poll, with a sent message to watch, scans and sets
    the history baseline."""
    [(_, outcome)] = mail.tick(NOW).fired
    assert outcome.outcome is SendOutcome.SENT
    mail.tick(NOW + timedelta(minutes=1))
    assert history_id(mail) is not None


def first_sent(mail: Mail) -> MessageRef:
    sent = mail.gmail.sent()[0]
    return MessageRef(sent.id, sent.thread_id)


def fresh_email(sender: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "me@example.com"
    message["Subject"] = subject
    message.set_content(body)
    return message


def inbound(mail: Mail, enrollment_id: int) -> list[tuple[str | None, str | None, str | None]]:
    return [
        (m.subject, m.snippet, m.body_rendered)
        for m in mail.messages(enrollment_id)
        if m.direction is MessageDirection.IN
    ]


def interactions(mail: Mail, kind: InteractionKind) -> list[Interaction]:
    return mail.read(
        lambda s: list(s.scalars(scoped(mail.user, Interaction).where(Interaction.kind == kind)))
    )


def user_of(mail: Mail, session: Session) -> User:
    user = session.get(User, mail.user.id)
    assert user is not None
    return user


def mailbox_of(mail: Mail, session: Session) -> Mailbox:
    mailbox = get_scoped(session, mail.user, Mailbox, mail.mailbox.id)
    assert mailbox is not None
    return mailbox


def history_id(mail: Mail) -> int | None:
    return mail.read(lambda s: mailbox_of(mail, s).history_id)


def set_mailbox(mail: Mail, **changes: object) -> None:
    def change(session: Session) -> None:
        mailbox = mailbox_of(mail, session)
        for name, value in changes.items():
            setattr(mailbox, name, value)

    mail.write(change)


# --- constants ------------------------------------------------------------------------


def test_the_detection_constants_are_pinned() -> None:
    """Safety constants against numbers written out here (CLAUDE.md)."""
    assert timedelta(minutes=10) == replies.REPLY_POLL_EVERY
    assert timedelta(days=30) == replies.WATCH_AFTER_COMPLETED
    assert replies.UNSUBSCRIBE_PHRASES == ("unsubscribe", "remove me", "stop emailing")
    assert frozenset({"mailer-daemon", "postmaster"}) == replies.DAEMON_LOCAL_PARTS
    assert replies.SEARCH_MAX == 20
    assert {s.value for s in replies.LIVE} == {"active", "paused"}


# --- replies ----------------------------------------------------------------------------


def test_a_reply_in_the_thread_ends_the_enrollment_and_the_follow_up_never_fires(
    mail: Mail,
) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    assert history_id(mail) is not None
    reply = mail.gmail.reply(
        first_sent(mail),
        sender=f"Ada <{ADA}>",
        body="Sounds good, let's talk.",
        at=NOW + timedelta(hours=2),
    )

    result = mail.tick(NOW + timedelta(hours=3))

    assert result.fired == []
    enrollment = mail.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.exit_reason) == (EnrollmentStatus.REPLIED, "replied")
    assert enrollment.replied_at == NOW + timedelta(hours=2)
    [message] = [m for m in mail.messages(enrollment_id) if m.direction is MessageDirection.IN]
    assert message.status is MessageStatus.RECEIVED
    assert (message.gmail_message_id, message.gmail_thread_id) == (reply.id, reply.thread_id)
    assert inbound(mail, enrollment_id) == [
        ("Re: Hello", "Sounds good, let's talk.", None)  # the snippet, never the body
    ]
    [interaction] = interactions(mail, InteractionKind.EMAIL_IN)
    assert (interaction.message_id, interaction.at) == (message.id, NOW + timedelta(hours=2))
    # The reply carries the campaign's label (spec 11.5).
    [label] = [lb for lb in mail.gmail.list_labels(purpose="test") if lb.name == mail.label]
    assert label.id in mail.gmail.get_message(reply.id, purpose="test").label_ids

    assert mail.tick(NOW + WEEK + timedelta(hours=1)).fired == []
    assert len(mail.gmail.sent()) == 1


def test_a_reply_from_the_contacts_address_outside_the_thread_counts(mail: Mail) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    mail.gmail.deliver(
        fresh_email(ADA, "Coffee next week?", "Saw your note. Free Tuesday?"),
        at=NOW + timedelta(days=1),
    )

    mail.tick(NOW + timedelta(days=1, hours=1))

    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    assert inbound(mail, enrollment_id) == [
        ("Coffee next week?", "Saw your note. Free Tuesday?", None)
    ]
    assert mail.tick(NOW + WEEK + timedelta(hours=1)).fired == []


def test_mail_from_anyone_else_or_from_before_the_first_send_is_not_a_reply(mail: Mail) -> None:
    mail.gmail.deliver(fresh_email(ADA, "Old thread", "From last year."), at=NOW - WEEK)
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    mail.gmail.deliver(
        fresh_email("bob@example.test", "Hi", "Unrelated."), at=NOW + timedelta(hours=1)
    )
    set_mailbox(mail, history_id=None)  # the scan reads the thread and searches from: too

    mail.tick(NOW + timedelta(hours=2))

    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    assert inbound(mail, enrollment_id) == []
    [(_, outcome)] = mail.tick(NOW + WEEK + timedelta(hours=1)).fired
    assert outcome.outcome is SendOutcome.SENT


# --- bounces ------------------------------------------------------------------------------


def test_a_bounce_marks_the_message_the_address_and_the_enrollment(mail: Mail) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    mail.gmail.bounce(first_sent(mail), at=NOW + timedelta(minutes=5))

    mail.tick(NOW + timedelta(minutes=30))

    enrollment = mail.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.exit_reason) == (EnrollmentStatus.BOUNCED, "bounced")
    [message] = mail.messages(enrollment_id)  # the notice itself is not stored
    assert message.status is MessageStatus.BOUNCED
    [address] = mail.read(lambda s: list(s.scalars(scoped(mail.user, ContactEmail))))
    assert (address.email, address.status) == (ADA, EmailStatus.BOUNCED)
    assert interactions(mail, InteractionKind.EMAIL_IN) == []
    assert mail.tick(NOW + WEEK + timedelta(hours=1)).fired == []


def test_a_notice_citing_the_message_id_outside_the_thread_is_a_bounce(mail: Mail) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    original = mail.gmail.get_message(first_sent(mail).id, purpose="test")
    notice = fresh_email("MAILER-DAEMON@mx.example.net", "Undeliverable", "No such user.")
    notice["References"] = original.header("Message-ID") or ""
    ref = mail.gmail.deliver(notice, at=NOW + timedelta(minutes=5))
    assert ref.thread_id != original.thread_id  # a different subject: its own thread

    mail.tick(NOW + timedelta(minutes=30))

    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.BOUNCED


# --- unsubscribe ----------------------------------------------------------------------------


def test_an_unsubscribe_phrase_opts_out_and_sets_do_not_contact(mail: Mail) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    mail.gmail.reply(
        first_sent(mail),
        sender=ADA,
        body="Please remove me from your list.",
        at=NOW + timedelta(hours=1),
    )

    mail.tick(NOW + timedelta(hours=2))

    enrollment = mail.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.exit_reason) == (
        EnrollmentStatus.OPTED_OUT,
        replies.UNSUBSCRIBE_REASON,
    )
    contact = mail.read(lambda s: get_scoped(s, mail.user, Contact, enrollment.contact_id))
    assert contact is not None and contact.do_not_contact
    assert contact.do_not_contact_reason is not None
    assert "unsubscribe" in contact.do_not_contact_reason
    assert len(inbound(mail, enrollment_id)) == 1
    assert mail.tick(NOW + WEEK + timedelta(hours=1)).fired == []


def test_a_word_that_only_contains_a_phrase_is_not_an_unsubscribe(mail: Mail) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    mail.gmail.reply(
        first_sent(mail),
        sender=ADA,
        body="Glad to reconnect; unsubscribed from nothing.",
        at=NOW + timedelta(hours=1),
    )
    mail.tick(NOW + timedelta(hours=2))
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED


# --- history expiry and idempotency ----------------------------------------------------------


def test_expired_history_falls_back_to_a_threads_scan_and_rebaselines(mail: Mail) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    first = history_id(mail)
    reply = mail.gmail.reply(first_sent(mail), sender=ADA, at=NOW + timedelta(hours=1))
    mail.gmail.forget_history()  # Gmail drops history: a start before now is a 404

    mail.tick(NOW + timedelta(hours=2))

    assert ("history.list", f"reply poll of mailbox {mail.mailbox.id}") in mail.gmail.calls
    assert ("threads.get", f"reply poll of mailbox {mail.mailbox.id}") in mail.gmail.calls
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    assert [
        m.gmail_message_id
        for m in mail.messages(enrollment_id)
        if m.direction is MessageDirection.IN
    ] == [reply.id]
    rebased = history_id(mail)
    assert first is not None and rebased is not None and rebased > first
    mail.gmail.calls.clear()
    mail.tick(NOW + timedelta(hours=3))  # the next poll reads history from the new baseline
    assert ("threads.get", f"reply poll of mailbox {mail.mailbox.id}") not in mail.gmail.calls


def test_polls_are_idempotent(mail: Mail) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    mail.gmail.reply(first_sent(mail), sender=ADA, at=NOW + timedelta(hours=1))
    mail.tick(NOW + timedelta(hours=2))
    for hours in (3, 4):  # forced rescans read the same reply again
        set_mailbox(mail, history_id=None)
        mail.tick(NOW + timedelta(hours=hours))

    assert len(inbound(mail, enrollment_id)) == 1
    assert len(interactions(mail, InteractionKind.EMAIL_IN)) == 1


def test_a_gmail_failure_leaves_the_history_where_it_was(mail: Mail) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    before = history_id(mail)
    mail.gmail.reply(first_sent(mail), sender=ADA, at=NOW + timedelta(hours=1))
    mail.gmail.fail_next("messages.get", GmailTransient("down", code="unavailable"))

    mail.tick(NOW + timedelta(hours=2))
    assert history_id(mail) == before
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE

    mail.tick(NOW + timedelta(hours=3))
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED


# --- arming and users --------------------------------------------------------------------------


def test_a_disarmed_mailbox_is_not_polled(mail: Mail) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    mail.gmail.reply(first_sent(mail), sender=ADA, at=NOW + timedelta(hours=1))
    set_mailbox(mail, armed_at=None, send_armed_at=None)
    mail.gmail.calls.clear()

    mail.tick(NOW + timedelta(hours=2))

    assert mail.gmail.calls == []
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE


def test_one_users_reply_changes_nothing_for_another(
    session_factory: sessionmaker[Session],
) -> None:
    ours = make_mail(session_factory, modes=(StepMode.SEND,) * 2, same_thread=(False, True))
    theirs = make_mail(session_factory, modes=(StepMode.SEND,) * 2, same_thread=(False, True))
    theirs.clock = ours.clock
    boxes = {ours.user.id: ours.gmail, theirs.user.id: theirs.gmail}
    ours.sender = theirs.sender = GmailSender(
        session_factory,
        opener=lambda user_id, mailbox_id: boxes[user_id],
        clock=ours.clock,
        drafts_every=timedelta(0),
        replies_every=timedelta(0),
    )
    mine, other = ours.enroll(ADA), theirs.enroll(ADA)  # the same address, two users
    ours.tick(NOW)  # one tick for both users
    ours.tick(NOW + timedelta(minutes=1))
    assert len(ours.gmail.sent()) == len(theirs.gmail.sent()) == 1
    ours.gmail.reply(first_sent(ours), sender=ADA, at=NOW + timedelta(hours=1))

    ours.tick(NOW + timedelta(hours=2))

    assert ours.enrollment(mine).status is EnrollmentStatus.REPLIED
    assert theirs.enrollment(other).status is EnrollmentStatus.ACTIVE
    assert inbound(theirs, other) == []
    assert interactions(theirs, InteractionKind.EMAIL_IN) == []


# --- the claim-time race -------------------------------------------------------------------------


def test_a_reply_after_the_poll_and_before_the_send_stops_the_follow_up(mail: Mail) -> None:
    """The poll ran, a reply lands, the follow-up is claimed: the sender's read of the thread
    sees the reply and sends nothing, and the next tick's poll ends the enrollment."""
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    due = NOW + WEEK
    mail.sender = poller(mail, every=timedelta(days=30))  # no poll comes on its own
    mail.tick(due - timedelta(minutes=1))  # this tick polls; nothing is due yet
    mail.gmail.reply(first_sent(mail), sender=ADA, at=due - timedelta(seconds=30))

    [(_, outcome)] = mail.tick(due).fired
    assert outcome.outcome is SendOutcome.NOT_SENT
    assert len(mail.gmail.sent()) == 1

    # The refusal asked for a poll at once: the retry's claim finds the reply and ends it.
    assert mail.tick(due + timedelta(minutes=16)).fired == []
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    assert len(mail.gmail.sent()) == 1


def test_a_reply_recorded_before_the_claim_ends_it_at_the_claim(mail: Mail) -> None:
    """The claim re-reads the enrollment's inbound messages in its writer session."""
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    reply = replies.Reply(
        enrollment_id,
        mail.gmail.get_message(
            mail.gmail.reply(first_sent(mail), sender=ADA, at=NOW + timedelta(days=1)).id,
            purpose="test",
        ),
        unsubscribe=False,
        label=mail.label,
    )
    # Recorded while the enrollment is paused (no end), then resumed: only the claim's
    # own check stands between the reply and the follow-up.
    mail.set_enrollment(enrollment_id, status=EnrollmentStatus.PAUSED)
    mail.write(lambda s: replies.record_reply(s, user_of(mail, s), reply))
    mail.set_enrollment(enrollment_id, status=EnrollmentStatus.ACTIVE, next_action_at=NOW + WEEK)
    mail.sender = poller(mail, every=timedelta(days=30))

    assert mail.tick(NOW + WEEK + timedelta(hours=1)).fired == []
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    assert len(mail.gmail.sent()) == 1


# --- the "done when": simulate ------------------------------------------------------------------


@dataclass
class ReplyingSender:
    """A Gmail sender over the fake, where ``replier`` answers step 1 a day after it went
    out. The virtual time is the reconcile's ``now``, which comes first in each tick."""

    inner: GmailSender
    gmail: FakeGmail
    clock: list[datetime]
    replier: str
    replied: bool = False
    fired: list[int] = field(default_factory=list)

    def send(self, firing: Firing) -> SendResult:
        self.fired.append(firing.enrollment_id)
        return self.inner.send(firing)

    def armed(self, session: Session, user: User, mailbox_id: int) -> MailboxArm | None:
        return self.inner.armed(session, user, mailbox_id)

    def reconcile(
        self,
        factory: sessionmaker[Session],
        user_id: int,
        *,
        settings: Settings,
        now: datetime,
    ) -> None:
        self.clock[0] = now
        first = next((m for m in self.gmail.sent() if m.header("To") == self.replier), None)
        if (
            first is not None
            and not self.replied
            and now >= first.internal_date + timedelta(days=1)
        ):
            self.gmail.reply(
                MessageRef(first.id, first.thread_id),
                sender=self.replier,
                at=first.internal_date + timedelta(days=1),
            )
            self.replied = True
        self.inner.reconcile(factory, user_id, settings=settings, now=now)


def test_in_simulate_a_reply_ends_the_enrollment_before_the_follow_up_fires(
    session_factory: sessionmaker[Session],
) -> None:
    """P3-08's "done when": the replier gets step 1 only; the other contact gets both."""
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        mailbox = make_mailbox(session, user, email="me@example.com", **ARMED_FOR_SEND)
        campaign = factories.make_campaign(
            session,
            user,
            channels=(TemplateChannel.EMAIL,) * 2,
            mailbox_id=mailbox.id,
            status=CampaignStatus.REVIEWING,
            approved_at=NOW,
        )
        for step in campaign.steps:
            step.mode = StepMode.SEND
        campaign.steps[1].same_thread = True
        enrolled: dict[str, int] = {}
        for address in (ADA, "grace@example.test"):
            contact = factories.make_contact(session, user, emails=[address])
            enrolled[address] = factories.make_enrollment(
                session, campaign, contact, status=EnrollmentStatus.PENDING
            ).id
        activate(session, user, campaign.id, settings=SETTINGS, now=NOW, gate=REVIEW_GATE)
        user_id, mailbox_id = user.id, mailbox.id
    clock = [NOW]
    gmail = FakeGmail("me@example.com", mailbox_id=mailbox_id, clock=lambda: clock[0])
    sender = ReplyingSender(
        GmailSender(
            session_factory,
            opener=lambda _user, _mailbox: gmail,
            clock=lambda: clock[0],
            drafts_every=timedelta(0),
            replies_every=replies.REPLY_POLL_EVERY,
        ),
        gmail,
        clock,
        ADA,
    )

    simulate_campaign(
        session_factory, settings=SETTINGS, start=NOW, end=NOW + timedelta(weeks=3), sender=sender
    )

    assert sender.replied
    assert sorted(sender.fired) == sorted([enrolled[ADA], *[enrolled["grace@example.test"]] * 2])
    assert [m.header("To") for m in gmail.sent()].count(ADA) == 1
    with session_scope(session_factory) as session:
        owner = session.get(User, user_id)
        assert owner is not None
        statuses = {e.id: e.status for e in session.scalars(scoped(owner, Enrollment))}
    assert statuses[enrolled[ADA]] is EnrollmentStatus.REPLIED
    assert statuses[enrolled["grace@example.test"]] is EnrollmentStatus.COMPLETED
