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
from test_campaign_sender import LATER, WEEK, Crash, CrashAfterSend, Mail, make_mail

from netkeeper.campaigns.gmail import GmailTransient, Message, MessageRef
from netkeeper.campaigns.gmail_fake import FakeGmail
from netkeeper.config import Settings
from netkeeper.crm import do_not_send
from netkeeper.db import session_scope
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    Contact,
    ContactEmail,
    DoNotSendReason,
    EmailStatus,
    Enrollment,
    EnrollmentStatus,
    Interaction,
    InteractionKind,
    Mailbox,
    MailboxArm,
    MailboxStatus,
    MessageDirection,
    MessageStatus,
    StepMode,
    TemplateChannel,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import campaign_replies as replies
from netkeeper.services import campaigns as campaign_service
from netkeeper.services import mailboxes as mailbox_service
from netkeeper.services.campaign_engine import (
    RECONCILE_SEARCH_EVERY,
    REVIEW_GATE,
    Firing,
    SendOutcome,
    SendResult,
    activate,
)
from netkeeper.services.campaign_sender import GmailSender
from netkeeper.services.mailboxes import MailboxNotReady
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
    assert replies.UNSUBSCRIBE_PHRASES == (
        "unsubscribe",
        "remove me",
        "stop emailing",
        "stop messaging",
    )
    assert frozenset({"mailer-daemon", "postmaster"}) == replies.DAEMON_LOCAL_PARTS
    assert replies.SEARCH_MAX == 20
    assert {s.value for s in replies.LIVE} == {"active", "paused"}
    assert {s.value for s in replies.CAMPAIGN_OVER} == {"completed", "archived"}
    assert replies.POLL_MAX_READS == 50
    assert replies.STALE_AFTER_POLLS == 2
    assert frozenset({"auto_reply", "bulk", "junk"}) == replies.AUTO_REPLY_PRECEDENCE


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


@pytest.mark.parametrize("path", ["history", "scan"])
def test_mail_from_anyone_else_or_from_before_the_first_send_is_not_a_reply(
    mail: Mail, path: str
) -> None:
    """A stranger answering in the watched thread, and the contact's own mail dated before
    the first send (arriving late, after the baseline), reach ``classify`` on both paths,
    and neither is a reply."""
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    mail.gmail.reply(first_sent(mail), sender="carol@example.test", at=NOW + timedelta(hours=1))
    mail.gmail.deliver(fresh_email(ADA, "Old thread", "From last year."), at=NOW - WEEK)
    mail.gmail.deliver(
        fresh_email("bob@example.test", "Hi", "Unrelated."), at=NOW + timedelta(hours=1)
    )
    if path == "scan":
        set_mailbox(mail, history_id=None)
    mail.gmail.calls.clear()

    mail.tick(NOW + timedelta(hours=2))

    polled = {method for method, _ in mail.gmail.calls}
    assert ("threads.get" in polled) is (path == "scan")
    assert ("history.list" in polled) is (path == "history")
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    assert inbound(mail, enrollment_id) == []
    assert interactions(mail, InteractionKind.EMAIL_IN) == []


def test_the_persons_own_mail_from_the_contacts_address_is_not_a_reply(mail: Mail) -> None:
    """Mail labelled ``SENT``, ``DRAFT`` or ``SCHEDULED`` (#278) is the person's, whatever
    its ``From`` says."""
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    for label in ("SENT", "DRAFT", "SCHEDULED"):
        mail.gmail.deliver(
            fresh_email(ADA, f"Note {label}", "Mine."),
            at=NOW + timedelta(hours=1),
            labels=(label, "INBOX"),
        )
    set_mailbox(mail, history_id=None)  # the from: search finds them

    mail.tick(NOW + timedelta(hours=2))

    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    assert inbound(mail, enrollment_id) == []


def test_a_reply_to_a_scheduled_draft_once_delivered_ends_the_enrollment(
    session_factory: sessionmaker[Session],
) -> None:
    """#278 leaves the reply poll (#296) as it was: a draft the person scheduled is
    watched once it is delivered, and the reply to it ends the enrollment."""
    mail = make_mail(session_factory, modes=(StepMode.DRAFT,) * 2, same_thread=(False, True))
    mail.sender = poller(mail)
    enrollment_id = mail.enroll(ADA)
    mail.tick(NOW)
    [draft_id] = mail.gmail.drafts()
    scheduled = mail.gmail.schedule_draft(draft_id, at=NOW + timedelta(minutes=5))
    mail.tick(NOW + timedelta(hours=1))
    mail.tick(NOW + timedelta(hours=2))
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE

    delivered_at = NOW + timedelta(days=1)
    mail.gmail.send_scheduled(scheduled, at=delivered_at)
    mail.tick(delivered_at + timedelta(minutes=1))  # the drafts poll sees it sent
    mail.tick(delivered_at + timedelta(minutes=2))  # the reply poll sets its baseline
    mail.gmail.reply(scheduled, sender=f"Ada <{ADA}>", at=delivered_at + timedelta(hours=1))
    mail.tick(delivered_at + timedelta(hours=2))

    enrollment = mail.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.replied_at) == (
        EnrollmentStatus.REPLIED,
        delivered_at + timedelta(hours=1),
    )
    assert mail.tick(delivered_at + WEEK + timedelta(hours=1)).fired == []
    assert mail.gmail.drafts() == {}


def test_a_draft_armed_mailbox_is_polled_for_replies(
    session_factory: sessionmaker[Session],
) -> None:
    """#327: the reply poll needs an armed mailbox, not a send-armed one. A mailbox armed
    for drafts only is polled like one armed to send, records ``replies_polled_at``, and a
    reply to a draft the person sent ends the enrollment."""
    mail = make_mail(session_factory, modes=(StepMode.DRAFT,) * 2, same_thread=(False, True))
    set_mailbox(mail, send_armed_at=None)
    assert mail.read(lambda s: mailbox_of(mail, s).arm) is MailboxArm.DRAFT
    mail.sender = poller(mail)
    enrollment_id = mail.enroll(ADA)
    mail.tick(NOW)
    [draft_id] = mail.gmail.drafts()
    scheduled = mail.gmail.schedule_draft(draft_id, at=NOW + timedelta(minutes=5))
    delivered_at = NOW + timedelta(days=1)
    mail.gmail.send_scheduled(scheduled, at=delivered_at)
    mail.tick(delivered_at + timedelta(minutes=1))  # the drafts poll sees it sent
    mail.tick(delivered_at + timedelta(minutes=2))  # the reply poll sets its baseline
    assert history_id(mail) is not None
    mail.gmail.reply(scheduled, sender=f"Ada <{ADA}>", at=delivered_at + timedelta(hours=1))
    polled_at = delivered_at + timedelta(hours=2)
    mail.tick(polled_at)

    enrollment = mail.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.replied_at) == (
        EnrollmentStatus.REPLIED,
        delivered_at + timedelta(hours=1),
    )
    assert mail.read(lambda s: mailbox_of(mail, s).replies_polled_at) == polled_at


