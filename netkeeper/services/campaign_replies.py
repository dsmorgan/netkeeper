"""Reply and bounce detection for Gmail (spec 11.5 "Bounces", 11.7; item P3-08).

:func:`poll_replies` runs in the campaign engine's tick thread, from
:meth:`netkeeper.services.campaign_sender.GmailSender.reconcile`, before anything
is chosen, every ``[campaigns] reply_poll_minutes`` per user. So it is off the
event loop (#259), and the tick's claim that follows sees what it recorded.

What it watches
---------------
Each armed mailbox's **watches** (:func:`reply_work`): enrollments of a campaign on
that mailbox with at least one sent email, that are ``active`` or ``paused``, or
``completed`` with their latest send under :data:`WATCH_AFTER_COMPLETED` ago (a
reply to the last step still belongs in the inbox). An ``active`` or ``paused``
enrollment of an ended (``completed`` or ``archived``) campaign is watched like a
completed one, for the same window after its latest send (#345). A disarmed mailbox gets no
Gmail call, as in reconcile (#277).

How it reads Gmail
------------------
- **History.** ``history.list`` from the mailbox's stored ``history_id``, for
  ``INBOX``; every message added there since is read (metadata only), whatever
  thread it is in. That one read catches a reply in the thread and one from the
  contact's address in a fresh email alike, at the cost of one ``messages.get``
  per message received, at most :data:`POLL_MAX_READS` a poll. A poll that stops
  short (the budget, a failed read) moves ``history_id`` only to just before the
  first message it did not read, and the next tick polls again.
- **Scan.** With no ``history_id`` yet, or one Gmail no longer keeps (a 404),
  the poll takes ``profile().history_id`` first, reads every watched thread, and
  searches ``from:<address> after:<first send>`` for each watched address (spec
  11.7, step 3), then stores that id as the new baseline. Taking it first means
  nothing that arrives during the scan is missed: the next history read covers it.
- A failure of the history call or the scan leaves ``history_id`` as it was.
  Recording is idempotent. ``replies_polled_at`` is set only by a poll that read
  everything up to then; the sender holds a new conversation while it is old.

What a message means
--------------------
A message netkeeper or the person sent (``SENT``, ``DRAFT``, or ``SCHEDULED``, #278)
is skipped.

- **Bounce**: a hard-failure notice (:func:`is_hard_bounce`; a delay notice is
  nothing) from a mailer daemon (``mailer-daemon@`` or ``postmaster@``), in a
  watched thread or citing one of its sent messages' Message-IDs in
  ``In-Reply-To`` or ``References``. The cited message (or the newest sent one in
  the thread) is ``bounced``, so is the address it went to (read from Gmail's
  copy's ``To``), and a live enrollment becomes ``bounced``. The notice is not
  stored.
- **Reply**: from one of the contact's addresses, after the enrollment's first
  send, in its thread or not. Stored as an inbound message (subject, snippet,
  thread; never the body) with an ``email_in`` interaction, labelled with the
  campaign's label, and a live enrollment becomes ``replied``. The engine's claim
  reads that inbound message in its writer session, so no later step fires.
- **Unsubscribe**: a reply whose subject or snippet holds one of
  :data:`UNSUBSCRIBE_PHRASES`. The contact is set ``do_not_contact`` with a reason
  (undone from the contact page), and a live enrollment becomes ``opted_out``.

An automatic answer (:func:`is_auto_reply`: out of office and the like) is ignored
(#296 review): it is no answer, and the sender's pre-send thread read ignores it too.

netkeeper deletes nothing in Gmail (ADR 0003): this module reads, and adds the
campaign label to a reply.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parseaddr
from typing import Final

from sqlalchemy import func
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.compose import campaign_label
from netkeeper.campaigns.gmail import Gmail, GmailError, GmailNotFound, History, Message
from netkeeper.crm import do_not_send
from netkeeper.crm.interactions import add_interaction
from netkeeper.db import session_scope
from netkeeper.models import (
    MESSAGE_SNIPPET_MAX_LENGTH,
    MESSAGE_SUBJECT_MAX_LENGTH,
    Campaign,
    CampaignStatus,
    Contact,
    ContactEmail,
    ContactSource,
    DoNotSendReason,
    EmailStatus,
    Enrollment,
    EnrollmentStatus,
    InteractionKind,
    Mailbox,
    MessageDirection,
    MessageStatus,
    TemplateChannel,
    User,
)
from netkeeper.models import Message as MessageRow
from netkeeper.models.base import utcnow
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import campaign_engine as engine

log = logging.getLogger(__name__)

REPLY_POLL_EVERY: Final = timedelta(minutes=10)
"""Spec 11.7's poll interval; ``serve`` passes ``[campaigns] reply_poll_minutes``."""

