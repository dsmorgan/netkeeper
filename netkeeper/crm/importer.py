"""Reading a CSV and turning its rows into incoming contacts (spec 10.5).

Pure: nothing here opens a session or writes anything. :func:`parse_csv` finds
the header, :func:`resolve_mapping` decides which column feeds which field, and
:func:`map_row` turns one row into an :class:`~netkeeper.crm.identity.IncomingContact`
for :func:`~netkeeper.crm.identity.resolve` and :func:`~netkeeper.crm.identity.apply`.
The run, the review, the commit, and the rollback are :mod:`netkeeper.crm.import_runs`.

Header detection
----------------
Exports do not all start with the header: the LinkedIn archive puts a short
"Notes:" preamble above it. The header is the first row with at least
:data:`MIN_HEADER_CELLS` non-empty cells; everything above it is preamble.
Header cells are trimmed, so ``"Location "`` and ``"Location"`` are one column;
an empty one is named after its position, and a repeat gets a numeric suffix, so
a row is always a dict with one key per column.

Presets
-------
A preset matches by header name, not by position, and matches a *normalized*
header (:func:`normalize_header`: lowercased, everything but letters and digits
removed), so ``"First Name"``, ``"first_name"``, and ``"FirstName"`` are the same
column. :func:`detect_preset` picks the preset that recognizes the most headers.
A person's own mapping is a plain ``{header: field}`` dict and always wins over
the preset it started from.

The three presets are :data:`PRESETS`: ``linkedin-archive`` (the archive's
``Connections.csv``), ``nine-column`` (the export layout in spec appendix A,
which round-trips), and ``linkedhelper``. Each carries aliases rather than one
spelling per field, so a column the tool renames between versions still lands.

What a row may lose
-------------------
A cell that cannot become a value is dropped and named in
:attr:`MappedRow.problems`; the rest of the row still imports. That covers a
malformed address, a phone number with no digits, a date in no recognized
format, a profile URL that is not a LinkedIn one, and a ``li_urn`` that is not a
URN — a bare member id looks plausible, and a wrong one would write a bogus
identity into the column resolution matches on first. A row with nothing left to
identify a person by (no LinkedIn identity, no name, no address, no phone) is not
importable at all and comes back with ``incoming`` as None.
"""

from __future__ import annotations

import csv
import enum
import io
import logging
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from netkeeper.crm.identity import (
    IncomingContact,
    IncomingEmail,
    IncomingLink,
    IncomingPhone,
    public_id_from_url,
)
from netkeeper.models import ContactSource
from netkeeper.models.base import utcnow

log = logging.getLogger(__name__)

# A header row has at least this many non-empty cells; a preamble line has fewer.
MIN_HEADER_CELLS: Final[int] = 2
# How far down the file the header may be before we give up looking.
MAX_PREAMBLE_ROWS: Final[int] = 20
# A preset must recognize this many of a file's headers before it is offered.
MIN_PRESET_MATCH: Final[int] = 3
# The largest single cell this reader will take. ``csv`` caps a field at 128 KiB
# by default and raises rather than truncating; a contact field is never close to
# either number, and a cap bounds what one quoted cell in a hostile file can make
# the reader hold. The cap is process-wide in ``csv`` and other readers set their
# own (the archive reader wants 16 MiB for a message body), so parse_csv() pins
# it for the read and puts back whatever was there.
FIELD_SIZE_LIMIT: Final[int] = 1024 * 1024

_NOT_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
# Deliberately loose: one @, a dot in the domain, no spaces or list separators.
# Anything stricter rejects addresses that exist; this catches what a CSV cell
# holds when it is not an address at all.
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")

_MONTHS: Final[dict[str, int]] = {
    name: number
    for number, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}
_DAY_MONTH_YEAR = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})$")
_MONTH_DAY_YEAR = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$")
_SLASHED = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")
# ``urn:li:<entity>/<id>`` or ``urn:li:<entity>:<id>`` (spec 8.1). Nothing here
# derives or repairs a URN; a cell that is not one is dropped, because ``li_urn``
# is a unique identity column and the first thing identity.resolve() matches on.
_LI_URN = re.compile(r"^urn:li:[A-Za-z][A-Za-z0-9_]*[:/]\S+$")