def _reply_in_the_gap(mail: Mail, to: MessageRef, at: datetime, *, in_thread: bool) -> None:
    if in_thread:
        mail.gmail.reply(to, sender=f"Ada <{ADA}>", at=at)
    else:
        mail.gmail.deliver(fresh_email(ADA, "Quick question", "Saw your note."), at=at)


def _never_followed_up(mail: Mail, enrollment_id: int, after: datetime) -> None:
    enrollment = mail.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.exit_reason) == (EnrollmentStatus.REPLIED, "replied")
    for days in (7, 8, 30):
        assert mail.tick(after + timedelta(days=days)).fired == []
    assert mail.gmail.drafts() == {}
    outbound = [m for m in mail.messages(enrollment_id) if m.direction is MessageDirection.OUT]
    assert len(outbound) == 1


@pytest.mark.parametrize("keep_date", [False, True])
@pytest.mark.parametrize("in_thread", [False, True])
def test_a_reply_between_a_scheduled_delivery_and_the_next_poll_is_recorded(
    session_factory: sessionmaker[Session], in_thread: bool, keep_date: bool
) -> None:
    """#278 re-review: a reply that lands after Gmail delivers a scheduled draft but
    before the drafts poll sees it sent is after ``sent_at``, whether or not Gmail
    re-dated the message, so it ends the enrollment and step 2 never goes out."""
    mail = make_mail(session_factory, modes=(StepMode.DRAFT,) * 2, same_thread=(False, True))
    mail.sender = poller(mail)
    enrollment_id = mail.enroll(ADA)
    mail.tick(NOW)
    [draft_id] = mail.gmail.drafts()
    scheduled = mail.gmail.schedule_draft(draft_id, at=NOW + timedelta(minutes=5))
    delivered_at = NOW + timedelta(days=1)
    mail.tick(NOW + timedelta(hours=1))
    mail.tick(delivered_at - timedelta(minutes=10))  # the last poll that sees it scheduled

    mail.gmail.send_scheduled(scheduled, at=delivered_at, keep_date=keep_date)
    _reply_in_the_gap(mail, scheduled, delivered_at + timedelta(seconds=30), in_thread=in_thread)
    mail.tick(delivered_at + timedelta(minutes=1))

    [sent] = [m for m in mail.messages(enrollment_id) if m.direction is MessageDirection.OUT]
    assert sent.status is MessageStatus.SENT
    assert sent.sent_at is not None and sent.sent_at <= delivered_at
    _never_followed_up(mail, enrollment_id, delivered_at)


@pytest.mark.parametrize("in_thread", [False, True])
def test_a_reply_in_a_scheduled_leftovers_search_gap_is_recorded(
    session_factory: sessionmaker[Session], in_thread: bool
) -> None:
    """#278 re-review: a leftover found in Scheduled is searched only every
    ``RECONCILE_SEARCH_EVERY``. A delivery and a reply inside that gap, with Gmail keeping
    the scheduling date, still date the send before the reply."""
    mail = make_mail(session_factory, modes=(StepMode.DRAFT,) * 2, same_thread=(False, True))
    mail.sender = poller(mail)
    enrollment_id = mail.enroll(ADA)
    with pytest.raises(Crash):
        mail.tick(NOW, sender=CrashAfterSend(mail.sender))
    [draft_id] = mail.gmail.drafts()
    scheduled = mail.gmail.schedule_draft(draft_id)
    first = NOW + LATER
    mail.tick(first)  # found in Scheduled; the next search is RECONCILE_SEARCH_EVERY later

    delivered_at = first + timedelta(minutes=5)
    mail.gmail.send_scheduled(scheduled, at=delivered_at, keep_date=True)
    _reply_in_the_gap(mail, scheduled, delivered_at + timedelta(minutes=1), in_thread=in_thread)
    mail.tick(delivered_at + timedelta(minutes=2))  # inside the gap: not searched
    assert mail.messages(enrollment_id)[0].status is MessageStatus.SCHEDULED
    mail.tick(first + RECONCILE_SEARCH_EVERY)

    [sent] = [m for m in mail.messages(enrollment_id) if m.direction is MessageDirection.OUT]
    assert (sent.status, sent.sent_at) == (MessageStatus.SENT, first)
    _never_followed_up(mail, enrollment_id, delivered_at)


def test_a_completed_enrollment_is_watched_for_thirty_days(
    session_factory: sessionmaker[Session],
) -> None:
    mail = make_mail(session_factory, modes=(StepMode.SEND,), same_thread=(False,))
    mail.sender = poller(mail)
    early, late = mail.enroll(ADA), mail.enroll("grace@example.test")
    mail.tick(NOW)
    mail.tick(NOW + timedelta(minutes=5))  # the spacing: the second goes out now
    mail.tick(NOW + timedelta(minutes=6))  # baseline
    assert (
        mail.enrollment(early).status is mail.enrollment(late).status is EnrollmentStatus.COMPLETED
    )
    to_early, to_late = (MessageRef(m.id, m.thread_id) for m in mail.gmail.sent())

    mail.gmail.reply(to_early, sender=ADA, at=NOW + timedelta(days=29))
    mail.tick(NOW + timedelta(days=29, hours=1))
    mail.gmail.reply(to_late, sender="grace@example.test", at=NOW + timedelta(days=31))
    mail.tick(NOW + timedelta(days=31, hours=1))

    assert len(inbound(mail, early)) == 1  # recorded; the status stays completed
    assert mail.enrollment(early).status is EnrollmentStatus.COMPLETED
    assert mail.enrollment(early).replied_at == NOW + timedelta(days=29)
    assert inbound(mail, late) == []  # past the window: not watched