WATCH_AFTER_COMPLETED: Final = timedelta(days=30)
"""A completed enrollment is watched this long after its latest send, for a late reply."""

UNSUBSCRIBE_PHRASES: Final = ("unsubscribe", "remove me", "stop emailing")
"""Spec 11.7's phrases. Matched as whole words, without case, in a reply's subject and
snippet."""

UNSUBSCRIBE_REASON: Final = "unsubscribe"
"""The enrollment's ``exit_reason`` when a reply asks to unsubscribe."""

DAEMON_LOCAL_PARTS: Final = frozenset({"mailer-daemon", "postmaster"})
"""The senders of delivery-status notices (spec 11.5: ``mailer-daemon@googlemail.com``)."""

SEARCH_MAX: Final = 20
"""At most this many messages one ``from:`` search of the scan reads."""

POLL_MAX_READS: Final = 50
"""At most this many ``messages.get`` one history read makes per mailbox and poll. The
rest wait for the next tick's poll, which comes at once (the sender asks for it)."""

STALE_AFTER_POLLS: Final = 2
"""A mailbox whose last complete poll is older than this many poll intervals holds its
follow-ups that start a new conversation (:meth:`GmailSender.send`): nothing reads the
thread for them before they go, so the poll is all that would see a reply."""

AUTO_REPLY_PRECEDENCE: Final = frozenset({"auto_reply", "bulk", "junk"})
"""``Precedence`` values that mark a message as automatic."""

CAMPAIGN_OVER: Final = frozenset({CampaignStatus.COMPLETED, CampaignStatus.ARCHIVED})
"""An ended campaign (#345), archived or not: its live enrollments send nothing more, so
they are watched only :data:`WATCH_AFTER_COMPLETED` after their latest send."""

LIVE: Final = frozenset({EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED})
"""What detection moves to ``replied``, ``bounced`` or ``opted_out`` (spec 11.3)."""

_UNSUBSCRIBE: Final = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in UNSUBSCRIBE_PHRASES) + r")\b", re.IGNORECASE
)
_MSGID: Final = re.compile(r"<[^<>\s]+>")
_SEARCHABLE: Final = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+")
_OWN: Final = frozenset({"SENT", "DRAFT", "SCHEDULED"})
_INBOX: Final = "INBOX"
_FAILURE: Final = re.compile(
    r"\b(?:failure|failed|undeliverable|undelivered|not delivered|returned|rejected)\b",
    re.IGNORECASE,
)
_DELAY: Final = re.compile(r"\b(?:delay|delayed|warning|still trying|temporar\w*)\b", re.IGNORECASE)


def sender_of(message: Message) -> str:
    """The ``From`` address, lower-cased; empty when there is none."""
    return parseaddr(message.header("From") or "")[1].lower()


