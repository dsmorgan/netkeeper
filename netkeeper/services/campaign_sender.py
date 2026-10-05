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
- **Outcomes.** Nothing sent, for a reason that may pass, is ``not_sent``,
  and the engine gives the claim back to try again later, each time a little
  later, until a day of tries fails the step for a person (#280): a rate limit,
  Gmail unavailable before the write, an authorization error, a mailbox that is
  not ready, a thread that could not be read (#273 review). Nothing sent, for a
  reason a retry would not change, is ``failed`` with a one-line reason: a
  thread that is gone, a message that cannot be built, a Gmail refusal. A
  write whose answer never came
  (:attr:`~netkeeper.campaigns.gmail.GmailTransient.outcome_unknown`) is
  looked up by its Message-ID at once. Found, it is ``sent`` or ``drafted``;
  not found (yet), it is ``unknown``, and the engine leaves it ``scheduled`` for
  :meth:`GmailSender.reconcile`. Nothing is ever sent a second time.

Reconciling
-----------
Each tick, before anything is chosen, :meth:`GmailSender.reconcile` looks up what
:func:`~netkeeper.services.campaign_engine.reconcile_work` lists:

- **Leftovers** (``scheduled`` after a crash or an unknown outcome): a search
  for ``rfc822msgid:``, in every folder. Sent, drafted, or not found. Not found
  is counted, and only several misses spread over hours rule it out, since
  Gmail's search can lag a send
  (:func:`~netkeeper.services.campaign_engine.settle_not_sent`).
- **Drafts** (the drafts poll, every :data:`DRAFTS_POLL_EVERY`): a draft gone
  from ``drafts.list`` with a new sent message in its thread is ``sent`` at the
  message's internal date, and the next step is scheduled from it. A sent
  message counts only when it is the draft's own message id kept, or is no
  older than the draft's claim and is not one the thread held when the draft
  was made: an earlier step, or a note the person sent before, is never the
  draft (#273 review). A draft gone with nothing sent is searched for in
  Scheduled (``in:scheduled rfc822msgid:``, #278): Gmail's Schedule send moves a
  draft there, out of ``drafts.list``, until it goes out. Found, it stays
  ``drafted`` (marked ``SCHEDULED_IN_GMAIL``) until it is seen sent, and is then
  dated no earlier than the last poll that saw it in Scheduled, since Gmail may
  keep the date it was scheduled
  (:func:`~netkeeper.services.campaign_engine.seen_sent_at`). Never later than the
  delivery, so a reply right after it still counts. Not
  there, it is searched for in Drafts: a canceled Schedule send comes back under
  a new draft id, which it then follows. A failed search changes nothing. A
  ``SCHEDULED`` message is never sent, even labelled ``SENT`` too. A draft gone
  with nothing sent, scheduled, or in Drafts is ``discarded`` and its enrollment
  ``removed``, on the second poll that finds it gone, not the first (undo send).
  *Known limitation* (#280): a draft the person edits into a new thread and then
  sends is sent outside the thread the poll reads, so it reads
  as ``discarded``. That is the conservative direction (nothing more is sent);
  confirm it at the first live draft run (#277).
- **Replies and bounces** (P3-08; every ``replies_every``, per user, last):
  :func:`netkeeper.services.campaign_replies.poll_replies`. A ``same_thread``
  follow-up whose thread holds a message from the other side since the first
  step sends nothing (``not_sent``) and asks for a poll on the next tick. So does a
  person's "Check now" (#409, :meth:`GmailSender.request_replies_poll`): the
  request sets a flag, and the next tick polls every armed mailbox, as a full poll
  each interval does.
  Every armed mailbox is polled once an interval; one that stopped at the read
  budget, was not ready (it needs signing in again, or the Keychain is locked), or
  holds a follow-up because its replies are stale is polled again alone at the next
  tick, never the others with it (#413). One whose poll failed is polled again alone
  after :data:`REPLY_BACKOFF_FIRST`, doubling up to the interval. A mailbox seen not
  ready keeps its old ``replies_polled_at`` and holds its own new conversations until
  a poll of it catches up after it is ready again, however recent its last good poll.

Arming (#277)
-------------
:class:`GmailSender` is :class:`~netkeeper.services.campaign_engine.ArmGated`: it
touches only a mailbox a person armed (:func:`netkeeper.services.mailboxes.arm`).

- **Disarmed**: the engine claims nothing on it, :meth:`GmailSender.send` makes no
  Gmail call for it, and reconcile skips it, so its leftovers and drafts wait
  until it is armed again.
- **Armed for drafts**: every step becomes a draft. :meth:`GmailSender.send` reads
  the arming again before any Gmail call, and a ``send`` firing on a mailbox not
  armed for send (disarmed or taken back to drafts since the claim) sends nothing:
  its claim is given back (``not_sent``). No ``messages.send``, ever.
- **The Message-ID check**: until one has passed, each drafts poll of an armed
  mailbox searches by Message-ID for its newest test drafts (the review's test of a
  step on a mailbox armed for drafts only, #304) and for its oldest waiting
  campaign draft. Found, the mailbox is recorded verified, which arming for send
  requires: the first live check that Gmail keeps the Message-ID netkeeper sets,
  which reconcile depends on. A test draft is only searched for: it is never a
  campaign message, and netkeeper never touches it otherwise.

netkeeper deletes nothing in Gmail (ADR 0003). A draft whose message a merge
discarded stays in Gmail for the person (#273, question 2).

A Gmail failure leaves the message as it is for the next tick; nothing here
raises for one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.compose import (
    ComposeError,
    build_message,
    is_message_id,
    reply_subject,
)
from netkeeper.campaigns.gmail import (
    Draft,
    Gmail,
    GmailAuthError,
    GmailError,
    GmailNotFound,
    GmailRateLimited,
    GmailRejected,
    GmailTransient,
    Message,
    ensure_label,
)
from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.models import (
    Mailbox,
    MailboxArm,
    MessageStatus,
    StepMode,
    TemplateChannel,
    User,
)
from netkeeper.models import Message as MessageRow
from netkeeper.models.base import utcnow
from netkeeper.scoping import get_scoped
from netkeeper.services import campaign_engine as engine
from netkeeper.services import campaign_replies as replies
from netkeeper.services import campaign_review as review
from netkeeper.services import mailboxes as mailbox_service
from netkeeper.services.campaign_engine import Firing, SendOutcome, SendResult, Tracked
from netkeeper.services.mailboxes import MailboxNotFound, MailboxNotReady, open_gmail

log = logging.getLogger(__name__)

REPLY_BACKOFF_FIRST: Final = timedelta(minutes=1)
"""After a mailbox's reply poll fails, the wait before it is polled again alone (#413). It
doubles with each failure in a row, up to the reply interval, and starts over once a poll
of it reads. It only delays reads: the stale-replies hold still guards the sends."""

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
_SCHEDULED: Final = "SCHEDULED"
_OWN: Final = frozenset({_SENT, _DRAFT, _SCHEDULED})
"""Labels of a message the mailbox wrote: sent, a draft, or waiting in Scheduled (#278)."""
_UNSENT: Final = frozenset({_DRAFT, _SCHEDULED})
"""Labels that mean a message has not gone out, whatever else it carries (#278 review)."""


def _is_sent(message: Message) -> bool:
    """Sent, and neither a draft nor waiting in Scheduled (#278 review): Gmail may label a
    scheduled message ``SENT`` before it goes out."""
    return _SENT in message.label_ids and not message.label_ids & _UNSENT


class _NotSent(Exception):
    """Nothing was sent, and why, in one line fit for ``messages.error``. ``retry`` when
    the reason may pass (``not_sent``), not when a retry would change nothing (``failed``)."""

    def __init__(self, reason: str, *, retry: bool = False) -> None:
        super().__init__(reason)
        self.retry = retry


@dataclass(frozen=True, slots=True)
class _Threading:
    thread_id: str | None
    subject: str
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    known: tuple[str, ...] = ()  # every Gmail message id the thread held when it was read


@dataclass(frozen=True, slots=True)
class Found:
    """What a search by Message-ID found: the message, and its draft when it is one."""

    message: Message
    sent: bool
    draft_id: str | None = None
    scheduled: bool = False


def _message_id_query(rfc822_message_id: str, folder: str) -> str:
    """A search for one Message-ID in ``folder``. Raises :class:`ValueError` for a value
    that is not one ``<local@domain>`` Message-ID, so nothing else reaches the query."""
    if not is_message_id(rfc822_message_id):
        raise ValueError("not a Message-ID")
    return f"rfc822msgid:{rfc822_message_id.strip('<>')} in:{folder}"


def find_by_message_id(gmail: Gmail, rfc822_message_id: str, *, purpose: str) -> Found | None:
    """The message Gmail holds with this Message-ID, in any folder, or None.

    A message that is neither a draft nor waiting in Scheduled counts as sent,
    whatever else its labels say (a sent message the person moved to Trash is still
    sent). A draft's id is read from ``drafts.list``. A scheduled message (Gmail's
    Schedule send, #278) is found unsent, with no draft id. Raises :class:`GmailError`.
    """
    query = _message_id_query(rfc822_message_id, "anywhere")
    messages: list[Message] = []
    for ref in gmail.search(query, max_results=FIND_MAX, purpose=purpose):
        try:
            messages.append(gmail.get_message(ref.id, purpose=purpose))
        except GmailNotFound:  # deleted between the search and the read
            continue
    sent = sorted(
        (m for m in messages if not m.label_ids & _UNSENT),
        key=lambda m: m.internal_date,
    )
    if sent:
        return Found(sent[0], sent=True)
    if not messages:
        return None
    scheduled = [m for m in messages if _SCHEDULED in m.label_ids]
    if scheduled:
        return Found(scheduled[0], sent=False, scheduled=True)
    draft = messages[0]
    draft_id = next(
        (d.id for d in gmail.list_drafts(purpose=purpose) if d.message.id == draft.id), None
    )
    return Found(draft, sent=False, draft_id=draft_id)


def is_scheduled(gmail: Gmail, rfc822_message_id: str, *, purpose: str) -> bool:
    """Whether a message with this Message-ID waits in Gmail's Scheduled: a draft the
    person sent with Schedule send, not delivered yet (#278). Raises :class:`GmailError`
    and, for a value that is not a Message-ID, :class:`ValueError`."""
    query = _message_id_query(rfc822_message_id, "scheduled")
    return bool(gmail.search(query, max_results=1, purpose=purpose))


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
        replies_every: timedelta = replies.REPLY_POLL_EVERY,
    ) -> None:
        if drafts_every < timedelta(0) or replies_every < timedelta(0):
            raise ValueError("a poll interval cannot be negative")
        self._factory = factory
        self._open: GmailOpener = opener or (
            lambda user_id, mailbox_id: open_gmail(factory, user_id, mailbox_id)
        )
        self._clock = clock
        self._drafts_every = drafts_every
        self._drafts_polled: dict[int, datetime] = {}
        self._replies_every = replies_every
        self._replies_polled: dict[int, datetime] = {}
        # Per user, the reply poll's per-mailbox state (#413). In memory, like the gate
        # above: a restart's first tick polls every mailbox anyway.
        # Mailboxes polled again before the interval: one that did not catch up, or that
        # holds a follow-up because its replies are stale.
        self._replies_due: dict[int, set[int]] = {}
        # A mailbox whose poll failed: how many times in a row, and when to try again.
        self._replies_backoff: dict[tuple[int, int], tuple[int, datetime]] = {}
        # Mailboxes the last poll of them could not open, with the code (the poll status).
        self._replies_not_ready: dict[int, dict[int, str]] = {}
        # Mailboxes seen not ready whose poll has not caught up since: their follow-ups
        # that start a new conversation wait for it, however recent the last good poll.
        self._replies_catch_up: dict[int, set[int]] = {}
        self._replies_requested: dict[int, datetime] = {}
        self._label_ids: dict[tuple[int, str], str] = {}

    # --- what the poll status reads (#401) -------------------------------------------

    @property
    def drafts_every(self) -> timedelta:
        """How often the drafts poll runs, per user."""
        return self._drafts_every

    @property
    def replies_every(self) -> timedelta:
        """How often the reply poll runs, per user."""
        return self._replies_every

    def replies_polled_at(self, user_id: int) -> datetime | None:
        """When this process last started a reply poll of every armed mailbox of
        ``user_id``; None before the first. Read-only, like :meth:`drafts_polled_at`: this
        is the time :meth:`reconcile` measures the next full poll from. A mailbox in
        :meth:`replies_due` is polled again sooner, alone."""
        return self._replies_polled.get(user_id)

    def replies_due(self, user_id: int) -> frozenset[int]:
        """The mailboxes polled again without waiting for the interval (#413): one whose
        last poll stopped at the read budget or failed (after a backoff), one that was not
        ready (it needs signing in again), and one holding a follow-up because its replies
        are stale. Read-only."""
        return frozenset(self._replies_due.get(user_id, ()))

    def replies_not_ready(self, user_id: int) -> Mapping[int, str]:
        """The mailboxes the last poll of them could not open, with the reason code
        (``keychain_unavailable``, ``reauth_required``, ...). Read-only."""
        return dict(self._replies_not_ready.get(user_id, {}))

    def _poll_soon(self, user_id: int, mailbox_id: int) -> None:
        """Poll this one mailbox at the next tick, leaving the others on their interval."""
        self._replies_due.setdefault(user_id, set()).add(mailbox_id)

    def replies_poll_requested(self, user_id: int) -> bool:
        """Whether a person asked for ``user_id``'s reply poll on the next tick (#409) and
        that tick has not started it yet."""
        return user_id in self._replies_requested

    def replies_poll_requested_at(self, user_id: int) -> datetime | None:
        """When the waiting "Check now" was first asked for; None when none waits. The
        poll status shows it, so a request no tick has served yet can say so."""
        return self._replies_requested.get(user_id)

    def request_replies_poll(self, user_id: int) -> bool:
        """Poll ``user_id``'s replies on the next tick, not at the end of the interval:
        the "Check now" request (#409). True when this asked; False when a request was
        already waiting, so repeated presses within one tick collapse into one poll.

        It only sets a flag. The poll itself still runs in :meth:`_poll_replies`, in the
        campaign tick, with the same arming, mailbox readiness and read budget as every
        other poll; nothing here opens Gmail or a session. It leaves the send hold
        (:meth:`_replies_stale`, the catch-up set) alone."""
        if user_id in self._replies_requested:
            return False
        self._replies_requested[user_id] = self._clock()
        return True

    def drafts_polled_at(self, user_id: int) -> datetime | None:
        """When this process last ran ``user_id``'s drafts poll; None before the first.

        Read-only: the poll status (:mod:`netkeeper.services.poll_status`) reads it, and
        nothing it does moves the next poll. Kept only in memory, so a restart clears it
        and the next tick polls."""
        return self._drafts_polled.get(user_id)

    # --- arming (#277) ----------------------------------------------------------------

    def armed(self, session: Session, user: User, mailbox_id: int) -> MailboxArm | None:
        """The engine's gate (:class:`~netkeeper.services.campaign_engine.ArmGated`)."""
        return mailbox_service.armed(session, user, mailbox_id)

    def _disarmed_for(self, firing: Firing) -> str | None:
        """Why ``firing`` must not reach Gmail now, read fresh; None when it may."""
        if firing.mailbox_id is None:
            return None  # refused by _ready, with no Gmail call either
        with session_scope(self._factory) as session:
            user = session.get(User, firing.user_id)
            arm = None if user is None else self.armed(session, user, firing.mailbox_id)
        if arm is None:
            return "the mailbox is disarmed; nothing was sent"
        if firing.mode is not StepMode.DRAFT and arm is not MailboxArm.SEND:
            return "the mailbox is armed for drafts only; nothing was sent"
        return None

    def _replies_stale(self, firing: Firing) -> str | None:
        """Why a follow-up that starts a new conversation must wait: the reply poll has not
        caught up within :data:`~netkeeper.services.campaign_replies.STALE_AFTER_POLLS`
        intervals (#296 review). A ``same_thread`` follow-up reads its thread before it
        goes, so it needs no poll; a first step has nothing to be replied to yet."""
        if firing.step_position <= 1 or firing.mailbox_id is None:
            return None
        if firing.same_thread and firing.thread_id is not None:
            return None
        if firing.mailbox_id in self._replies_catch_up.get(firing.user_id, ()):
            # Not ready since its last good poll (#413): a reply may have come meanwhile.
            self._poll_soon(firing.user_id, firing.mailbox_id)
            return "replies have not been read since the mailbox was ready again; nothing was sent"
        with session_scope(self._factory) as session:
            user = session.get(User, firing.user_id)
            mailbox = (
                None if user is None else get_scoped(session, user, Mailbox, firing.mailbox_id)
            )
            polled = None if mailbox is None else mailbox.replies_polled_at
        limit = replies.STALE_AFTER_POLLS * self._replies_every
        if polled is not None and self._clock() - polled <= limit:
            return None
        self._poll_soon(firing.user_id, firing.mailbox_id)  # not the user's other mailboxes
        return "replies have not been polled recently; nothing was sent"

    # --- sending ---------------------------------------------------------------------

    def send(self, firing: Firing) -> SendResult:
        """Send or draft one firing. Never raises for a Gmail failure."""
        verb = "draft" if firing.mode is StepMode.DRAFT else "send"
        purpose = f"{verb} step {firing.step_position} for enrollment {firing.enrollment_id}"
        refused = self._disarmed_for(firing) or self._replies_stale(firing)
        if refused is not None:
            return _not_sent(firing, refused)
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
            return _not_sent(firing, str(exc)) if exc.retry else _failed(firing, str(exc))
        except ComposeError as exc:
            return _failed(firing, f"the message could not be built: {exc}")
        if self._replied(firing):
            # A reply on any channel (the LinkedIn inbox poll writes in its own session)
            # landed after the claim's reply check: nothing goes out, no label is made,
            # and the retry's claim reads the reply and ends the enrollment (#416 review).
            return _not_sent(firing, "a reply arrived after the claim; nothing was sent")
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
            return _not_sent(firing, f"Gmail was unavailable ({exc.code}); nothing was sent")
        except (GmailRateLimited, GmailAuthError) as exc:
            return _not_sent(firing, f"Gmail said {type(exc).__name__} ({exc.code})")
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
                thread_known=tuple(k for k in threading_.known if k != ref.id),
            )
        at = self._clock()
        self._apply_label(gmail, ref.id, label_id, purpose)
        log.info("message %d sent for enrollment %d", firing.message_id, firing.enrollment_id)
        return SendResult(
            SendOutcome.SENT, at=at, gmail_message_id=ref.id, gmail_thread_id=ref.thread_id
        )

    def _replied(self, firing: Firing) -> bool:
        """Whether the enrollment holds a reply now, read just before Gmail is asked to
        send or draft. Read-only."""
        with session_scope(self._factory) as session:
            user = session.get(User, firing.user_id)
            if user is None:
                return False
            return engine._reply_at(session, user, firing.enrollment_id) is not None

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
            raise _NotSent(
                f"the mailbox is not ready ({exc.code}); nothing was sent", retry=True
            ) from exc
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
        except GmailRejected as exc:
            raise _NotSent(
                f"Gmail refused to read the earlier step's thread ({exc.code}); nothing was sent"
            ) from exc
        except GmailError as exc:
            raise _NotSent(
                f"the earlier step's thread could not be read ({exc.code}); nothing was sent",
                retry=True,
            ) from exc
        earlier = [m for m in thread.messages if _is_sent(m)]
        cited = [
            value.strip()
            for m in earlier
            if (value := m.header("Message-ID")) is not None and is_message_id(value.strip())
        ]
        if not cited:
            raise _NotSent("the earlier step is not in its Gmail thread; nothing was sent")
        if any(
            not m.label_ids & _OWN
            and m.internal_date >= earlier[0].internal_date
            and not _automatic(m)
            for m in thread.messages
        ):
            # A reply (or a bounce notice) landed after the claim's reply check (P3-08):
            # nothing goes out, and the next tick's reply poll records it before the
            # retry is claimed, so the claim ends the enrollment instead.
            if firing.mailbox_id is not None:  # always, past _ready
                self._poll_soon(firing.user_id, firing.mailbox_id)
            raise _NotSent(
                "a message from the other side is in the thread; nothing was sent", retry=True
            )
        return _Threading(
            thread_id=thread.id,
            subject=reply_subject(earlier[0].header("Subject")),
            in_reply_to=cited[-1],
            references=tuple(cited),
            known=tuple(m.id for m in thread.messages),
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
                thread_known=_others_in_thread(gmail, ref.id, ref.thread_id, purpose),
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
        """Look up the user's leftovers and drafts in Gmail (the module)."""
        with session_scope(factory) as session:
            user = session.get(User, user_id)
            if user is None:
                return
            work = engine.reconcile_work(session, user, now=now)
            # A disarmed mailbox gets no Gmail call: what it holds waits for it (#277).
            unverified: dict[int, bool] = {}
            for mailbox_id in work.mailboxes():
                mailbox = get_scoped(session, user, Mailbox, mailbox_id)
                if mailbox is not None and mailbox.arm is not None:
                    unverified[mailbox_id] = mailbox.message_id_verified_at is None
            test_drafts = review.test_drafts_to_verify(session, user)
        last = self._drafts_polled.get(user_id)
        poll = last is None or now - last >= self._drafts_every
        drafts = work.drafts if poll else ()
        checks = test_drafts if poll else ()
        if drafts or checks:
            self._drafts_polled[user_id] = now
        run = _Reconcile(self, factory, user_id, settings, now)
        # Test drafts first (#304): a mailbox verified by one skips the campaign draft's
        # search below. Before any campaign is active, they are the only drafts there are.
        for mailbox_id in sorted({c.mailbox_id for c in checks}):
            try:
                gmail = self._open(user_id, mailbox_id)
            except (MailboxNotReady, MailboxNotFound) as exc:
                log.info(
                    "mailbox %d is not ready; the Message-ID check waits (%s)", mailbox_id, exc
                )
                continue
            mine = [c for c in checks if c.mailbox_id == mailbox_id]
            if run.verify_test_drafts(gmail, mailbox_id, mine) and mailbox_id in unverified:
                unverified[mailbox_id] = False
        for mailbox_id in sorted({t.mailbox_id for t in (*work.leftovers, *drafts)}):
            if mailbox_id not in unverified:
                continue
            try:
                gmail = self._open(user_id, mailbox_id)
            except (MailboxNotReady, MailboxNotFound) as exc:
                log.info("mailbox %d is not ready; reconciling waits (%s)", mailbox_id, exc)
                continue
            run.mailbox(
                gmail,
                [t for t in work.leftovers if t.mailbox_id == mailbox_id],
                [t for t in drafts if t.mailbox_id == mailbox_id],
                verify=unverified[mailbox_id],
            )
        self._poll_replies(factory, user_id, now)

    def _poll_replies(self, factory: sessionmaker[Session], user_id: int, now: datetime) -> None:
        """Every armed mailbox once an interval; between, only the mailboxes due again
        (#413). A mailbox that stopped part way, or is not ready, is polled again alone at
        the next tick, so it never makes the healthy ones poll every minute.

        A person's "Check now" (#409) makes this tick's poll a full one: every armed
        mailbox, whatever its backoff, and the due set starts over from what it finds."""
        last = self._replies_polled.get(user_id)
        due = self._replies_due.get(user_id, set())
        only: frozenset[int] | None = None
        requested = user_id in self._replies_requested
        if requested or last is None or now - last >= self._replies_every:
            # Cleared before any mailbox is polled: a press during the poll asks for
            # another. A mailbox that is not ready goes back in the due set, not here.
            self._replies_requested.pop(user_id, None)
            self._replies_polled[user_id] = now
        else:
            only = frozenset(m for m in due if self._retry_at(user_id, m) <= now)
            if not only:
                return
        polled = replies.poll_replies(
            factory, user_id, open_gmail=self._open, now=now, label=self._label_reply, only=only
        )
        # A due mailbox still in its backoff was not polled: it stays due.
        waiting = set() if only is None else due - only
        self._replies_due[user_id] = waiting | polled.retry
        self._settle(user_id, polled, only=only, now=now)

    def _retry_at(self, user_id: int, mailbox_id: int) -> datetime:
        backoff = self._replies_backoff.get((user_id, mailbox_id))
        return datetime.min.replace(tzinfo=UTC) if backoff is None else backoff[1]

    def _settle(
        self,
        user_id: int,
        polled: replies.RepliesPolled,
        *,
        only: frozenset[int] | None,
        now: datetime,
    ) -> None:
        """Record how each polled mailbox ended (#413): a failed one backs off 1, 2, 4, ...
        minutes, never longer than the interval, and starts over once one reads; one not
        ready must catch up before it starts a new conversation again."""
        for mailbox_id in polled.failed:
            failures = self._replies_backoff.get((user_id, mailbox_id), (0, now))[0] + 1
            # The full poll each interval reads it anyway; the cap keeps the next due time
            # honest.
            wait = min(REPLY_BACKOFF_FIRST * 2 ** min(failures - 1, 16), self._replies_every)
            self._replies_backoff[(user_id, mailbox_id)] = (failures, now + wait)
        # Read, or not ready: a not-ready mailbox is tried at every tick instead, so it is
        # polled the minute after it is signed in again or unlocked.
        for mailbox_id in polled.caught_up | polled.behind | set(polled.not_ready):
            self._replies_backoff.pop((user_id, mailbox_id), None)
        not_ready = self._replies_not_ready.setdefault(user_id, {})
        for mailbox_id in [m for m in not_ready if only is None or m in only]:
            del not_ready[mailbox_id]
        not_ready.update(polled.not_ready)
        catch_up = self._replies_catch_up.setdefault(user_id, set())
        catch_up |= set(polled.not_ready)
        catch_up -= polled.caught_up

    def _label_reply(self, gmail: Gmail, mailbox_id: int, name: str, gmail_message_id: str) -> None:
        purpose = f"label a reply in mailbox {mailbox_id}"
        label_id = self._label_named(gmail, mailbox_id, name, purpose)
        self._apply_label(gmail, gmail_message_id, label_id, purpose)


class _Reconcile:
    """One user's reconcile pass: each lookup in Gmail, then its own short writer session."""

    def __init__(
        self,
        sender: GmailSender,
        factory: sessionmaker[Session],
        user_id: int,
        settings: Settings,
        now: datetime,
    ) -> None:
        self.sender = sender
        self.factory = factory
        self.user_id = user_id
        self.settings = settings
        self.now = now

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
        *,
        verify: bool = False,
    ) -> None:
        for tracked in leftovers:
            self.guarded(tracked, self.leftover, gmail, tracked)
        if not drafts:
            return
        if verify:
            oldest = min(drafts, key=lambda t: t.message_id)
            self.guarded(oldest, self.verify, gmail, oldest)
        try:
            listed = gmail.list_drafts(purpose=f"drafts poll for user {self.user_id}")
        except GmailError as exc:
            log.warning("the drafts poll could not list drafts (%s); it waits", exc.code)
            return
        present = {d.id: d for d in listed}
        by_message = {d.message.id: d for d in listed}
        for tracked in drafts:
            self.guarded(tracked, self.draft, gmail, tracked, present, by_message)

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
        settings, now = self.settings, self.now
        if found is None:
            self.write(
                lambda s, u: engine.settle_not_sent(s, u, settings, tracked.message_id, now=now)
            )
            return
        ref = found.message
        if found.sent:
            at = engine.seen_sent_at(tracked, ref.internal_date)
            self.write(
                lambda s, u: _settled_sent(
                    s,
                    u,
                    engine.settle_sent(
                        s,
                        u,
                        settings,
                        tracked.message_id,
                        expect=(MessageStatus.SCHEDULED,),
                        at=at,
                        gmail_message_id=ref.id,
                        gmail_thread_id=ref.thread_id,
                    ),
                    tracked,
                    now=self.now,
                )
            )
            self.label(gmail, tracked, ref.id, purpose)
        elif found.draft_id is not None:
            draft_id = found.draft_id
            known = _others_in_thread(gmail, ref.id, ref.thread_id, purpose)
            self.write(
                lambda s, u: engine.settle_drafted(
                    s,
                    u,
                    settings,
                    tracked.message_id,
                    gmail_message_id=ref.id,
                    gmail_thread_id=ref.thread_id,
                    gmail_draft_id=draft_id,
                    thread_known=known,
                )
            )
        elif found.scheduled:
            log.info("message %d waits in Gmail's Scheduled; it waits", tracked.message_id)
            self.write(lambda s, u: engine.settle_seen_scheduled(s, u, tracked.message_id, now=now))
        else:
            log.info("message %d is a Gmail draft not listed yet; it waits", tracked.message_id)

    def verify(self, gmail: Gmail, tracked: Tracked) -> None:
        """Search for a draft by the Message-ID netkeeper set; found, the mailbox is
        verified and can be armed for send (#277). Not found is not a failure: Gmail's
        search can lag, and the next poll tries again."""
        purpose = self.purpose(tracked, "Message-ID check of")
        if find_by_message_id(gmail, tracked.rfc822_message_id, purpose=purpose) is None:
            log.info(
                "mailbox %d: draft message %d not found by its Message-ID yet",
                tracked.mailbox_id,
                tracked.message_id,
            )
            return
        now, mailbox_id = self.now, tracked.mailbox_id
        self.write(
            lambda s, u: mailbox_service.record_message_id_verified(s, u, mailbox_id, now=now)
        )

    def verify_test_drafts(
        self, gmail: Gmail, mailbox_id: int, checks: Collection[review.TestDraftCheck]
    ) -> bool:
        """Search for the mailbox's test drafts by Message-ID, newest first, until one is
        found; found, the mailbox is verified (#304). True when one was found. Not found,
        or a Gmail error, is not a failure: the next poll tries again. None found is
        recorded on the test drafts (``not_found_at``), for arming's refusal to show; a
        Gmail error records nothing."""
        found = None
        for check in checks:
            purpose = f"Message-ID check of test draft {check.test_send_id}"
            try:
                found = find_by_message_id(gmail, check.rfc822_message_id, purpose=purpose)
            except GmailError as exc:
                log.warning(
                    "test draft %d could not be looked up in Gmail (%s); it waits",
                    check.test_send_id,
                    exc.code,
                )
                return False
            if found is not None:
                break
        now = self.now
        if found is None:
            log.info("mailbox %d: no test draft found by its Message-ID yet", mailbox_id)
            ids = [c.test_send_id for c in checks]
            self.write(lambda s, u: review.record_test_drafts_not_found(s, u, ids, now=now))
            return False
        self.write(
            lambda s, u: mailbox_service.record_message_id_verified(s, u, mailbox_id, now=now)
        )
        return True

    def draft(
        self,
        gmail: Gmail,
        tracked: Tracked,
        present: Mapping[str, Draft],
        by_message: Mapping[str, Draft],
    ) -> None:
        if tracked.gmail_draft_id in present:
            if tracked.marked_missing or tracked.seen_scheduled:
                self.write(lambda s, u: engine.settle_draft_present(s, u, tracked.message_id))
            return
        purpose = self.purpose(tracked, "drafts poll for")
        sent = self.sent_in_thread(gmail, tracked, purpose)
        if sent is None:
            self.not_seen_sent(gmail, tracked, by_message, purpose)
            return
        settings, at = self.settings, engine.seen_sent_at(tracked, sent.internal_date)
        self.write(
            lambda s, u: _settled_sent(
                s,
                u,
                engine.settle_sent(
                    s,
                    u,
                    settings,
                    tracked.message_id,
                    expect=(MessageStatus.DRAFTED,),
                    at=at,
                    gmail_message_id=sent.id,
                    gmail_thread_id=sent.thread_id,
                ),
                tracked,
                now=self.now,
            )
        )
        self.label(gmail, tracked, sent.id, purpose)

    def not_seen_sent(
        self, gmail: Gmail, tracked: Tracked, by_message: Mapping[str, Draft], purpose: str
    ) -> None:
        """A draft gone from ``drafts.list`` with nothing sent in its thread (#278).

        Schedule send moves a draft into Scheduled before anything is sent: found there,
        it stays ``drafted`` until it is seen sent. A canceled Schedule send puts it
        back in Drafts, possibly under a new draft id: found there, it is followed under
        that id. Only a draft in neither is marked missing, then discarded. A failed
        search raises, and the guard leaves the message as it is.
        """
        message_id, now = tracked.message_id, self.now
        if is_scheduled(gmail, tracked.rfc822_message_id, purpose=purpose):
            log.info(
                "message %d: its draft waits in Gmail's Scheduled; it stays drafted", message_id
            )
            # Stamped every time: the last sighting dates the send (#278 review).
            self.write(lambda s, u: engine.settle_seen_scheduled(s, u, message_id, now=now))
            return
        query = _message_id_query(tracked.rfc822_message_id, "drafts")
        refs = gmail.search(query, max_results=FIND_MAX, purpose=purpose)
        moved = next((by_message[r.id] for r in refs if r.id in by_message), None)
        if moved is not None:
            self.write(
                lambda s, u: engine.settle_draft_moved(
                    s,
                    u,
                    message_id,
                    gmail_draft_id=moved.id,
                    gmail_message_id=moved.message.id,
                    gmail_thread_id=moved.message.thread_id,
                )
            )
            return
        if refs:  # in Drafts, but not in the listing read just before: it waits
            log.info("message %d: its draft is in Drafts but not listed yet; it waits", message_id)
            return
        self.write(lambda s, u: engine.settle_draft_missing(s, u, message_id))

    @staticmethod
    def sent_in_thread(gmail: Gmail, tracked: Tracked, purpose: str) -> Message | None:
        """The draft, sent: the first sent message in its thread that could be it. None
        when the thread has none, or is gone.

        A sent message that kept the draft's own message id is the draft. Any other
        must be no older than the draft's claim and not one of ``thread_known``: an
        earlier step, a message another row of the user holds, or one the thread
        held when the draft was made (#273 review). What is left is a message the
        person sent in the thread after the draft was made, which a sent draft with a
        new id cannot be told apart from.
        """
        if tracked.gmail_thread_id is None:
            return None
        try:
            thread = gmail.get_thread(tracked.gmail_thread_id, purpose=purpose)
        except GmailNotFound:
            return None
        sent = [m for m in thread.messages if _is_sent(m)]
        kept = [m for m in sent if m.id == tracked.gmail_message_id]
        if kept:
            return kept[0]
        since = tracked.scheduled_at
        candidates = [
            m
            for m in sent
            if m.id not in tracked.thread_known and since is not None and m.internal_date >= since
        ]
        return min(candidates, key=lambda m: m.internal_date) if candidates else None

    def label(self, gmail: Gmail, tracked: Tracked, gmail_message_id: str, purpose: str) -> None:
        label_id = self.sender._label_named(gmail, tracked.mailbox_id, tracked.label, purpose)
        self.sender._apply_label(gmail, gmail_message_id, label_id, purpose)


def _others_in_thread(
    gmail: Gmail, gmail_message_id: str, thread_id: str, purpose: str
) -> tuple[str, ...]:
    """Every other Gmail message id in the thread, or none when it cannot be read. For a
    draft found by its Message-ID: what the thread held when it was seen."""
    try:
        thread = gmail.get_thread(thread_id, purpose=purpose)
    except GmailError as exc:
        log.info("the draft's thread could not be read (%s); nothing is known in it", exc.code)
        return ()
    return tuple(m.id for m in thread.messages if m.id != gmail_message_id)


def _automatic(message: Message) -> bool:
    """An out-of-office answer, or a mail system's notice that is not a hard bounce: neither
    holds a follow-up (#296 review). A hard bounce does, until the poll records it."""
    if replies.is_daemon(message):
        return not replies.is_hard_bounce(message)
    return replies.is_auto_reply(message)


def _not_sent(firing: Firing, reason: str) -> SendResult:
    log.warning("message %d sent nothing, for now: %s", firing.message_id, reason)
    return SendResult(SendOutcome.NOT_SENT, error=reason)


def _failed(firing: Firing, reason: str) -> SendResult:
    log.warning("message %d not sent: %s", firing.message_id, reason)
    return SendResult(SendOutcome.FAILED, error=reason)


def _settled_sent(
    session: Session, user: User, settled: bool, tracked: Tracked, *, now: datetime
) -> None:
    """After Gmail showed a message sent: an answer the LinkedIn inbox poll recorded
    before this send was known is the enrollment's reply (P4-02, #381), recorded before
    the next claim can fire the step this just scheduled."""
    if not settled:
        return
    # A merge may have moved the message to another enrollment meanwhile: follow it.
    message = get_scoped(session, user, MessageRow, tracked.message_id)
    if message is None:
        return
    try:
        # A savepoint: a failure here takes back only the catch-up, never the settle.
        with session.begin_nested():
            replies.catch_up_linkedin_replies(session, user, message.enrollment_id, now=now)
    except Exception as exc:
        # The settle stands (Gmail did send it); an earlier LinkedIn answer stays
        # unrecorded, so this is an error for a person. By type alone: rows carry text.
        log.error(
            "message %d: recording earlier LinkedIn replies failed (%s)",
            tracked.message_id,
            type(exc).__name__,
        )
