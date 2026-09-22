"""Read a LinkedIn data archive: ``Connections.csv``, ``messages.csv``, ``Invitations.csv``.

Spec 9.2, the archive row of the data-sources table.

Pure parsing behind the extractor boundary (spec 9.10, ADR 0005): nothing here
imports the models or opens a session. A zip or a single CSV goes in; typed,
frozen rows come out one table at a time, streamed, so an archive with years
of messages is never held in memory at once. The core's importer
(:mod:`netkeeper.crm.archive`) maps the rows onto contacts and interactions.

What the archive looks like (LinkedIn's "Settings & Privacy > Data privacy >
Get a copy of your data"): a zip of CSV files in UTF-8, some starting with a
byte-order mark. ``Connections.csv`` opens with a preamble before its header
(a line ``Notes:``, a sentence about missing email addresses, and a blank
line). Tables are recognized by their header, never by their file name, so a
member with any name, or a single CSV uploaded on its own, reads the same way.
Unknown columns are ignored; a member that is none of the three tables is
skipped; a table that lacks a column the reader needs is an
:class:`ArchiveFormatError` naming the file.

Dates: ``Connected On`` is a calendar date, day first and month abbreviated
(``12 Mar 2019``, and ``12 Mar 19`` in exports old enough to write the year
with two digits); a message ``DATE`` is ``2023-05-01 14:22:10``, with or
without a trailing ``UTC`` depending on the export, and either way it is read
as UTC because that is the only zone LinkedIn ever names here; an invitation
``Sent At`` (``5/12/21, 3:14 PM``, month first) names no zone and is read as
UTC too, the closest thing to a documented meaning it has. An unparseable date
is ``None`` on the row rather than an error: the row is still a record of a
person or a message, and the importer decides what to do without a time and
counts how often it had to. A cell that held something and still did not parse
is logged at warning level with the member and the row number, never the cell,
so a change in LinkedIn's formats is visible without putting a date of a real
person's life in a log file.
"""

from __future__ import annotations

import csv
import enum
import io
import logging
import re
import struct
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import IO, Final, TextIO
from urllib.parse import unquote

log = logging.getLogger(__name__)

# How many records to look through for a header before giving up on a table.
# The header is one of them, so this tolerates a preamble one record shorter.
# LinkedIn's is three records; the allowance leaves room for it to grow.
MAX_HEADER_SEARCH_RECORDS: Final = 10
# A message body is a CSV field; the module default (128 KiB) is too small for
# the longest ones.
FIELD_SIZE_LIMIT: Final = 16 * 1024 * 1024
# A zip member larger than this, uncompressed, is refused before it is read.
# messages.csv for a heavy user is tens of megabytes; this is far above that
# and keeps a crafted archive from expanding without bound.
MAX_MEMBER_BYTES: Final = 512 * 1024 * 1024
# Every declared uncompressed size in a zip, summed, refused before any member
# is opened. A real export's total is a couple of megabytes (the sample this
# was checked against: about 1.4 MB across thirty-odd members, 34 files); 64 MiB
# is still about 45x that, but it also directly bounds how much decompression a
# hostile zip can buy under the ratio guard below, so it is kept much closer to
# a real export's size than :data:`MAX_MEMBER_BYTES` is.
MAX_TOTAL_UNCOMPRESSED_BYTES: Final = 64 * 1024 * 1024
# How many members a zip may declare. The sample above has 34 files, and a
# "Basic" LinkedIn export is not the largest kind: a "Complete" export, or one
# with message attachments, plausibly has several times that. This is checked
# from the End Of Central Directory record before :class:`zipfile.ZipFile` ever
# parses an entry (:func:`_declared_member_count`), so raising it does not
# raise the cost of refusing a "many tiny files" zip — that cost is paid only
# up to whatever a hostile zip declares, and a zip declaring more than this is
# refused without a single entry being materialized.
MAX_MEMBERS: Final = 5000
# A member's declared uncompressed size divided by its declared compressed
# size, refused above this ratio — the classic zip bomb shape, a few bytes of
# input inflating to gigabytes. Ordinary CSV text under DEFLATE rarely clears
# single digits. The ratio is only judged once a member is big enough for the
# question to matter (below the floor, a high ratio is just DEFLATE doing well
# on a short repetitive cell, not a bomb); the sample archive's largest member,
# messages.csv, compresses at under 6x and stays well under the floor too, so
# nothing in a real export exercises this branch — the boundary is exercised by
# test, not by any export this was checked against.
MAX_COMPRESSION_RATIO: Final = 100
COMPRESSION_RATIO_FLOOR_BYTES: Final = 8 * 1024 * 1024
# Zip member timestamps older than LinkedIn itself are placeholders, not the export time.
EARLIEST_EXPORT: Final = datetime(2003, 1, 1, tzinfo=UTC)

# The signature and fixed-size layout of the record :func:`_declared_member_count`
# reads: the End Of Central Directory record (and, for a zip large enough to
# need it, the Zip64 locator and record that stand in for its 16-bit fields).
_EOCD_SIGNATURE: Final = b"PK\x05\x06"
_EOCD_FIXED_SIZE: Final = 22
_EOCD_MAX_COMMENT: Final = 65535
_ZIP64_EOCD_LOCATOR_SIGNATURE: Final = b"PK\x06\x07"
_ZIP64_EOCD_LOCATOR_SIZE: Final = 20
_ZIP64_EOCD_SIGNATURE: Final = b"PK\x06\x06"
_ZIP64_EOCD_FIXED_SIZE: Final = 56
# The fixed part of one central-directory record, before its variable-length
# filename, extra field, and comment (``zipfile.sizeCentralDir``). A record
# cannot be smaller than this, so the central directory's declared byte size
# divided by it is an upper bound on how many entries it could possibly hold —
# read alongside the entry count itself, in case the two disagree.
_CENTRAL_DIR_RECORD_MIN_SIZE: Final = 46

