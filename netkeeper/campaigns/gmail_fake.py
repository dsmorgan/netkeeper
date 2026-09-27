"""An in-memory Gmail with :class:`~netkeeper.campaigns.gmail.Gmail`'s interface (item P3-02).

Every engine test runs against :class:`FakeGmail` (P3-06 to P3-08), so it keeps
the state those items read, and behaves as Gmail does where the engine could
get it wrong:

- **Threading.** A message sent or drafted with ``thread_id`` joins that thread
  only when its ``In-Reply-To`` or ``References`` names a message in the thread
  *and* its subject matches the thread's once ``Re:`` and ``Fwd:`` are stripped.
  Otherwise it starts a new thread, silently, as Gmail does. An unknown
  ``thread_id`` is :class:`GmailNotFound`. An inbound message (:meth:`FakeGmail.deliver`)
  joins the thread its headers and subject point to, the same way.
- **Ids.** A thread's id is its first message's id. Sending a draft (the
  person's part, :meth:`FakeGmail.send_draft`) removes the draft and adds a
  ``SENT`` message with a new id in the same thread.
- **History.** One ``history_id`` that only grows: every added message, label
  change and draft moves it on, and each message carries the id of its last
  change. :meth:`FakeGmail.history` returns the messages added after a start,
  in order, and :class:`GmailNotFound` for a start older than
  :meth:`FakeGmail.forget_history` left.
- **Headers.** A message without ``Message-ID`` gets one, and one without
  ``From`` gets the mailbox's address, as Gmail fills them in. A message with no
  recipient is :class:`GmailRejected`.
- **Search.** ``from:``, ``to:``, ``rfc822msgid:``, ``in:`` (``inbox``, ``sent``,
  ``drafts``, ``anywhere``), ``label:``, and ``after:``/``before:`` with epoch
  seconds only (Gmail reads a ``YYYY/MM/DD`` date in Pacific time, a trap for
  the engine). Terms are ANDed. Anything else raises :class:`ValueError` rather
  than matching everything, so a test cannot pass on a query Gmail would read
  differently.

Nothing here is sent anywhere. A test scripts failures with
:meth:`FakeGmail.fail_next`, and reads :attr:`FakeGmail.calls` for the methods
and purposes the engine used.
"""

from __future__ import annotations

import email
import itertools
import logging
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import getaddresses, make_msgid
from typing import Final

from netkeeper.campaigns.gmail import (
    METADATA_HEADERS,
    Draft,
    GmailConflict,
    GmailError,
    GmailNotFound,
    GmailRejected,
    History,
    Label,
    Message,
    MessageRef,
    Profile,
    Thread,
    check_purpose,
    log_call,
)
from netkeeper.models.base import utcnow

log = logging.getLogger(__name__)

#: Gmail's system labels the engine reads.
SYSTEM_LABELS: Final = ("INBOX", "SENT", "DRAFT", "UNREAD", "SPAM", "TRASH", "IMPORTANT")

#: The address Gmail's bounce notices come from (spec 11.5).
MAILER_DAEMON: Final = "mailer-daemon@googlemail.com"

_PREFIX: Final = re.compile(r"^\s*(?:re|fwd?|aw)\s*:\s*", re.IGNORECASE)
_MSGID: Final = re.compile(r"<[^<>\s]+>")
_IN_FOLDERS: Final = {"inbox": "INBOX", "sent": "SENT", "drafts": "DRAFT"}


def normalize_subject(subject: str | None) -> str:
    """``subject`` without its ``Re:``/``Fwd:`` prefixes, folded, as threading compares it."""
    text = subject or ""
    while True:
        stripped = _PREFIX.sub("", text, count=1)
        if stripped == text:
            return " ".join(text.split()).casefold()
        text = stripped


@dataclass
class _Stored:
    id: str
    thread_id: str
    labels: set[str]
    history_id: int
    internal_date: datetime
    parsed: EmailMessage = field(repr=False)

    def header(self, name: str) -> str | None:
        value = self.parsed.get(name)
        return None if value is None else str(value)

    def message_id(self) -> str:
        return (self.header("Message-ID") or "").strip()