class ImportField(enum.StrEnum):
    """What a column may feed.

    The first ten are the scalar columns of ``contacts`` that carry per-field
    provenance (:data:`netkeeper.crm.provenance.PROVENANCE_ORDER`); the last
    three add child rows, which provenance does not rank. A field a person owns
    (``preferred_name``, ``notes``, ``met``, tags) is deliberately absent: no
    import touches those (spec 10.5).
    """

    LI_URN = "li_urn"
    LI_PUBLIC_ID = "li_public_id"
    LI_URL = "li_url"
    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    HEADLINE = "headline"
    CURRENT_TITLE = "current_title"
    CURRENT_COMPANY = "current_company"
    LOCATION = "location"
    CONNECTED_ON = "connected_on"
    EMAIL = "email"
    PHONE = "phone"
    LINK = "link"


SCALAR_FIELDS: Final[frozenset[ImportField]] = frozenset(
    {
        ImportField.LI_URN,
        ImportField.LI_PUBLIC_ID,
        ImportField.LI_URL,
        ImportField.FIRST_NAME,
        ImportField.LAST_NAME,
        ImportField.HEADLINE,
        ImportField.CURRENT_TITLE,
        ImportField.CURRENT_COMPANY,
        ImportField.LOCATION,
        ImportField.CONNECTED_ON,
    }
)
"""The fields that write a column of ``contacts``; the rest add child rows."""

# A row needs at least one of these before it is worth importing: without them
# there is nothing to resolve it by and nothing to call the person.
IDENTIFYING_FIELDS: Final[frozenset[ImportField]] = frozenset(
    {
        ImportField.LI_URN,
        ImportField.LI_PUBLIC_ID,
        ImportField.LI_URL,
        ImportField.FIRST_NAME,
        ImportField.LAST_NAME,
        ImportField.EMAIL,
        ImportField.PHONE,
    }
)


class CsvImportError(Exception):
    """Base of everything this module raises on purpose."""


class EmptyFile(CsvImportError, ValueError):
    """The file has no header row: it is empty, or it is all preamble."""


class MalformedCsv(CsvImportError, ValueError):
    """The bytes are not CSV this reader can take.

    Above all a quoted field past :mod:`csv`'s own field-size limit (128 KiB),
    which is the one hostile shape that reaches the reader rather than parsing
    into nonsense. ``_csv.Error`` is no relation of anything else here, so it is
    caught and re-raised as this, which the API turns into a 422.
    """


class UnknownPreset(CsvImportError, LookupError):
    """No preset by that name."""


class InvalidMapping(CsvImportError, ValueError):
    """A mapping naming a column the file does not have, or feeding no field at all."""


def normalize_header(header: str) -> str:
    """A header reduced to its letters and digits, lowercased.

    ``"First Name"``, ``"first_name"``, and ``"FIRSTNAME"`` all become
    ``"firstname"``, so a preset matches a column however the tool spells it.
    """
    return _NOT_ALPHANUMERIC.sub("", header.strip().lower())


@dataclass(frozen=True)
class Preset:
    """A named mapping from normalized header to field, with aliases per field."""

    name: str
    aliases: Mapping[str, ImportField]

    def mapping_for(self, headers: Sequence[str]) -> dict[str, ImportField]:
        """``{header: field}`` for the headers this preset recognizes, in file order."""
        return {
            header: self.aliases[normalize_header(header)]
            for header in headers
            if normalize_header(header) in self.aliases
        }

    def score(self, headers: Sequence[str]) -> int:
        """How many of ``headers`` this preset recognizes."""
        return len(self.mapping_for(headers))


def _aliases(**fields: str) -> dict[str, ImportField]:
    """``field="header one, header two"`` to ``{normalized header: field}``."""
    built: dict[str, ImportField] = {}
    for field, spellings in fields.items():
        for spelling in spellings.split(","):
            built[normalize_header(spelling)] = ImportField(field)
    return built


PRESETS: Final[tuple[Preset, ...]] = (
    Preset(
        "linkedin-archive",
        _aliases(
            first_name="First Name",
            last_name="Last Name",
            li_url="URL, Profile URL",
            email="Email Address",
            current_company="Company",
            current_title="Position",
            connected_on="Connected On",
        ),
    ),
    Preset(
        "nine-column",
        # Spec appendix A, so a file exported with this preset reads back into the
        # same columns. The contact does not round-trip exactly: appendix A writes
        # ``preferred_name`` into "First Name", and an import reads that column
        # into ``first_name``, because ``preferred_name`` is a person's own and no
        # import touches it (spec 10.5).
        _aliases(
            li_url="LinkedIn Profile URL",
            email="Email Address",
            first_name="First Name",
            last_name="Last Name",
            location="CityState, City State",
            current_company="Current Company",
            current_title="Current Job Title",
            phone="Phone Number",
        ),
    ),
    Preset(
        "linkedhelper",
        # Aliases rather than one spelling per field: the tool's column names have
        # varied between versions, and a file that spells one of them differently
        # should still land on the right field.
        _aliases(
            li_url="Profile Url, Profile Link",
            li_public_id="Public Id, Public Identifier",
            li_urn="Provider Id, Member Urn",
            first_name="First Name",
            last_name="Last Name",
            headline="Headline",
            current_company="Company Name, Organization",
            current_title="Position, Job Title",
            location="Location",
            email="Email, Email Address",
            phone="Phone, Phone Number",
            link="Website",
            connected_on="Connected At, Connection Date",
        ),
    ),
)