@pytest.mark.parametrize("archived", [False, True])
def test_an_ended_campaigns_live_enrollments_are_watched_for_thirty_days(
    session_factory: sessionmaker[Session], archived: bool
) -> None:
    """#345: an ended (or archived) campaign sends nothing more, so its enrollments still
    ``active`` are watched like completed ones: a reply inside the window counts, one
    after it is not read."""
    mail = make_mail(session_factory, modes=(StepMode.SEND,) * 2, same_thread=(False, True))
    mail.sender = poller(mail)
    early, late = mail.enroll(ADA), mail.enroll("grace@example.test")
    mail.tick(NOW)
    mail.tick(NOW + timedelta(minutes=5))  # the spacing: the second goes out now
    mail.tick(NOW + timedelta(minutes=6))  # baseline

    def end(session: Session) -> None:
        user = user_of(mail, session)
        campaign_service.end(session, user, mail.campaign.id)
        if archived:
            campaign_service.archive(session, user, mail.campaign.id)

    mail.write(end)
    assert mail.enrollment(early).status is mail.enrollment(late).status is EnrollmentStatus.ACTIVE
    to_early, to_late = (MessageRef(m.id, m.thread_id) for m in mail.gmail.sent())

    in_window = NOW + timedelta(days=29)
    assert {w.enrollment_id for w in watched(mail, in_window)} == {early, late}
    mail.gmail.reply(to_early, sender=ADA, at=in_window)
    mail.tick(in_window + timedelta(hours=1))
    after = NOW + timedelta(days=31)
    assert watched(mail, after) == []
    mail.gmail.reply(to_late, sender="grace@example.test", at=after)
    mail.tick(after + timedelta(hours=1))

    assert len(inbound(mail, early)) == 1
    assert mail.enrollment(early).replied_at == in_window
    assert mail.enrollment(early).status is EnrollmentStatus.REPLIED
    assert inbound(mail, late) == []  # past the window: not watched
    assert mail.enrollment(late).status is EnrollmentStatus.ACTIVE
    assert len(mail.gmail.sent()) == 2  # step 2 never went


@pytest.mark.parametrize("status", [CampaignStatus.ACTIVE, CampaignStatus.PAUSED])
def test_a_running_campaigns_live_enrollment_is_watched_past_thirty_days(
    session_factory: sessionmaker[Session], status: CampaignStatus
) -> None:
    """#345 review: the 30-day bound is for an ended campaign only. An active or paused
    campaign's live enrollment, waiting 45 days for step 2, is still watched on day 35,
    and a reply then ends it, so step 2 never fires."""
    mail = make_mail(session_factory, modes=(StepMode.SEND,) * 2, same_thread=(False, True))
    mail.sender = poller(mail)

    def slow_follow_up(session: Session) -> None:
        campaign = get_scoped(session, mail.user, Campaign, mail.campaign.id)
        assert campaign is not None
        campaign.steps[1].delay_days = 45

    mail.write(slow_follow_up)
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    if status is CampaignStatus.PAUSED:
        mail.write(lambda s: campaign_service.pause(s, user_of(mail, s), mail.campaign.id))
    enrollment = mail.enrollment(enrollment_id)
    assert enrollment.status is EnrollmentStatus.ACTIVE
    assert enrollment.next_action_at is not None
    assert enrollment.next_action_at > NOW + timedelta(days=44)

    for days in (31, 35):
        assert [w.enrollment_id for w in watched(mail, NOW + timedelta(days=days))] == [
            enrollment_id
        ], days
    day_35 = NOW + timedelta(days=35)
    mail.gmail.reply(first_sent(mail), sender=ADA, at=day_35)
    mail.tick(day_35 + timedelta(hours=1))

    enrollment = mail.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.replied_at) == (EnrollmentStatus.REPLIED, day_35)
    if status is CampaignStatus.PAUSED:
        mail.write(lambda s: campaign_service.resume(s, user_of(mail, s), mail.campaign.id))
    assert mail.tick(NOW + timedelta(days=50)).fired == []
    assert len(mail.gmail.sent()) == 1  # step 2 never went


def watched(mail: Mail, now: datetime) -> list[replies.Watch]:
    return mail.read(
        lambda s: [
            w for box in replies.reply_work(s, user_of(mail, s), now=now) for w in box.watches
        ]
    )


# --- bounces ------------------------------------------------------------------------------


def test_a_bounce_marks_the_message_the_address_and_the_enrollment(mail: Mail) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    # Another contact of the user holds the same address: only the enrollment's is marked.
    other = mail.write(lambda s: factories.make_contact(s, mail.user, emails=[ADA]).id)
    mail.gmail.bounce(first_sent(mail), at=NOW + timedelta(minutes=5))

    mail.tick(NOW + timedelta(minutes=30))

    enrollment = mail.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.exit_reason) == (EnrollmentStatus.BOUNCED, "bounced")
    [message] = mail.messages(enrollment_id)  # the notice itself is not stored
    assert message.status is MessageStatus.BOUNCED
    assert message.bounced_at is not None  # the inbox's time for it (#300)
    statuses = {
        (a.contact_id, a.email): a.status
        for a in mail.read(lambda s: list(s.scalars(scoped(mail.user, ContactEmail))))
    }
    assert statuses == {
        (enrollment.contact_id, ADA): EmailStatus.BOUNCED,
        (other, ADA): EmailStatus.OK,
    }
    # The address itself is on the do-not-send list, so the other contact is not mailed (#238).
    listed = mail.read(
        lambda s: [(e.email, e.reason, e.contact_id) for e in do_not_send.entries(s, mail.user)]
    )
    assert listed == [(ADA, DoNotSendReason.BOUNCED, enrollment.contact_id)]
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


