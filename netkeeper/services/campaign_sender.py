"""The Gmail sender: send and draft modes, follow-ups in thread, labels, and the
drafts poll (spec 11.5; item P3-07).

:class:`GmailSender` is the campaign engine's :class:`~netkeeper.services.campaign_engine.Sender`
and :class:`~netkeeper.services.campaign_engine.Reconciler`. The engine calls it
in a worker thread with no session open. It builds each message with
:mod:`netkeeper.campaigns.compose` and talks to Gmail only through the
:class:`~netkeeper.campaigns.gmail.Gmail` interface, so every test runs it
against :class:`~netkeeper.campaigns.gmail_fake.FakeGmail`.

Sending
-------
- **Modes.** A ``send`` step goes out with ``messages.send``; a ``draft`` step
  becomes a Gmail draft (``drafts.create``) the person sends by hand.
- **The Message-ID** is the firing's ``rfc822_message_id``, derived from the
  message row before the send (#269), so a search for it after a crash or an
  unknown outcome says whether Gmail has the message.
- **Follow-ups** (``same_thread``). The Gmail thread is read first, and the
  follow-up cites every sent message in it, oldest first, in ``References``,
  and the newest in ``In-Reply-To``, with the thread's first subject behind
  one ``Re:``. It goes in with the thread's ``threadId``. Gmail joins it to
  the thread only when both the citation and the subject match (the fake's
  rule); a follow-up Gmail put in a new thread anyway is logged. The citations
  come from Gmail's copy, not from the database: a draft the person sent has a
  Message-ID of Gmail's making, and that is the one a reply must cite.
- **Labels.** The campaign's label (``<label prefix>/<campaign name>``) is
  created when missing and applied to each sent message. A draft gets it once
  it is seen sent. A label failure never fails a send that went out.
- **Outcomes.** Nothing sent is ``failed`` with a one-line reason: a mailbox
  that is not ready, a thread that is gone, a message that cannot be built,
  or any Gmail refusal. A write whose answer never came
  (:attr:`~netkeeper.campaigns.gmail.GmailTransient.outcome_unknown`) is
  looked up by its Message-ID at once. Found, it is ``sent`` or ``drafted``;
  not found (yet), it is ``unknown``, and the engine leaves it ``scheduled`` for
  :meth:`GmailSender.reconcile`. Nothing is ever sent a second time.

Reconciling
-----------
Each tick, before anything is chosen, :meth:`GmailSender.reconcile` looks up what
:func:`~netkeeper.services.campaign_engine.reconcile_work` lists:

- **Leftovers** (``scheduled`` after a crash or an unknown outcome): a search
  for ``rfc822msgid:``, in every folder. Sent, drafted, or not in Gmail at all.
- **Drafts** (the drafts poll, every :data:`DRAFTS_POLL_EVERY`): a draft gone
  from ``drafts.list`` with a new sent message in its thread is ``sent`` at the
  message's internal date, and the next step is scheduled from it. A draft gone
  with nothing sent is ``discarded`` and its enrollment ``removed``, on the
  second poll that finds it gone, not the first (undo send).
- **Discarded drafts** (#269): a message a merge discarded while its Gmail
  draft was waiting. The draft is deleted. If the person sent it already, the
  message is ``sent`` after all.

A Gmail failure leaves the message as it is for the next tick; nothing here
raises for one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.compose import (
    ComposeError,
    build_message,
    is_message_id,
    reply_subject,
)
from netkeeper.campaigns.gmail import (
    Gmail,
    GmailError,
    GmailNotFound,
    GmailTransient,
    Message,
    ensure_label,
)
from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.models import MessageStatus, StepMode, TemplateChannel, User
from netkeeper.models.base import utcnow
from netkeeper.services import campaign_engine as engine
from netkeeper.services.campaign_engine import Firing, SendOutcome, SendResult, Tracked
from netkeeper.services.mailboxes import MailboxNotFound, MailboxNotReady, open_gmail

log = logging.getLogger(__name__)

DRAFTS_POLL_EVERY: Final = timedelta(minutes=10)
"""How often the drafts poll (and the discarded-draft pass) looks at Gmail, per user.
A draft seen sent is dated by Gmail's internal date, so the poll's delay never moves
the next step; it only decides how soon the step is scheduled."""

FIND_MAX: Final = 10
"""At most this many messages a ``rfc822msgid:`` search reads. One is the usual answer."""

GmailOpener = Callable[[int, int], Gmail]
"""``(user_id, mailbox_id)`` to a Gmail client for that mailbox: a fresh one each call,
since a client is not thread-safe. Raises :class:`MailboxNotReady` or :class:`MailboxNotFound`."""

_SENT: Final = "SENT"
_DRAFT: Final = "DRAFT"


class _NotSent(Exception):
    """Nothing was sent, and why, in one line fit for ``messages.error``."""


@dataclass(frozen=True, slots=True)
class _Threading:
    thread_id: str | None
    subject: str
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Found:
    """What a search by Message-ID found: the message, and its draft when it is one."""

    message: Message
    sent: bool
    draft_id: str | None = None


def find_by_message_id(gmail: Gmail, rfc822_message_id: str, *, purpose: str) -> Found | None:
    """The message Gmail holds with this Message-ID, in any folder, or None.

    A message that is not a draft counts as sent, whatever else its labels say (a
    sent message the person moved to Trash is still sent). A draft's id is read
    from ``drafts.list``. Raises :class:`GmailError`.
    """
    if not is_message_id(rfc822_message_id):
        raise ValueError("not a Message-ID")
    query = f"rfc822msgid:{rfc822_message_id.strip('<>')} in:anywhere"
    messages: list[Message] = []
    for ref in gmail.search(query, max_results=FIND_MAX, purpose=purpose):
        try:
            messages.append(gmail.get_message(ref.id, purpose=purpose))
        except GmailNotFound:  # deleted between the search and the read
            continue
    sent = sorted((m for m in messages if _DRAFT not in m.label_ids), key=lambda m: m.internal_date)
    if sent:
        return Found(sent[0], sent=True)
    if not messages:
        return None
    draft = messages[0]
    draft_id = next(
        (d.id for d in gmail.list_drafts(purpose=purpose) if d.message.id == draft.id), None
    )
    return Found(draft, sent=False, draft_id=draft_id)


class GmailSender:
    """The engine's Gmail sender and reconciler. See the module docstring.

    ``opener`` gives a Gmail client for a mailbox; it defaults to
    :func:`netkeeper.services.mailboxes.open_gmail`. Tests pass a
    :class:`~netkeeper.campaigns.gmail_fake.FakeGmail`. ``clock`` dates a send;
    ``drafts_every`` is how often the drafts poll runs, per user.
    """

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        opener: GmailOpener | None = None,
        clock: Callable[[], datetime] = utcnow,
        drafts_every: timedelta = DRAFTS_POLL_EVERY,
    ) -> None:
        if drafts_every < timedelta(0):
            raise ValueError("the drafts poll interval cannot be negative")
        self._open: GmailOpener = opener or (
            lambda user_id, mailbox_id: open_gmail(factory, user_id, mailbox_id)
        )
        self._clock = clock
        self._drafts_every = drafts_every
        self._drafts_polled: dict[int, datetime] = {}
        self._label_ids: dict[tuple[int, str], str] = {}

    # --- sending ---------------------------------------------------------------------

    def send(self, firing: Firing) -> SendResult:
        """Send or draft one firing. Never raises for a Gmail failure."""
        verb = "draft" if firing.mode is StepMode.DRAFT else "send"
        purpose = f"{verb} step {firing.step_position} for enrollment {firing.enrollment_id}"
        try:
            gmail, message_id = self._ready(firing)
            threading_ = self._threading(gmail, firing, purpose)
            message = build_message(
                to=firing.to_address or "",
                subject=threading_.subject,
                body=firing.body,
                message_id=message_id,
                in_reply_to=threading_.in_reply_to,
                references=threading_.references,
            )
        except _NotSent as exc:
            return _failed(firing, str(exc))
        except ComposeError as exc:
            return _failed(firing, f"the message could not be built: {exc}")
        label_id = self._label_id(gmail, firing, purpose)
        draft_id: str | None = None
        try:
            if firing.mode is StepMode.DRAFT:
                draft = gmail.create_draft(message, thread_id=threading_.thread_id, purpose=purpose)
                ref, draft_id = draft.message, draft.id
            else:
                ref = gmail.send(message, thread_id=threading_.thread_id, purpose=purpose)
        except GmailTransient as exc:
            if exc.outcome_unknown:
                return self._look(gmail, firing, message_id, purpose, exc.code)
            return _failed(firing, f"Gmail was unavailable ({exc.code}); nothing was sent")
        except GmailError as exc:
            return _failed(firing, f"Gmail refused it ({type(exc).__name__}: {exc.code})")
        if threading_.thread_id is not None and ref.thread_id != threading_.thread_id:
            log.warning(
                "message %d: Gmail put step %d in a new thread, not the earlier step's",
                firing.message_id,
                firing.step_position,
            )
        if draft_id is not None:
            log.info(
                "message %d drafted for enrollment %d", firing.message_id, firing.enrollment_id
            )
            return SendResult(
                SendOutcome.DRAFTED,
                gmail_message_id=ref.id,
                gmail_thread_id=ref.thread_id,
                gmail_draft_id=draft_id,
            )
        at = self._clock()
        self._apply_label(gmail, ref.id, label_id, purpose)
        log.info("message %d sent for enrollment %d", firing.message_id, firing.enrollment_id)
        return SendResult(
            SendOutcome.SENT, at=at, gmail_message_id=ref.id, gmail_thread_id=ref.thread_id
        )

    def _ready(self, firing: Firing) -> tuple[Gmail, str]:
        if firing.channel is not TemplateChannel.EMAIL or firing.mode not in (
            StepMode.SEND,
            StepMode.DRAFT,
        ):
            raise _NotSent(f"the Gmail sender does not do {firing.channel} {firing.mode} steps")
        if firing.mailbox_id is None or firing.rfc822_message_id is None:
            raise _NotSent("the firing has no mailbox or no Message-ID")
        if not firing.to_address:
            raise _NotSent("the firing has no recipient")
        try:
            gmail = self._open(firing.user_id, firing.mailbox_id)
        except MailboxNotReady as exc:
            raise _NotSent(f"the mailbox is not ready ({exc.code}); nothing was sent") from exc
        except MailboxNotFound as exc:
            raise _NotSent("the campaign's mailbox is gone; nothing was sent") from exc
        return gmail, firing.rfc822_message_id

    def _threading(self, gmail: Gmail, firing: Firing, purpose: str) -> _Threading:
        """How the message threads: a new conversation, or a reply in the earlier step's."""
        subject = firing.subject or ""
        if not firing.same_thread or firing.thread_id is None:
            return _Threading(thread_id=None, subject=subject)
        try:
            thread = gmail.get_thread(firing.thread_id, purpose=purpose)
        except GmailNotFound as exc:
            raise _NotSent("the earlier step's Gmail thread is gone; nothing was sent") from exc
        except GmailError as exc:
            raise _NotSent(
                f"the earlier step's thread could not be read ({exc.code}); nothing was sent"
            ) from exc
        earlier = [m for m in thread.messages if _SENT in m.label_ids and _DRAFT not in m.label_ids]
        cited = [
            value.strip()
            for m in earlier
            if (value := m.header("Message-ID")) is not None and is_message_id(value.strip())
        ]
        if not cited:
            raise _NotSent("the earlier step is not in its Gmail thread; nothing was sent")
        return _Threading(
            thread_id=thread.id,
            subject=reply_subject(earlier[0].header("Subject")),
            in_reply_to=cited[-1],
            references=tuple(cited),
        )

    def _look(
        self, gmail: Gmail, firing: Firing, message_id: str, purpose: str, code: str
    ) -> SendResult:
        """The write's answer never came: search for its Message-ID before anything else."""
        log.warning(
            "message %d: no answer from Gmail (%s); searching for its Message-ID",
            firing.message_id,
            code,
        )
        unknown = SendResult(SendOutcome.UNKNOWN, error=f"no answer from Gmail ({code})")
        try:
            found = find_by_message_id(gmail, message_id, purpose=purpose)
        except GmailError as exc:
            log.warning("message %d: the search failed too (%s)", firing.message_id, exc.code)
            return unknown
        if found is None:
            return unknown
        ref = found.message
        if found.sent:
            self._apply_label(gmail, ref.id, self._label_id(gmail, firing, purpose), purpose)
            return SendResult(
                SendOutcome.SENT,
                at=ref.internal_date,
                gmail_message_id=ref.id,
                gmail_thread_id=ref.thread_id,
            )
        if found.draft_id is not None:
            return SendResult(
                SendOutcome.DRAFTED,
                gmail_message_id=ref.id,
                gmail_thread_id=ref.thread_id,
                gmail_draft_id=found.draft_id,
            )
        return unknown

    # --- labels ----------------------------------------------------------------------

    def _label_id(self, gmail: Gmail, firing: Firing, purpose: str) -> str | None:
        if firing.label is None or firing.mailbox_id is None:
            return None
        return self._label_named(gmail, firing.mailbox_id, firing.label, purpose)

    def _label_named(self, gmail: Gmail, mailbox_id: int, name: str, purpose: str) -> str | None:
        key = (mailbox_id, name.lower())
        cached = self._label_ids.get(key)
        if cached is not None:
            return cached
        try:
            label = ensure_label(gmail, name, purpose=purpose)
        except GmailError as exc:
            log.warning("mailbox %d: the campaign label is unavailable (%s)", mailbox_id, exc.code)
            return None
        self._label_ids[key] = label.id
        return label.id

    def _apply_label(
        self, gmail: Gmail, gmail_message_id: str, label_id: str | None, purpose: str
    ) -> None:
        if label_id is None:
            return
        try:
            gmail.modify_labels(gmail_message_id, add=(label_id,), purpose=purpose)
        except GmailError as exc:  # the message went out; only the label is missing
            log.warning("labelling a campaign message failed (%s)", exc.code)
            # The person may have deleted the label: look it up again next time.
            self._label_ids = {k: v for k, v in self._label_ids.items() if v != label_id}

    # --- reconciling -----------------------------------------------------------------

    def reconcile(
        self,
        factory: sessionmaker[Session],
        user_id: int,
        *,
        settings: Settings,
        now: datetime,
    ) -> None:
        """Look up the user's leftovers, drafts and discarded drafts in Gmail (the module)."""
        with session_scope(factory) as session:
            user = session.get(User, user_id)
            if user is None:
                return
            work = engine.reconcile_work(session, user, now=now)
        last = self._drafts_polled.get(user_id)
        poll = last is None or now - last >= self._drafts_every
        drafts = work.drafts if poll else ()
        discarded = work.discarded_drafts if poll else ()
        if poll and (work.drafts or work.discarded_drafts):
            self._drafts_polled[user_id] = now
        run = _Reconcile(self, factory, user_id, settings)
        for mailbox_id in sorted({t.mailbox_id for t in (*work.leftovers, *drafts, *discarded)}):
            try:
                gmail = self._open(user_id, mailbox_id)
            except (MailboxNotReady, MailboxNotFound) as exc:
                log.info("mailbox %d is not ready; reconciling waits (%s)", mailbox_id, exc)
                continue
            run.mailbox(
                gmail,
                [t for t in work.leftovers if t.mailbox_id == mailbox_id],
                [t for t in drafts if t.mailbox_id == mailbox_id],
                [t for t in discarded if t.mailbox_id == mailbox_id],
            )


