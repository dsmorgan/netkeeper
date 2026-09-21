"""Exporters: CSV, JSON, and vCard 4.0 over a filtered, sorted stream of contacts (spec 10.6).

Four presets:

- ``nine-column`` (and its ``headerless`` CSV variant): the eight columns of
  Appendix A, in that exact order, headed by the labels the reference
  workflow's mailing tool expects on import. This is the preset whose CSV
  form must round-trip through that tool's importer unchanged (P1-11's "done
  when"; the netkeeper-side importer for it is P1-04, issue #13).
- ``linkedin-archive``: the columns of LinkedIn's own ``Connections.csv``
  (``First Name, Last Name, URL, Email Address, Company, Position, Connected
  On``), so a re-import through the archive importer (also P1-04) sees values
  shaped the way LinkedIn itself reports them. ``First Name``/``Last Name``
  here are the LinkedIn-sourced ``contacts.first_name``/``last_name``, not the
  triaged ``preferred_name`` nine-column uses, because this preset's job is to
  look like LinkedIn's own file. The three-line "Notes:" preamble LinkedIn
  puts above its own header carries no column of its own, so it is not
  reproduced; an importer that wants it should look for the header row rather
  than assume a fixed line offset.
- ``full``: every user-facing field, including the child tables (emails,
  phones, links, positions, tags) — but not the database ids, foreign keys,
  and sync-internal bookkeeping (``field_sources``, ``synced_values``,
  ``enrich_priority``, ``last_enriched_at``, ``created_at``, ``updated_at``,
  and a child row's own ``source``/``observed_at``) that would leak the
  tool's implementation into a file sent elsewhere.
- ``campaign-audience``: the campaign engine's merge fields (spec 11.1) plus
  the recipient email, so the file is directly usable as a mail-merge source.

Every export goes through :func:`netkeeper.crm.filters.compile_filter` and
:func:`netkeeper.crm.filters.apply_sort` — the same compiler the contacts
table and smart lists use — and fetches in bounded batches with
:func:`netkeeper.crm.filters.paginate` rather than loading the whole result
set at once. Each function here is a generator, so the web layer can hand the
whole thing to ``StreamingResponse`` without building the file in memory.
"""

from __future__ import annotations

import contextlib
import csv
import json
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session, selectinload

from netkeeper.crm.filters import FilterTree, SortKey, apply_sort, compile_filter, paginate
from netkeeper.models import Contact, User

ExportFormat = Literal["csv", "json", "vcard"]
ExportPreset = Literal["nine-column", "linkedin-archive", "full", "campaign-audience"]

EXTENSIONS: Final[dict[ExportFormat, str]] = {"csv": "csv", "json": "json", "vcard": "vcf"}
MEDIA_TYPES: Final[dict[ExportFormat, str]] = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
    "vcard": "text/vcard; charset=utf-8",
}

_BATCH_SIZE = 200


def filename_for(preset: ExportPreset, output_format: ExportFormat) -> str:
    """The ``Content-Disposition`` filename for this preset and format."""
    return f"contacts-{preset}.{EXTENSIONS[output_format]}"


# --- streaming the rows -------------------------------------------------------


def _iter_contacts(
    session: Session, user: User, tree: FilterTree, sort: Sequence[SortKey], *, now: datetime
) -> Iterator[Contact]:
    """Every contact ``tree`` selects, ordered by ``sort``, fetched in bounded batches.

    Reuses :func:`compile_filter`, :func:`apply_sort`, and :func:`paginate` from
    the filter language rather than a second query path. Each batch is its own
    complete, closed query (a ``LIMIT``/``OFFSET`` page, not a held-open server
    cursor), so eager-loading the child collections below is safe on every
    backend: nothing here shares a cursor with a lazy or ``selectin`` load the
    way a straight ``yield_per`` stream would.
    """
    base = apply_sort(compile_filter(user, tree, now=now), sort).options(
        selectinload(Contact.emails),
        selectinload(Contact.phones),
        selectinload(Contact.links),
        selectinload(Contact.positions),
        selectinload(Contact.snapshots),
        selectinload(Contact.tags),
    )
    offset = 0
    while True:
        batch = session.scalars(paginate(base, limit=_BATCH_SIZE, offset=offset)).all()
        yield from batch
        if len(batch) < _BATCH_SIZE:
            return
        offset += _BATCH_SIZE


def _local_today(user: User, now: datetime) -> date:
    """``now`` as a date in ``user``'s timezone, falling back to UTC for a bad zone."""
    tz: tzinfo = UTC
    with contextlib.suppress(ZoneInfoNotFoundError):
        tz = ZoneInfo(user.timezone)
    return now.astimezone(tz).date()


# --- shared field helpers -------------------------------------------------------