PRESETS_BY_NAME: Final[dict[str, Preset]] = {preset.name: preset for preset in PRESETS}


def get_preset(name: str) -> Preset:
    """The built-in preset called ``name``. ``UnknownPreset`` when there is none."""
    preset = PRESETS_BY_NAME.get(name)
    if preset is None:
        raise UnknownPreset(f"no preset called {name!r}; the built-in ones are {_preset_names()}")
    return preset


def _preset_names() -> str:
    return ", ".join(sorted(PRESETS_BY_NAME))


def detect_preset(headers: Sequence[str]) -> Preset | None:
    """The built-in preset that recognizes the most of ``headers``, or None.

    A preset has to recognize at least :data:`MIN_PRESET_MATCH` columns to be
    offered at all, so a file with a lone ``First Name`` column is not claimed by
    everything. Ties go to the preset declared first in :data:`PRESETS`.
    """
    best: Preset | None = None
    best_score = MIN_PRESET_MATCH - 1
    for preset in PRESETS:
        score = preset.score(headers)
        if score > best_score:
            best, best_score = preset, score
    return best


# --- reading the file -------------------------------------------------------


@dataclass(frozen=True)
class ParsedCsv:
    """A CSV read into its header and its data rows.

    ``rows`` are dicts from header to trimmed cell, one key per header, whatever
    the row's own length: a short row's missing columns are empty strings and a
    long row's extra cells are dropped (and counted in ``dropped_cells``).
    Entirely empty rows are not rows at all and never reach here.
    """

    headers: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    preamble_rows: int
    """Lines above the header, as the LinkedIn archive's "Notes:" block."""
    dropped_cells: int
    """Cells past the last header, in rows longer than the header row."""


def decode(content: str | bytes) -> str:
    """Text from a file's bytes: UTF-8 (BOM tolerated), then Windows-1252.

    Exports from Windows tools are often Windows-1252, which has no invalid
    bytes, so the fallback always succeeds rather than losing the file.
    """
    if isinstance(content, str):
        return content
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        log.info("file is not UTF-8; reading it as Windows-1252")
        return content.decode("cp1252")


def parse_csv(content: str | bytes) -> ParsedCsv:
    """Read ``content`` as CSV: find the header, then the rows. ``EmptyFile`` when there is none."""
    text = decode(content)
    reader = csv.reader(io.StringIO(text, newline=""))
    headers: tuple[str, ...] | None = None
    preamble = 0
    rows: list[dict[str, str]] = []
    dropped = 0
    try:
        with _field_size_limit(FIELD_SIZE_LIMIT):
            for cells in reader:
                if headers is None:
                    if sum(1 for cell in cells if cell.strip()) >= MIN_HEADER_CELLS:
                        headers = _header_names(cells)
                        continue
                    preamble += 1
                    if preamble > MAX_PREAMBLE_ROWS:
                        raise EmptyFile(
                            f"no header row in the first {MAX_PREAMBLE_ROWS} lines: "
                            f"a header needs at least {MIN_HEADER_CELLS} non-empty columns"
                        )
                    continue
                if not any(cell.strip() for cell in cells):
                    continue  # a blank line, including the one a trailing newline makes
                dropped += max(0, len(cells) - len(headers))
                rows.append(
                    {
                        header: cells[index].strip() if index < len(cells) else ""
                        for index, header in enumerate(headers)
                    }
                )
    except csv.Error as exc:
        # A quoted field past csv's field-size limit above all. Its exception is
        # no relation of CsvImportError, so without this it leaves the API as a 500.
        raise MalformedCsv(f"the file is not readable as CSV: {exc}") from exc
    if headers is None:
        raise EmptyFile("the file has no header row")
    return ParsedCsv(headers, tuple(rows), preamble_rows=preamble, dropped_cells=dropped)