class FakeGmail:
    """An in-memory mailbox for ``address``. See the module docstring for the rules."""

    def __init__(
        self,
        address: str = "sender@example.com",
        *,
        mailbox_id: int = 0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.address = address.lower()
        self.mailbox_id = mailbox_id
        self.clock = clock
        #: ``(method, purpose)`` for every call, in order.
        self.calls: list[tuple[str, str]] = []
        self._messages: dict[str, _Stored] = {}
        self._threads: dict[str, list[str]] = {}
        self._drafts: dict[str, str] = {}  # draft id -> message id
        self._labels: dict[str, Label] = {
            name: Label(id=name, name=name, type="system") for name in SYSTEM_LABELS
        }
        self._history: list[tuple[int, str]] = []  # (history id, message id added)
        self._history_id = 1000
        self._history_floor = 0
        self._ids = itertools.count(0x18F0000000000000)
        self._drafts_made = itertools.count(1)
        self._labels_made = itertools.count(1)
        self._failures: dict[str, list[GmailError]] = {}

    # --- the Gmail interface ----------------------------------------------------------

    def profile(self, *, purpose: str) -> Profile:
        self._call("users.getProfile", purpose)
        return Profile(email=self.address, history_id=self._history_id)

    def send(
        self, message: EmailMessage, *, thread_id: str | None = None, purpose: str
    ) -> MessageRef:
        self._call("messages.send", purpose)
        stored = self._add(message, thread_id=thread_id, labels={"SENT"}, outbound=True)
        return MessageRef(stored.id, stored.thread_id)

    def create_draft(
        self, message: EmailMessage, *, thread_id: str | None = None, purpose: str
    ) -> Draft:
        self._call("drafts.create", purpose)
        stored = self._add(message, thread_id=thread_id, labels={"DRAFT"}, outbound=True)
        draft_id = f"r{next(self._drafts_made)}"
        self._drafts[draft_id] = stored.id
        return Draft(id=draft_id, message=MessageRef(stored.id, stored.thread_id))

    def get_draft(self, draft_id: str, *, purpose: str) -> Draft:
        self._call("drafts.get", purpose)
        message_id = self._drafts.get(draft_id)
        if message_id is None:
            raise GmailNotFound("no such draft", code="notFound")
        stored = self._messages[message_id]
        return Draft(id=draft_id, message=MessageRef(stored.id, stored.thread_id))

    def get_message(self, message_id: str, *, purpose: str) -> Message:
        self._call("messages.get", purpose)
        return self._view(self._get(message_id))

    def get_thread(self, thread_id: str, *, purpose: str) -> Thread:
        self._call("threads.get", purpose)
        ids = self._threads.get(thread_id)
        if not ids:
            raise GmailNotFound("no such thread", code="notFound")
        messages = tuple(self._view(self._messages[message_id]) for message_id in ids)
        return Thread(
            id=thread_id,
            history_id=max(message.history_id for message in messages),
            messages=messages,
        )

    def search(self, query: str, *, max_results: int = 100, purpose: str) -> list[MessageRef]:
        self._call("messages.list", purpose)
        if max_results < 1:
            raise ValueError("max_results must be at least 1")
        tests = [self._term(term) for term in shlex.split(query)]
        hits = [
            stored
            for stored in self._messages.values()
            if not stored.labels & {"SPAM", "TRASH"} and all(test(stored) for test in tests)
        ]
        hits.sort(key=lambda stored: (stored.internal_date, stored.id), reverse=True)
        return [MessageRef(stored.id, stored.thread_id) for stored in hits[:max_results]]

    def list_labels(self, *, purpose: str) -> list[Label]:
        self._call("labels.list", purpose)
        return list(self._labels.values())

    def create_label(self, name: str, *, purpose: str) -> Label:
        self._call("labels.create", purpose)
        if not name.strip():
            raise GmailRejected("a label needs a name", code="invalidArgument")
        if any(label.name.lower() == name.lower() for label in self._labels.values()):
            raise GmailConflict("Label name exists or conflicts", code="conflict")
        label = Label(id=f"Label_{next(self._labels_made)}", name=name, type="user")
        self._labels[label.id] = label
        return label

    def modify_labels(
        self,
        message_id: str,
        *,
        add: Sequence[str] = (),
        remove: Sequence[str] = (),
        purpose: str,
    ) -> None:
        self._call("messages.modify", purpose)
        stored = self._get(message_id)
        unknown = [label for label in (*add, *remove) if label not in self._labels]
        if unknown:
            raise GmailRejected("Invalid label", code="invalidArgument")
        stored.labels |= set(add)
        stored.labels -= set(remove)
        stored.history_id = self._bump()

    def history(
        self, start_history_id: int, *, label_id: str | None = None, purpose: str
    ) -> History:
        self._call("history.list", purpose)
        if start_history_id < self._history_floor:
            raise GmailNotFound("Requested entity was not found.", code="notFound")
        added: list[MessageRef] = []
        for history_id, message_id in self._history:
            stored = self._messages.get(message_id)
            if history_id <= start_history_id or stored is None:
                continue
            if label_id is not None and label_id not in stored.labels:
                continue
            added.append(MessageRef(stored.id, stored.thread_id))
        return History(history_id=self._history_id, messages_added=tuple(added))

    # --- the other side: people and servers ------------------------------------------

    def deliver(
        self,
        message: EmailMessage,
        *,
        at: datetime | None = None,
        labels: Sequence[str] = ("INBOX", "UNREAD"),
    ) -> MessageRef:
        """Mail arriving from someone else, threaded by its headers and subject."""
        stored = self._add(message, thread_id=None, labels=set(labels), outbound=False, at=at)
        return MessageRef(stored.id, stored.thread_id)

    def reply(
        self,
        to: MessageRef,
        *,
        sender: str,
        body: str = "Thanks, happy to talk.",
        at: datetime | None = None,
    ) -> MessageRef:
        """``sender`` replying to the message ``to``, in its thread."""
        original = self._get(to.id)
        message = EmailMessage()
        message["From"] = sender
        message["To"] = self.address
        message["Subject"] = f"Re: {original.header('Subject') or ''}"
        message["In-Reply-To"] = original.message_id()
        message["References"] = original.message_id()
        message.set_content(body)
        return self.deliver(message, at=at)

    def bounce(self, to: MessageRef, *, at: datetime | None = None) -> MessageRef:
        """Gmail's delivery-failure notice for the message ``to``, in its thread."""
        original = self._get(to.id)
        message = EmailMessage()
        message["From"] = f"Mail Delivery Subsystem <{MAILER_DAEMON}>"
        message["To"] = self.address
        message["Subject"] = f"Delivery Status Notification (Failure) {original.header('Subject')}"
        message["In-Reply-To"] = original.message_id()
        message["References"] = original.message_id()
        message["Auto-Submitted"] = "auto-replied"
        message.set_content("Address not found. Your message wasn't delivered.")
        stored = self._add(
            message, thread_id=None, labels={"INBOX", "UNREAD"}, outbound=False, at=at
        )
        # Gmail files the notice with the message that bounced, whatever its subject.
        self._move(stored, original.thread_id)
        return MessageRef(stored.id, stored.thread_id)

    def send_draft(self, draft_id: str, *, at: datetime | None = None) -> MessageRef:
        """The person pressing Send on a draft: the draft goes, a ``SENT`` message
        with a new id takes its place in the same thread."""
        message_id = self._drafts.pop(draft_id)
        draft = self._messages.pop(message_id)
        self._threads[draft.thread_id].remove(message_id)
        sent = _Stored(
            id=self._new_id(),
            thread_id=draft.thread_id,
            labels={"SENT"},
            history_id=self._bump(),
            internal_date=at or self.clock(),
            parsed=draft.parsed,
        )
        self._store(sent)
        return MessageRef(sent.id, sent.thread_id)

    def discard_draft(self, draft_id: str) -> None:
        """The person deleting a draft instead of sending it."""
        message_id = self._drafts.pop(draft_id)
        stored = self._messages.pop(message_id)
        self._threads[stored.thread_id].remove(message_id)
        if not self._threads[stored.thread_id]:
            del self._threads[stored.thread_id]
        self._bump()

    def forget_history(self) -> None:
        """Gmail dropping old history: any start before now is :class:`GmailNotFound`."""
        self._history_floor = self._history_id

    def fail_next(self, method: str, error: GmailError) -> None:
        """The next call to ``method`` (``"messages.send"``, ``"history.list"``...)
        raises ``error`` instead of doing anything. Queued, first in first out."""
        self._failures.setdefault(method, []).append(error)

    # --- reading the state (tests only) ----------------------------------------------

    @property
    def history_id(self) -> int:
        return self._history_id

    def drafts(self) -> dict[str, MessageRef]:
        return {
            draft_id: MessageRef(message_id, self._messages[message_id].thread_id)
            for draft_id, message_id in self._drafts.items()
        }

    def sent(self) -> list[Message]:
        """Every ``SENT`` message, oldest first."""
        return [self._view(stored) for stored in self._messages.values() if "SENT" in stored.labels]

    def raw(self, message_id: str) -> EmailMessage:
        """The whole message as it was stored, body included."""
        return self._get(message_id).parsed

    def thread_ids(self) -> list[str]:
        return list(self._threads)

    # --- plumbing --------------------------------------------------------------------

    def _call(self, method: str, purpose: str) -> None:
        checked = check_purpose(purpose)
        log_call(method, self.mailbox_id, checked)
        self.calls.append((method, checked))
        queued = self._failures.get(method)
        if queued:
            raise queued.pop(0)

    def _bump(self) -> int:
        self._history_id += 1
        return self._history_id

    def _new_id(self) -> str:
        return f"{next(self._ids):016x}"

    def _get(self, message_id: str) -> _Stored:
        stored = self._messages.get(message_id)
        if stored is None:
            raise GmailNotFound("no such message", code="notFound")
        return stored

    def _add(
        self,
        message: EmailMessage,
        *,
        thread_id: str | None,
        labels: set[str],
        outbound: bool,
        at: datetime | None = None,
    ) -> _Stored:
        # A round trip through bytes, as the real client sends it, so a message
        # that cannot be serialized fails here too.
        parsed = email.message_from_bytes(message.as_bytes(policy=SMTP), policy=SMTP)
        assert isinstance(parsed, EmailMessage)
        if outbound:
            if not any(parsed.get(name) for name in ("To", "Cc", "Bcc")):
                raise GmailRejected("Recipient address required", code="invalidArgument")
            if parsed.get("From") is None:
                parsed["From"] = self.address
        if parsed.get("Message-ID") is None:
            parsed["Message-ID"] = make_msgid(domain="mail.gmail.com")
        if thread_id is not None and thread_id not in self._threads:
            raise GmailNotFound("Requested entity was not found.", code="notFound")
        message_id = self._new_id()
        stored = _Stored(
            id=message_id,
            thread_id=self._thread_for(parsed, thread_id, outbound=outbound) or message_id,
            labels=labels,
            history_id=self._bump(),
            internal_date=at or self.clock(),
            parsed=parsed,
        )
        self._store(stored)
        return stored

    def _store(self, stored: _Stored) -> None:
        self._messages[stored.id] = stored
        self._threads.setdefault(stored.thread_id, []).append(stored.id)
        self._history.append((stored.history_id, stored.id))

    def _move(self, stored: _Stored, thread_id: str) -> None:
        self._threads[stored.thread_id].remove(stored.id)
        if not self._threads[stored.thread_id]:
            del self._threads[stored.thread_id]
        stored.thread_id = thread_id
        self._threads[thread_id].append(stored.id)

    def _thread_for(
        self, parsed: EmailMessage, thread_id: str | None, *, outbound: bool
    ) -> str | None:
        """The thread ``parsed`` joins, or None for a new one (the module docstring's rule)."""
        cited = set(
            _MSGID.findall(f"{parsed.get('In-Reply-To', '')} {parsed.get('References', '')}")
        )
        if not cited:
            return None
        subject = normalize_subject(parsed.get("Subject"))
        candidates = [thread_id] if outbound else list(self._threads)
        for candidate in candidates:
            if candidate is None:
                continue
            members = [self._messages[message_id] for message_id in self._threads[candidate]]
            if not any(member.message_id() in cited for member in members):
                continue
            if normalize_subject(members[0].header("Subject")) == subject:
                return candidate
        return None

    def _view(self, stored: _Stored) -> Message:
        headers = tuple(
            (name, str(value))
            for name in METADATA_HEADERS
            for value in (stored.parsed.get_all(name) or [])
        )
        return Message(
            id=stored.id,
            thread_id=stored.thread_id,
            label_ids=frozenset(stored.labels),
            history_id=stored.history_id,
            internal_date=stored.internal_date,
            snippet=_snippet(stored.parsed),
            headers=headers,
        )

    def _term(self, term: str) -> Callable[[_Stored], bool]:
        key, sep, value = term.partition(":")
        key = key.lower()
        if not sep or not value:
            raise ValueError(f"the fake does not search for {term!r}; use an operator it knows")
        if key in {"from", "to"}:
            wanted = value.lower()
            names = ("From",) if key == "from" else ("To", "Cc", "Bcc")
            return lambda stored: any(
                wanted in f"{name} {address}".lower()
                for header in names
                for name, address in getaddresses(
                    [str(v) for v in stored.parsed.get_all(header) or []]
                )
            )
        if key == "rfc822msgid":
            wanted = value.strip("<>")
            return lambda stored: stored.message_id().strip("<>") == wanted
        if key in {"after", "before"}:
            if not value.isdigit():
                raise ValueError(
                    f"{key}: takes epoch seconds here; Gmail reads a date in Pacific time"
                )
            bound = datetime.fromtimestamp(int(value), tz=UTC)
            if key == "after":
                return lambda stored: stored.internal_date > bound
            return lambda stored: stored.internal_date < bound
        if key == "in":
            folder = value.lower()
            if folder == "anywhere":
                return lambda stored: True
            label = _IN_FOLDERS.get(folder)
            if label is None:
                raise ValueError(f"the fake does not know in:{value}")
            return lambda stored: label in stored.labels
        if key == "label":
            wanted = value.lower()
            ids = {label.id for label in self._labels.values() if label.name.lower() == wanted}
            return lambda stored: bool(stored.labels & ids)
        raise ValueError(f"the fake does not search for {key}:")


def _snippet(parsed: EmailMessage) -> str:
    part = parsed.get_body(preferencelist=("plain",))
    if part is None:
        return ""
    assert isinstance(part, EmailMessage)
    return " ".join(str(part.get_content()).split())[:100]
