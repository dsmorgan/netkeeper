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
format, and a profile URL that is not a LinkedIn one. A row with nothing left to
identify a person by (no LinkedIn identity, no name, no address, no phone) is not
importable at all and comes back with ``incoming`` as None.
"""

from __future__ import annotations

import csv
import enum
import io
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
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
        # Spec appendix A, so an export of this preset imports back unchanged.
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
    if headers is None:
        raise EmptyFile("the file has no header row")
    return ParsedCsv(headers, tuple(rows), preamble_rows=preamble, dropped_cells=dropped)


def _header_names(cells: Sequence[str]) -> tuple[str, ...]:
    """Header cells trimmed, empty ones named after their position, repeats suffixed."""
    names: list[str] = []
    seen: dict[str, int] = {}
    for index, cell in enumerate(cells, start=1):
        name = cell.strip() or f"column {index}"
        count = seen.get(name, 0) + 1
        seen[name] = count
        names.append(name if count == 1 else f"{name} ({count})")
    return tuple(names)


# --- mapping ----------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedMapping:
    """Which column feeds which field, and what was left over."""

    mapping: dict[str, ImportField]
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
    mapping, which wins over a preset. An override with no field (``None`` or an
    empty string) unmaps that column, which is how a person drops a preset's
    guess. ``InvalidMapping`` when a key names no header of this file, when a
    value is not an :class:`ImportField`, or when nothing is mapped in the end.
    """
    known = set(headers)
    mapping: dict[str, ImportField] = preset.mapping_for(headers) if preset is not None else {}
    for source, pairs in (("saved mapping", saved), ("mapping", overrides)):
        for header, field in (pairs or {}).items():
            if header not in known:
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
    if not mapping:
        raise InvalidMapping(
            "no column is mapped to a field; pick a preset or map at least one column"
        )
    ordered = {header: mapping[header] for header in headers if header in mapping}
    return ResolvedMapping(
        ordered,
        preset=preset.name if preset is not None else None,
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
            links.append(IncomingLink(url=value))
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