@contextmanager
def _field_size_limit(limit: int) -> Iterator[None]:
    """Pin ``csv``'s field cap for the block, then put back what was there.

    The cap is one global inside the ``csv`` module, so without this a file's
    fate would depend on which other reader ran first in the process.
    """
    previous = csv.field_size_limit(limit)
    try:
        yield
    finally:
        csv.field_size_limit(previous)


def _header_names(cells: Sequence[str]) -> tuple[str, ...]:
    """Header cells trimmed, empty ones named after their position, repeats suffixed.

    The suffix climbs until the name is free, so a file whose own header already
    holds the name a suffix would produce (``A,A,A (2)``) still ends with one key
    per column and no row loses a cell.
    """
    names: list[str] = []
    used: set[str] = set()
    for index, cell in enumerate(cells, start=1):
        base = cell.strip() or f"column {index}"
        name, suffix = base, 1
        while name in used:
            suffix += 1
            name = f"{base} ({suffix})"
        used.add(name)
        names.append(name)
    return tuple(names)


# --- mapping ----------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedMapping:
    """Which column feeds which field, and what was left over."""

    mapping: dict[str, ImportField]
    """Empty when nothing claims a column; only a run refuses that (``create_run``)."""
    preset: str | None
    """The preset the mapping started from, when it came from one."""
    unmapped: tuple[str, ...]
    """Headers no column of the mapping claims; their cells are kept in ``raw_json`` only."""