_PROFILE_URL = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)

# Members whose header is the messages header but whose rows are not messages
# between people: LinkedIn exports its own assistants' chat logs in the same
# shape. Recognizing tables by header alone would import a coaching session as
# a conversation with a contact, so these three are skipped by name. Names are
# the only thing that separates them; the rows are indistinguishable.
NOT_CONVERSATIONS: Final[frozenset[str]] = frozenset(
    {
        "guide_messages.csv",
        "learning_coach_messages.csv",
        "learning_role_play_messages.csv",
    }
)


class ArchiveRefusalCode(enum.StrEnum):
    """Why an upload was refused, stable across a reword of the message.

    The API endpoint this feeds (P1-20) puts one of these on every ``422`` it
    answers for a bad archive, alongside the human-readable message, so the
    wizard built on top of it (P1-21) can key off the code instead of
    matching the message's words — which broke the moment this module's own
    wording changed underneath it. Named for what happened, not for which
    guard or code path happens to catch it today, so a later refactor here
    never forces a rename a client has to follow.
    """

    NOT_A_ZIP = "not_a_zip"
    """The upload is not a zip, and not a single recognizable table either."""
    WRONG_ARCHIVE = "wrong_archive"
    """A real zip (or directory, or single table), but none of the three this
    importer reads are anywhere in it."""
    NESTED_ZIP = "nested_zip"
    """The zip holds nothing but another zip. Not unwrapped; extract it by hand."""
    ENCRYPTED = "encrypted"
    """A member is password-protected."""
    DAMAGED = "damaged"
    """A member's declared metadata does not match its actual bytes — a lie or
    (far more often) an ordinary corruption in transit."""
    MALFORMED_TABLE = "malformed_table"
    """A recognized table is missing a column the reader needs, or a table's
    rows are not readable as UTF-8 CSV at all."""
    TOO_LARGE = "too_large"
    """The upload itself, the zip's total declared size, or one member's
    declared size, is over its limit."""
    TOO_MANY_MEMBERS = "too_many_members"
    """The zip (or directory) declares more members than the limit."""
    COMPRESSION_RATIO_TOO_HIGH = "compression_ratio_too_high"
    """A member's declared compression ratio marks it as a zip bomb."""
    UNSAFE_MEMBER_PATH = "unsafe_member_path"
    """A member's path would escape wherever it was ever used as one."""


class ArchiveFormatError(ValueError):
    """The file is not a LinkedIn archive, or one of its tables cannot be read.

    The message names the file (and inside a zip, the member) the problem is
    in; ``code`` is the stable, machine-readable reason behind it
    (:class:`ArchiveRefusalCode`), required on every instance so a refusal can
    never reach a caller without one.
    """

    def __init__(self, message: str, code: ArchiveRefusalCode) -> None:
        super().__init__(message)
        self.code = code


class ArchiveKind(enum.StrEnum):
    """The tables this reader recognizes.

    The first three carry the rows an import writes. ``PROFILE`` is the
    archive's own owner, which no import writes but which names whose archive
    this is when the message traffic cannot say.
    """

    CONNECTIONS = "connections"
    MESSAGES = "messages"
    INVITATIONS = "invitations"
    PROFILE = "profile"


IMPORTABLE_KINDS: Final[frozenset[ArchiveKind]] = frozenset(
    {ArchiveKind.CONNECTIONS, ArchiveKind.MESSAGES, ArchiveKind.INVITATIONS}
)
"""The kinds that carry rows to import. An archive with none of them is useless."""


class InvitationDirection(enum.StrEnum):
    OUTGOING = "OUTGOING"
    INCOMING = "INCOMING"


# --- rows -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectionRow:
    """One line of ``Connections.csv``: a 1st-degree connection.

    ``row_number`` counts records after the header from 1. ``public_id`` is the
    slug of ``url``; ``email`` is lowercased and present only for people who
    let their connections see it. Empty cells are ``None``.
    """

    row_number: int
    first_name: str
    last_name: str
    url: str | None
    public_id: str | None
    email: str | None
    company: str | None
    position: str | None
    connected_on: date | None


@dataclass(frozen=True, slots=True)
class MessageRow:
    """One line of ``messages.csv``: a message in one of the owner's conversations.

    The archive owner appears as the sender or among the recipients; nothing
    in the file says which name is theirs. ``recipients`` and
    ``recipient_urls`` are the comma-separated lists as LinkedIn writes them;
    ``recipient_public_ids`` holds the slugs of the URLs that are profile URLs,
    so it can be shorter than ``recipients``. ``sent_at`` is aware UTC, or
    ``None`` when the date did not parse.
    """

    row_number: int
    conversation_id: str
    conversation_title: str | None
    sender: str
    sender_url: str | None
    sender_public_id: str | None
    recipients: tuple[str, ...]
    recipient_urls: tuple[str, ...]
    recipient_public_ids: tuple[str, ...]
    sent_at: datetime | None
    subject: str | None
    content: str
    folder: str | None


@dataclass(frozen=True, slots=True)
class InvitationRow:
    """One line of ``Invitations.csv``: a connection invitation the owner sent or received.

    ``direction`` says which; ``None`` when the cell is neither ``OUTGOING``
    nor ``INCOMING``. The profile URLs, and the slugs derived from them, are
    the newer columns and may be absent in an old export.
    """

    row_number: int
    sender: str
    recipient: str
    sent_at: datetime | None
    message: str | None
    direction: InvitationDirection | None
    inviter_url: str | None
    invitee_url: str | None
    inviter_public_id: str | None
    invitee_public_id: str | None