def _primary_email(contact: Contact) -> str | None:
    return contact.emails[0].email if contact.emails else None


def _primary_phone(contact: Contact) -> str | None:
    return contact.phones[0].raw if contact.phones else None


def _iso_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _iso_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _linkedin_date(value: date | None) -> str | None:
    """LinkedIn's own ``Connected On`` format, e.g. ``05 Jan 2023``."""
    return value.strftime("%d %b %Y") if value else None


def _connected_year(contact: Contact) -> str | None:
    return str(contact.connected_on.year) if contact.connected_on else None


def _years_since_connected(contact: Contact, *, today: date) -> str | None:
    connected = contact.connected_on
    if connected is None:
        return None
    years = today.year - connected.year
    if (today.month, today.day) < (connected.month, connected.day):
        years -= 1
    return str(max(years, 0))


def _last_position_change(contact: Contact) -> str | None:
    return contact.snapshots[0].observed_at.date().isoformat() if contact.snapshots else None


# --- column-shaped presets: nine-column, linkedin-archive, campaign-audience ---


@dataclass(frozen=True)
class _Column:
    """One column of a flat (non-``full``) preset.

    ``role`` names what the column means for :func:`_vcard_for` (``given``,
    ``family``, ``email``, ``phone``, ``org``, ``title``, ``url``, ``adr``); a
    column with no vCard equivalent (LinkedIn's ``Connected On``, for one)
    leaves it None and is simply absent from that format.
    """

    header: str
    json_key: str
    get: Callable[[Contact, date], str | None]
    role: str | None = None


NINE_COLUMN: Final[tuple[_Column, ...]] = (
    _Column("LinkedIn Profile URL", "linkedin_profile_url", lambda c, _today: c.li_url, "url"),
    _Column("Email Address", "email_address", lambda c, _today: _primary_email(c), "email"),
    _Column("First Name", "first_name", lambda c, _today: c.preferred_name, "given"),
    _Column("Last Name", "last_name", lambda c, _today: c.last_name, "family"),
    _Column("CityState", "city_state", lambda c, _today: c.location, "adr"),
    _Column("Current Company", "current_company", lambda c, _today: c.current_company, "org"),
    _Column("Current Job Title", "current_job_title", lambda c, _today: c.current_title, "title"),
    _Column("Phone Number", "phone_number", lambda c, _today: _primary_phone(c), "phone"),
)
"""Appendix A, in order. The CSV header is the left column; the JSON key is the
right column, snake_cased. Do not reorder: the round-trip test depends on it."""

LINKEDIN_ARCHIVE: Final[tuple[_Column, ...]] = (
    _Column("First Name", "first_name", lambda c, _today: c.first_name, "given"),
    _Column("Last Name", "last_name", lambda c, _today: c.last_name, "family"),
    _Column("URL", "url", lambda c, _today: c.li_url, "url"),
    _Column("Email Address", "email_address", lambda c, _today: _primary_email(c), "email"),
    _Column("Company", "company", lambda c, _today: c.current_company, "org"),
    _Column("Position", "position", lambda c, _today: c.current_title, "title"),
    _Column("Connected On", "connected_on", lambda c, _today: _linkedin_date(c.connected_on), None),
)

CAMPAIGN_AUDIENCE: Final[tuple[_Column, ...]] = (
    _Column("Email Address", "email", lambda c, _today: _primary_email(c), "email"),
    _Column("First Name", "first_name", lambda c, _today: c.preferred_name, "given"),
    _Column("Last Name", "last_name", lambda c, _today: c.last_name, "family"),
    _Column("Company", "company", lambda c, _today: c.current_company, "org"),
    _Column("Title", "title", lambda c, _today: c.current_title, "title"),
    _Column("Location", "location", lambda c, _today: c.location, "adr"),
    _Column("Connected Year", "connected_year", lambda c, _today: _connected_year(c), None),
    _Column(
        "Years Since Connected",
        "years_since_connected",
        lambda c, today: _years_since_connected(c, today=today),
        None,
    ),
    _Column(
        "Last Position Change",
        "last_position_change",
        lambda c, _today: _last_position_change(c),
        None,
    ),
    _Column("LinkedIn Profile URL", "linkedin_profile_url", lambda c, _today: c.li_url, "url"),
)

_COLUMN_PRESETS: Final[dict[ExportPreset, tuple[_Column, ...] | None]] = {
    "nine-column": NINE_COLUMN,
    "linkedin-archive": LINKEDIN_ARCHIVE,
    "campaign-audience": CAMPAIGN_AUDIENCE,
    "full": None,  # full has its own row shape (nested children); see below
}


class _Echo:
    """A write-only file-like object that hands the string straight back.

    ``csv.writer`` wants something with a ``write`` method; this lets each
    ``writerow()`` call yield its own line instead of buffering a file.
    """

    def write(self, value: str) -> str:
        return value