@pytest.mark.parametrize(
    ("sender", "subject"),
    [
        (None, None),  # Gmail's own "(Delay)" notice
        ("postmaster@mx.example.net", "Warning: message delayed; still trying"),
        ("MAILER-DAEMON@mx.example.net", "Delivery delayed: failure is not final"),
    ],
)
def test_a_delivery_delayed_notice_is_not_a_bounce(
    mail: Mail, sender: str | None, subject: str | None
) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    if sender is None:
        mail.gmail.bounce(first_sent(mail), at=NOW + timedelta(minutes=5), delayed=True)
    else:
        original = mail.gmail.get_message(first_sent(mail).id, purpose="test")
        notice = fresh_email(sender, subject or "", "Still trying.")
        notice["References"] = original.header("Message-ID") or ""
        mail.gmail.deliver(notice, at=NOW + timedelta(minutes=5))

    mail.tick(NOW + timedelta(minutes=30))

    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    [message] = mail.messages(enrollment_id)
    assert message.status is MessageStatus.SENT
    [address] = mail.read(lambda s: list(s.scalars(scoped(mail.user, ContactEmail))))
    assert address.status is EmailStatus.OK
    # A delay notice in the thread doesn't hold the follow-up either.
    [(_, outcome)] = mail.tick(NOW + WEEK + timedelta(hours=1)).fired
    assert outcome.outcome is SendOutcome.SENT


def _notice(sender: str, subject: str, *, failed: str | None) -> Message:
    headers = [("From", sender), ("Subject", subject)]
    if failed is not None:
        headers.append(("X-Failed-Recipients", failed))
    return Message(
        id="m1",
        thread_id="t1",
        label_ids=frozenset({"INBOX"}),
        history_id=1,
        internal_date=NOW,
        snippet="",
        headers=tuple(headers),
    )


@pytest.mark.parametrize(
    ("subject", "failed", "is_bounce"),
    [
        # A delay notice is never a bounce, even with the failed-recipient header.
        ("Delivery Status Notification (Delay)", "ada@example.com", False),
        ("Warning: message delayed; still trying", "ada@example.com", False),
        # A failure notice with the header is a bounce.
        ("Delivery Status Notification (Failure)", "ada@example.com", True),
        # Without the header, a failure subject alone still is.
        ("Undeliverable: hello", None, True),
        # Delay and failure words together: a bounce only with the header.
        ("Delivery delayed: failure is not final", None, False),
        ("Delivery delayed: failure is not final", "ada@example.com", True),
        # A failure notice quoting an original subject that has a delay word.
        ("Undeliverable: Quick warning about the delay", "ada@example.com", True),
        ("Undeliverable: Quick warning about the delay", None, False),
        # A blank header names no recipient.
        ("Hello", "  ", False),
    ],
)
def test_is_hard_bounce_checks_the_delay_subject_first(
    subject: str, failed: str | None, is_bounce: bool
) -> None:
    notice = _notice("MAILER-DAEMON@mx.example.net", subject, failed=failed)
    assert replies.is_hard_bounce(notice) is is_bounce


def test_a_person_is_never_a_hard_bounce() -> None:
    notice = _notice("ada@example.com", "Undeliverable", failed="ada@example.com")
    assert replies.is_hard_bounce(notice) is False


# --- auto-replies (#296 review: ignored) ---------------------------------------------------


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Auto-Submitted", "auto-replied"),
        ("X-Autoreply", "yes"),
        ("Precedence", "auto_reply"),
        ("Precedence", "bulk"),
    ],
)
def test_an_auto_reply_is_not_a_reply_and_does_not_hold_the_follow_up(
    mail: Mail, header: str, value: str
) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    original = mail.gmail.get_message(first_sent(mail).id, purpose="test")
    ooo = fresh_email(ADA, "Re: Hello", "I am out of the office until Monday.")
    ooo["In-Reply-To"] = ooo["References"] = original.header("Message-ID") or ""
    ooo[header] = value
    ref = mail.gmail.deliver(ooo, at=NOW + timedelta(minutes=5))
    assert ref.thread_id == original.thread_id  # in the campaign thread

    mail.tick(NOW + timedelta(hours=1))
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    assert inbound(mail, enrollment_id) == []

    [(_, outcome)] = mail.tick(NOW + WEEK + timedelta(hours=1)).fired  # the pre-send read
    assert outcome.outcome is SendOutcome.SENT


def test_auto_submitted_no_is_a_person_writing(mail: Mail) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    message = fresh_email(ADA, "Hi back", "Real answer.")
    message["Auto-Submitted"] = "no"
    mail.gmail.deliver(message, at=NOW + timedelta(minutes=5))
    mail.tick(NOW + timedelta(hours=1))
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED


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
    listed = mail.read(
        lambda s: [(e.email, e.reason, e.contact_id) for e in do_not_send.entries(s, mail.user)]
    )
    assert listed == [(ADA, DoNotSendReason.OPTED_OUT, contact.id)]  # #238
    assert len(inbound(mail, enrollment_id)) == 1
    asks = [
        m.asks_unsubscribe
        for m in mail.messages(enrollment_id)
        if m.direction is MessageDirection.IN
    ]
    assert asks == [True]  # the inbox's kind for it (#300)
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
    asks = [
        m.asks_unsubscribe
        for m in mail.messages(enrollment_id)
        if m.direction is MessageDirection.IN
    ]
    assert asks == [False]


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


def test_history_skips_mail_that_never_reaches_the_inbox(mail: Mail) -> None:
    """History is read for ``INBOX`` only: the person's own sent mail costs no read."""
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    mail.gmail.send(fresh_email("me@example.com", "Unrelated", "x"), purpose="test")
    mail.gmail.deliver(fresh_email(ADA, "Archived", "Filtered away."), labels=("Label_9",))
    mail.gmail.calls.clear()

    mail.tick(NOW + timedelta(hours=1))

    assert ("messages.get" in {m for m, _ in mail.gmail.calls}) is False
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE


def test_a_busy_inbox_is_read_a_budget_at_a_time_without_skipping(
    mail: Mail, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(replies, "POLL_MAX_READS", 3)
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    start = history_id(mail)
    for n in range(7):
        mail.gmail.deliver(fresh_email(f"n{n}@example.test", f"News {n}", "x"))
    reply = mail.gmail.reply(first_sent(mail), sender=ADA, at=NOW + timedelta(hours=1))

    mail.gmail.calls.clear()
    mail.tick(NOW + timedelta(hours=2))  # 3 read; the history moves only past those
    assert [m for m, _ in mail.gmail.calls].count("messages.get") == 3
    first = history_id(mail)
    assert start is not None and first is not None and start < first < mail.gmail.history_id
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.ACTIVE
    # A partial poll is no complete one: the last complete poll is still send_first's.
    assert mail.read(lambda s: mailbox_of(mail, s).replies_polled_at) == NOW + timedelta(minutes=1)

    # Not caught up: the next tick polls again at once, not an interval later.
    mail.sender = poller(mail, every=timedelta(days=1))
    mail.sender._replies_polled.clear()
    mail.tick(NOW + timedelta(hours=2, minutes=1))  # sets the interval's clock; 3 more
    mail.tick(NOW + timedelta(hours=2, minutes=2))  # the last 2, with the reply
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    assert [m.gmail_message_id for m in mail.messages(enrollment_id)][-1] == reply.id
    assert mail.read(lambda s: mailbox_of(mail, s).replies_polled_at) == NOW + timedelta(
        hours=2, minutes=2
    )  # caught up only now


def test_a_failed_read_keeps_what_was_read_before_it(mail: Mail) -> None:
    enrollment_id = mail.enroll(ADA)
    send_first(mail)
    start = history_id(mail)
    mail.gmail.reply(first_sent(mail), sender=ADA, at=NOW + timedelta(hours=1))
    mail.gmail.deliver(fresh_email("n@example.test", "News", "x"))

    reads = {"n": 0}
    real = mail.gmail.get_message

    def flaky(message_id: str, *, purpose: str) -> Message:
        reads["n"] += 1
        if reads["n"] == 2:
            raise GmailTransient("down", code="unavailable")
        return real(message_id, purpose=purpose)

    mail.gmail.get_message = flaky  # type: ignore[method-assign]
    mail.tick(NOW + timedelta(hours=2))

    # The reply (read first) is recorded; the history moves to just before the news item.
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    moved = history_id(mail)
    assert start is not None and moved is not None and start < moved < mail.gmail.history_id


# --- the stale-poll hold (#296 review) ---------------------------------------------------------


def test_a_new_conversation_waits_while_replies_are_not_polled(
    session_factory: sessionmaker[Session],
) -> None:
    mail = make_mail(session_factory, modes=(StepMode.SEND,) * 2, same_thread=(False, False))
    mail.sender = poller(mail, every=timedelta(minutes=10))
    enrollment_id = mail.enroll(ADA)
    mail.tick(NOW)
    mail.tick(NOW + timedelta(minutes=10))  # the baseline scan
    polled = mail.read(lambda s: mailbox_of(mail, s).replies_polled_at)
    assert polled == NOW + timedelta(minutes=10)
    due = NOW + WEEK
    mail.gmail.fail_next("history.list", GmailTransient("down", code="unavailable"))

    [(_, outcome)] = mail.tick(due).fired  # the poll failed: last success a week ago
    assert outcome.outcome is SendOutcome.NOT_SENT
    assert outcome.error is not None and "not been polled" in outcome.error
    assert len(mail.gmail.sent()) == 1

    [(_, outcome)] = mail.tick(due + timedelta(minutes=16)).fired  # polled first: goes out
    assert outcome.outcome is SendOutcome.SENT
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.COMPLETED


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
        activate(
            session, user, campaign.id, settings=SETTINGS, now=NOW, starts_at=NOW, gate=REVIEW_GATE
        )
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


# --- a mailbox that needs signing in again (#413) ------------------------------------------------

GRACE = "grace@example.test"
INTERVAL = timedelta(minutes=10)
HELD_STALE = "replies have not been polled recently; nothing was sent"
HELD_CATCH_UP = "replies have not been read since the mailbox was ready again; nothing was sent"


@dataclass
class TwoBoxes:
    """``mail``'s mailbox (healthy) and a second one, ``reauth``, each with a two-step
    campaign whose second step starts a new conversation (so the stale rule holds it)."""

    mail: Mail
    reauth_id: int
    reauth_gmail: FakeGmail
    campaign_id: int
    locked: set[int]
    """Mailboxes whose secrets cannot be read (the Keychain is locked): still ``ok``, so
    the engine claims their steps, but the sender cannot open them."""

    def polls(self, gmail: FakeGmail) -> int:
        return sum(1 for method, purpose in gmail.calls if purpose.startswith("reply poll"))

    def not_ready(self, how: str) -> None:
        """``reauth``: it needs signing in again; ``locked``: its Keychain is locked."""
        if how == "reauth":
            self.needs_sign_in(True)
        else:
            self.locked.add(self.reauth_id)

    def needs_sign_in(self, needed: bool) -> None:
        status = MailboxStatus.REAUTH_REQUIRED if needed else MailboxStatus.OK

        def change(session: Session) -> None:
            mailbox = get_scoped(session, self.mail.user, Mailbox, self.reauth_id)
            assert mailbox is not None
            mailbox.status = status

        self.mail.write(change)

    def set_polled_at(self, at: datetime) -> None:
        def change(session: Session) -> None:
            mailbox = get_scoped(session, self.mail.user, Mailbox, self.reauth_id)
            assert mailbox is not None
            mailbox.replies_polled_at = at

        self.mail.write(change)

    def polled_at(self, mailbox_id: int) -> datetime | None:
        def read(session: Session) -> datetime | None:
            mailbox = get_scoped(session, self.mail.user, Mailbox, mailbox_id)
            assert mailbox is not None
            return mailbox.replies_polled_at

        return self.mail.read(read)


@pytest.fixture
def two(session_factory: sessionmaker[Session]) -> TwoBoxes:
    mail = make_mail(session_factory, modes=(StepMode.SEND,) * 2, same_thread=(False, False))
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, mail.user.id)
        assert user is not None
        mailbox = make_mailbox(
            session, user, email="b@example.com", keychain_ref="gmail/mailbox/2", **ARMED_FOR_SEND
        )
        campaign = factories.make_campaign(
            session, user, channels=(TemplateChannel.EMAIL,) * 2, mailbox_id=mailbox.id
        )
        for step in campaign.steps:
            step.mode = StepMode.SEND
            step.same_thread = False
        reauth_id, campaign_id = mailbox.id, campaign.id
    reauth_gmail = FakeGmail("b@example.com", mailbox_id=reauth_id, clock=mail.clock)
    boxes: dict[int, FakeGmail] = {mail.mailbox.id: mail.gmail, reauth_id: reauth_gmail}
    locked: set[int] = set()

    def open_gmail(user_id: int, mailbox_id: int) -> FakeGmail:
        # What netkeeper.services.mailboxes.open_gmail does first: a mailbox not ``ok``
        # is refused before any secret is read or Gmail is called.
        def status(session: Session) -> MailboxStatus:
            mailbox = get_scoped(session, mail.user, Mailbox, mailbox_id)
            assert mailbox is not None
            return mailbox.status

        found = mail.read(status)
        if found is not MailboxStatus.OK:
            raise MailboxNotReady(mailbox_id, found.value)
        if mailbox_id in locked:
            raise MailboxNotReady(mailbox_id, "keychain_unavailable")
        return boxes[mailbox_id]

    mail.sender = GmailSender(
        session_factory,
        opener=open_gmail,
        clock=mail.clock,
        drafts_every=timedelta(0),
        replies_every=INTERVAL,
    )
    return TwoBoxes(mail, reauth_id, reauth_gmail, campaign_id, locked)