@dataclass(frozen=True, slots=True)
class ProfileRow:
    """One line of ``Profile.csv``: the archive owner's own profile.

    LinkedIn writes exactly one. It carries no profile URL, so the only thing
    here that identifies the owner is their name.
    """

    row_number: int
    first_name: str
    last_name: str
    headline: str | None

    @property
    def full_name(self) -> str | None:
        """``"First Last"`` as the owner's messages show it, or ``None`` when unnamed."""
        name = f"{self.first_name} {self.last_name}".strip()
        return name or None


ArchiveRow = ConnectionRow | MessageRow | InvitationRow | ProfileRow


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    """A recognized table inside an :class:`Archive`.

    ``name`` is the member's path inside the zip or the unpacked directory, or
    the file name of a single CSV. ``modified_at`` is the member's timestamp as
    UTC when it is plausible (after :data:`EARLIEST_EXPORT` and not in the
    future), else ``None``; a single CSV read from a stream has none. A zip
    member's is approximate, to the exporting machine's UTC offset: see
    :func:`_zip_time`.
    """

    name: str
    kind: ArchiveKind
    modified_at: datetime | None


# --- parsing helpers --------------------------------------------------------


def public_id_from_url(url: str | None) -> str | None:
    """The ``/in/`` slug of a LinkedIn profile URL, lowercased and URL-decoded, or ``None``.

    The importer's identity resolution normalizes the same way; this copy keeps
    the parser free of core imports (spec 9.10).
    """
    if url is None:
        return None
    match = _PROFILE_URL.search(url.strip())
    if match is None:
        return None
    slug = unquote(match.group(1)).strip().lower()
    return slug or None


def parse_connected_on(text: str | None) -> date | None:
    """``12 Mar 2019``, or an old export's ``12 Mar 19``, as a date; else ``None``.

    The two forms cannot be confused: ``%Y`` matches four digits and ``%y``
    exactly two, so a year is read the way it was written.
    """
    for pattern in ("%d %b %Y", "%d %b %y"):
        parsed = _parse(text, pattern, lambda value: value.date())
        if parsed is not None:
            return parsed
    return None


def parse_message_date(text: str | None) -> datetime | None:
    """``2023-05-01 14:22:10``, with or without ``UTC``, as an aware UTC datetime.

    ``None`` when unparseable. Exports differ on the suffix; neither form names
    another zone, so both are read as UTC.
    """
    for pattern in ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M:%S"):
        parsed = _parse(text, pattern, lambda value: value.replace(tzinfo=UTC))
        if parsed is not None:
            return parsed
    return None


def parse_invitation_date(text: str | None) -> datetime | None:
    """``5/12/21, 3:14 PM`` (month first, as LinkedIn writes it) as aware UTC; else ``None``."""
    return _parse(text, "%m/%d/%y, %I:%M %p", lambda value: value.replace(tzinfo=UTC))


def _parse[T](text: str | None, pattern: str, convert: Callable[[datetime], T]) -> T | None:
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        return convert(datetime.strptime(cleaned, pattern))  # convert applies the zone
    except ValueError:
        return None


def _normalize(name: str) -> str:
    return name.strip().strip("﻿").casefold()


def _split_list(text: str) -> tuple[str, ...]:
    """A comma-separated cell as LinkedIn writes recipients and their URLs."""
    return tuple(part.strip() for part in text.split(",") if part.strip())


class _Fields:
    """Cell access by normalized column name for one record. Missing or empty is ``None``."""

    __slots__ = ("columns", "record")

    def __init__(self, columns: dict[str, int], record: list[str]) -> None:
        self.columns = columns
        self.record = record

    def get(self, column: str) -> str | None:
        index = self.columns.get(column)
        if index is None or index >= len(self.record):
            return None
        value = self.record[index].strip()
        return value or None

    def text(self, column: str) -> str:
        return self.get(column) or ""


# --- headers ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Layout:
    """How a table is recognized (``signature``) and what the reader needs (``required``)."""

    kind: ArchiveKind
    signature: frozenset[str]
    required: frozenset[str]


# The signatures are disjoint from every other file in a real archive, which
# was checked against all thirty-odd members of one. Several carry First Name
# and Last Name (Receipts, Verifications, the endorsement tables); only
# Connections.csv adds Connected On, and only Profile.csv adds Headline.
# Connections comes first, so a table that somehow had both reads as the one
# with rows worth importing.
LAYOUTS: Final[tuple[_Layout, ...]] = (
    _Layout(
        ArchiveKind.CONNECTIONS,
        signature=frozenset({"first name", "last name", "connected on"}),
        required=frozenset({"url"}),
    ),
    _Layout(
        ArchiveKind.MESSAGES,
        signature=frozenset({"conversation id", "from", "to"}),
        required=frozenset({"sender profile url", "recipient profile urls", "date", "content"}),
    ),
    _Layout(
        ArchiveKind.INVITATIONS,
        signature=frozenset({"direction", "sent at"}),
        required=frozenset({"from", "to"}),
    ),
    _Layout(
        ArchiveKind.PROFILE,
        signature=frozenset({"first name", "last name", "headline"}),
        required=frozenset(),
    ),
)


@dataclass(frozen=True, slots=True)
class _Header:
    kind: ArchiveKind
    columns: dict[str, int]


def _records(text: TextIO, name: str) -> Iterator[list[str]]:
    """CSV records from ``text``; a decoding or CSV error becomes an :class:`ArchiveFormatError`."""
    csv.field_size_limit(FIELD_SIZE_LIMIT)
    try:
        yield from csv.reader(text)
    except (csv.Error, UnicodeDecodeError) as exc:
        raise ArchiveFormatError(
            f"{name}: cannot be read as UTF-8 CSV: {exc}", ArchiveRefusalCode.MALFORMED_TABLE
        ) from exc


