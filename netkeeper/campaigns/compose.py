"""Building a campaign email: RFC 2822 headers, the Message-ID, threading, labels (P3-07).

Pure functions, no Gmail and no database. :mod:`netkeeper.services.campaign_sender`
builds each message here and hands it to :class:`~netkeeper.campaigns.gmail.Gmail`.

**The Message-ID is known before the send** (#269, requirement 1).
:func:`message_id_for` derives it from the engine's message row: the user,
the row's id and its ``created_at``, and the domain of the mailbox's address.
Nothing is stored for it, and the same row always gives the same id, so after
a crash or an answer that never came, a search for ``rfc822msgid:`` finds the
message if Gmail has it. ``created_at`` keeps two databases apart: a database
made again from nothing numbers its messages from 1 again, and without it a
search could find a message a former database sent. The local part is a hash,
so the header says nothing about netkeeper, the row, or the contact.

**Threading** (spec 11.5). A ``same_thread`` follow-up cites every earlier
message of the Gmail thread in ``References``, oldest first, and the latest in
``In-Reply-To``. Its subject is the thread's first subject with one ``Re:``
in front (:func:`reply_subject`). Gmail joins a message to ``threadId`` only
when it cites a message of that thread and its subject matches once ``Re:``
and ``Fwd:`` are stripped; otherwise it silently starts a new thread (the
fake's rule, :mod:`netkeeper.campaigns.gmail_fake`).

**From** is left out. Gmail fills it in with the account's address and the
name the person set in Gmail, which is the sender the recipient knows.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Final

#: Bumped only if the derivation changes: a new version gives every row a new id.
MESSAGE_ID_VERSION: Final = "v1"

#: Hex characters of the hash in a Message-ID's local part (128 bits).
MESSAGE_ID_HASH_LENGTH: Final = 32

#: At most this many earlier messages go into ``References``: the first, and the
#: newest after it. RFC 5322 lets a client trim the list that way.
REFERENCES_MAX: Final = 20

_REPLY_PREFIX: Final = re.compile(r"^\s*re\s*:", re.IGNORECASE)
_MSGID: Final = re.compile(r"^<[^<>\s@]+@[^<>\s@]+>$")


class ComposeError(ValueError):
    """The message cannot be built as asked (no address, a header that is not one)."""


def message_id_for(*, user_id: int, message_id: int, created_at: datetime, address: str) -> str:
    """The RFC 822 Message-ID of the engine's message ``message_id`` (see the module)."""
    if created_at.tzinfo is None:
        raise ComposeError("created_at must be timezone-aware")
    _, at, domain = address.strip().rpartition("@")
    domain = domain.lower()
    if not at or not domain or not re.fullmatch(r"[a-z0-9.-]+", domain):
        raise ComposeError("the mailbox address has no domain a Message-ID can use")
    stamp = created_at.astimezone(UTC).isoformat(timespec="microseconds")
    seed = f"netkeeper-message-id:{MESSAGE_ID_VERSION}:{user_id}:{message_id}:{stamp}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:MESSAGE_ID_HASH_LENGTH]
    return f"<{digest}@{domain}>"


def is_message_id(value: str) -> bool:
    """Whether ``value`` is one ``<local@domain>`` Message-ID, fit for a search or a header."""
    return bool(_MSGID.match(value))


def reply_subject(subject: str | None) -> str:
    """``subject`` as a reply's: one ``Re:`` in front, never two."""
    text = " ".join((subject or "").split())
    if _REPLY_PREFIX.match(text):
        return text
    return f"Re: {text}" if text else "Re:"


def build_message(
    *,
    to: str,
    subject: str,
    body: str,
    message_id: str,
    in_reply_to: str | None = None,
    references: Sequence[str] = (),
) -> EmailMessage:
    """A plain-text message for Gmail's ``raw``, with ``message_id`` as its Message-ID.

    :class:`ComposeError` for an address or a header value with a line break in
    it, or a Message-ID that is not one: nothing a merge value holds can add a
    header (spec 11.1).
    """
    if not to.strip() or "@" not in to:
        raise ComposeError("a campaign message needs one recipient address")
    for value in (to, subject):
        if "\r" in value or "\n" in value:
            raise ComposeError("a header value is one line")
    cited = [*references, *([in_reply_to] if in_reply_to else [])]
    for value in (message_id, *cited):
        if not is_message_id(value):
            raise ComposeError("a Message-ID is <local@domain>")
    message = EmailMessage()
    message["To"] = to.strip()
    message["Subject"] = subject
    message["Message-ID"] = message_id
    if in_reply_to is not None:
        message["In-Reply-To"] = in_reply_to
    if references:
        cites = list(references)
        if len(cites) > REFERENCES_MAX:  # the first and the newest (RFC 5322, 3.6.4)
            cites = [cites[0], *cites[-(REFERENCES_MAX - 1) :]]
        message["References"] = " ".join(cites)
    message.set_content(body)
    return message


def campaign_label(prefix: str, campaign_name: str) -> str:
    """The campaign's Gmail label, ``<prefix>/<campaign name>`` (spec 11.5)."""
    name = " ".join(campaign_name.split())
    return f"{prefix.strip().rstrip('/')}/{name}"