def _columns_csv(
    columns: tuple[_Column, ...], rows: Iterable[Contact], *, headerless: bool, today: date
) -> Iterator[str]:
    writer = csv.writer(_Echo())
    if not headerless:
        yield writer.writerow([column.header for column in columns])
    for contact in rows:
        yield writer.writerow([column.get(contact, today) or "" for column in columns])


def _columns_json(
    columns: tuple[_Column, ...], rows: Iterable[Contact], *, today: date
) -> Iterator[dict[str, Any]]:
    for contact in rows:
        yield {column.json_key: column.get(contact, today) for column in columns}


# --- the "full" preset: every user-facing field, including children -----------

FULL_FIELDS: Final[tuple[str, ...]] = (
    "linkedin_profile_url",
    "linkedin_public_id",
    "first_name",
    "last_name",
    "preferred_name",
    "headline",
    "current_title",
    "current_company",
    "location",
    "connected_on",
    "degree",
    "met",
    "triaged_at",
    "do_not_contact",
    "do_not_contact_reason",
    "last_contacted_at",
    "li_disconnected_at",
    "archived_at",
    "notes",
    "source",
    "emails",
    "phones",
    "links",
    "positions",
    "tags",
)


def _full_row(contact: Contact) -> dict[str, Any]:
    row: dict[str, Any] = {
        "linkedin_profile_url": contact.li_url,
        "linkedin_public_id": contact.li_public_id,
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "preferred_name": contact.preferred_name,
        "headline": contact.headline,
        "current_title": contact.current_title,
        "current_company": contact.current_company,
        "location": contact.location,
        "connected_on": _iso_date(contact.connected_on),
        "degree": contact.degree,
        "met": contact.met.value,
        "triaged_at": _iso_datetime(contact.triaged_at),
        "do_not_contact": contact.do_not_contact,
        "do_not_contact_reason": contact.do_not_contact_reason,
        "last_contacted_at": _iso_datetime(contact.last_contacted_at),
        "li_disconnected_at": _iso_datetime(contact.li_disconnected_at),
        "archived_at": _iso_datetime(contact.archived_at),
        "notes": contact.notes,
        "source": contact.source.value,
        "emails": [
            {
                "email": e.email,
                "kind": e.kind.value,
                "is_primary": e.is_primary,
                "status": e.status.value,
            }
            for e in contact.emails
        ],
        "phones": [
            {"number": p.raw, "kind": p.kind.value, "is_primary": p.is_primary}
            for p in contact.phones
        ],
        "links": [{"url": link.url, "kind": link.kind.value} for link in contact.links],
        "positions": [
            {
                "title": p.title,
                "company": p.company,
                "started_on": _iso_date(p.started_on),
                "ended_on": _iso_date(p.ended_on),
                "is_current": p.is_current,
            }
            for p in contact.positions
        ],
        "tags": [tag.name for tag in contact.tags],
    }
    return row


def _flatten_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _flatten_child(item: dict[str, Any]) -> str:
    return ":".join(_flatten_scalar(v) for v in item.values())


def _flatten_full_row(row: dict[str, Any]) -> dict[str, str]:
    flat: dict[str, str] = {}
    for key in FULL_FIELDS:
        value = row[key]
        if isinstance(value, list):
            if value and isinstance(value[0], dict):
                flat[key] = "; ".join(_flatten_child(item) for item in value)
            else:
                flat[key] = "; ".join(str(item) for item in value)
        else:
            flat[key] = _flatten_scalar(value)
    return flat


def _full_csv(rows: Iterable[Contact], *, headerless: bool) -> Iterator[str]:
    writer = csv.writer(_Echo())
    if not headerless:
        yield writer.writerow(list(FULL_FIELDS))
    for contact in rows:
        flat = _flatten_full_row(_full_row(contact))
        yield writer.writerow([flat[key] for key in FULL_FIELDS])


def _full_vcard(contact: Contact) -> str:
    given, family = contact.first_name, contact.last_name
    full_name = contact.preferred_name or " ".join(p for p in (given, family) if p) or "Unknown"
    lines = [
        "BEGIN:VCARD",
        "VERSION:4.0",
        f"N:{_vcard_escape(family)};{_vcard_escape(given)};;;",
        f"FN:{_vcard_escape(full_name)}",
    ]
    if contact.current_company:
        lines.append(f"ORG:{_vcard_escape(contact.current_company)}")
    if contact.current_title:
        lines.append(f"TITLE:{_vcard_escape(contact.current_title)}")
    for email in contact.emails:
        lines.append(f"EMAIL:{_vcard_escape(email.email)}")
    for phone in contact.phones:
        lines.append(f"TEL:{_vcard_escape(phone.raw)}")
    if contact.li_url:
        lines.append(f"URL:{_vcard_escape(contact.li_url)}")
    for link in contact.links:
        lines.append(f"URL:{_vcard_escape(link.url)}")
    if contact.location:
        lines.append(f"ADR:;;;{_vcard_escape(contact.location)};;;")
    if contact.notes:
        lines.append(f"NOTE:{_vcard_escape(contact.notes)}")
    tag_names = [tag.name for tag in contact.tags]
    if tag_names:
        lines.append(f"CATEGORIES:{','.join(_vcard_escape(name) for name in tag_names)}")
    lines.append("END:VCARD")
    return _vcard_body(lines)