def _find_header(records: Iterator[list[str]], name: str) -> _Header | None:
    """Consume records up to and including the header; ``None`` when no table is recognized.

    A record that carries a table's signature but lacks a column the reader
    needs is that table, malformed: an :class:`ArchiveFormatError`.
    """
    for index, record in enumerate(records):
        if index >= MAX_HEADER_SEARCH_RECORDS:
            break
        names = [_normalize(cell) for cell in record]
        present = set(names)
        for layout in LAYOUTS:
            if not layout.signature <= present:
                continue
            missing = sorted(layout.required - present)
            if missing:
                raise ArchiveFormatError(
                    f"{name}: the {layout.kind.value} table is missing "
                    f"column(s) {', '.join(missing)}",
                    ArchiveRefusalCode.MALFORMED_TABLE,
                )
            columns: dict[str, int] = {}
            for position, column in enumerate(names):
                if column and column not in columns:  # the first of a repeated name wins
                    columns[column] = position
            return _Header(layout.kind, columns)
    return None


def _table(text: TextIO, name: str) -> tuple[_Header | None, Iterator[list[str]]]:
    """The header of the table in ``text`` and an iterator over the records after it."""
    records = _records(text, name)
    return _find_header(records, name), records


def _data_rows(records: Iterator[list[str]]) -> Iterator[tuple[int, list[str]]]:
    """``(row_number, record)`` for every non-blank record; numbering counts blank ones too."""
    for row_number, record in enumerate(records, start=1):
        if any(cell.strip() for cell in record):
            yield row_number, record


# --- row readers ------------------------------------------------------------


def read_connections(text: TextIO, name: str = "Connections.csv") -> Iterator[ConnectionRow]:
    """Stream :class:`ConnectionRow` from a ``Connections.csv`` text stream (preamble and all).

    ``ArchiveFormatError`` when ``text`` is not that table.
    """
    header, records = _table(text, name)
    header = _expect(header, ArchiveKind.CONNECTIONS, name)
    for row_number, record in _data_rows(records):
        fields = _Fields(header.columns, record)
        url = fields.get("url")
        connected_on = fields.get("connected on")
        yield ConnectionRow(
            row_number=row_number,
            first_name=fields.text("first name"),
            last_name=fields.text("last name"),
            url=url,
            public_id=public_id_from_url(url),
            email=_lower(fields.get("email address")),
            company=fields.get("company"),
            position=fields.get("position"),
            connected_on=_dated(parse_connected_on(connected_on), connected_on, name, row_number),
        )


def read_messages(text: TextIO, name: str = "messages.csv") -> Iterator[MessageRow]:
    """Stream :class:`MessageRow` from a ``messages.csv`` text stream.

    ``ArchiveFormatError`` when ``text`` is not that table.
    """
    header, records = _table(text, name)
    header = _expect(header, ArchiveKind.MESSAGES, name)
    for row_number, record in _data_rows(records):
        fields = _Fields(header.columns, record)
        sender_url = fields.get("sender profile url")
        sent_at = fields.get("date")
        recipient_urls = _split_list(fields.text("recipient profile urls"))
        yield MessageRow(
            row_number=row_number,
            conversation_id=fields.text("conversation id"),
            conversation_title=fields.get("conversation title"),
            sender=fields.text("from"),
            sender_url=sender_url,
            sender_public_id=public_id_from_url(sender_url),
            recipients=_split_list(fields.text("to")),
            recipient_urls=recipient_urls,
            recipient_public_ids=tuple(
                slug for url in recipient_urls if (slug := public_id_from_url(url)) is not None
            ),
            sent_at=_dated(parse_message_date(sent_at), sent_at, name, row_number),
            subject=fields.get("subject"),
            content=fields.text("content"),
            folder=fields.get("folder"),
        )


def read_invitations(text: TextIO, name: str = "Invitations.csv") -> Iterator[InvitationRow]:
    """Stream :class:`InvitationRow` from an ``Invitations.csv`` text stream.

    ``ArchiveFormatError`` when ``text`` is not that table.
    """
    header, records = _table(text, name)
    header = _expect(header, ArchiveKind.INVITATIONS, name)
    for row_number, record in _data_rows(records):
        fields = _Fields(header.columns, record)
        inviter_url = fields.get("inviterprofileurl")
        invitee_url = fields.get("inviteeprofileurl")
        sent_at = fields.get("sent at")
        yield InvitationRow(
            row_number=row_number,
            sender=fields.text("from"),
            recipient=fields.text("to"),
            sent_at=_dated(parse_invitation_date(sent_at), sent_at, name, row_number),
            message=fields.get("message"),
            direction=_direction(fields.get("direction")),
            inviter_url=inviter_url,
            invitee_url=invitee_url,
            inviter_public_id=public_id_from_url(inviter_url),
            invitee_public_id=public_id_from_url(invitee_url),
        )


def _dated[T](parsed: T | None, raw: str | None, name: str, row_number: int) -> T | None:
    """``parsed``, warning first when ``raw`` held something the parser could not read.

    An empty cell is silent: LinkedIn leaves dates out all the time. A cell with
    something in it that is not a date means a format changed, which is worth
    knowing; the cell itself is a date in a real person's life and stays out of
    the log (CLAUDE.md).
    """
    if parsed is None and raw is not None:
        log.warning("%s row %d: the date cell is in no form this reader knows", name, row_number)
    return parsed