def start_both(two: TwoBoxes) -> tuple[int, int]:
    """Step 1 goes out on each mailbox in the first minutes (sends are spaced apart); the
    poll at NOW + 10 min, the first with something to watch, reads both."""
    mail = two.mail
    healthy = mail.enroll(ADA)
    stuck = mail.enroll(GRACE, campaign_id=two.campaign_id)
    for minute in range(10):
        mail.tick(NOW + timedelta(minutes=minute))
    assert len(mail.gmail.sent()) == len(two.reauth_gmail.sent()) == 1
    mail.tick(NOW + INTERVAL)
    assert two.polled_at(mail.mailbox.id) == two.polled_at(two.reauth_id) == NOW + INTERVAL
    return healthy, stuck


@pytest.mark.parametrize("how", ["reauth", "locked"])
def test_a_mailbox_not_ready_leaves_the_others_on_their_interval(two: TwoBoxes, how: str) -> None:
    """#413: the healthy mailbox is polled once an interval, not at every minute tick, while
    the other is not ready. Needing sign-in, the engine claims nothing on it; with its
    Keychain locked, it is claimed and the sender holds its follow-up (stale replies),
    which asks for a poll of that mailbox alone."""
    mail = two.mail
    healthy, stuck = start_both(two)
    two.not_ready(how)
    mail.gmail.calls.clear()
    two.reauth_gmail.calls.clear()

    for minute in range(11, 41):  # polls at +20, +30, +40 only
        mail.tick(NOW + timedelta(minutes=minute))
    assert two.polls(mail.gmail) == 3
    assert two.reauth_gmail.calls == []
    assert two.polled_at(mail.mailbox.id) == NOW + timedelta(minutes=40)
    assert two.polled_at(two.reauth_id) == NOW + INTERVAL  # never marked caught up

    # Step 2 comes due on both: the healthy one sends; the other's stays held.
    due = NOW + WEEK
    mail.gmail.calls.clear()
    held: list[str | None] = []
    for minute in range(0, 40):
        result = mail.tick(due + timedelta(minutes=minute))
        held += [o.error for f, o in result.fired if f.enrollment_id == stuck]
        if how == "reauth":
            assert result.skipped().get(stuck, ("mailbox_unhealthy",)) == ("mailbox_unhealthy",)
    assert len(mail.gmail.sent()) == 2
    assert mail.enrollment(healthy).status is EnrollmentStatus.COMPLETED
    assert len(two.reauth_gmail.sent()) == 1
    if how == "locked":  # claimed again after each retry wait, and held each time
        assert held
        assert set(held) == {HELD_CATCH_UP}
    else:
        assert held == []
    assert two.reauth_gmail.calls == []
    # A full poll at due, +10, +20 and +30: the held follow-up asks for its mailbox only.
    assert two.polls(mail.gmail) == 4
    assert two.polled_at(two.reauth_id) == NOW + INTERVAL


def test_a_mailbox_is_polled_at_the_next_tick_after_signing_in_again(two: TwoBoxes) -> None:
    """Recovery does not wait for the interval, and does not poll the healthy one with it.
    A reply that came while it needed signing in ends the enrollment before step 2."""
    mail = two.mail
    _, stuck = start_both(two)
    two.needs_sign_in(True)
    due = NOW + WEEK + INTERVAL  # step 2 is due on both
    mail.tick(due)  # the full poll skips it; its step 2 is held
    assert len(two.reauth_gmail.sent()) == 1
    two.reauth_gmail.reply(
        MessageRef(two.reauth_gmail.sent()[0].id, two.reauth_gmail.sent()[0].thread_id),
        sender=GRACE,
        at=due + timedelta(seconds=10),
    )
    two.needs_sign_in(False)
    mail.gmail.calls.clear()

    mail.tick(due + timedelta(minutes=1))

    assert two.polled_at(two.reauth_id) == due + timedelta(minutes=1)
    assert two.polls(mail.gmail) == 0  # the healthy one waits for its interval
    assert mail.enrollment(stuck).status is EnrollmentStatus.REPLIED
    assert len(two.reauth_gmail.sent()) == 1


def test_after_signing_in_again_a_follow_up_waits_for_a_poll_that_catches_up(
    two: TwoBoxes,
) -> None:
    """The safety rule (#296 review) holds across #413's skip: a skipped mailbox keeps its
    old ``replies_polled_at``, so once it is ready again, a failed first poll still holds
    the follow-up that starts a new conversation. Nothing has read its replies."""
    mail = two.mail
    _, stuck = start_both(two)
    two.needs_sign_in(True)
    due = NOW + WEEK + INTERVAL  # step 2 is due on both
    for minute in range(0, 25):  # several full polls skip it
        mail.tick(due - timedelta(minutes=30) + timedelta(minutes=minute))
    two.reauth_gmail.reply(
        MessageRef(two.reauth_gmail.sent()[0].id, two.reauth_gmail.sent()[0].thread_id),
        sender=GRACE,
        at=due - timedelta(minutes=2),
    )
    two.needs_sign_in(False)
    two.reauth_gmail.fail_next("history.list", GmailTransient("down", code="unavailable"))

    [outcome] = [o for f, o in mail.tick(due).fired if f.enrollment_id == stuck]

    assert outcome.outcome is SendOutcome.NOT_SENT
    assert outcome.error == HELD_CATCH_UP
    assert len(two.reauth_gmail.sent()) == 1
    mail.tick(due + timedelta(minutes=1))  # polled again a minute on: the reply ends it
    assert mail.enrollment(stuck).status is EnrollmentStatus.REPLIED
    assert len(two.reauth_gmail.sent()) == 1


