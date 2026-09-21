"""Turn ``messages.csv`` rows into conversations with one identified counterpart each.

Pure, behind the extractor boundary (spec 9.10, ADR 0005): rows in, frozen
dataclasses out, no models and no session. :mod:`netkeeper.crm.archive` writes
what comes out as interactions.

The problem this module exists for: a message row carries a profile URL only
sometimes. In exports we have measured, a quarter to three quarters of the rows
name neither a sender nor a recipient profile URL, and matching row by row
throws that history away. But a conversation has the same two people in it from
beginning to end, so one row that does carry the other party's URL identifies
every row in that conversation. :func:`group` does exactly that: it groups by
``CONVERSATION ID``, takes the profile URLs that appear anywhere inside the
group, and attributes the whole conversation to the one that is not the
archive's owner.

Two rules keep that from inventing history:

* A display name resolves a party only **inside its own conversation**. Names
  are not unique, and matching them across the file would fold strangers who
  share a name into one person. The single exception is the owner's own name,
  which is safe to match anywhere because the owner is one identified person,
  and which is what tells an unlabelled row whether it was sent or received.
* A conversation with more than one other party is a group thread. It is
  reported and skipped, never attributed: the owner posting in a group is not
  evidence that the owner and any one member spoke to each other, and counting
  it as one would push every member's "last contacted" forward and put
  someone else's words in front of the person triaging. A thread whose other
  party changed their vanity URL mid-conversation looks the same from here and
  is skipped for the same reason; the sync (P2) repairs identity, and a rerun
  then attributes it.

Who the owner is: the archive never labels them. ``Profile.csv`` gives their
name but no URL, so :func:`detect_owner` uses the traffic instead. The owner is
in every conversation and nobody else is in many, so the profile URL that
appears in the most conversations wins, provided it appears in more than one
and no other URL ties it. On a tie the name from ``Profile.csv`` breaks it, and
failing that the owner is unknown and no message is attributed at all, which is
the right outcome: guessing the owner wrong reverses the direction of every
interaction in the import.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

from netkeeper.linkedin.archive import MessageRow

log = logging.getLogger(__name__)

# The owner must appear in more conversations than this before the traffic alone
# names them. One conversation says nothing: both parties appear in exactly one.
MIN_OWNER_CONVERSATIONS: Final = 2


@dataclass(frozen=True, slots=True)
class Owner:
    """The archive's owner: their profile slug, and the display names they send under.

    ``names`` are casefolded, and are the only names this module compares
    outside a single conversation. ``by`` records what identified them, for the
    import report and for tests.
    """

    public_id: str
    names: frozenset[str]
    by: str  # "traffic" | "profile-name" | "given"


@dataclass(frozen=True, slots=True)
class Message:
    """One row of a conversation, with the direction resolved.

    ``outbound`` is True when the owner sent it. ``by`` names what decided
    that: ``url`` when the row carried a profile URL, ``name`` when only the
    display name did.
    """

    row: MessageRow
    outbound: bool
    by: str  # "url" | "name"


@dataclass(frozen=True, slots=True)
class Conversation:
    """A one-to-one conversation between the owner and one identified counterpart."""

    conversation_id: str
    counterpart_public_id: str
    messages: tuple[Message, ...]


@dataclass(frozen=True, slots=True)
class Skipped:
    """A conversation that was not attributed, and why.

    ``no_counterpart``: no profile URL for the other party appears on any row of
    the conversation, so there is nothing to attach its messages to.
    ``group``: more than one other party, or a row addressed to several people.
    ``no_owner``: the owner could not be identified, so every conversation is
    skipped for this reason and no others are reported.
    """

    conversation_id: str
    reason: str  # "no_counterpart" | "group" | "no_owner"
    rows: int


@dataclass(frozen=True, slots=True)
class Threads:
    """Everything :func:`group` made of the message table.

    ``conversations`` are the attributed ones; ``skipped`` says what happened to
    the rest, one entry per conversation, so a caller can report the shape of
    what it could not use without holding the rows.
    """

    owner: Owner | None
    conversations: tuple[Conversation, ...] = ()
    skipped: tuple[Skipped, ...] = ()
    rows: int = 0
    undated_rows: int = 0

    @property
    def total_conversations(self) -> int:
        return len(self.conversations) + len(self.skipped)

    def skipped_for(self, reason: str) -> int:
        """How many conversations were skipped for ``reason``."""
        return sum(1 for entry in self.skipped if entry.reason == reason)


@dataclass(slots=True)
class _Group:
    """One conversation while it is being assembled."""

    conversation_id: str
    rows: list[MessageRow] = field(default_factory=list)
    slugs: set[str] = field(default_factory=set)
    addressed_to_several: bool = False


def group(rows: Iterable[MessageRow], *, owner: Owner | str | None = None) -> Threads:
    """Group message rows by conversation and attribute each to one counterpart.

    ``owner`` may be given as an :class:`Owner` or a profile slug when the
    caller already knows whose archive this is; otherwise :func:`detect_owner`
    reads it out of the traffic. Rows keep the order they arrived in, which for
    an archive is LinkedIn's own, and conversations come out in the order their
    first row did, so two runs over the same file produce the same result.
    """
    groups = _collect(rows)
    row_count = sum(len(entry.rows) for entry in groups)
    undated = sum(1 for entry in groups for row in entry.rows if row.sent_at is None)
    resolved = owner if isinstance(owner, Owner) else _owner_from(groups, owner)
    if resolved is None:
        log.warning(
            "archive: owner not identified, %d conversations left unattributed", len(groups)
        )
        return Threads(
            owner=None,
            skipped=tuple(
                Skipped(entry.conversation_id, "no_owner", len(entry.rows)) for entry in groups
            ),
            rows=row_count,
            undated_rows=undated,
        )
    conversations: list[Conversation] = []
    skipped: list[Skipped] = []
    for entry in groups:
        others = entry.slugs - {resolved.public_id}
        if entry.addressed_to_several or len(others) > 1:
            skipped.append(Skipped(entry.conversation_id, "group", len(entry.rows)))
        elif not others:
            skipped.append(Skipped(entry.conversation_id, "no_counterpart", len(entry.rows)))
        else:
            counterpart = next(iter(others))
            conversations.append(
                Conversation(
                    conversation_id=entry.conversation_id,
                    counterpart_public_id=counterpart,
                    messages=_directions(entry.rows, resolved, counterpart),
                )
            )
    return Threads(
        owner=resolved,
        conversations=tuple(conversations),
        skipped=tuple(skipped),
        rows=row_count,
        undated_rows=undated,
    )


def detect_owner(rows: Iterable[MessageRow], *, profile_name: str | None = None) -> Owner | None:
    """Who the archive belongs to, from the message traffic; ``None`` when unclear.

    The profile URL in the most conversations wins, provided it is in at least
    :data:`MIN_OWNER_CONVERSATIONS` of them and no other URL matches its count.
    ``profile_name`` is the owner's name as ``Profile.csv`` writes it
    (``"First Last"``); it breaks a tie by picking the tied slug that sends
    under that name, and is ignored otherwise.
    """
    return _owner_from(_collect(rows), None, profile_name=profile_name)


def _collect(rows: Iterable[MessageRow]) -> list[_Group]:
    groups: dict[str, _Group] = {}
    for row in rows:
        entry = groups.get(row.conversation_id)
        if entry is None:
            entry = groups[row.conversation_id] = _Group(row.conversation_id)
        entry.rows.append(row)
        if row.sender_public_id is not None:
            entry.slugs.add(row.sender_public_id)
        entry.slugs.update(row.recipient_public_ids)
        # A row addressed to several people is a group thread even when the
        # export gave a profile URL for only one of them, or for none.
        if len(row.recipient_public_ids) > 1 or len(row.recipients) > 1:
            entry.addressed_to_several = True
    return list(groups.values())


def _owner_from(
    groups: Sequence[_Group],
    given: str | None,
    *,
    profile_name: str | None = None,
) -> Owner | None:
    if given is not None:
        return Owner(given, _names_of(groups, given), by="given")
    appearances: Counter[str] = Counter()
    for entry in groups:
        appearances.update(entry.slugs)
    if not appearances:
        return None
    best, count = appearances.most_common(1)[0]
    if count < MIN_OWNER_CONVERSATIONS:
        return None
    tied = [slug for slug, seen in appearances.items() if seen == count]
    if len(tied) == 1:
        return Owner(best, _names_of(groups, best), by="traffic")
    wanted = _fold(profile_name)
    if wanted is None:
        return None
    named = [slug for slug in sorted(tied) if wanted in _names_of(groups, slug)]
    if len(named) != 1:
        return None
    return Owner(named[0], _names_of(groups, named[0]), by="profile-name")


def _names_of(groups: Sequence[_Group], public_id: str) -> frozenset[str]:
    """Every casefolded display name a slug sends under, across the whole file.

    The one name comparison this module makes globally, and only for the owner:
    see the module docstring.
    """
    names = {
        folded
        for entry in groups
        for row in entry.rows
        if row.sender_public_id == public_id and (folded := _fold(row.sender)) is not None
    }
    return frozenset(names)


def _directions(rows: Sequence[MessageRow], owner: Owner, counterpart: str) -> tuple[Message, ...]:
    """Say of each row whether the owner sent it, by URL where there is one, else by name.

    Names only settle a row inside this conversation: the counterpart's names
    are the ones this conversation's own rows show them sending under, plus the
    ones the owner's rows address. A row from neither known name, in a
    conversation that by now has exactly two people in it, is the counterpart's.
    """
    theirs = {
        folded
        for row in rows
        if row.sender_public_id == counterpart and (folded := _fold(row.sender)) is not None
    }
    theirs.update(
        folded
        for row in rows
        if row.sender_public_id == owner.public_id
        for name in row.recipients
        if (folded := _fold(name)) is not None
    )
    resolved: list[Message] = []
    for row in rows:
        if row.sender_public_id is not None:
            resolved.append(Message(row, row.sender_public_id == owner.public_id, by="url"))
            continue
        sender = _fold(row.sender)
        if sender is not None and sender in owner.names and sender not in theirs:
            resolved.append(Message(row, True, by="name"))
        else:
            # Theirs, or unattributable by name: in a two-person conversation the
            # sender is the counterpart unless it is demonstrably the owner.
            resolved.append(Message(row, False, by="name"))
    return tuple(resolved)


def _fold(value: str | None) -> str | None:
    folded = (value or "").strip().casefold()
    return folded or None