def read_profile(text: TextIO, name: str = "Profile.csv") -> Iterator[ProfileRow]:
    """Stream :class:`ProfileRow` from a ``Profile.csv`` text stream.

    ``ArchiveFormatError`` when ``text`` is not that table.
    """
    header, records = _table(text, name)
    header = _expect(header, ArchiveKind.PROFILE, name)
    for row_number, record in _data_rows(records):
        fields = _Fields(header.columns, record)
        yield ProfileRow(
            row_number=row_number,
            first_name=fields.text("first name"),
            last_name=fields.text("last name"),
            headline=fields.get("headline"),
        )


def _expect(header: _Header | None, kind: ArchiveKind, name: str) -> _Header:
    if header is None:
        raise ArchiveFormatError(
            f"{name}: no LinkedIn {kind.value} table header found", ArchiveRefusalCode.WRONG_ARCHIVE
        )
    if header.kind is not kind:
        raise ArchiveFormatError(
            f"{name}: this is the {header.kind.value} table, not {kind.value}",
            ArchiveRefusalCode.WRONG_ARCHIVE,
        )
    return header


def _lower(value: str | None) -> str | None:
    return value.lower() if value is not None else None


def _direction(value: str | None) -> InvitationDirection | None:
    if value is None:
        return None
    try:
        return InvitationDirection(value.strip().upper())
    except ValueError:
        return None


# --- the archive ------------------------------------------------------------

_TextOpener = Callable[[], AbstractContextManager[TextIO]]


class Archive:
    """An open archive: its recognized tables, and a fresh row stream for each on demand.

    Get one from :func:`open_archive`. ``members`` lists the recognized tables
    in reading order (connections, messages, invitations, then by name);
    ``exported_at`` is the newest plausible member timestamp, the closest thing
    the archive has to an export time, or ``None`` when no member carries one.
    ``ignored`` names every other ``.csv`` table the scan found — a real
    header this reader does not recognize, such as ``Positions.csv`` until
    P1-20's companion item adds it — sorted for a stable report; a member
    skipped by name or extension (an assistant log, a macOS resource fork,
    anything not ``.csv``) never appears there, since that is noise the export
    format itself produces rather than data the person's report should call
    out. Each reader may be called more than once; every call starts the table
    over.
    """

    def __init__(
        self,
        name: str,
        members: list[tuple[ArchiveMember, _TextOpener]],
        close: Callable[[], None],
        *,
        ignored: Iterable[str] = (),
    ) -> None:
        self.name = name
        self._members = dict(members)
        self._close = close
        self.members: tuple[ArchiveMember, ...] = tuple(
            sorted(self._members, key=lambda member: (_KIND_ORDER[member.kind], member.name))
        )
        self.ignored: tuple[str, ...] = tuple(sorted(ignored))
        stamps = [member.modified_at for member in self.members if member.modified_at is not None]
        self.exported_at: datetime | None = max(stamps) if stamps else None

    def connections(self, member: ArchiveMember) -> Iterator[ConnectionRow]:
        """Stream the rows of a ``connections`` member. ``ValueError`` for another kind."""
        return self._stream(member, ArchiveKind.CONNECTIONS, read_connections)

    def messages(self, member: ArchiveMember) -> Iterator[MessageRow]:
        """Stream the rows of a ``messages`` member. ``ValueError`` for another kind."""
        return self._stream(member, ArchiveKind.MESSAGES, read_messages)

    def invitations(self, member: ArchiveMember) -> Iterator[InvitationRow]:
        """Stream the rows of an ``invitations`` member. ``ValueError`` for another kind."""
        return self._stream(member, ArchiveKind.INVITATIONS, read_invitations)

    def profile(self, member: ArchiveMember) -> Iterator[ProfileRow]:
        """Stream the rows of a ``profile`` member. ``ValueError`` for another kind."""
        return self._stream(member, ArchiveKind.PROFILE, read_profile)

    def owner_name(self) -> str | None:
        """The owner's name as ``Profile.csv`` writes it, or ``None`` when it is absent.

        The archive's only statement about whose it is;
        :mod:`netkeeper.linkedin.conversations` uses it to settle who the owner
        is when the message traffic alone cannot.
        """
        for member in self.members:
            if member.kind is not ArchiveKind.PROFILE:
                continue
            for row in self.profile(member):
                if (name := row.full_name) is not None:
                    return name
        return None

    def _stream[R](
        self,
        member: ArchiveMember,
        kind: ArchiveKind,
        reader: Callable[[TextIO, str], Iterator[R]],
    ) -> Iterator[R]:
        if member.kind is not kind:
            raise ValueError(f"{member.name} is the {member.kind.value} table, not {kind.value}")
        opener = self._members.get(member)
        if opener is None:
            raise ValueError(f"{member.name} is not a member of {self.name}")
        with opener() as text:
            yield from reader(text, member.name)

    def close(self) -> None:
        self._close()


_KIND_ORDER: Final[dict[ArchiveKind, int]] = {
    ArchiveKind.CONNECTIONS: 0,
    ArchiveKind.MESSAGES: 1,
    ArchiveKind.INVITATIONS: 2,
    ArchiveKind.PROFILE: 3,
}