def test_signing_in_again_soon_still_waits_for_a_poll_that_catches_up(two: TwoBoxes) -> None:
    """#420 review: the mailbox signs in again within STALE_AFTER_POLLS intervals of its
    last good poll, and the first poll after fails. Its last good poll is recent enough
    for the stale rule, but a reply came while it was not ready: the follow-up that
    starts a new conversation waits for a poll of it that catches up."""
    mail = two.mail
    _, stuck = start_both(two)
    due = NOW + WEEK + INTERVAL  # step 2 is due on both
    mail.tick(due - timedelta(minutes=15))  # a full poll reads both
    assert two.polled_at(two.reauth_id) == due - timedelta(minutes=15)
    two.needs_sign_in(True)
    mail.tick(due - timedelta(minutes=5))  # the next full poll finds it not ready
    two.reauth_gmail.reply(
        MessageRef(two.reauth_gmail.sent()[0].id, two.reauth_gmail.sent()[0].thread_id),
        sender=GRACE,
        at=due - timedelta(minutes=4),
    )
    two.needs_sign_in(False)
    two.reauth_gmail.fail_next("history.list", GmailTransient("down", code="unavailable"))

    [outcome] = [o for f, o in mail.tick(due).fired if f.enrollment_id == stuck]

    # The stale rule alone would let it go: the last good poll is 15 minutes old.
    assert due - two.polled_at(two.reauth_id) <= replies.STALE_AFTER_POLLS * INTERVAL  # type: ignore[operator]
    assert outcome.outcome is SendOutcome.NOT_SENT
    assert outcome.error == HELD_CATCH_UP
    assert len(two.reauth_gmail.sent()) == 1
    mail.tick(due + timedelta(minutes=1))  # the backoff's first retry reads the reply
    assert mail.enrollment(stuck).status is EnrollmentStatus.REPLIED
    assert len(two.reauth_gmail.sent()) == 1


def test_signing_in_again_clears_the_last_poll_so_a_restart_holds_too(
    session_factory: sessionmaker[Session], memory_keyring: object
) -> None:
    """The in-memory hold above is gone after a restart, and a running sender may never see
    the mailbox not ready (it signs in again between two polls). Signing in again clears
    ``replies_polled_at``, so the stale rule holds the follow-up until a poll catches up."""
    mail = make_mail(session_factory, modes=(StepMode.SEND,) * 2, same_thread=(False, False))
    mail.sender = poller(mail, every=INTERVAL)
    enrollment_id = mail.enroll(ADA)
    mail.tick(NOW)
    due = NOW + WEEK
    mail.tick(due - timedelta(minutes=5))  # a good poll, recent enough for the stale rule
    set_mailbox(mail, status=MailboxStatus.REAUTH_REQUIRED, status_reason="invalid_grant")
    mail.gmail.reply(first_sent(mail), sender=ADA, at=due - timedelta(minutes=4))

    def sign_in(session: Session) -> None:
        mailbox_service.connect(
            session, user_of(mail, session), "me@example.com", "rt", daily_cap=80
        )

    mail.write(sign_in)
    assert mail.read(lambda s: mailbox_of(mail, s).replies_polled_at) is None
    mail.sender = poller(mail, every=INTERVAL)  # serve restarted
    mail.gmail.fail_next("history.list", GmailTransient("down", code="unavailable"))

    [(_, outcome)] = mail.tick(due).fired

    assert outcome.outcome is SendOutcome.NOT_SENT
    assert outcome.error == HELD_STALE
    assert len(mail.gmail.sent()) == 1
    mail.tick(due + timedelta(minutes=1))
    assert mail.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED
    assert len(mail.gmail.sent()) == 1


def test_signing_in_while_ok_keeps_the_last_poll(
    session_factory: sessionmaker[Session], memory_keyring: object
) -> None:
    mail = make_mail(session_factory)
    set_mailbox(mail, replies_polled_at=NOW)

    def sign_in(session: Session) -> None:
        mailbox_service.connect(
            session, user_of(mail, session), "me@example.com", "rt", daily_cap=80
        )

    mail.write(sign_in)
    assert mail.read(lambda s: mailbox_of(mail, s).replies_polled_at) == NOW


def test_a_failing_mailbox_backs_off_and_starts_over_once_it_reads(two: TwoBoxes) -> None:
    """A mailbox whose poll keeps failing is retried after 1, 2, 4, ... minutes, never
    longer than the interval, not at every tick; a poll that reads starts it over."""
    mail = two.mail
    start_both(two)
    mail.gmail.calls.clear()
    for _ in range(5):
        two.reauth_gmail.fail_next("history.list", GmailTransient("down", code="unavailable"))
    polled: list[int] = []
    for minute in range(20, 41):  # 21 ticks
        before = two.polls(two.reauth_gmail)
        mail.tick(NOW + timedelta(minutes=minute))
        if two.polls(two.reauth_gmail) > before:
            polled.append(minute)
    # Fails at 20 (the full poll), 21 (+1), 23 (+2), 27 (+4) and 30 (the full poll, which
    # sets the wait to the 10-minute cap); reads at 40.
    assert polled == [20, 21, 23, 27, 30, 40]
    assert two.polled_at(two.reauth_id) == NOW + timedelta(minutes=40)
    assert two.polls(mail.gmail) == 3  # the healthy one: 20, 30, 40

    two.reauth_gmail.fail_next("history.list", GmailTransient("down", code="unavailable"))
    polled.clear()
    for minute in range(41, 53):
        before = two.polls(two.reauth_gmail)
        mail.tick(NOW + timedelta(minutes=minute))
        if two.polls(two.reauth_gmail) > before:
            polled.append(minute)
    assert polled == [50, 51]  # the wait starts over at one minute
    assert two.polled_at(two.reauth_id) == NOW + timedelta(minutes=51)


