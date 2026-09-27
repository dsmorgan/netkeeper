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

**The recipient** is one bare address (#269). A domain written in Unicode
(``a@bücher.example``) goes into ``To`` as IDNA, ``a@xn--bcher-kva.example``
(IDNA 2008 with UTS 46 mapping, what browsers and mail providers use): the
header's own encoding would have made it ``a@=?utf-8?q?...?=.example``, which is
no address (#280). A local part outside ASCII needs SMTPUTF8, which a campaign
does not use, so it is refused.

**The subject** is one line of text. A line separator of any kind (not only CR
and LF: VT, FF, NEL, U+2028 ...), any other control character but tab, and
anything shaped like an RFC 2047 encoded word (``=?charset?q?...?=``, which
the header would decode instead of showing) are refused (#280).

**From** is left out. Gmail fills it in with the account's address and the
name the person set in Gmail, which is the sender the recipient knows.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Final

import idna

from netkeeper.models.contacts import single_address

#: Bumped only if the derivation changes: a new version gives every row a new id.
MESSAGE_ID_VERSION: Final = "v1"

#: Hex characters of the hash in a Message-ID's local part (128 bits).
MESSAGE_ID_HASH_LENGTH: Final = 32

#: At most this many earlier messages go into ``References``: the first, and the
#: newest after it. RFC 5322 lets a client trim the list that way.
REFERENCES_MAX: Final = 20

_REPLY_PREFIX: Final = re.compile(r"^\s*re\s*:", re.IGNORECASE)
_MSGID: Final = re.compile(r"^<[^<>\s@]+@[^<>\s@]+>$")
_ENCODED_WORD: Final = re.compile(r"=\?[^?]*\?[^?]*\?[^?]*\?=")


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

    :class:`ComposeError` for a ``to`` that is not one bare address (a list, a
    group or a display name would send to someone else too, #269; see
    :func:`recipient`), a subject that is not one line of text
    (:func:`check_subject`), or a Message-ID that is not one: nothing a merge
    value holds can add a header or a recipient (spec 11.1). Whatever else the
    email package refuses is a :class:`ComposeError` too, so a message that
    cannot be built fails at once and never reads as an unknown outcome (#280).
    """
    address = recipient(to)
    check_subject(subject)
    cited = [*references, *([in_reply_to] if in_reply_to else [])]
    for value in (message_id, *cited):
        if not is_message_id(value):
            raise ComposeError("a Message-ID is <local@domain>")
    message = EmailMessage()
    try:
        message["To"] = address
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
    except ValueError as exc:  # the email package's own refusal: never an unknown outcome
        raise ComposeError(f"the email package refused it ({type(exc).__name__})") from None
    return message


def recipient(to: str) -> str:
    """``to`` as it goes into ``To``: one bare address, its domain in IDNA (see the module).

    :class:`ComposeError` for anything else, a local part outside ASCII, or a
    domain IDNA refuses.
    """
    refused = "a campaign message needs exactly one bare recipient address"
    try:
        address = single_address(to.strip())
    except ValueError:
        raise ComposeError(refused) from None
    local, _, domain = address.rpartition("@")
    if not local.isascii():
        raise ComposeError("a recipient address outside ASCII before the @ cannot be sent")
    if domain.isascii():
        return address
    try:
        encoded = idna.encode(domain, uts46=True).decode("ascii")
    except idna.IDNAError:
        raise ComposeError("the recipient's domain is not a valid internationalized name") from None
    try:
        return single_address(f"{local}@{encoded}")
    except ValueError:
        raise ComposeError(refused) from None


def check_subject(subject: str) -> None:
    """:class:`ComposeError` unless ``subject`` is one line of text fit for the header."""
    for ch in subject:
        if ch == "\t":
            continue
        if ch in "\u2028\u2029" or unicodedata.category(ch) == "Cc":
            raise ComposeError("a subject is one line, with no control characters")
    if _ENCODED_WORD.search(subject):
        raise ComposeError("a subject cannot hold an encoded word (=?...?=)")


def campaign_label(prefix: str, campaign_name: str) -> str:
    """The campaign's Gmail label, ``<prefix>/<campaign name>`` (spec 11.5)."""
    name = " ".join(campaign_name.split())
    return f"{prefix.strip().rstrip('/')}/{name}"