@contextmanager
def open_archive(source: Path | IO[bytes], *, filename: str | None = None) -> Iterator[Archive]:
    """Open a LinkedIn archive and yield an :class:`Archive`.

    ``source`` is the export zip, a directory the zip was unpacked into, one of
    its CSVs on its own, or a seekable binary stream holding any of the first
    two (an upload). Whether a file is a zip is decided from its bytes, not its
    name; ``filename`` is what error messages call it (default: the path's
    name, or ``upload``).

    A zip and a directory are both scanned whole, subdirectories included, and
    a member is recognized by its header rather than its file name, so an
    export that renames or moves a table still reads. Two kinds of member are
    passed over on sight: anything that is not a ``.csv``, and the assistant
    chat logs in :data:`NOT_CONVERSATIONS`, whose header is the messages header
    but whose rows are not messages between people. Naming one of those files
    directly reads it anyway; that is an explicit request, not a scan.

    The scan happens once, on open, reading only the head of each member, so a
    table missing a column the reader needs, an archive with none of the three
    tables, a single CSV that is none of them, or a file that is neither zip
    nor CSV raise :class:`ArchiveFormatError` here, before any row is read. A
    stream passed in is left open; a path is closed on exit.
    """
    if isinstance(source, Path):
        if source.is_dir():
            archive = _open_dir(source, filename or source.name)
            try:
                yield archive
            finally:
                archive.close()
            return
        name = filename or source.name
        with source.open("rb") as handle:
            archive = _open(handle, name)
            try:
                yield archive
            finally:
                archive.close()
        return
    archive = _open(source, filename or "upload")
    try:
        yield archive
    finally:
        archive.close()


def _open(handle: IO[bytes], name: str) -> Archive:
    handle.seek(0)
    is_zip = zipfile.is_zipfile(handle)
    handle.seek(0)
    return _open_zip(handle, name) if is_zip else _open_csv(handle, name)


def _open_zip(handle: IO[bytes], name: str) -> Archive:
    # Read before ``zipfile.ZipFile`` ever runs, from the End Of Central
    # Directory record alone: a zip that declares more members than the limit
    # is refused without a single ``ZipInfo`` being built. ``ZipFile.__init__``
    # parses every central-directory entry up front, so checking only after
    # (as :func:`_guard_zip_members` used to) still pays for all of them first.
    declared = _declared_member_count(handle)
    if declared > MAX_MEMBERS:
        raise ArchiveFormatError(
            f"{name}: {declared} members in the zip, over the {MAX_MEMBERS} member limit",
            ArchiveRefusalCode.TOO_MANY_MEMBERS,
        )
    try:
        zf = zipfile.ZipFile(handle)
    except zipfile.BadZipFile as exc:
        raise ArchiveFormatError(
            f"{name}: not a valid zip file ({exc})", ArchiveRefusalCode.NOT_A_ZIP
        ) from exc
    members: list[tuple[ArchiveMember, _TextOpener]] = []
    ignored: list[str] = []
    try:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        _guard_zip_members(infos, name)
        for info in infos:
            if not _looks_like_csv(info.filename):
                continue
            opener = _zip_opener(zf, info, f"{name}:{info.filename}")
            with opener() as text:
                header, _ = _table(text, f"{name}:{info.filename}")
            if header is None:
                log.debug("%s: skipping %s, not a table the importer reads", name, info.filename)
                ignored.append(info.filename)
                continue
            members.append((ArchiveMember(info.filename, header.kind, _zip_time(info)), opener))
    except BaseException:
        zf.close()
        raise
    if not _has_rows_to_import(members):
        zf.close()
        reason, code = _no_table_reason(infos)
        raise ArchiveFormatError(f"{name}: {reason}", code)
    return Archive(name, members, zf.close, ignored=ignored)