class _Reconcile:
    """One user's reconcile pass: each lookup in Gmail, then its own short writer session."""

    def __init__(
        self, sender: GmailSender, factory: sessionmaker[Session], user_id: int, settings: Settings
    ) -> None:
        self.sender = sender
        self.factory = factory
        self.user_id = user_id
        self.settings = settings

    def write(self, fn: Callable[[Session, User], object]) -> None:
        with session_scope(self.factory, write=True) as session:
            user = session.get(User, self.user_id)
            if user is not None:
                fn(session, user)

    def mailbox(
        self,
        gmail: Gmail,
        leftovers: Collection[Tracked],
        drafts: Collection[Tracked],
        discarded: Collection[Tracked],
    ) -> None:
        for tracked in leftovers:
            self.guarded(tracked, self.leftover, gmail, tracked)
        if not drafts and not discarded:
            return
        try:
            present = {
                d.id for d in gmail.list_drafts(purpose=f"drafts poll for user {self.user_id}")
            }
        except GmailError as exc:
            log.warning("the drafts poll could not list drafts (%s); it waits", exc.code)
            return
        for tracked in drafts:
            self.guarded(tracked, self.draft, gmail, tracked, present)
        for tracked in discarded:
            self.guarded(tracked, self.discarded, gmail, tracked, present)

    @staticmethod
    def guarded[*Ts](tracked: Tracked, fn: Callable[[*Ts], None], *args: *Ts) -> None:
        try:
            fn(*args)
        except GmailError as exc:  # left as it is for the next tick
            log.warning(
                "message %d could not be looked up in Gmail (%s); it waits",
                tracked.message_id,
                exc.code,
            )

    @staticmethod
    def purpose(tracked: Tracked, what: str) -> str:
        return f"{what} message {tracked.message_id} for enrollment {tracked.enrollment_id}"

    def leftover(self, gmail: Gmail, tracked: Tracked) -> None:
        purpose = self.purpose(tracked, "reconcile")
        found = find_by_message_id(gmail, tracked.rfc822_message_id, purpose=purpose)
        settings = self.settings
        if found is None:
            self.write(lambda s, u: engine.settle_not_sent(s, u, settings, tracked.message_id))
            return
        ref = found.message
        if found.sent:
            self.write(
                lambda s, u: engine.settle_sent(
                    s,
                    u,
                    settings,
                    tracked.message_id,
                    expect=(MessageStatus.SCHEDULED,),
                    at=ref.internal_date,
                    gmail_message_id=ref.id,
                    gmail_thread_id=ref.thread_id,
                )
            )
            self.label(gmail, tracked, ref.id, purpose)
        elif found.draft_id is not None:
            draft_id = found.draft_id
            self.write(
                lambda s, u: engine.settle_drafted(
                    s,
                    u,
                    settings,
                    tracked.message_id,
                    gmail_message_id=ref.id,
                    gmail_thread_id=ref.thread_id,
                    gmail_draft_id=draft_id,
                )
            )
        else:
            log.info("message %d is a Gmail draft not listed yet; it waits", tracked.message_id)

    def draft(self, gmail: Gmail, tracked: Tracked, present: Collection[str]) -> None:
        if tracked.gmail_draft_id in present:
            if tracked.marked_missing:
                self.write(lambda s, u: engine.settle_draft_present(s, u, tracked.message_id))
            return
        purpose = self.purpose(tracked, "drafts poll for")
        sent = self.sent_in_thread(gmail, tracked, purpose)
        if sent is None:
            self.write(lambda s, u: engine.settle_draft_missing(s, u, tracked.message_id))
            return
        self.settle_sent(tracked, sent, MessageStatus.DRAFTED)
        self.label(gmail, tracked, sent.id, purpose)

    def discarded(self, gmail: Gmail, tracked: Tracked, present: Collection[str]) -> None:
        purpose = self.purpose(tracked, "delete the discarded draft of")
        if tracked.gmail_draft_id in present:
            assert tracked.gmail_draft_id is not None
            try:
                gmail.delete_draft(tracked.gmail_draft_id, purpose=purpose)
            except GmailNotFound:
                pass  # sent or deleted since the list: look in the thread below
            else:
                self.write(lambda s, u: engine.forget_draft(s, u, tracked.message_id))
                log.info("message %d: its discarded Gmail draft is deleted", tracked.message_id)
                return
        sent = self.sent_in_thread(gmail, tracked, purpose)
        if sent is None:
            self.write(lambda s, u: engine.forget_draft(s, u, tracked.message_id))
            return
        self.settle_sent(tracked, sent, MessageStatus.DISCARDED)
        self.label(gmail, tracked, sent.id, purpose)

    def settle_sent(self, tracked: Tracked, sent: Message, status: MessageStatus) -> None:
        settings = self.settings
        self.write(
            lambda s, u: engine.settle_sent(
                s,
                u,
                settings,
                tracked.message_id,
                expect=(status,),
                at=sent.internal_date,
                gmail_message_id=sent.id,
                gmail_thread_id=sent.thread_id,
            )
        )

    @staticmethod
    def sent_in_thread(gmail: Gmail, tracked: Tracked, purpose: str) -> Message | None:
        """The first sent message in the draft's thread that no other message of the user
        already holds: the draft, sent. None when the thread has none, or is gone."""
        if tracked.gmail_thread_id is None:
            return None
        try:
            thread = gmail.get_thread(tracked.gmail_thread_id, purpose=purpose)
        except GmailNotFound:
            return None
        # The draft itself carries DRAFT until it is sent. Its own message id is not left
        # out: whether a sent draft keeps it or gets a new one, the sent copy counts.
        sent = [
            m
            for m in thread.messages
            if _SENT in m.label_ids
            and _DRAFT not in m.label_ids
            and m.id not in tracked.thread_known
        ]
        return min(sent, key=lambda m: m.internal_date) if sent else None

    def label(self, gmail: Gmail, tracked: Tracked, gmail_message_id: str, purpose: str) -> None:
        label_id = self.sender._label_named(gmail, tracked.mailbox_id, tracked.label, purpose)
        self.sender._apply_label(gmail, gmail_message_id, label_id, purpose)


def _failed(firing: Firing, reason: str) -> SendResult:
    log.warning("message %d not sent: %s", firing.message_id, reason)
    return SendResult(SendOutcome.FAILED, error=reason)