def resolve_mapping(
    headers: Sequence[str],
    *,
    preset: Preset | None = None,
    saved: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> ResolvedMapping:
    """Work out ``{header: field}`` from a preset, a saved mapping, and explicit overrides.

    The three are applied in that order, so an override wins over a saved
    mapping, which wins over a preset. ``preset`` on the result names the preset
    only when nothing edited what it produced. An override with no field (``None`` or an
    empty string) unmaps that column, which is how a person drops a preset's
    guess. ``InvalidMapping`` when a key names no header of this file, or when a
    value is not an :class:`ImportField`.

    A mapping that claims no column at all is *not* refused here. A file whose
    headers no preset recognizes has to be readable before it can be mapped by
    hand, and :func:`inspect_csv` exists to show those headers; refusing at this
    depth left the only screen that can fix it out of reach. The requirement
    that an import map something belongs to the one path that writes, and lives
    in :func:`netkeeper.crm.import_runs.create_run`.
    """
    known = set(headers)
    mapping: dict[str, ImportField] = preset.mapping_for(headers) if preset is not None else {}
    # A saved preset is meant to be reused across files, so a column this one
    # lacks is simply not mapped, as a built-in preset's unmatched aliases are.
    # An explicit override is a choice someone just made about *this* file, so a
    # column that is not there is a mistake worth reporting.
    for source, pairs, strict in (("saved mapping", saved, False), ("mapping", overrides, True)):
        for header, field in (pairs or {}).items():
            if header not in known:
                if not strict:
                    continue
                raise InvalidMapping(
                    f"the {source} names a column {header!r} this file does not have; "
                    f"its columns are {', '.join(repr(name) for name in headers)}"
                )
            if not field:
                mapping.pop(header, None)
                continue
            try:
                mapping[header] = ImportField(field)
            except ValueError as exc:
                raise InvalidMapping(
                    f"{field!r} is not a field an import can write; "
                    f"the fields are {', '.join(sorted(ImportField))}"
                ) from exc
    ordered = {header: mapping[header] for header in headers if header in mapping}
    # The preset's name is recorded only when the mapping is still the preset's
    # own. A mapping someone has edited is theirs, and filing the run under
    # "linkedin-archive" would put a name on it that no longer describes it;
    # every screen that reads `preset` then repeats that.
    unedited = preset is not None and ordered == preset.mapping_for(headers)
    return ResolvedMapping(
        ordered,
        preset=preset.name if unedited and preset is not None else None,
        unmapped=tuple(header for header in headers if header not in ordered),
    )


@dataclass(frozen=True)
class MappedRow:
    """One row turned into an incoming contact, with whatever had to be dropped.

    ``incoming`` is None when the row identifies nobody; ``problems`` then says
    why, and always names every cell that was dropped.
    """

    incoming: IncomingContact | None
    problems: tuple[str, ...]

    @property
    def problem_text(self) -> str | None:
        """The problems as one line, or None when there were none."""
        return "; ".join(self.problems) if self.problems else None


def map_row(
    raw: Mapping[str, str],
    mapping: Mapping[str, ImportField],
    *,
    source: ContactSource = ContactSource.CSV,
    observed_at: datetime | None = None,
) -> MappedRow:
    """Turn one row's cells into an :class:`IncomingContact` under ``mapping``.

    Empty cells are "not provided" and are skipped. Where two columns feed one
    scalar field the first non-empty one wins; where two feed a child field
    (address, phone, link) both are kept, the first being the primary. A cell
    that cannot become a value is dropped and named in ``problems``.
    """
    scalars: dict[str, str | date] = {}
    emails: list[IncomingEmail] = []
    phones: list[IncomingPhone] = []
    links: list[IncomingLink] = []
    problems: list[str] = []
    provided: set[ImportField] = set()

    for header, field in mapping.items():
        value = raw.get(header, "").strip()
        if not value:
            continue
        if field in SCALAR_FIELDS:
            if field.value in scalars:
                continue  # an earlier column already fed this field
            converted = _scalar(field, value, header, problems)
            if converted is None:
                continue
            scalars[field.value] = converted
        elif field is ImportField.EMAIL:
            if not _EMAIL.match(value):
                problems.append(f"{header}: {value!r} is not an email address")
                continue
            emails.append(IncomingEmail(email=value, is_primary=not emails))
        elif field is ImportField.PHONE:
            try:
                phones.append(IncomingPhone(raw=value, is_primary=not phones))
            except ValueError:
                problems.append(f"{header}: {value!r} has no digits to dial")
                continue
        else:
            try:
                links.append(IncomingLink(url=value))
            except ValueError:
                problems.append(f"{header}: {value!r} is not an http or https url")
                continue
        provided.add(field)

    if not provided & IDENTIFYING_FIELDS:
        problems.append(
            "the row identifies nobody: it has no LinkedIn URL or id, no name, "
            "no address, and no phone number"
        )
        return MappedRow(None, tuple(problems))
    incoming = IncomingContact(
        source=source,
        observed_at=observed_at if observed_at is not None else utcnow(),
        emails=tuple(emails),
        phones=tuple(phones),
        links=tuple(links),
        **scalars,  # type: ignore[arg-type]
    )
    return MappedRow(incoming, tuple(problems))


def _scalar(field: ImportField, value: str, header: str, problems: list[str]) -> str | date | None:
    """One scalar cell converted, or None when it had to be dropped (and said so)."""
    if field is ImportField.CONNECTED_ON:
        parsed = parse_date(value)
        if parsed is None:
            problems.append(f"{header}: {value!r} is not a date this importer reads")
            return None
        return parsed
    if field is ImportField.LI_URL and public_id_from_url(value) is None:
        problems.append(f"{header}: {value!r} is not a LinkedIn profile URL")
        return None
    if field is ImportField.LI_URN and not _LI_URN.match(value):
        # A bare member id looks plausible and is not a URN. Letting one through
        # would write a bogus identity into a unique column that resolution
        # trusts, and unlike a mismapped name it would not show as unmapped.
        problems.append(f"{header}: {value!r} is not a LinkedIn URN (urn:li:...)")
        return None
    return value


def parse_date(value: str) -> date | None:
    """A date from the spellings these exports use, or None.

    ISO first (``2026-03-24``, and an ISO timestamp's date part), then
    ``24 Mar 2026`` and ``Mar 24, 2026`` with English month names, then
    ``03/24/2026``, read month-first: that is what the tools this importer reads
    emit, and a slashed date is ambiguous in principle. Month names are matched
    from a table rather than ``strptime``, whose ``%b`` follows the machine's
    locale.
    """
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for pattern, order in ((_DAY_MONTH_YEAR, "dmy"), (_MONTH_DAY_YEAR, "mdy")):
        match = pattern.match(text)
        if match is None:
            continue
        day, month_name, year = (
            (match.group(1), match.group(2), match.group(3))
            if order == "dmy"
            else (match.group(2), match.group(1), match.group(3))
        )
        month = _MONTHS.get(month_name.lower())
        if month is None:
            return None
        return _date_or_none(int(year), month, int(day))
    match = _SLASHED.match(text)
    if match is not None:
        year = match.group(3)
        return _date_or_none(
            int(year) + 2000 if len(year) == 2 else int(year),
            int(match.group(1)),
            int(match.group(2)),
        )
    return None


def _date_or_none(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def mapping_as_json(mapping: Mapping[str, ImportField]) -> dict[str, str]:
    """A mapping as the plain ``{header: field}`` dict the run stores."""
    return {header: field.value for header, field in mapping.items()}


def mapping_from_json(stored: Mapping[str, str], headers: Iterable[str]) -> dict[str, ImportField]:
    """A stored mapping back as fields, keeping only the columns this file has."""
    known = set(headers)
    return {header: ImportField(field) for header, field in stored.items() if header in known}