# --- vCard 4.0 rendering (RFC 6350): escaping and 75-octet line folding -------


def _vcard_escape(value: str) -> str:
    """Escape a TEXT value per RFC 6350 §3.4: backslash first, then newline, comma, semicolon."""
    value = value.replace("\\", "\\\\")
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    value = value.replace(",", "\\,").replace(";", "\\;")
    return value


def _fold(line: str) -> str:
    """Fold ``line`` to 75-octet physical lines (RFC 6350 §3.2).

    A continuation line starts with a single space, which counts against its
    own 75-octet budget, and a fold never splits a multi-byte UTF-8 sequence.
    """
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    chunks: list[bytes] = []
    start = 0
    limit = 75
    while start < len(data):
        end = min(start + limit, len(data))
        while end < len(data) and (data[end] & 0xC0) == 0x80:  # a UTF-8 continuation byte
            end -= 1
        chunks.append(data[start:end])
        start = end
        limit = 74  # the next physical line also carries the one-space fold marker
    return "\r\n ".join(chunk.decode("utf-8") for chunk in chunks)


def _vcard_body(lines: list[str]) -> str:
    return "".join(_fold(line) + "\r\n" for line in lines)


def _vcard_for(columns: tuple[_Column, ...], contact: Contact, *, today: date) -> str:
    values = {column.role: column.get(contact, today) for column in columns if column.role}
    given, family = values.get("given") or "", values.get("family") or ""
    full_name = " ".join(p for p in (given, family) if p) or "Unknown"
    lines = [
        "BEGIN:VCARD",
        "VERSION:4.0",
        f"N:{_vcard_escape(family)};{_vcard_escape(given)};;;",
        f"FN:{_vcard_escape(full_name)}",
    ]
    if values.get("org"):
        lines.append(f"ORG:{_vcard_escape(values['org'] or '')}")
    if values.get("title"):
        lines.append(f"TITLE:{_vcard_escape(values['title'] or '')}")
    if values.get("email"):
        lines.append(f"EMAIL:{_vcard_escape(values['email'] or '')}")
    if values.get("phone"):
        lines.append(f"TEL:{_vcard_escape(values['phone'] or '')}")
    if values.get("url"):
        lines.append(f"URL:{_vcard_escape(values['url'] or '')}")
    if values.get("adr"):
        lines.append(f"ADR:;;;{_vcard_escape(values['adr'] or '')};;;")
    lines.append("END:VCARD")
    return _vcard_body(lines)


# --- JSON streaming, shared by every preset ------------------------------------


def _json_stream(rows: Iterable[dict[str, Any]]) -> Iterator[str]:
    yield "["
    first = True
    for row in rows:
        prefix = "" if first else ","
        yield prefix + json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        first = False
    yield "]"


# --- the entry point ------------------------------------------------------------


def export_stream(
    session: Session,
    user: User,
    *,
    preset: ExportPreset,
    output_format: ExportFormat,
    headerless: bool,
    tree: FilterTree,
    sort: Sequence[SortKey],
    now: datetime,
) -> Iterator[str]:
    """The exported file for ``preset``/``output_format``, one chunk at a time.

    ``tree`` and ``sort`` are what :mod:`netkeeper.crm.filters` compiles and
    orders by; ``now`` is the instant relative fields (``years_since_connected``,
    and the filter's own relative windows) are computed from, fixed once per
    call so a long export is internally consistent.
    """
    today = _local_today(user, now)
    contacts = _iter_contacts(session, user, tree, sort, now=now)
    columns = _COLUMN_PRESETS[preset]
    if columns is None:  # "full"
        if output_format == "csv":
            yield from _full_csv(contacts, headerless=headerless)
        elif output_format == "json":
            yield from _json_stream(_full_row(contact) for contact in contacts)
        else:
            for contact in contacts:
                yield _full_vcard(contact)
        return
    if output_format == "csv":
        yield from _columns_csv(columns, contacts, headerless=headerless, today=today)
    elif output_format == "json":
        yield from _json_stream(_columns_json(columns, contacts, today=today))
    else:
        for contact in contacts:
            yield _vcard_for(columns, contact, today=today)