def _declared_member_count(handle: IO[bytes]) -> int:
    """An upper bound on the zip's central-directory entries, unparsed.

    Read from the End Of Central Directory record — the last handful of bytes
    of the file, not the whole thing — so a zip declaring far more members
    than :data:`MAX_MEMBERS` is refused before :class:`zipfile.ZipFile` spends
    any time or memory building a :class:`zipfile.ZipInfo` per entry (it does
    not trust the entry-count field either: it reads central-directory records
    until it has consumed the *declared byte size*, so a mismatched pair of
    fields is exactly the case this has to catch, not only the honest one).
    The greater of the entry-count field and (central directory size divided
    by the smallest a record can be) is returned, so understating one while
    inflating the other buys nothing. Zip64's extension (more than 65 535
    entries, which sets the 16-bit count field to that sentinel and stores the
    real numbers in a second record) is followed when present. ``0`` when the
    record cannot be found at all; that is not a claim the zip is empty, only
    that whatever is wrong with it is for :class:`zipfile.ZipFile` to raise on
    next, in its own words.
    """
    handle.seek(0, io.SEEK_END)
    file_size = handle.tell()
    window = min(file_size, _EOCD_FIXED_SIZE + _EOCD_MAX_COMMENT)
    handle.seek(file_size - window)
    tail = handle.read(window)
    index = tail.rfind(_EOCD_SIGNATURE)
    if index == -1 or len(tail) - index < _EOCD_FIXED_SIZE:
        return 0
    eocd = tail[index : index + _EOCD_FIXED_SIZE]
    entries: int = struct.unpack_from("<H", eocd, 10)[0]
    central_dir_size: int = struct.unpack_from("<I", eocd, 12)[0]
    central_dir_offset: int = struct.unpack_from("<I", eocd, 16)[0]
    bound = central_dir_size // _CENTRAL_DIR_RECORD_MIN_SIZE
    if entries != 0xFFFF and central_dir_offset != 0xFFFFFFFF:
        return max(entries, bound)
    locator_start = file_size - window + index - _ZIP64_EOCD_LOCATOR_SIZE
    if locator_start < 0:
        return max(entries, bound)
    handle.seek(locator_start)
    locator = handle.read(_ZIP64_EOCD_LOCATOR_SIZE)
    if locator[:4] != _ZIP64_EOCD_LOCATOR_SIGNATURE:
        return max(entries, bound)
    zip64_eocd_offset: int = struct.unpack_from("<Q", locator, 8)[0]
    handle.seek(zip64_eocd_offset)
    record = handle.read(_ZIP64_EOCD_FIXED_SIZE)
    if record[:4] != _ZIP64_EOCD_SIGNATURE or len(record) < _ZIP64_EOCD_FIXED_SIZE:
        return max(entries, bound)
    zip64_entries: int = struct.unpack_from("<Q", record, 32)[0]
    zip64_central_dir_size: int = struct.unpack_from("<Q", record, 40)[0]
    return max(zip64_entries, zip64_central_dir_size // _CENTRAL_DIR_RECORD_MIN_SIZE)


def _no_table_reason(infos: Sequence[zipfile.ZipInfo]) -> tuple[str, ArchiveRefusalCode]:
    """Why ``infos`` held nothing to import: a plain miss, or a zip inside the zip.

    LinkedIn's own download is not nested — its doubled ``.zip.zip`` name is
    only that, checked by hand against a real export — so this never unwraps
    one; it only makes the dead end explicit when the zip a person uploaded
    really does hold nothing but another zip (their own file manager or mail
    client having wrapped the download again, say), instead of sending them
    looking for a ``Connections.csv`` that is one level down.
    """
    default = "no Connections.csv, messages.csv, or Invitations.csv table in the archive"
    nested = [info.filename for info in infos if _looks_like_nested_zip(info.filename)]
    if len(nested) == 1:
        inner = nested[0]
        message = (
            f"{default} — but it contains another zip ({inner}); "
            "extract it and upload the file inside"
        )
        return message, ArchiveRefusalCode.NESTED_ZIP
    return default, ArchiveRefusalCode.WRONG_ARCHIVE


def _looks_like_nested_zip(path: str) -> bool:
    base = path.replace("\\", "/").split("/")[-1]
    return base.lower().endswith(".zip") and not base.startswith("._")


def _guard_zip_members(infos: Sequence[zipfile.ZipInfo], name: str) -> None:
    """Refuse a hostile zip on its declared metadata, before any member is opened.

    By the time this runs, ``infos`` already exists — :func:`_declared_member_count`
    is what keeps a zip declaring far too many members from paying to build it
    in the first place, checked before :class:`zipfile.ZipFile` parses a single
    entry. The count is checked again here in case that estimate and what
    :class:`zipfile.ZipFile` actually materialized disagree; every other check
    reads only what the central directory already states for each member — a
    declared path, a declared size, a declared compressed size — so refusing
    one costs no decompression. Checked over every member, not only the
    ``.csv``-looking ones, because a bomb does not have to look like a table to
    cost something if it were opened.
    """
    if len(infos) > MAX_MEMBERS:
        raise ArchiveFormatError(
            f"{name}: {len(infos)} members in the zip, over the {MAX_MEMBERS} member limit",
            ArchiveRefusalCode.TOO_MANY_MEMBERS,
        )
    total = 0
    for info in infos:
        if not _is_safe_member_path(info.filename):
            raise ArchiveFormatError(
                f"{name}: member {info.filename!r} has an unsafe path",
                ArchiveRefusalCode.UNSAFE_MEMBER_PATH,
            )
        if info.file_size > MAX_MEMBER_BYTES:
            raise ArchiveFormatError(
                f"{name}: member {info.filename} is {info.file_size} bytes uncompressed, "
                f"over the {MAX_MEMBER_BYTES} byte limit",
                ArchiveRefusalCode.TOO_LARGE,
            )
        if info.file_size > COMPRESSION_RATIO_FLOOR_BYTES:
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > MAX_COMPRESSION_RATIO:
                raise ArchiveFormatError(
                    f"{name}: member {info.filename} compresses {ratio:.0f}x, over the "
                    f"{MAX_COMPRESSION_RATIO}x ratio a real export never approaches",
                    ArchiveRefusalCode.COMPRESSION_RATIO_TOO_HIGH,
                )
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ArchiveFormatError(
                f"{name}: more than {MAX_TOTAL_UNCOMPRESSED_BYTES} bytes uncompressed "
                "across its members",
                ArchiveRefusalCode.TOO_LARGE,
            )


def _is_safe_member_path(filename: str) -> bool:
    """False for a path that would escape wherever a member's name was ever used as a path.

    Nothing here extracts a member to disk — :func:`_zip_opener` reads it into
    memory — but a name has to earn the right to be treated as one before it
    is used in a log line or an error message either, and ``zipfile`` only
    sanitizes a path on ``extract``, never on ``infolist`` or ``open``.
    """
    normalized = filename.replace("\\", "/")
    if normalized.startswith("/"):
        return False
    if len(normalized) > 1 and normalized[1] == ":":  # a drive letter, e.g. "C:/"
        return False
    return ".." not in normalized.split("/")


def _open_dir(root: Path, name: str) -> Archive:
    """Scan an unpacked export, in sorted order so two runs list its tables the same way."""
    members: list[tuple[ArchiveMember, _TextOpener]] = []
    ignored: list[str] = []
    paths = [path for path in sorted(root.rglob("*")) if path.is_file()]
    if len(paths) > MAX_MEMBERS:
        raise ArchiveFormatError(
            f"{name}: {len(paths)} files, over the {MAX_MEMBERS} file limit",
            ArchiveRefusalCode.TOO_MANY_MEMBERS,
        )
    # Sizes are checked over every file, not only the ``.csv``-looking ones —
    # the same "a bomb does not have to look like a table" reasoning as the
    # zip guard (:func:`_guard_zip_members`), so the two paths agree on what a
    # directory this large is allowed to cost, not only a zip this large.
    total = 0
    for path in paths:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        if size > MAX_MEMBER_BYTES:
            raise ArchiveFormatError(
                f"{name}: member {relative} is {size} bytes, "
                f"over the {MAX_MEMBER_BYTES} byte limit",
                ArchiveRefusalCode.TOO_LARGE,
            )
        total += size
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ArchiveFormatError(
                f"{name}: more than {MAX_TOTAL_UNCOMPRESSED_BYTES} bytes across its files",
                ArchiveRefusalCode.TOO_LARGE,
            )
        if not _looks_like_csv(relative):
            continue
        member_name = f"{name}:{relative}"
        opener = _path_opener(path, member_name)
        with opener() as text:
            header, _ = _table(text, member_name)
        if header is None:
            log.debug("%s: skipping %s, not a table the importer reads", name, relative)
            ignored.append(relative)
            continue
        members.append((ArchiveMember(relative, header.kind, _file_time(path)), opener))
    if not _has_rows_to_import(members):
        raise ArchiveFormatError(
            f"{name}: no Connections.csv, messages.csv, or Invitations.csv table in the directory",
            ArchiveRefusalCode.WRONG_ARCHIVE,
        )
    return Archive(name, members, lambda: None, ignored=ignored)


def _open_csv(handle: IO[bytes], name: str) -> Archive:
    opener = _stream_opener(handle, name)
    with opener() as text:
        header, _ = _table(text, name)
    if header is None:
        raise ArchiveFormatError(
            f"{name}: not a LinkedIn archive zip, and no Connections.csv, messages.csv, or "
            "Invitations.csv header in its first lines",
            ArchiveRefusalCode.NOT_A_ZIP,
        )
    if header.kind not in IMPORTABLE_KINDS:
        # Profile.csv on its own is recognizable but holds nothing to import.
        raise ArchiveFormatError(
            f"{name}: this is the {header.kind.value} table, which nothing here imports",
            ArchiveRefusalCode.WRONG_ARCHIVE,
        )
    return Archive(name, [(ArchiveMember(name, header.kind, None), opener)], lambda: None)


def _has_rows_to_import(members: Sequence[tuple[ArchiveMember, _TextOpener]]) -> bool:
    """True when at least one member carries rows an import writes.

    Profile.csv alone is not an archive worth opening: it names the owner and
    holds nothing else.
    """
    return any(member.kind in IMPORTABLE_KINDS for member, _ in members)


def _looks_like_csv(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    base = parts[-1]
    return (
        base.lower().endswith(".csv")
        and not base.startswith("._")  # macOS resource forks
        and "__MACOSX" not in parts
        and base.lower() not in NOT_CONVERSATIONS
    )


def _zip_time(info: zipfile.ZipInfo) -> datetime | None:
    """A zip member's timestamp, read as UTC although the format does not say.

    Zip stores MS-DOS local time with no zone, so this is out by whatever
    offset the machine that wrote the archive was on. Converting would only
    trade that for the offset of the machine reading it, and would make
    ``exported_at`` depend on the reader's clock settings. It is left as it is
    because the one thing that consumes it, ``observed_at`` on an import, needs
    the export times of successive archives to be ordered rather than exact,
    and a constant offset keeps that order. A caller that needs an exact
    instant passes its own ``observed_at``.
    """
    try:
        return _plausible(datetime(*info.date_time, tzinfo=UTC))
    except (TypeError, ValueError):
        return None


def _file_time(path: Path) -> datetime | None:
    try:
        return _plausible(datetime.fromtimestamp(path.stat().st_mtime, UTC))
    except (OSError, OverflowError, ValueError):
        return None


def _plausible(stamp: datetime) -> datetime | None:
    """A member timestamp, or ``None`` when it predates LinkedIn or sits in the future."""
    if stamp < EARLIEST_EXPORT or stamp > datetime.now(UTC) + timedelta(days=1):
        return None
    return stamp


def _zip_opener(zf: zipfile.ZipFile, info: zipfile.ZipInfo, name: str) -> _TextOpener:
    @contextmanager
    def opener() -> Iterator[TextIO]:
        if info.flag_bits & 0x1:
            # Checked from the central directory, before ``zf.open`` ever runs: an
            # encrypted member's ``RuntimeError`` embeds ``repr(ZipInfo)``, which
            # is not a message to show a person.
            raise ArchiveFormatError(
                f"{name}: the zip is encrypted; netkeeper cannot read a password-protected export",
                ArchiveRefusalCode.ENCRYPTED,
            )
        try:
            raw = zf.open(info)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
            # A corrupt member or an unsupported compression method.
            raise ArchiveFormatError(
                f"{name}: cannot be opened ({exc})", ArchiveRefusalCode.DAMAGED
            ) from exc
        with raw:
            try:
                yield io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            except (zipfile.BadZipFile, zlib.error, EOFError, csv.Error) as exc:
                # zipfile decompresses and CRC-checks lazily, as the reader pulls
                # bytes through this wrapper — a declared size that understates
                # the real one, or a flipped byte in the deflate stream, surfaces
                # here rather than at ``zf.open``, from deep inside whatever was
                # reading (the header scan, or a row reader mid-file). An ordinary
                # damaged download hits this exact path, not only a crafted one.
                raise ArchiveFormatError(
                    f"{name}: damaged inside the zip ({exc})", ArchiveRefusalCode.DAMAGED
                ) from exc

    return opener


def _path_opener(path: Path, name: str) -> _TextOpener:
    @contextmanager
    def opener() -> Iterator[TextIO]:
        try:
            handle = path.open("r", encoding="utf-8-sig", newline="")
        except OSError as exc:
            raise ArchiveFormatError(
                f"{name}: cannot be opened ({exc})", ArchiveRefusalCode.DAMAGED
            ) from exc
        with handle:
            yield handle

    return opener


def _stream_opener(handle: IO[bytes], name: str) -> _TextOpener:
    @contextmanager
    def opener() -> Iterator[TextIO]:
        handle.seek(0)
        wrapper = io.TextIOWrapper(handle, encoding="utf-8-sig", newline="")
        try:
            yield wrapper
        finally:
            wrapper.detach()  # the caller's stream stays open

    return opener