def is_auto_reply(message: Message) -> bool:
    """Whether the message says it was sent automatically (RFC 3834 ``Auto-Submitted``
    other than ``no``, ``X-Autoreply``, or ``Precedence: auto_reply``, ``bulk`` or ``junk``):
    an out-of-office answer is not a reply."""
    auto = (message.header("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return True
    if message.header("X-Autoreply") is not None:
        return True
    return (message.header("Precedence") or "").strip().lower() in AUTO_REPLY_PRECEDENCE


def asks_to_unsubscribe(message: Message) -> bool:
    """Whether the message's subject or snippet holds one of :data:`UNSUBSCRIBE_PHRASES`,
    as a whole word, without case. The one test for an unsubscribe request: the reply
    poll and the history scan (#65) both use it."""
    return bool(_UNSUBSCRIBE.search(f"{message.header('Subject') or ''} {message.snippet}"))


def is_daemon(message: Message) -> bool:
    """Whether a mail system sent it: ``mailer-daemon@`` or ``postmaster@``."""
    return sender_of(message).partition("@")[0] in DAEMON_LOCAL_PARTS


def is_hard_bounce(message: Message) -> bool:
    """A mail system's notice that delivery **failed**, not that it is delayed.

    1. A delay word in the subject with no failure word is a delay notice, never a bounce,
       even when it carries ``X-Failed-Recipients`` (the mail system is still retrying).
    2. Otherwise a named failed recipient (``X-Failed-Recipients``) is a bounce. This
       covers a failure notice whose subject quotes the original, such as
       ``Undeliverable: Quick warning about the delay``.
    3. Otherwise a failure word with no delay word is a bounce.

    A subject with both kinds of word is therefore a bounce only with the header: a
    missed bounce costs one more send that Gmail rejects again, while a false bounce ends
    a sequence wrongly. Only the metadata is read, never the body's DSN part."""
    if not is_daemon(message):
        return False
    subject = message.header("Subject") or ""
    delayed = bool(_DELAY.search(subject))
    failed = bool(_FAILURE.search(subject))
    if delayed and not failed:
        return False
    if (message.header("X-Failed-Recipients") or "").strip():
        return True
    return failed and not delayed


# --- what is watched --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sent:
    """One sent outbound message of a watch."""

    row_id: int
    gmail_message_id: str
    thread_id: str
    sent_at: datetime


@dataclass(frozen=True, slots=True)
class Watch:
    enrollment_id: int
    addresses: frozenset[str]
    first_sent_at: datetime
    sent: tuple[Sent, ...]  # oldest first
    label: str

    @property
    def threads(self) -> frozenset[str]:
        return frozenset(s.thread_id for s in self.sent)


@dataclass(frozen=True, slots=True)
class MailboxWatch:
    """An armed mailbox and what it watches; ``watches`` may be empty (nothing to read)."""

    mailbox_id: int
    history_id: int | None
    watches: tuple[Watch, ...]


def reply_work(session: Session, user: User, *, now: datetime) -> list[MailboxWatch]:
    """Each armed mailbox of ``user``, with what it watches (the module docstring)."""
    mailboxes = {m.id: m for m in session.scalars(scoped(user, Mailbox)) if m.arm is not None}
    if not mailboxes:
        return []
    enrollments = list(
        session.scalars(
            scoped(user, Enrollment).where(
                Enrollment.status.in_((*LIVE, EnrollmentStatus.COMPLETED))
            )
        )
    )
    campaigns = {
        c.id: c
        for c in session.scalars(
            scoped(user, Campaign).where(Campaign.id.in_({e.campaign_id for e in enrollments}))
        )
    }
    sent: dict[int, list[Sent]] = {}
    for row in session.scalars(
        scoped(user, MessageRow)
        .where(
            MessageRow.enrollment_id.in_({e.id for e in enrollments}),
            MessageRow.channel == TemplateChannel.EMAIL,
            MessageRow.direction == MessageDirection.OUT,
            MessageRow.status.in_((MessageStatus.SENT, MessageStatus.BOUNCED)),
            MessageRow.gmail_message_id.is_not(None),
            MessageRow.gmail_thread_id.is_not(None),
            MessageRow.sent_at.is_not(None),
        )
        .order_by(MessageRow.sent_at, MessageRow.id)
    ):
        assert row.gmail_message_id and row.gmail_thread_id and row.sent_at
        sent.setdefault(row.enrollment_id, []).append(
            Sent(row.id, row.gmail_message_id, row.gmail_thread_id, row.sent_at)
        )
    addresses: dict[int, set[str]] = {}
    for contact_id, address in session.execute(
        scoped(user, ContactEmail)
        .with_only_columns(ContactEmail.contact_id, ContactEmail.email)
        .where(ContactEmail.contact_id.in_({e.contact_id for e in enrollments}))
    ):
        addresses.setdefault(contact_id, set()).add(address.lower())
    by_mailbox: dict[int, list[Watch]] = {mailbox_id: [] for mailbox_id in mailboxes}
    for enrollment in enrollments:
        campaign = campaigns[enrollment.campaign_id]
        mailbox = mailboxes.get(campaign.mailbox_id or 0)
        messages = sent.get(enrollment.id)
        if mailbox is None or not messages:
            continue
        # A live enrollment of an ended or archived campaign (#345) sends nothing more,
        # so it is watched like a completed enrollment: for a while after its last send.
        finished = enrollment.status is EnrollmentStatus.COMPLETED or (
            campaign.status in CAMPAIGN_OVER
        )
        if finished and now - messages[-1].sent_at > WATCH_AFTER_COMPLETED:
            continue
        by_mailbox.setdefault(mailbox.id, []).append(
            Watch(
                enrollment_id=enrollment.id,
                addresses=frozenset(addresses.get(enrollment.contact_id, ())),
                first_sent_at=messages[0].sent_at,
                sent=tuple(messages),
                label=campaign_label(mailbox.label_prefix, campaign.name),
            )
        )
    return [
        MailboxWatch(mailbox_id, mailboxes[mailbox_id].history_id, tuple(watches))
        for mailbox_id, watches in sorted(by_mailbox.items())
    ]


# --- reading Gmail -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reply:
    enrollment_id: int
    message: Message
    unsubscribe: bool
    label: str


@dataclass(frozen=True, slots=True)
class Bounce:
    enrollment_id: int
    notice_id: str
    bounced_row_id: int
    address: str | None
    at: datetime


@dataclass(frozen=True, slots=True)
class PollResult:
    """What one mailbox's poll found; ``history_id`` to store (None: leave it), and
    whether it read everything up to now (``caught_up``)."""

    found: list[Reply | Bounce]
    history_id: int | None
    caught_up: bool


@dataclass
class _Poll:
    gmail: Gmail
    work: MailboxWatch
    purpose: str
    seen: set[str] = field(default_factory=set)
    _cited: dict[str, tuple[Watch, Sent, str | None]] | None = None

    def run(self) -> PollResult:
        """Read what the mailbox received since its ``history_id`` (the module)."""
        own = {s.gmail_message_id for w in self.work.watches for s in w.sent}
        self.seen |= own
        start = self.work.history_id
        if start is not None:
            try:
                # Replies and notices arrive in the inbox; the person's own sent mail,
                # drafts and archived newsletters never cost a read.
                history = self.gmail.history(start, label_id=_INBOX, purpose=self.purpose)
            except GmailNotFound:
                log.info("mailbox %d: Gmail's history expired; scanning", self.work.mailbox_id)
            else:
                return self.history(start, history)
        baseline = self.gmail.profile(purpose=self.purpose).history_id
        return PollResult(self.classify(self.scan()), baseline, caught_up=True)

    def history(self, start: int, history: History) -> PollResult:
        """The history's messages in order, at most :data:`POLL_MAX_READS` reads. Stopped
        part way (the budget, or a Gmail failure), the ``history_id`` moves only to just
        before the first message not read, so nothing unread is ever skipped."""
        messages: list[Message] = []
        stop: int | None = None
        reads = 0
        for index, ref in enumerate(history.messages_added):
            if ref.id in self.seen:
                continue
            if reads >= POLL_MAX_READS:
                stop = index
                break
            reads += 1
            try:
                messages.append(self.gmail.get_message(ref.id, purpose=self.purpose))
            except GmailNotFound:  # deleted since it arrived
                pass
            except GmailError as exc:
                log.warning("mailbox %d: a read failed (%s)", self.work.mailbox_id, exc.code)
                stop = index
                break
            self.seen.add(ref.id)
        found = self.classify(messages)
        if stop is None:
            return PollResult(found, history.history_id, caught_up=True)
        resume: int | None = None
        if len(history.record_ids) == len(history.messages_added):
            resume = history.record_ids[stop] - 1
        return PollResult(
            found, resume if resume is not None and resume > start else None, caught_up=False
        )

    def read(self, ids: Iterable[str]) -> list[Message]:
        found: list[Message] = []
        for gmail_id in ids:
            if gmail_id in self.seen:
                continue
            self.seen.add(gmail_id)
            try:
                found.append(self.gmail.get_message(gmail_id, purpose=self.purpose))
            except GmailNotFound:  # deleted since it arrived
                continue
        return found

    def scan(self) -> list[Message]:
        found: list[Message] = []
        for thread_id in sorted({t for w in self.work.watches for t in w.threads}):
            try:
                thread = self.gmail.get_thread(thread_id, purpose=self.purpose)
            except GmailNotFound:
                continue
            for message in thread.messages:
                if message.id not in self.seen:
                    self.seen.add(message.id)
                    found.append(message)
        for watch in self.work.watches:
            after = int(watch.first_sent_at.timestamp())
            for address in sorted(watch.addresses):
                if not _SEARCHABLE.fullmatch(address):
                    log.info("a watched address cannot be searched for; skipped")
                    continue
                refs = self.gmail.search(
                    f"from:{address} after:{after}", max_results=SEARCH_MAX, purpose=self.purpose
                )
                found.extend(self.read(r.id for r in refs))
        return found

    def classify(self, messages: Iterable[Message]) -> list[Reply | Bounce]:
        found: list[Reply | Bounce] = []
        for message in messages:
            if message.label_ids & _OWN:
                continue
            if is_daemon(message):
                bounce = self.bounce(message) if is_hard_bounce(message) else None
                if bounce is not None:
                    found.append(bounce)
                continue
            if is_auto_reply(message):  # out of office, not an answer (#296 review)
                continue
            sender = sender_of(message)
            in_thread = [w for w in self.work.watches if message.thread_id in w.threads]
            candidates = in_thread or list(self.work.watches)
            for watch in candidates:
                if sender in watch.addresses and message.internal_date > watch.first_sent_at:
                    found.append(
                        Reply(
                            watch.enrollment_id, message, asks_to_unsubscribe(message), watch.label
                        )
                    )
        return found

    def bounce(self, notice: Message) -> Bounce | None:
        cited = set(
            _MSGID.findall(f"{notice.header('In-Reply-To') or ''} {notice.header('References')}")
        )
        ours = self.cited()
        match = next((ours[c] for c in sorted(cited) if c in ours), None)
        if match is None:
            # Gmail files a notice with the message that bounced: the newest sent in its thread.
            for watch in self.work.watches:
                before = [
                    s
                    for s in watch.sent
                    if s.thread_id == notice.thread_id and s.sent_at <= notice.internal_date
                ]
                if before:
                    sent = before[-1]
                    to = next((v[2] for v in ours.values() if v[1] == sent), None)
                    match = (watch, sent, to)
                    break
        if match is None:
            return None
        watch, sent, to = match
        return Bounce(watch.enrollment_id, notice.id, sent.row_id, to, notice.internal_date)

    def cited(self) -> dict[str, tuple[Watch, Sent, str | None]]:
        """Each sent message's Message-ID, with its watch and ``To``, from Gmail's copy.
        Read once per poll, and only when a notice needs it."""
        if self._cited is None:
            self._cited = {}
            for watch in self.work.watches:
                for sent in watch.sent:
                    try:
                        copy = self.gmail.get_message(sent.gmail_message_id, purpose=self.purpose)
                    except GmailNotFound:
                        continue
                    rfc822 = (copy.header("Message-ID") or "").strip()
                    to = parseaddr(copy.header("To") or "")[1].lower() or None
                    if rfc822:
                        self._cited[rfc822] = (watch, sent, to)
        return self._cited


# --- recording ---------------------------------------------------------------------------


def record_reply(session: Session, user: User, reply: Reply) -> bool:
    """Store an inbound message and its ``email_in`` interaction, and end a live enrollment.
    False when the message was already recorded for the enrollment. Needs a writer session."""
    enrollment = get_scoped(session, user, Enrollment, reply.enrollment_id)
    if enrollment is None:
        return False
    message = reply.message
    known = session.scalar(
        scoped(user, MessageRow)
        .with_only_columns(MessageRow.id)
        .where(
            MessageRow.enrollment_id == enrollment.id,
            MessageRow.direction == MessageDirection.IN,
            MessageRow.gmail_message_id == message.id,
        )
        .limit(1)
    )
    if known is not None:
        return False
    step_id = session.scalar(
        scoped(user, MessageRow)
        .with_only_columns(MessageRow.step_id)
        .where(
            MessageRow.enrollment_id == enrollment.id,
            MessageRow.direction == MessageDirection.OUT,
            MessageRow.sent_at.is_not(None),
            MessageRow.sent_at <= message.internal_date,
        )
        .order_by(MessageRow.sent_at.desc(), MessageRow.id.desc())
        .limit(1)
    )
    subject = message.header("Subject")
    row = MessageRow(
        user_id=user.id,
        enrollment_id=enrollment.id,
        step_id=step_id,
        contact_id=enrollment.contact_id,
        channel=TemplateChannel.EMAIL,
        direction=MessageDirection.IN,
        status=MessageStatus.RECEIVED,
        subject=None if subject is None else subject[:MESSAGE_SUBJECT_MAX_LENGTH],
        snippet=message.snippet[:MESSAGE_SNIPPET_MAX_LENGTH],
        asks_unsubscribe=reply.unsubscribe,
        sent_at=message.internal_date,
        gmail_message_id=message.id,
        gmail_thread_id=message.thread_id,
    )
    session.add(row)
    session.flush()
    add_interaction(
        session,
        user,
        enrollment.contact_id,
        InteractionKind.EMAIL_IN,
        message.internal_date,
        summary="Campaign reply" + (" asking to unsubscribe" if reply.unsubscribe else ""),
        message_id=row.id,
        source=ContactSource.SYNC,
    )
    enrollment.replied_at = enrollment.replied_at or message.internal_date
    if reply.unsubscribe:
        contact = get_scoped(session, user, Contact, enrollment.contact_id)
        if contact is not None and not contact.do_not_contact:
            contact.do_not_contact = True
            contact.do_not_contact_reason = (
                f"asked to unsubscribe in a reply to a campaign (message {row.id})"
            )
        if contact is not None:
            # Every address of theirs, so another contact holding one is not mailed (#238).
            for email in contact.emails:
                do_not_send.add(
                    session, user, email.email, DoNotSendReason.OPTED_OUT, contact_id=contact.id
                )
    if enrollment.status in LIVE:
        if reply.unsubscribe:
            engine.end_enrollment(
                session, user, enrollment, EnrollmentStatus.OPTED_OUT, UNSUBSCRIBE_REASON
            )
        else:
            engine.end_enrollment(session, user, enrollment, EnrollmentStatus.REPLIED, "replied")
    session.flush()
    log.info("enrollment %d: reply recorded as message %d", enrollment.id, row.id)
    return True


def record_bounce(session: Session, user: User, bounce: Bounce) -> bool:
    """Mark the message, its address and a live enrollment ``bounced``. False when the
    message was already. Needs a writer session."""
    message = get_scoped(session, user, MessageRow, bounce.bounced_row_id)
    enrollment = get_scoped(session, user, Enrollment, bounce.enrollment_id)
    if message is None or enrollment is None or message.status is MessageStatus.BOUNCED:
        return False
    message.status = MessageStatus.BOUNCED
    message.bounced_at = utcnow()
    if bounce.address is not None:
        rows = list(
            session.scalars(
                scoped(user, ContactEmail).where(
                    ContactEmail.contact_id == message.contact_id,
                    func.lower(ContactEmail.email) == bounce.address,
                )
            )
        )
        for row in rows:
            row.status = EmailStatus.BOUNCED
        if not rows:
            log.warning(
                "message %d bounced, but its address is no longer on the contact", message.id
            )
        # On the list whether or not a contact still holds it (#238).
        do_not_send.add(
            session, user, bounce.address, DoNotSendReason.BOUNCED, contact_id=message.contact_id
        )
    if enrollment.status in LIVE:
        engine.end_enrollment(session, user, enrollment, EnrollmentStatus.BOUNCED, "bounced")
    session.flush()
    log.info(
        "message %d bounced; enrollment %d is %s", message.id, enrollment.id, enrollment.status
    )
    return True


def _store_poll(
    session: Session, user: User, mailbox_id: int, result: PollResult, *, now: datetime
) -> None:
    mailbox = get_scoped(session, user, Mailbox, mailbox_id)
    if mailbox is None:
        return
    if result.history_id is not None:
        mailbox.history_id = result.history_id
    if result.caught_up:
        mailbox.replies_polled_at = now


# --- the poll ----------------------------------------------------------------------------

Labeler = Callable[[Gmail, int, str, str], None]
"""``(gmail, mailbox_id, label name, gmail message id)``: put the campaign label on a reply."""


def poll_replies(
    factory: sessionmaker[Session],
    user_id: int,
    *,
    open_gmail: Callable[[int, int], Gmail],
    now: datetime,
    label: Labeler | None = None,
) -> bool:
    """One poll of every armed mailbox of the user (the module). Blocking. True when every
    mailbox read everything up to now; False when one stopped part way or failed, and the
    next tick should poll again.

    A mailbox with nothing to watch gets no Gmail call: it is caught up by definition.
    ``replies_polled_at`` is set only for a mailbox that caught up, and it is what
    :meth:`~netkeeper.services.campaign_sender.GmailSender.send` reads to hold a new
    conversation while replies may be going unseen."""
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        if user is None:
            return True
        work = reply_work(session, user, now=now)
    caught_up = True
    for mailbox in work:
        gmail: Gmail | None = None
        if not mailbox.watches:
            result = PollResult([], None, caught_up=True)
        else:
            purpose = f"reply poll of mailbox {mailbox.mailbox_id}"
            try:
                gmail = open_gmail(user_id, mailbox.mailbox_id)
                result = _Poll(gmail, mailbox, purpose).run()
            except Exception as exc:  # the next poll reads the same messages again
                code = exc.code if isinstance(exc, GmailError) else type(exc).__name__
                log.warning("mailbox %d: the reply poll waits (%s)", mailbox.mailbox_id, code)
                caught_up = False
                continue
        caught_up = caught_up and result.caught_up
        new: list[Reply | Bounce] = []
        with session_scope(factory, write=True) as session:
            user = session.get(User, user_id)
            if user is None:
                return caught_up
            for item in result.found:
                done = (
                    record_reply(session, user, item)
                    if isinstance(item, Reply)
                    else record_bounce(session, user, item)
                )
                if done:
                    new.append(item)
            _store_poll(session, user, mailbox.mailbox_id, result, now=now)
        if label is not None and gmail is not None:
            for item in new:
                if isinstance(item, Reply):
                    label(gmail, mailbox.mailbox_id, item.label, item.message.id)
    return caught_up