def test_a_stale_follow_up_asks_for_a_poll_of_its_mailbox_alone(two: TwoBoxes) -> None:
    """A ready mailbox whose last complete poll is old while the user's poll timer is fresh
    (it was just armed, say): its held follow-up asks for a poll of it at the next tick,
    not of the user's other mailboxes."""
    mail = two.mail
    _, other = start_both(two)
    due = NOW + WEEK + INTERVAL  # step 2 is due on both
    mail.tick(due - timedelta(minutes=1))  # the full poll; the next is at due + 9 min
    two.set_polled_at(due - timedelta(days=2))
    held_at: datetime | None = None
    for minute in range(0, 6):  # sends are spaced apart: the other's comes within minutes
        at = due + timedelta(minutes=minute)
        if [o for f, o in mail.tick(at).fired if f.enrollment_id == other]:
            held_at = at
            break
    assert held_at is not None
    assert mail.enrollment(other).status is EnrollmentStatus.ACTIVE
    assert len(two.reauth_gmail.sent()) == 1
    mail.gmail.calls.clear()

    mail.tick(held_at + timedelta(minutes=1))

    assert two.polled_at(two.reauth_id) == held_at + timedelta(minutes=1)
    assert two.polls(mail.gmail) == 0


def test_a_failed_read_backs_off_too(two: TwoBoxes) -> None:
    """A poll that stops at a failed read, not the budget, waits like a failed poll."""
    mail = two.mail
    start_both(two)
    two.reauth_gmail.deliver(fresh_email("n@example.test", "News", "x"))
    for _ in range(3):
        two.reauth_gmail.fail_next("messages.get", GmailTransient("down", code="unavailable"))
    polled: list[int] = []
    for minute in range(20, 30):
        before = two.polls(two.reauth_gmail)
        mail.tick(NOW + timedelta(minutes=minute))
        if two.polls(two.reauth_gmail) > before:
            polled.append(minute)
    assert polled == [20, 21, 23, 27]
    assert two.polled_at(two.reauth_id) == NOW + timedelta(minutes=27)


def test_once_caught_up_after_signing_in_a_follow_up_goes(two: TwoBoxes) -> None:
    """The hold after signing in again ends with the first poll that catches up."""
    mail = two.mail
    _, stuck = start_both(two)
    two.needs_sign_in(True)
    due = NOW + WEEK + INTERVAL  # step 2 is due on both
    for minute in range(0, 5):  # the full poll finds it not ready; its step 2 waits
        mail.tick(due + timedelta(minutes=minute))
    assert len(two.reauth_gmail.sent()) == 1
    two.needs_sign_in(False)

    mail.tick(due + timedelta(minutes=5))  # the poll catches up, then step 2 is claimed

    assert two.polled_at(two.reauth_id) == due + timedelta(minutes=5)
    assert len(two.reauth_gmail.sent()) == 2
    assert mail.enrollment(stuck).status is EnrollmentStatus.COMPLETED


def test_a_mailbox_waiting_out_its_backoff_stays_due(
    two: TwoBoxes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Polling another due mailbox meanwhile leaves the waiting one due."""
    mail, sender = two.mail, two.mail.sender
    assert isinstance(sender, GmailSender)
    healthy, stuck = mail.mailbox.id, two.reauth_id
    script = iter(
        [
            replies.RepliesPolled(failed=frozenset({stuck}), behind=frozenset({healthy})),
            replies.RepliesPolled(failed=frozenset({stuck}), behind=frozenset({healthy})),
            replies.RepliesPolled(caught_up=frozenset({healthy})),
        ]
    )
    asked: list[frozenset[int] | None] = []

    def poll(*args: object, only: frozenset[int] | None, **kwargs: object) -> object:
        asked.append(only)
        return next(script)

    monkeypatch.setattr(replies, "poll_replies", poll)
    for minute in range(3):
        sender._poll_replies(mail.factory, mail.user.id, NOW + timedelta(minutes=minute))

    # The full poll, both a minute later, then the healthy one while the other waits.
    assert asked == [None, frozenset({healthy, stuck}), frozenset({healthy})]
    assert sender.replies_due(mail.user.id) == frozenset({stuck})


def test_the_reply_backoff_starts_at_one_minute() -> None:
    from netkeeper.services import campaign_sender

    assert timedelta(minutes=1) == campaign_sender.REPLY_BACKOFF_FIRST


def test_after_the_keychain_is_unlocked_the_poll_status_stops_saying_so(two: TwoBoxes) -> None:
    """The not-ready reason lasts only until a poll opens the mailbox again."""
    from netkeeper.services import poll_status

    mail = two.mail
    start_both(two)
    sender = mail.sender
    assert isinstance(sender, GmailSender)

    def locked_reason() -> str | None:
        def read(session: Session) -> str | None:
            status = poll_status.poll_status(
                session,
                user_of(mail, session),
                now=mail.clock.now,
                settings=SETTINGS,
                serving=poll_status.Serving(
                    campaign_engine=True,
                    replies_polled_at=sender.replies_polled_at(mail.user.id),
                    replies_every=sender.replies_every,
                    replies_due=sender.replies_due(mail.user.id),
                    replies_not_ready=sender.replies_not_ready(mail.user.id),
                ),
            )
            [row] = [m for m in status.mailboxes if m.mailbox_id == two.reauth_id]
            return row.reason

        return mail.read(read)

    two.not_ready("locked")
    mail.tick(NOW + timedelta(minutes=20))
    assert sender.replies_not_ready(mail.user.id) == {two.reauth_id: "keychain_unavailable"}
    assert "Keychain is locked" in (locked_reason() or "")

    two.locked.clear()
    mail.tick(NOW + timedelta(minutes=21))  # retried alone; it opens and reads

    assert two.polled_at(two.reauth_id) == NOW + timedelta(minutes=21)
    assert sender.replies_not_ready(mail.user.id) == {}
    assert locked_reason() is None


def test_signing_in_again_ends_a_backoff(two: TwoBoxes) -> None:
    """A mailbox backing off after failed polls, then needing sign-in, is polled the minute
    after it is signed in again, not when its old backoff ends."""
    mail = two.mail
    start_both(two)
    for _ in range(4):
        two.reauth_gmail.fail_next("history.list", GmailTransient("down", code="unavailable"))
    polled: list[int] = []
    for minute in range(20, 33):
        if minute == 30:
            two.needs_sign_in(True)
        if minute == 32:
            two.needs_sign_in(False)  # signed in again at 31
        before = two.polls(two.reauth_gmail)
        mail.tick(NOW + timedelta(minutes=minute))
        if two.polls(two.reauth_gmail) > before:
            polled.append(minute)
    # Fails at 20, 21, 23 and 27 (the next try would be 35); not ready at 30 and 31.
    assert polled == [20, 21, 23, 27, 32]
    assert two.polled_at(two.reauth_id) == NOW + timedelta(minutes=32)
