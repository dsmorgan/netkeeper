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
import cannot swallow one, and cannot be blocked by one either. The
``li_in`` and ``li_out`` rows the LinkedIn inbox poll recorded (source
``sync``, P4-08) are counted with the archive's own, at the same contact, kind,
and second, so an archive imported after a poll does not record a polled
message twice.

The auto-tag rules run at the end of the import, over the contacts it created
or enriched and no others, in the same transaction (spec 10.3, #64), and the
default rule set is seeded first when this user has never had it: an import is
often the first thing that happens to a database, and rules nobody has seeded
tag nobody. So an
address book is tagged the moment it lands rather than when somebody finds the
button, and a failed import takes its tags down with it. The counts are in
``ArchiveImport.tagging``. A row that resolved to a candidate was not written,
so it is not in the run; the next import that resolves it will be.

Every import is recorded as an ``import_runs`` row of kind ``archive`` (#132),
committed with the rest in the same transaction, so it shows in the import
history and can be rolled back by run like a CSV import
(:func:`netkeeper.crm.import_runs.rollback`). Each ``Connections.csv`` row is an
``import_rows`` row that records what it did in the same shape a CSV row does:
a contact it created, or the values it found on a contact it enriched. The
interactions the messages and invitations tables wrote are recorded on the run
by id (``created_json``), since each belongs to a contact rather than to a row.
The run's CSV-shaped counts describe ``Connections.csv`` (updated counts as
matched, needs review as candidate); ``report_json`` keeps every table's.
A rollback does not touch the user's own job history from ``Positions.csv``,
which is not about a contact and which the import only upserts.

Transactions belong to the caller: nothing here commits, and the session must
be a writer (``session_scope(factory, write=True)``) because every step reads
before it writes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Final

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from netkeeper.crm import positions as positions_service
from netkeeper.crm.identity import (
    Candidate,
    IncomingContact,
    IncomingEmail,
    Matched,
    New,
    apply,
    resolve,
    resolve_survivor,
)
from netkeeper.crm.import_runs import (
    contact_effect,
    created_effect,
    prefetch_contacts,
    snapshot_contact,
)
from netkeeper.crm.interactions import INVITATION_SUMMARY, add_interaction
from netkeeper.crm.positions import PositionCounts
from netkeeper.crm.tags import RuleRun, ensure_default_rules, run_rules
from netkeeper.db import is_writer
from netkeeper.linkedin.archive import (
    Archive,
    ArchiveKind,
    ConnectionRow,
    InvitationDirection,
    InvitationRow,
    MessageRow,
    PositionRow,
)
from netkeeper.linkedin.conversations import Owner, group
from netkeeper.models import (
    ContactSource,
    EmailKind,
    ImportResolution,
    ImportRow,
    ImportRun,
    ImportSourceKind,
    ImportStatus,
    Interaction,
    InteractionKind,
    User,
)
from netkeeper.models.base import utcnow
from netkeeper.models.imports import FILENAME_MAX_LENGTH
from netkeeper.scoping import scoped

log = logging.getLogger(__name__)

# The one name a real message history carries in LinkedIn's export. A member
# recognized as the messages table under any other name is read all the same
# (a renamed or moved table still imports), but reported, because LinkedIn
# also exports its own assistants' chat logs with the messages header. The
# known ones are skipped by name in netkeeper.linkedin.archive
# (NOT_CONVERSATIONS); a new one would read as conversations with contacts,
# and this is how that shows up instead of passing unnoticed (#74).
MESSAGES_FILENAME: Final = "messages.csv"

# How much of a message body an interaction's summary keeps. Triage reads it as
# evidence, so it holds the message rather than a label, but a body is a CSV
# field with no length limit and the timeline is not a mail reader.
SUMMARY_MAX_CHARS: Final = 2000
# How far _trim will back off from the cap to land on whitespace rather than
# split a word. A single token longer than this window (a URL, most often)
# still gets a hard cut; anything shorter loses at most this many characters
# to land on a boundary that means something for plain text.
_BOUNDARY_LOOKBACK: Final = 200

# Tags that separate the plain text they wrap from what comes next; folded to
# a newline rather than deleted so "<p>one</p><p>two</p>" reads as two lines,
# not "onetwo". Only tags LinkedIn's own rich-text editor (Quill, per the
# "spinmail-quill-editor" class LinkedIn ships) is actually known to emit: an
# arbitrary bracketed word is not assumed to be one of these (see
# _KNOWN_TAGS below on why that assumption is exactly the bug this replaced).
_BLOCK_TAGS: Final[frozenset[str]] = frozenset(
    {"p", "div", "br", "li", "tr", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol"}
)
# Formatting tags folded away with no separator, so the text they wrap reads
# as part of the surrounding sentence. Quill's own defaults for bold/italic
# are "strong"/"em", not the shorter "b"/"i" -- which are deliberately left
# off this list, because a single letter right after "<" is exactly the shape
# a person's own plain-text angle brackets are most likely to collide with
# ("<b and c>", "<i>" as a roman numeral aside). "img" has no useful text of
# its own to keep. "script" and "style" are here for a different reason: they
# are the two tags nobody types as prose and the two whose stored source text
# would most alarm anyone who read the column, so their text is kept and the
# tag itself goes. Folded, not deleted with their contents: an unclosed
# "<script" puts HTMLParser into CDATA mode and everything after it is the
# element's content, so deleting content would lose the rest of the message --
# the exact failure this conversion exists to avoid.
_INLINE_TAGS: Final[frozenset[str]] = frozenset(
    {"a", "strong", "em", "u", "span", "img", "script", "style"}
)
_KNOWN_TAGS: Final[frozenset[str]] = _BLOCK_TAGS | _INLINE_TAGS


class _TextExtractor(HTMLParser):
    """Plain text from an HTML fragment: entities unescaped, known tags dropped (#75).

    ``convert_charrefs=True`` does the unescaping. An unterminated tag at the
    end -- exactly the shape a body cut at a raw character limit used to leave
    behind -- is simply never emitted: the parser buffers it waiting for a
    ``>`` that never comes and drops the buffer on :meth:`close`, which is why
    this runs on the full body *before* :func:`_trim` cuts it, not after.

    A tag whose name is not in :data:`_KNOWN_TAGS` is not assumed to be markup
    at all: its exact source text is put back rather than discarded
    (:meth:`HTMLParser.get_starttag_text`, or a reconstructed ``</tag>`` for
    an end tag, since the parser keeps no equivalent for those). LinkedIn's
    own editor never emits anything outside the known set, so this only ever
    preserves the other case -- a person's own text that happens to contain a
    ``<``, an email address in angle brackets, a ``<placeholder>``, or
    anything else no importer should be guessing is safe to delete. The
    tradeoff is real: an unrecognized element (a genuinely unusual tag, or
    deliberately adversarial input) can end up stored looking tag-shaped.
    That is an accepted cost here, not an oversight -- losing what someone
    actually wrote is the worse failure, and the API's own contract already
    requires every renderer to escape this field regardless of what an
    importer thought it was (see ``InteractionOut.summary``), so nothing
    downstream trusts this text to be markup-free for safety.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")
        elif tag in _INLINE_TAGS:
            pass
        else:
            self._parts.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")
        elif tag in _INLINE_TAGS:
            pass
        else:
            self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def close(self) -> None:
        # Whatever is still buffered at the end of input is settled here, not by
        # HTMLParser.close(), whose handling of it changed across 3.12 patch
        # releases (#231). This matches the newest ones (3.12.12+, 3.13): an
        # unclosed <script> or <style> keeps the rest of the message, raw, as its
        # text (3.12.0-3.12.11 drop it); an unterminated tag, comment or
        # declaration is never emitted (3.12.11 alone emits it as text). A bare
        # "<" or "</" stays text on every release, so super() gets those, and any
        # tail that is not markup at all, such as a pending "&amp".
        tail, self.rawdata = self.rawdata, ""
        if self.cdata_elem is not None:
            self.handle_data(tail)
        elif not tail.startswith("<") or tail in ("<", "</"):
            self.rawdata = tail
        super().close()

    def text(self) -> str:
        return "".join(self._parts)


def _plain_pass(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    lines = (line.strip() for line in parser.text().splitlines())
    return "\n".join(line for line in lines if line)


def _html_to_text(value: str) -> str:
    """``value`` as plain text: entities unescaped, known tags stripped (#75).

    LinkedIn InMail arrives as an HTML fragment
    (``<p class="spinmail-quill-editor">...</p>``); the evidence panel is worth
    more when it shows what was actually written than when it shows markup, so
    this runs at import and what lands in ``interactions.summary`` is text a
    person can read.

    Run twice. Unescaping happens inline as the parser reads *data* -- a body
    that says ``&lt;p&gt;`` rather than ``<p>`` decodes to a literal ``<p>``
    sitting in that data, never reinterpreted as a tag within the same pass,
    so the first pass alone would leave a real, recognized tag sitting in the
    output as plain text once unescaped. The second pass parses whatever the
    first pass revealed the same way the first parsed the original text, so a
    real tag is stripped either way it arrives; once nothing new is revealed
    the second pass is a no-op, so the result is stable under further passes.
    """
    return _plain_pass(_plain_pass(value))


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
    did to the contacts this import created or enriched. ``run_id`` is the
    ``import_runs`` row that records the import (#132).

    ``unfamiliar_message_files`` names every member read as the messages table
    whose file name is not ``messages.csv`` (#74). Each was imported like any
    other, but it may be an assistant chat log LinkedIn added after
    :data:`netkeeper.linkedin.archive.NOT_CONVERSATIONS` was written, whose
    rows then show up in the message counts (``no_counterpart`` or
    ``no_owner``, since an assistant has no profile URL) and never as
    interactions.
    """

    observed_at: datetime
    run_id: int | None = None
    owner_public_id: str | None = None
    owner_by: str | None = None
    connections: ConnectionCounts = field(default_factory=ConnectionCounts)
    messages: MessageCounts = field(default_factory=MessageCounts)
    invitations: InvitationCounts = field(default_factory=InvitationCounts)
    positions: PositionCounts = field(default_factory=PositionCounts)
    tagging: RuleRun = field(default_factory=lambda: RuleRun(0, 0, 0, 0))
    unfamiliar_message_files: list[str] = field(default_factory=list)


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
    run = ImportRun(
        user_id=user.id,
        source_kind=ImportSourceKind.ARCHIVE,
        filename=archive.name[:FILENAME_MAX_LENGTH],
        preset=None,
        mapping_json=dict(ARCHIVE_ROW_MAPPING),
        status=ImportStatus.DRAFT,
    )
    session.add(run)
    touched: list[int] = []
    connections = [
        row
        for member in archive.members
        if member.kind is ArchiveKind.CONNECTIONS
        for row in archive.connections(member)
    ]
    _import_connections(session, user, run, connections, when, report.connections, touched)
    for member in archive.members:
        if member.kind is ArchiveKind.POSITIONS:
            _import_positions(session, user, archive.positions(member), when, report)
    written = _Interactions(session, user)
    profile_name = archive.owner_name()
    for member in archive.members:
        if member.kind is ArchiveKind.MESSAGES:
            _note_unfamiliar_messages(member.name, report)
            _import_messages(
                archive.messages(member), owner_public_id, profile_name, written, report
            )
    for member in archive.members:
        if member.kind is ArchiveKind.INVITATIONS:
            _import_invitations(archive.invitations(member), written, report)
    # The rules, over the contacts this import touched and no others, in the
    # caller's transaction (spec 10.3, #64): a freshly imported address book is
    # tagged when the import returns, with no button to find and press first.
    # The defaults are seeded here too, because an import is often the first
    # thing that ever happens to a database -- `netkeeper import archive` before
    # the server has ever started -- and rules that do not exist tag nobody.
    # It is idempotent and records itself, so a default the user deleted stays
    # deleted (netkeeper.crm.tags.ensure_default_rules).
    ensure_default_rules(session, user)
    report.tagging = run_rules(session, user, sorted(set(touched)))
    _finish_run(run, report, written.created, ignored=archive.ignored)
    session.flush()
    report.run_id = run.id
    log.info(
        "archive %s imported for user %d as import run %d: %d connections (%d created, %d updated, "
        "%d for review), %d positions (%d created, %d updated), %d message interactions, "
        "%d invitation interactions",
        archive.name,
        user.id,
        run.id,
        report.connections.rows,
        report.connections.created,
        report.connections.updated,
        report.connections.needs_review,
        report.positions.rows,
        report.positions.created,
        report.positions.updated,
        report.messages.added,
        report.invitations.added,
    )
    return report


# --- positions ----------------------------------------------------------


def _finish_run(
    run: ImportRun, report: ArchiveImport, interactions: list[int], *, ignored: Iterable[str]
) -> None:
    """Mark ``run`` committed with the import's counts, and what it created by id (#132)."""
    counts = report.connections
    run.total_rows = counts.rows
    run.matched_count = counts.updated
    run.created_count = counts.created
    run.candidate_count = counts.needs_review
    run.skipped_count = counts.skipped
    run.tagged_contacts = report.tagging.contacts
    run.tags_added = report.tagging.added
    run.tags_removed = report.tagging.removed
    run.report_json = report_json(report, ignored_files=ignored)
    run.created_json = {Interaction.__tablename__: sorted(interactions)} if interactions else None
    run.status = ImportStatus.COMMITTED
    run.committed_at = utcnow()


def report_json(report: ArchiveImport, *, ignored_files: Iterable[str] = ()) -> dict[str, Any]:
    """``report`` as ``import_runs.report_json`` keeps it: every table's counts, JSON-ready."""
    return {
        "observed_at": report.observed_at.isoformat(),
        "owner_public_id": report.owner_public_id,
        "owner_by": report.owner_by,
        "connections": asdict(report.connections),
        "messages": asdict(report.messages),
        "invitations": asdict(report.invitations),
        "positions": asdict(report.positions),
        "ignored_files": list(ignored_files),
        "unfamiliar_message_files": list(report.unfamiliar_message_files),
    }


def _import_positions(
    session: Session,
    user: User,
    rows: Iterable[PositionRow],
    observed_at: datetime,
    report: ArchiveImport,
) -> None:
    counted = positions_service.import_positions(
        session, user, rows, source=ContactSource.ARCHIVE, observed_at=observed_at
    )
    counts = report.positions
    counts.rows += counted.rows
    counts.created += counted.created
    counts.updated += counted.updated
    counts.unchanged += counted.unchanged
    counts.skipped += counted.skipped
    counts.undated += counted.undated


# --- connections ------------------------------------------------------------


def _import_connections(
    session: Session,
    user: User,
    run: ImportRun,
    rows: Iterable[ConnectionRow],
    observed_at: datetime,
    counts: ConnectionCounts,
    touched: list[int],
) -> None:
    """Import every row as a row of ``run``, collecting into ``touched`` the contacts written.

    Each row is recorded the way a CSV commit records one (#132): what it
    resolved to, and what it changed, in the shape
    :func:`netkeeper.crm.import_runs.rollback` reads.
    """
    planned = [(row, _incoming_contact(row, observed_at)) for row in rows]
    held = prefetch_contacts(  # noqa: F841 - held so the identity map keeps what it loaded
        session, user, [incoming for _, incoming in planned if incoming is not None]
    )
    for number, (row, incoming) in enumerate(planned, start=1):
        counts.rows += 1
        if row.connected_on is None:
            counts.undated += 1
        record = ImportRow(
            user_id=user.id,
            row_number=number,
            raw_json=_raw_cells(row),
            resolution=ImportResolution.SKIPPED,
        )
        run.rows.append(record)
        if incoming is None:
            counts.skipped += 1
            record.error = "no profile URL and no name: this row names nobody"
            continue
        if incoming.emails:
            counts.with_email += 1
        resolution = resolve(session, user, incoming)
        match resolution:
            case Candidate(contact_ids=contact_ids):
                counts.needs_review += 1
                record.resolution = ImportResolution.CANDIDATE
                record.candidate_ids_json = list(contact_ids)
                record.error = (
                    "matches more than one contact; left alone for a CSV import to decide"
                )
            case Matched(contact_id=contact_id, by=by):
                before = snapshot_contact(resolve_survivor(session, user, contact_id))
                contact = apply(session, user, incoming, resolution)
                touched.append(contact.id)
                counts.updated += 1
                record.resolution = ImportResolution.MATCHED
                record.contact_id = contact.id
                record.matched_by = by
                record.changes_json = contact_effect(before, contact)
            case New():
                contact = apply(session, user, incoming, resolution)
                touched.append(contact.id)
                counts.created += 1
                record.resolution = ImportResolution.CREATED
                record.contact_id = contact.id
                record.changes_json = created_effect()


ARCHIVE_ROW_MAPPING: Final[dict[str, str]] = {
    "First Name": "first_name",
    "Last Name": "last_name",
    "URL": "li_url",
    "Email Address": "email",
    "Company": "current_company",
    "Position": "current_title",
    "Connected On": "connected_on",
}
"""What each column of an archive run's ``raw_json`` is, as a CSV run's mapping says it (#132)."""


def _raw_cells(row: ConnectionRow) -> dict[str, str]:
    """A connection row as ``import_rows.raw_json`` keeps one: its columns, as text."""
    return {
        "First Name": row.first_name,
        "Last Name": row.last_name,
        "URL": row.url or "",
        "Email Address": row.email or "",
        "Company": row.company or "",
        "Position": row.position or "",
        "Connected On": row.connected_on.isoformat() if row.connected_on is not None else "",
    }


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


def _note_unfamiliar_messages(name: str, report: ArchiveImport) -> None:
    """Report a messages-shaped member not named ``messages.csv`` (#74).

    Compared on the base name, case-folded, with either separator, the way
    the reader's own skip list is: ``export/Messages.csv`` is the familiar
    table in a subdirectory, ``interview_prep_messages.csv`` is not.
    """
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    if base.casefold() == MESSAGES_FILENAME:
        return
    report.unfamiliar_message_files.append(name)
    log.warning(
        "archive: %s has the messages header but is not %s; importing it as message "
        "history, which is wrong if it is an assistant chat log",
        name,
        MESSAGES_FILENAME,
    )


def _note_owner(owner: Owner, report: ArchiveImport) -> None:
    if report.owner_public_id is not None and report.owner_public_id != owner.public_id:
        log.warning("archive: message tables disagree on the owner; keeping the first")
        return
    report.owner_public_id = owner.public_id
    report.owner_by = owner.by


def _message_summary(row: MessageRow) -> str | None:
    """A message as triage evidence: its subject and body, as plain text, trimmed (#75)."""
    parts = [_html_to_text(part) for part in (row.subject, row.content) if part]
    parts = [part for part in parts if part]
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
    """The invitation's note, as plain text and marked as one; the marker alone with no note."""
    if row.message is None:
        return INVITATION_SUMMARY
    note = _html_to_text(row.message)
    if not note:
        return INVITATION_SUMMARY
    return _trim(f"{INVITATION_SUMMARY}: {note}")


# --- writing interactions ---------------------------------------------------


class _Interactions:
    """Resolves counterparts to contacts once each, and writes interactions once each.

    Two lookups an import would otherwise repeat thousands of times. The slug
    cache holds the contact a profile slug resolves to, or ``None`` for one that
    is nobody, for as long as the import runs; nothing here creates a contact,
    so an entry cannot go stale underneath it. The interaction ledger is every
    ``(contact, kind, second)`` this user already has from an earlier archive
    import, counted, so a re-import recognizes its own work and adds only what
    is new. See the module docstring on why that triple is the key. It also
    counts the ``li_in`` and ``li_out`` rows the LinkedIn inbox poll recorded
    (``source = sync``, P4-08), so an archive imported after a poll does not
    record a polled message a second time. The poll truncates its times to whole
    seconds, so the ledger keys on the second.
    """

    __slots__ = ("_contacts", "_seen", "_session", "_user", "created")

    def __init__(self, session: Session, user: User) -> None:
        self._session = session
        self._user = user
        self._contacts: dict[str, int | None] = {}
        self.created: list[int] = []
        """The ids of the interactions this import wrote, for its run (#132)."""
        self._seen: dict[tuple[int, InteractionKind, datetime], int] = {}
        statement = scoped(user, Interaction).where(
            or_(
                Interaction.source == ContactSource.ARCHIVE,
                and_(
                    Interaction.source == ContactSource.SYNC,
                    Interaction.kind.in_(_POLLED_KINDS),
                ),
            )
        )
        for row in session.scalars(statement):
            key = (row.contact_id, row.kind, _second(row.at))
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
        key = (contact_id, kind, _second(at))
        planned = self._seen.get(key, 0)
        if planned > 0:
            self._seen[key] = planned - 1
            return False
        interaction = add_interaction(
            self._session,
            self._user,
            contact_id,
            kind,
            at,
            summary,
            source=ContactSource.ARCHIVE,
        )
        self.created.append(interaction.id)
        return True


#: The kinds the LinkedIn inbox poll records (P4-08), which the ledger also counts.
_POLLED_KINDS: Final = (InteractionKind.LI_IN, InteractionKind.LI_OUT)


def _second(at: datetime) -> datetime:
    """``at`` truncated to the whole second, the ledger's resolution."""
    return at.replace(microsecond=0)


def _trim(text: str) -> str | None:
    """``text`` at most :data:`SUMMARY_MAX_CHARS` long, with an ellipsis when cut; empty is None.

    ``text`` is plain text by the time this runs (#75), so the boundary that
    matters is whitespace, not a tag: a cut backs off, within
    :data:`_BOUNDARY_LOOKBACK` characters, to the nearest space or newline
    rather than split a word. A single token longer than that window -- a URL,
    most often -- gets a hard cut, same as before.
    """
    stripped = text.strip()
    if not stripped:
        return None
    if len(stripped) <= SUMMARY_MAX_CHARS:
        return stripped
    limit = SUMMARY_MAX_CHARS - 1
    cut = stripped[:limit]
    window_start = max(0, limit - _BOUNDARY_LOOKBACK)
    boundary = max(cut.rfind(" ", window_start), cut.rfind("\n", window_start))
    if boundary > window_start:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


def _require_writer(session: Session) -> None:
    """Fail at once rather than with "database is locked" on the first write (CLAUDE.md)."""
    if not is_writer(session):
        raise RuntimeError(
            "importing an archive needs a writer session; use session_scope(factory, write=True)"
        )
