"""Import a LinkedIn data archive into contacts and interactions (P1-03, spec 10.5).

This is the core side of the boundary (spec 9.10, ADR 0005):
:mod:`netkeeper.linkedin.archive` reads the files and
:mod:`netkeeper.linkedin.conversations` works out who each conversation is
with, both without touching the database; everything here takes those plain
dataclasses and writes them through :mod:`netkeeper.crm.identity` and
:mod:`netkeeper.crm.interactions`.

What the three tables become:

* ``Connections.csv`` is the contact list. Each row goes through identity
  resolution as an :class:`~netkeeper.crm.identity.IncomingContact` with source
  ``archive``, so it creates a contact or enriches one that is already there.
  ``archive`` sits below ``sync`` and below ``manual`` in
  :mod:`netkeeper.crm.provenance`, which is what makes a hand-edited name or
  company survive a re-import untouched while still being recorded in
  ``synced_values`` to revert to. An email address is written only when the row
  carries one; most rows do not, because LinkedIn shows an address only for
  people who opted in, and an empty cell means "not provided", never "no
  address". A row that resolves to a :class:`~netkeeper.crm.identity.Candidate`
  is counted and left alone: deciding between two contacts needs the review
  screen that arrives with P1-04, and guessing here would merge two people.
  The handful of rows an export gives no profile URL for land there on every
  run after the first, because a name and a company together are a candidate
  and never a match (spec 8.2 step 4); they are counted under ``needs_review``
  rather than written twice.
* ``messages.csv`` and ``Invitations.csv`` become ``li_in`` and ``li_out``
  interactions, which is both the timeline and the evidence P1-09 shows while
  triaging. They never create a contact. The archive's message history is full
  of recruiters, newsletters and strangers who were never connections, and
  inviting them into the CRM would bury the people the network is actually
  made of; a message whose counterpart is not already a contact is counted and
  dropped. So connections are imported first, in one pass, before either.

Re-import is idempotent. A contact takes the same values again and nothing
changes. An interaction has no natural key in the schema, so this module
matches on what an archive row determines — the contact, the kind, and the
instant — and counts how many of each it already wrote under source
``archive``, adding only the surplus. Two identical rows in one file therefore
still produce two interactions, and importing that file twice still produces
two. Interactions a person entered by hand are never matched against, so an
import cannot swallow one, and cannot be blocked by one either.

The auto-tag rules run at the end of the import, over the contacts it created
or enriched and no others, in the same transaction (spec 10.3, #64). So an
address book is tagged the moment it lands rather than when somebody finds the
button, and a failed import takes its tags down with it. The counts are in
``ArchiveImport.tagging``. A row that resolved to a candidate was not written,
so it is not in the run; the next import that resolves it will be.

Transactions belong to the caller: nothing here commits, and the session must
be a writer (``session_scope(factory, write=True)``) because every step reads
before it writes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy.orm import Session

from netkeeper.crm.identity import (
    Candidate,
    IncomingContact,
    IncomingEmail,
    Matched,
    New,
    apply,
    resolve,
)
from netkeeper.crm.interactions import INVITATION_SUMMARY, add_interaction
from netkeeper.crm.tags import RuleRun, run_rules
from netkeeper.db import is_writer
from netkeeper.linkedin.archive import (
    Archive,
    ArchiveKind,
    ConnectionRow,
    InvitationDirection,
    InvitationRow,
    MessageRow,
)
from netkeeper.linkedin.conversations import Owner, group
from netkeeper.models import ContactSource, EmailKind, Interaction, InteractionKind, User
from netkeeper.models.base import utcnow
from netkeeper.scoping import scoped

log = logging.getLogger(__name__)

# How much of a message body an interaction's summary keeps. Triage reads it as
# evidence, so it holds the message rather than a label, but a body is a CSV
# field with no length limit and the timeline is not a mail reader.
SUMMARY_MAX_CHARS: Final = 2000


@dataclass(slots=True)
class ConnectionCounts:
    """What ``Connections.csv`` did. ``rows`` is every non-blank data row read."""

    rows: int = 0
    created: int = 0
    updated: int = 0
    needs_review: int = 0
    skipped: int = 0
    with_email: int = 0
    undated: int = 0


@dataclass(slots=True)
class MessageCounts:
    """What ``messages.csv`` did, per conversation and then per row.

    ``conversations`` is every distinct ``CONVERSATION ID``, and it splits
    exactly into ``attributed`` and the four reasons a conversation produced
    nothing: ``no_counterpart`` (no profile URL for the other party appears
    anywhere in it), ``group_threads``, ``unknown_contact`` (the other party is
    not a contact) and ``no_owner`` (the archive's owner could not be
    identified, which stops the whole table at once). ``added`` and
    ``already_present`` count rows inside attributed conversations, and
    ``undated`` counts rows anywhere in the file whose ``DATE`` did not parse.
    """

    rows: int = 0
    conversations: int = 0
    attributed: int = 0
    no_counterpart: int = 0
    group_threads: int = 0
    unknown_contact: int = 0
    no_owner: int = 0
    added: int = 0
    already_present: int = 0
    undated: int = 0
    outbound: int = 0
    inbound: int = 0


@dataclass(slots=True)
class InvitationCounts:
    """What ``Invitations.csv`` did, per row.

    ``undirected`` is a row whose ``Direction`` is neither ``OUTGOING`` nor
    ``INCOMING``, ``no_counterpart`` one that names no profile URL for the
    other party, and ``unknown_contact`` one whose other party is not a contact.
    """

    rows: int = 0
    added: int = 0
    already_present: int = 0
    unknown_contact: int = 0
    no_counterpart: int = 0
    undated: int = 0
    undirected: int = 0


@dataclass(slots=True)
class ArchiveImport:
    """The counts one import produced, and what it decided along the way.

    ``owner_public_id`` is whose archive this was taken to be and ``owner_by``
    what identified them (see :mod:`netkeeper.linkedin.conversations`); both
    are ``None`` when the messages table was absent or the owner unclear, in
    which case no message was imported at all. ``observed_at`` is the instant
    every row was recorded as observed. ``tagging`` is what the auto-tag rules
    did to the contacts this import created or enriched.
    """

    observed_at: datetime
    owner_public_id: str | None = None
    owner_by: str | None = None
    connections: ConnectionCounts = field(default_factory=ConnectionCounts)
    messages: MessageCounts = field(default_factory=MessageCounts)
    invitations: InvitationCounts = field(default_factory=InvitationCounts)
    tagging: RuleRun = field(default_factory=lambda: RuleRun(0, 0, 0, 0))


def import_archive(
    session: Session,
    user: User,
    archive: Archive,
    *,
    observed_at: datetime | None = None,
    owner_public_id: str | None = None,
) -> ArchiveImport:
    """Import every table ``archive`` holds for ``user`` and return the counts.

    ``observed_at`` is when the archive saw what it says, which is what per-field
    provenance compares to decide whether a value is newer than the one on
    record. It defaults to the archive's own export time when it has one
    (``Archive.exported_at``, the newest member timestamp, which for a zip is
    approximate to the exporting machine's UTC offset) and to now when it does
    not; a naive value is a ``ValueError``. ``owner_public_id`` says whose
    archive this is when the caller knows, skipping the detection in
    :mod:`netkeeper.linkedin.conversations`; without it, the owner is read out
    of the message traffic, with ``Profile.csv``'s name to settle a tie.

    Nothing is committed. ``RuntimeError`` when ``session`` is not a writer.
    """
    _require_writer(session)
    when = observed_at or archive.exported_at or utcnow()
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    report = ArchiveImport(observed_at=when)
    touched: list[int] = []
    for member in archive.members:
        if member.kind is ArchiveKind.CONNECTIONS:
            _import_connections(
                session, user, archive.connections(member), when, report.connections, touched
            )
    written = _Interactions(session, user)
    profile_name = archive.owner_name()
    for member in archive.members:
        if member.kind is ArchiveKind.MESSAGES:
            _import_messages(
                archive.messages(member), owner_public_id, profile_name, written, report
            )
    for member in archive.members:
        if member.kind is ArchiveKind.INVITATIONS:
            _import_invitations(archive.invitations(member), written, report)
    # The rules, over the contacts this import touched and no others, in the
    # caller's transaction (spec 10.3, #64): a freshly imported address book is
    # tagged when the import returns, with no button to find and press first.
    report.tagging = run_rules(session, user, sorted(set(touched)))
    log.info(
        "archive %s imported for user %d: %d connections (%d created, %d updated, "
        "%d for review), %d message interactions, %d invitation interactions",
        archive.name,
        user.id,
        report.connections.rows,
        report.connections.created,
        report.connections.updated,
        report.connections.needs_review,
        report.messages.added,
        report.invitations.added,
    )
    return report


# --- connections ------------------------------------------------------------


def _import_connections(
    session: Session,
    user: User,
    rows: Iterable[ConnectionRow],
    observed_at: datetime,
    counts: ConnectionCounts,
    touched: list[int],
) -> None:
    """Import every row, collecting into ``touched`` the contacts that were written."""
    for row in rows:
        counts.rows += 1
        if row.connected_on is None:
            counts.undated += 1
        incoming = _incoming_contact(row, observed_at)
        if incoming is None:
            # Neither a profile URL nor a name: nothing to resolve or to create.
            counts.skipped += 1
            continue
        if incoming.emails:
            counts.with_email += 1
        resolution = resolve(session, user, incoming)
        match resolution:
            case Candidate():
                counts.needs_review += 1
            case Matched():
                touched.append(apply(session, user, incoming, resolution).id)
                counts.updated += 1
            case New():
                touched.append(apply(session, user, incoming, resolution).id)
                counts.created += 1


def _incoming_contact(row: ConnectionRow, observed_at: datetime) -> IncomingContact | None:
    """One connection row as an incoming contact, or ``None`` when it identifies nobody."""
    if row.public_id is None and row.first_name == "" and row.last_name == "":
        return None
    return IncomingContact(
        source=ContactSource.ARCHIVE,
        observed_at=observed_at,
        li_public_id=row.public_id,
        li_url=row.url,
        first_name=row.first_name or None,
        last_name=row.last_name or None,
        current_title=row.position,
        current_company=row.company,
        connected_on=row.connected_on,
        # Only where the export actually carried one: an empty cell means the
        # person did not share an address, not that they have none.
        emails=(
            (IncomingEmail(row.email, kind=EmailKind.OTHER, is_primary=True),)
            if row.email is not None
            else ()
        ),
    )


# --- messages ---------------------------------------------------------------


def _import_messages(
    rows: Iterable[MessageRow],
    owner_public_id: str | None,
    profile_name: str | None,
    written: _Interactions,
    report: ArchiveImport,
) -> None:
    counts = report.messages
    threads = group(rows, owner=owner_public_id, profile_name=profile_name)
    counts.rows += threads.rows
    counts.undated += threads.undated_rows
    counts.conversations += threads.total_conversations
    counts.no_counterpart += threads.skipped_for("no_counterpart")
    counts.group_threads += threads.skipped_for("group")
    counts.no_owner += threads.skipped_for("no_owner")
    if threads.owner is None:
        return
    _note_owner(threads.owner, report)
    for conversation in threads.conversations:
        contact_id = written.contact_for(conversation.counterpart_public_id)
        if contact_id is None:
            counts.unknown_contact += 1
            continue
        counts.attributed += 1
        for message in conversation.messages:
            if message.row.sent_at is None:
                continue  # already counted in undated; an interaction needs an instant
            kind = InteractionKind.LI_OUT if message.outbound else InteractionKind.LI_IN
            if written.add(contact_id, kind, message.row.sent_at, _message_summary(message.row)):
                counts.added += 1
                if message.outbound:
                    counts.outbound += 1
                else:
                    counts.inbound += 1
            else:
                counts.already_present += 1


def _note_owner(owner: Owner, report: ArchiveImport) -> None:
    if report.owner_public_id is not None and report.owner_public_id != owner.public_id:
        log.warning("archive: message tables disagree on the owner; keeping the first")
        return
    report.owner_public_id = owner.public_id
    report.owner_by = owner.by


def _message_summary(row: MessageRow) -> str | None:
    """A message as triage evidence: its subject and body, trimmed to a readable length."""
    parts = [part for part in (row.subject, row.content.strip()) if part]
    return _trim("\n".join(parts))


# --- invitations ------------------------------------------------------------


def _import_invitations(
    rows: Iterable[InvitationRow],
    written: _Interactions,
    report: ArchiveImport,
) -> None:
    counts = report.invitations
    for row in rows:
        counts.rows += 1
        if row.sent_at is None:
            counts.undated += 1
            continue
        if row.direction is InvitationDirection.OUTGOING:
            # The owner invited, so the other party is the invitee; incoming is
            # the other way round, and a row that says neither is unusable.
            public_id, kind = row.invitee_public_id, InteractionKind.LI_OUT
        elif row.direction is InvitationDirection.INCOMING:
            public_id, kind = row.inviter_public_id, InteractionKind.LI_IN
        else:
            counts.undirected += 1
            continue
        if public_id is None:
            counts.no_counterpart += 1
            continue
        contact_id = written.contact_for(public_id)
        if contact_id is None:
            counts.unknown_contact += 1
            continue
        if written.add(contact_id, kind, row.sent_at, _invitation_summary(row)):
            counts.added += 1
        else:
            counts.already_present += 1


def _invitation_summary(row: InvitationRow) -> str | None:
    """The invitation's note, marked as one; the marker alone when it carried no note."""
    if row.message is None:
        return INVITATION_SUMMARY
    return _trim(f"{INVITATION_SUMMARY}: {row.message}")


# --- writing interactions ---------------------------------------------------


class _Interactions:
    """Resolves counterparts to contacts once each, and writes interactions once each.

    Two lookups an import would otherwise repeat thousands of times. The slug
    cache holds the contact a profile slug resolves to, or ``None`` for one that
    is nobody, for as long as the import runs; nothing here creates a contact,
    so an entry cannot go stale underneath it. The interaction ledger is every
    ``(contact, kind, instant)`` this user already has from an earlier archive
    import, counted, so a re-import recognizes its own work and adds only what
    is new. See the module docstring on why that triple is the key.
    """

    __slots__ = ("_contacts", "_seen", "_session", "_user")

    def __init__(self, session: Session, user: User) -> None:
        self._session = session
        self._user = user
        self._contacts: dict[str, int | None] = {}
        self._seen: dict[tuple[int, InteractionKind, datetime], int] = {}
        statement = scoped(user, Interaction).where(Interaction.source == ContactSource.ARCHIVE)
        for row in session.scalars(statement):
            key = (row.contact_id, row.kind, row.at)
            self._seen[key] = self._seen.get(key, 0) + 1

    def contact_for(self, public_id: str) -> int | None:
        """The contact a profile slug belongs to, or ``None`` when it belongs to none.

        Only an unambiguous identity match counts. A slug that resolves to
        several contacts is as good as unknown here: a message is evidence about
        one person, and picking one of two would attach somebody's words to a
        stranger. Aliases are followed, so a contact whose vanity URL changed
        still answers to the slug the archive recorded.
        """
        if public_id in self._contacts:
            return self._contacts[public_id]
        incoming = IncomingContact(
            source=ContactSource.ARCHIVE,
            observed_at=utcnow(),
            li_public_id=public_id,
        )
        resolution = resolve(self._session, self._user, incoming)
        found = resolution.contact_id if isinstance(resolution, Matched) else None
        self._contacts[public_id] = found
        return found

    def add(
        self,
        contact_id: int,
        kind: InteractionKind,
        at: datetime,
        summary: str | None,
    ) -> bool:
        """Write one interaction unless an earlier import of this archive already did.

        True when it was written. The ledger counts rather than flags, so a file
        that really does hold two identical rows yields two interactions the
        first time and none the second.
        """
        key = (contact_id, kind, at)
        planned = self._seen.get(key, 0)
        if planned > 0:
            self._seen[key] = planned - 1
            return False
        add_interaction(
            self._session,
            self._user,
            contact_id,
            kind,
            at,
            summary,
            source=ContactSource.ARCHIVE,
        )
        return True


def _trim(text: str) -> str | None:
    """``text`` at most :data:`SUMMARY_MAX_CHARS` long, with an ellipsis when cut; empty is None."""
    stripped = text.strip()
    if not stripped:
        return None
    if len(stripped) <= SUMMARY_MAX_CHARS:
        return stripped
    return stripped[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"


def _require_writer(session: Session) -> None:
    """Fail at once rather than with "database is locked" on the first write (CLAUDE.md)."""
    if not is_writer(session):
        raise RuntimeError(
            "importing an archive needs a writer session; use session_scope(factory, write=True)"
        )
