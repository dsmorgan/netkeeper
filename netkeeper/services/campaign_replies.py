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
reply to the last step still belongs in the inbox). A disarmed mailbox gets no
Gmail call, as in reconcile (#277).

How it reads Gmail
------------------
- **History.** ``history.list`` from the mailbox's stored ``history_id``; every
  message added since is read (metadata only), whatever thread it is in. That
  one read catches a reply in the thread and one from the contact's address in a
  fresh email alike, at the cost of one ``messages.get`` per message received.
- **Scan.** With no ``history_id`` yet, or one Gmail no longer keeps (a 404),
  the poll takes ``profile().history_id`` first, reads every watched thread, and
  searches ``from:<address> after:<first send>`` for each watched address (spec
  11.7, step 3), then stores that id as the new baseline. Taking it first means
  nothing that arrives during the scan is missed: the next history read covers it.
- A Gmail failure stops the mailbox's poll without moving its ``history_id``, so
  the next poll reads the same messages again. Recording is idempotent.

What a message means
--------------------
A message netkeeper or the person sent (``SENT`` or ``DRAFT``) is skipped.

- **Bounce**: from a mailer daemon (``mailer-daemon@`` or ``postmaster@``), in a
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

An auto-reply (out of office) from the contact's address counts as a reply: the
conservative direction, since nothing further is sent.

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
from netkeeper.campaigns.gmail import Gmail, GmailError, GmailNotFound, Message
from netkeeper.crm.interactions import add_interaction
from netkeeper.db import session_scope
from netkeeper.models import (
    MESSAGE_SNIPPET_MAX_LENGTH,
    MESSAGE_SUBJECT_MAX_LENGTH,
    Campaign,
    Contact,
    ContactEmail,
    ContactSource,
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

LIVE: Final = frozenset({EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED})
"""What detection moves to ``replied``, ``bounced`` or ``opted_out`` (spec 11.3)."""

_UNSUBSCRIBE: Final = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in UNSUBSCRIBE_PHRASES) + r")\b", re.IGNORECASE
)
_MSGID: Final = re.compile(r"<[^<>\s]+>")
_SEARCHABLE: Final = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+")
_OWN: Final = frozenset({"SENT", "DRAFT"})


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
    mailbox_id: int
    history_id: int | None
    watches: tuple[Watch, ...]


def reply_work(session: Session, user: User, *, now: datetime) -> list[MailboxWatch]:
    """Each armed mailbox of ``user`` with something to watch (the module docstring)."""
    enrollments = list(
        session.scalars(
            scoped(user, Enrollment).where(
                Enrollment.status.in_((*LIVE, EnrollmentStatus.COMPLETED))
            )
        )
    )
    if not enrollments:
        return []
    campaigns = {
        c.id: c
        for c in session.scalars(
            scoped(user, Campaign).where(Campaign.id.in_({e.campaign_id for e in enrollments}))
        )
    }
    mailboxes = {
        m.id: m
        for m in session.scalars(
            scoped(user, Mailbox).where(
                Mailbox.id.in_({c.mailbox_id for c in campaigns.values() if c.mailbox_id})
            )
        )
        if m.arm is not None
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
    by_mailbox: dict[int, list[Watch]] = {}
    for enrollment in enrollments:
        campaign = campaigns[enrollment.campaign_id]
        mailbox = mailboxes.get(campaign.mailbox_id or 0)
        messages = sent.get(enrollment.id)
        if mailbox is None or not messages:
            continue
        if (
            enrollment.status is EnrollmentStatus.COMPLETED
            and now - messages[-1].sent_at > WATCH_AFTER_COMPLETED
        ):
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


@dataclass
class _Poll:
    gmail: Gmail
    work: MailboxWatch
    purpose: str
    seen: set[str] = field(default_factory=set)
    _cited: dict[str, tuple[Watch, Sent, str | None]] | None = None

    def run(self) -> tuple[list[Reply | Bounce], int]:
        """What the mailbox received, and the ``history_id`` to read from next."""
        own = {s.gmail_message_id for w in self.work.watches for s in w.sent}
        self.seen |= own
        if self.work.history_id is not None:
            try:
                history = self.gmail.history(self.work.history_id, purpose=self.purpose)
            except GmailNotFound:
                log.info("mailbox %d: Gmail's history expired; scanning", self.work.mailbox_id)
            else:
                return self.classify(self.read(r.id for r in history.messages_added)), (
                    history.history_id
                )
        baseline = self.gmail.profile(purpose=self.purpose).history_id
        return self.classify(self.scan()), baseline

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
            sender = parseaddr(message.header("From") or "")[1].lower()
            local = sender.partition("@")[0]
            if local in DAEMON_LOCAL_PARTS:
                bounce = self.bounce(message)
                if bounce is not None:
                    found.append(bounce)
                continue
            in_thread = [w for w in self.work.watches if message.thread_id in w.threads]
            candidates = in_thread or list(self.work.watches)
            for watch in candidates:
                if sender in watch.addresses and message.internal_date > watch.first_sent_at:
                    text = f"{message.header('Subject') or ''} {message.snippet}"
                    found.append(
                        Reply(
                            watch.enrollment_id,
                            message,
                            bool(_UNSUBSCRIBE.search(text)),
                            watch.label,
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
    if enrollment.status in LIVE:
        engine.end_enrollment(session, user, enrollment, EnrollmentStatus.BOUNCED, "bounced")
    session.flush()
    log.info(
        "message %d bounced; enrollment %d is %s", message.id, enrollment.id, enrollment.status
    )
    return True


def _store_history(session: Session, user: User, mailbox_id: int, history_id: int) -> None:
    mailbox = get_scoped(session, user, Mailbox, mailbox_id)
    if mailbox is not None:
        mailbox.history_id = history_id


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
) -> int:
    """One poll of every armed mailbox of the user (the module). Blocking. The number of
    replies and bounces newly recorded."""
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        if user is None:
            return 0
        work = reply_work(session, user, now=now)
    recorded = 0
    for mailbox in work:
        purpose = f"reply poll of mailbox {mailbox.mailbox_id}"
        try:
            gmail = open_gmail(user_id, mailbox.mailbox_id)
            found, history_id = _Poll(gmail, mailbox, purpose).run()
        except Exception as exc:  # the next poll reads the same messages again
            code = exc.code if isinstance(exc, GmailError) else type(exc).__name__
            log.warning("mailbox %d: the reply poll waits (%s)", mailbox.mailbox_id, code)
            continue
        new: list[Reply | Bounce] = []
        with session_scope(factory, write=True) as session:
            user = session.get(User, user_id)
            if user is None:
                return recorded
            for item in found:
                done = (
                    record_reply(session, user, item)
                    if isinstance(item, Reply)
                    else record_bounce(session, user, item)
                )
                if done:
                    new.append(item)
            _store_history(session, user, mailbox.mailbox_id, history_id)
        recorded += len(new)
        if label is not None:
            for item in new:
                if isinstance(item, Reply):
                    label(gmail, mailbox.mailbox_id, item.label, item.message.id)
    return recorded
