"""Exporters: CSV, JSON, and vCard over a filtered, sorted stream of contacts (spec 10.6).

Five presets:

- ``nine-column`` (and its ``headerless`` CSV variant): the eight columns of
  Appendix A, in that exact order, headed by the labels the reference
  workflow's mailing tool expects on import. This is the preset whose CSV
  form must round-trip through ``netkeeper.crm.importer``'s ``nine-column``
  preset unchanged (P1-11's "done when"; see ``tests/test_exports.py``, whose
  round-trip test drives the real importer, not a stand-in for it).

  Two things it drops on the way out rather than produce a file that silently
  fails to come back:

  * A contact with nothing identifying in any of the eight columns — no
    LinkedIn URL, no email, no name (``preferred_name`` *and* ``last_name``
    both empty), no phone — is not written as a row at all. The importer's
    own ``IDENTIFYING_FIELDS`` rule refuses exactly such a row ("the row
    identifies nobody") and skips it, so exporting one would produce a file
    that silently loses a row on its own round trip; better to never claim it
    round-tripped in the first place. Company, title, and location alone
    never make a contact "identified" here, matching the importer.
  * A phone number with no digits at all (someone typed "ask reception" into
    the field) is exported as no phone rather than as that text, because the
    importer's ``IncomingPhone`` refuses a raw value with no digits and drops
    the cell. This one is judgment, not derived from a fixed rule: a phone
    column holding un-dialable text is already not doing its job, so leaving
    it out costs nothing a mail tool would have used anyway.

  What it does *not* try to make symmetric: Appendix A's "First Name" is
  ``preferred_name`` on the way out, but ``importer.PRESETS["nine-column"]``
  reads "First Name" back into ``first_name`` on the way in, not
  ``preferred_name``. A contact with ``first_name="Robert",
  preferred_name="Bob"`` exports "Bob", reimports with ``first_name="Bob"``
  (and ``preferred_name`` defaulting to it, spec 8.1), and exports "Bob"
  again — **the file round-trips byte for byte; the contact behind it does
  not** keep both names. That is correct, not a bug: the file has one name
  column, a mail merge wants the name you actually address someone by, and a
  contact carrying two names cannot fit through a format with one column
  without a decision being made somewhere. Do not "fix" this by switching the
  column to ``first_name`` — that would break the mail-merge case the preset
  exists for instead.
- ``linkedin-archive``: the columns of LinkedIn's own ``Connections.csv``
  (``First Name, Last Name, URL, Email Address, Company, Position, Connected
  On``), so a re-import through the archive importer (also
  ``netkeeper.crm.importer``) sees values shaped the way LinkedIn itself
  reports them. ``First Name``/``Last Name`` here are the LinkedIn-sourced
  ``contacts.first_name``/``last_name``, not the triaged ``preferred_name``
  nine-column uses, because this preset's job is to look like LinkedIn's own
  file. The three-line "Notes:" preamble LinkedIn puts above its own header
  carries no column of its own, so it is not reproduced; an importer that
  wants it should look for the header row rather than assume a fixed line
  offset. Like nine-column, a contact identifying nobody in these columns is
  not written as a row.
- ``full``: every user-facing field, including the child tables (emails,
  phones, links, positions, tags) — but not the database ids, foreign keys,
  and sync-internal bookkeeping (``field_sources``, ``synced_values``,
  ``enrich_priority``, ``last_enriched_at``, ``created_at``, ``updated_at``,
  and a child row's own ``source``/``observed_at``) that would leak the
  tool's implementation into a file sent elsewhere.
- ``campaign-audience``: the campaign engine's merge fields (spec 11.1) plus
  the recipient email, so the file is directly usable as a mail-merge source.
  A contact with ``do_not_contact`` set is never in it: producing a mail-merge
  file is a send path by proxy (spec F15's "every send path" includes the
  paths outside this tool, once the file leaves it), and the whole point of
  the preset is that someone pastes it straight into a mailing tool. Nor is a
  contact waiting for review (``needs_review_at``, #184): one read off a
  connections-page card is not somebody to reach until it is confirmed.
  Nor is a contact holding any address the do-not-send list has as
  ``opted_out`` (#238), even when the contact itself never got ``do_not_contact``.
  Its email column skips a ``bounced`` address, and any address on the
  do-not-send list, and takes the next one, if there is one (spec 11.9, "channel
  address present and not bounced"; #77, #238). A contact left with no address
  stays in the file with an empty email cell, as a contact with no email at all
  always has: the row still carries a LinkedIn URL, and a bounce leaves the
  contact eligible for LinkedIn steps (spec 11.5).
- ``macos-contacts`` (vCard only, P6-02, #249): a vCard 3.0 file for macOS
  Contacts, the version it imports most reliably, where every other preset's
  vCard is 4.0. Each contact carries every email, phone, and link, its notes,
  and its tags in ``CATEGORIES``; after the contacts comes one group card per
  tag (``X-ADDRESSBOOKSERVER-KIND:group``, one
  ``X-ADDRESSBOOKSERVER-MEMBER`` per member), which Contacts turns into a
  group. A tag none of the exported contacts carries gets no group. Every card
  has a ``UID`` derived from the database id (:func:`_contact_uid`,
  :func:`_tag_uid`), so exporting twice gives the same cards and the members
  resolve without a lookup. A contact with ``do_not_contact`` set is left out,
  as ``campaign-audience`` leaves it out: Mail and Messages complete addresses
  from Contacts, so an address book is a send path by proxy too. Archived
  contacts follow the filter as they do in every preset. Contacts waiting for
  review are left out too, as in every vCard export (below). Asking for this
  preset as CSV or JSON raises :class:`ExportError` before anything renders.
  The import steps are in ``docs/macos-contacts.md``.

Every vCard export, whatever the preset, leaves out contacts waiting for review
(``needs_review_at``, #184; decided on #254). A vCard goes into an address
book, where a card read off a connections page would sit beside real contacts
with nothing to say it is unconfirmed, and Mail and Messages would offer its
name. CSV and JSON keep them, except ``campaign-audience``, which never
includes them.

``spreadsheet_safe`` (CSV only, off by default, #76) prefixes ``'`` to any cell
whose first character, or first after leading whitespace, is one a spreadsheet
reads as the start of a formula (:data:`FORMULA_TRIGGERS`). Names, headlines,
titles, and companies come from other people's profiles, so a cell like
``=HYPERLINK(...)`` is attacker-chosen text that Excel, Sheets, or LibreOffice
would otherwise evaluate on open. It is opt-in because it changes the bytes: a
spreadsheet-safe file no longer round-trips through the importer, and every
``+1 …`` phone number gains a leading quote. Safe to open in a spreadsheet, not
safe to re-import. JSON and vCard ignore it; neither is opened as a grid.

Every export goes through :func:`netkeeper.crm.filters.compile_filter` and
:func:`netkeeper.crm.filters.apply_sort` — the same compiler the contacts
table and smart lists use — and fetches in bounded batches with
:func:`netkeeper.crm.filters.paginate` rather than loading the whole result
set at once. The rendering is a generator, so the web layer can hand the whole
thing to ``StreamingResponse`` without building the file in memory —
:func:`export_stream` itself is not, so that compiling the filter, the one
step that can fail, happens before the caller commits to a ``200``.
"""

from __future__ import annotations

import contextlib
import csv
import json
import uuid
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import ColumnElement, Select, and_, exists
from sqlalchemy.orm import Session, selectinload

from netkeeper.crm import do_not_send
from netkeeper.crm.filters import FilterTree, SortKey, apply_sort, compile_filter, paginate
from netkeeper.crm.identity import phone_key
from netkeeper.models import (
    Contact,
    ContactEmail,
    DoNotSendAddress,
    DoNotSendReason,
    EmailKind,
    EmailStatus,
    PhoneKind,
    User,
)
from netkeeper.scoping import scoped

ExportFormat = Literal["csv", "json", "vcard"]
ExportPreset = Literal[
    "nine-column", "linkedin-archive", "full", "campaign-audience", "macos-contacts"
]

VCARD_ONLY_PRESETS: Final[frozenset[ExportPreset]] = frozenset({"macos-contacts"})
"""Presets that only exist as a vCard file; CSV or JSON of one is an :class:`ExportError`."""

EXTENSIONS: Final[dict[ExportFormat, str]] = {"csv": "csv", "json": "json", "vcard": "vcf"}
MEDIA_TYPES: Final[dict[ExportFormat, str]] = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
    "vcard": "text/vcard; charset=utf-8",
}

_BATCH_SIZE = 200

FORMULA_TRIGGERS: Final[tuple[str, ...]] = ("=", "+", "-", "@", "\t", "\r")
"""A cell starting with one of these is read as a formula by at least one of
Excel, Sheets, and LibreOffice (#76). Tab and carriage return are on the list
because some importers strip leading whitespace before looking for ``=``."""


class ExportError(ValueError):
    """An export that cannot be produced as asked, such as a vCard-only preset as CSV."""


def filename_for(preset: ExportPreset, output_format: ExportFormat) -> str:
    """The ``Content-Disposition`` filename for this preset and format."""
    return f"contacts-{preset}.{EXTENSIONS[output_format]}"


# --- streaming the rows -------------------------------------------------------


def _contacts_statement(
    session: Session,
    user: User,
    tree: FilterTree,
    sort: Sequence[SortKey],
    *,
    now: datetime,
    extra_where: ColumnElement[bool] | None = None,
) -> Select[tuple[Contact]]:
    """The statement :func:`_iter_contacts` pages: ``tree`` compiled, sorted, children eager.

    Reuses :func:`compile_filter` and :func:`apply_sort` from the filter
    language rather than a second query path. Not a generator, and called
    before the response body starts: this is where
    :class:`~netkeeper.crm.filters.FilterError` comes from, and the caller can
    still turn it into a 422 (#95).

    ``extra_where`` ANDs onto the compiled filter before sorting and paging —
    ``campaign-audience`` uses it to hold out ``do_not_contact`` rows (and
    contacts waiting for review, #184) regardless
    of what the caller's own filter says, so that preset can never produce a
    mail-merge file containing someone who asked to be left alone. Every vCard
    export uses it to hold out contacts waiting for review (#254).
    """
    statement = compile_filter(user, tree, session=session, now=now)
    if extra_where is not None:
        statement = statement.where(extra_where)
    return apply_sort(statement, sort).options(
        selectinload(Contact.emails),
        selectinload(Contact.phones),
        selectinload(Contact.links),
        selectinload(Contact.positions),
        selectinload(Contact.snapshots),
        selectinload(Contact.tags),
    )


def _iter_contacts(session: Session, base: Select[tuple[Contact]]) -> Iterator[Contact]:
    """Every contact ``base`` selects, fetched in bounded batches.

    Each batch is its own complete, closed query (a ``LIMIT``/``OFFSET`` page,
    not a held-open server cursor), so eager-loading the child collections is
    safe on every backend: nothing here shares a cursor with a lazy or
    ``selectin`` load the way a straight ``yield_per`` stream would.
    """
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


def _sendable_email(contact: Contact, listed: Collection[str] = frozenset()) -> str | None:
    """The first address that has not bounced and is not in ``listed``, primary first;
    ``campaign-audience`` only.

    ``listed`` is the user's do-not-send list (#238): an address on it is skipped
    as a bounce is, whichever contact it was found on.

    ``Contact.emails`` is ordered ``is_primary DESC, id ASC``, so this is the
    primary unless the primary bounced. A mail-merge file is a send path by
    proxy, and spec 11.9 guards every send on "channel address present and not
    bounced" (#77). An ``invalid`` status alone still exports the address
    (#215), but not once the address is on the do-not-send list, which marking it
    invalid by hand does (#238). The
    re-importable presets keep :func:`_primary_email`, bounced or not, by the
    same decision: they are a copy of the data, not a send list. The campaign
    guards refuse ``invalid`` too (#226); a file is not a send.
    """
    for email in contact.emails:
        if email.status is not EmailStatus.BOUNCED and email.email not in listed:
            return email.email
    return None


def _primary_phone(contact: Contact) -> str | None:
    """The primary phone's raw text, or None if there isn't one or it has no digits to dial.

    ``netkeeper.crm.identity.IncomingPhone`` refuses a raw value with no digits
    (free text like "ask reception" typed into the field) and the importer
    drops that cell; exporting it anyway would produce a nine-column file that
    silently loses the number on its own round trip, so it is treated as no
    phone here instead.
    """
    if not contact.phones:
        return None
    raw = contact.phones[0].raw
    return raw if phone_key(raw) else None


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
    _Column("Email Address", "email", lambda c, _today: _sendable_email(c), "email"),
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


def _campaign_audience_columns(listed: Collection[str]) -> tuple[_Column, ...]:
    """:data:`CAMPAIGN_AUDIENCE` with its email column skipping the do-not-send list."""
    email, *rest = CAMPAIGN_AUDIENCE
    column = _Column(
        email.header, email.json_key, lambda c, _today: _sendable_email(c, listed), email.role
    )
    return (column, *rest)


_COLUMN_PRESETS: Final[dict[ExportPreset, tuple[_Column, ...] | None]] = {
    "nine-column": NINE_COLUMN,
    "linkedin-archive": LINKEDIN_ARCHIVE,
    "campaign-audience": CAMPAIGN_AUDIENCE,
    "full": None,  # full has its own row shape (nested children); see below
    "macos-contacts": None,  # vCard 3.0 cards plus group cards; see below
}

# nine-column and linkedin-archive round-trip through netkeeper.crm.importer, whose
# IDENTIFYING_FIELDS rule refuses a row with none of these; company, title, and
# location alone never identify anyone there either, so they are not roles here.
_IDENTIFYING_ROLES: Final[frozenset[str]] = frozenset({"given", "family", "email", "phone", "url"})
_REIMPORTABLE_PRESETS: Final[frozenset[ExportPreset]] = frozenset(
    {"nine-column", "linkedin-archive"}
)


def _is_identified(columns: tuple[_Column, ...], contact: Contact, today: date) -> bool:
    """Whether any identifying column of ``columns`` has a value for ``contact``.

    A row with nothing identifying in it is not importable (the importer's
    "the row identifies nobody" refusal), so :func:`export_stream` drops it
    before it is ever rendered for a re-importable preset, rather than write a
    file that would silently lose the row on its own round trip.
    """
    return any(
        column.get(contact, today) for column in columns if column.role in _IDENTIFYING_ROLES
    )


def _spreadsheet_safe(value: str) -> str:
    """``value`` with a leading ``'`` if a spreadsheet would read it as a formula.

    Checked as written and after ``lstrip()``: some importers trim leading
    whitespace, so `` =1+1`` or a leading newline still reaches ``=`` (#215
    review). The raw check stays because ``lstrip()`` also removes the tab and
    carriage-return triggers themselves. The quote goes before the value
    unchanged; its whitespace is kept.
    """
    if value.startswith(FORMULA_TRIGGERS) or value.lstrip().startswith(FORMULA_TRIGGERS):
        return "'" + value
    return value


def _as_is(value: str) -> str:
    return value


class _Echo:
    """A write-only file-like object that hands the string straight back.

    ``csv.writer`` wants something with a ``write`` method; this lets each
    ``writerow()`` call yield its own line instead of buffering a file.
    """

    def write(self, value: str) -> str:
        return value


def _columns_csv(
    columns: tuple[_Column, ...],
    rows: Iterable[Contact],
    *,
    headerless: bool,
    today: date,
    cell: Callable[[str], str],
) -> Iterator[str]:
    writer = csv.writer(_Echo())
    if not headerless:
        yield writer.writerow([column.header for column in columns])
    for contact in rows:
        yield writer.writerow([cell(column.get(contact, today) or "") for column in columns])


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


def _full_csv(
    rows: Iterable[Contact], *, headerless: bool, cell: Callable[[str], str]
) -> Iterator[str]:
    writer = csv.writer(_Echo())
    if not headerless:
        yield writer.writerow(list(FULL_FIELDS))
    for contact in rows:
        flat = _flatten_full_row(_full_row(contact))
        yield writer.writerow([cell(flat[key]) for key in FULL_FIELDS])


def _full_vcard(contact: Contact) -> str:
    given, family = contact.first_name, contact.last_name
    # FN is the RFC's canonical display name, so it keeps the surname even when
    # the contact goes by a preferred first name (#77).
    shown_given = contact.preferred_name or given
    full_name = " ".join(p for p in (shown_given, family) if p) or "Unknown"
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
        lines.append(f"URL:{_vcard_escape_uri(contact.li_url)}")
    for link in contact.links:
        lines.append(f"URL:{_vcard_escape_uri(link.url)}")
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


def _vcard_escape_uri(value: str) -> str:
    """Escape a URI value (``URL``): percent-encode ``,`` and ``;``, escape backslash and newline.

    RFC 6350 §3.4's backslash escaping of ``,`` and ``;`` is for TEXT values;
    in a URI a strict parser reads ``\\,`` as a literal backslash (#77). Left
    bare, though, they are not safe either: vobject splits the value at the
    first ``,`` and keeps only what precedes it (#215 review). ``%2C`` and
    ``%3B`` are ordinary URI characters, so a strict parser and vobject read
    the same URL. A newline still has to be escaped, or it would end the
    content line.
    """
    value = value.replace(",", "%2C").replace(";", "%3B")
    value = value.replace("\\", "\\\\")
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


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
        lines.append(f"URL:{_vcard_escape_uri(values['url'] or '')}")
    if values.get("adr"):
        lines.append(f"ADR:;;;{_vcard_escape(values['adr'] or '')};;;")
    lines.append("END:VCARD")
    return _vcard_body(lines)


# --- macos-contacts: vCard 3.0 with a group card per tag (P6-02, #249) ---------

UID_NAMESPACE: Final[uuid.UUID] = uuid.UUID("853e7fc6-408e-4b6c-833f-998c611189f2")
"""The namespace every ``macos-contacts`` ``UID`` is a version-5 UUID in.

Changing it changes every UID, and a second import into Contacts would then
see a new card for everybody instead of the one already there."""

_EMAIL_TYPES: Final[dict[EmailKind, str]] = {EmailKind.PERSONAL: "HOME", EmailKind.WORK: "WORK"}
_PHONE_TYPES: Final[dict[PhoneKind, str]] = {
    PhoneKind.MOBILE: "CELL",
    PhoneKind.HOME: "HOME",
    PhoneKind.WORK: "WORK",
}


def _contact_uid(user: User, contact: Contact) -> str:
    """A stable ``UID`` for ``contact``: the same database row always gets the same one."""
    return str(uuid.uuid5(UID_NAMESPACE, f"{user.id}:contact:{contact.id}"))


def _tag_uid(user: User, tag_id: int) -> str:
    """A stable ``UID`` for a tag's group card. A different string from any contact's."""
    return str(uuid.uuid5(UID_NAMESPACE, f"{user.id}:tag:{tag_id}"))


def _type_params(*types: str | None) -> str:
    """``;TYPE=X`` for each type given, as Contacts itself writes them (one param per type)."""
    return "".join(f";TYPE={kind}" for kind in types if kind)


def _macos_vcard(contact: Contact, uid: str) -> str:
    """One contact as a vCard 3.0 card (RFC 2426) for macOS Contacts.

    RFC 2426 escapes TEXT the same way RFC 6350 does and folds the same way,
    so the 4.0 helpers serve here unchanged.
    """
    given, family = contact.first_name, contact.last_name
    shown_given = contact.preferred_name or given
    full_name = " ".join(p for p in (shown_given, family) if p) or "Unknown"
    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"UID:{uid}",
        f"N:{_vcard_escape(family)};{_vcard_escape(given)};;;",
        f"FN:{_vcard_escape(full_name)}",
    ]
    if contact.preferred_name and contact.preferred_name != given:
        lines.append(f"NICKNAME:{_vcard_escape(contact.preferred_name)}")
    if contact.current_company:
        lines.append(f"ORG:{_vcard_escape(contact.current_company)}")
    if contact.current_title:
        lines.append(f"TITLE:{_vcard_escape(contact.current_title)}")
    for email in contact.emails:
        params = _type_params(
            "INTERNET", _EMAIL_TYPES.get(email.kind), "PREF" if email.is_primary else None
        )
        lines.append(f"EMAIL{params}:{_vcard_escape(email.email)}")
    for phone in contact.phones:
        if not phone_key(phone.raw):  # "ask reception" is not a number to dial
            continue
        params = _type_params(_PHONE_TYPES.get(phone.kind), "PREF" if phone.is_primary else None)
        lines.append(f"TEL{params}:{_vcard_escape(phone.raw)}")
    if contact.li_url:
        lines.append(f"URL:{_vcard_escape_uri(contact.li_url)}")
    for link in contact.links:
        lines.append(f"URL:{_vcard_escape_uri(link.url)}")
    if contact.location:
        lines.append(f"ADR:;;;{_vcard_escape(contact.location)};;;")
    if contact.notes:
        lines.append(f"NOTE:{_vcard_escape(contact.notes)}")
    if contact.tags:
        lines.append(f"CATEGORIES:{','.join(_vcard_escape(tag.name) for tag in contact.tags)}")
    lines.append("END:VCARD")
    return _vcard_body(lines)


def _macos_group_vcard(name: str, uid: str, member_uids: Iterable[str]) -> str:
    """A tag as a Contacts group card: ``X-ADDRESSBOOKSERVER-KIND:group`` plus its members."""
    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"UID:{uid}",
        f"N:{_vcard_escape(name)};;;;",
        f"FN:{_vcard_escape(name)}",
        "X-ADDRESSBOOKSERVER-KIND:group",
    ]
    lines.extend(f"X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:{member}" for member in member_uids)
    lines.append("END:VCARD")
    return _vcard_body(lines)


def _macos_contacts(user: User, contacts: Iterable[Contact]) -> Iterator[str]:
    """Every contact's card, then one group card per tag any of them carries.

    The groups come last so each member it names is already in the file, and
    because membership is only known once every contact has streamed. What is
    held meanwhile is a UID per tagged contact, not the contacts themselves.
    Groups are in tag-name order, like ``Contact.tags``.
    """
    groups: dict[int, tuple[str, str, list[str]]] = {}
    for contact in contacts:
        uid = _contact_uid(user, contact)
        yield _macos_vcard(contact, uid)
        for tag in contact.tags:
            groups.setdefault(tag.id, (tag.name_key, tag.name, []))[2].append(uid)
    for tag_id, (_key, name, members) in sorted(groups.items(), key=lambda item: item[1][0]):
        yield _macos_group_vcard(name, _tag_uid(user, tag_id), members)


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
    spreadsheet_safe: bool = False,
) -> Iterator[str]:
    """The exported file for ``preset``/``output_format``, one chunk at a time.

    ``tree`` and ``sort`` are what :mod:`netkeeper.crm.filters` compiles and
    orders by; ``now`` is the instant relative fields (``years_since_connected``,
    and the filter's own relative windows) are computed from, fixed once per
    call so a long export is internally consistent. ``spreadsheet_safe``
    quotes formula-looking CSV cells (see the module docstring; #76).

    **Not a generator.** It compiles ``tree`` and returns the iterator over the
    rendered file, so a filter that cannot compile raises
    :class:`~netkeeper.crm.filters.FilterError` *here*, to a caller that has
    not sent a status line yet and can still answer 422. A generator would
    defer the compile to the first ``next()``, which for
    ``StreamingResponse`` is after the ``200`` is on the wire: the failure
    would reach the client as a truncated file with a success status, which is
    worse than a 500 because nothing about it looks like an error (#95).
    :class:`ExportError`, for a vCard-only preset asked for as CSV or JSON, is
    raised here for the same reason.
    """
    if preset in VCARD_ONLY_PRESETS and output_format != "vcard":
        raise ExportError(f"the {preset} preset is vCard only; ask for format vcard")
    today = _local_today(user, now)
    held_out: list[ColumnElement[bool]] = []
    listed: frozenset[str] = frozenset()
    if preset in ("campaign-audience", "macos-contacts"):
        held_out.append(Contact.do_not_contact.is_(False))
    if preset == "campaign-audience" or output_format == "vcard":
        held_out.append(Contact.needs_review_at.is_(None))
    if preset == "campaign-audience":
        held_out.append(~_holds_an_opted_out_address(user))
        listed = frozenset(entry.email for entry in do_not_send.entries(session, user))
    extra_where = and_(*held_out) if held_out else None
    base = _contacts_statement(session, user, tree, sort, now=now, extra_where=extra_where)
    return _render(
        session,
        base,
        user=user,
        preset=preset,
        output_format=output_format,
        headerless=headerless,
        today=today,
        cell=_spreadsheet_safe if spreadsheet_safe else _as_is,
        listed=listed,
    )


def _holds_an_opted_out_address(user: User) -> ColumnElement[bool]:
    """The contact holds an address the do-not-send list has as ``opted_out`` (#238).

    Such a contact is left out of ``campaign-audience`` as a ``do_not_contact`` one
    is: its owner asked not to be contacted, even on a contact row that never
    recorded it.
    """
    return exists(
        scoped(user, ContactEmail)
        .with_only_columns(ContactEmail.id)
        .join(
            DoNotSendAddress,
            and_(DoNotSendAddress.user_id == user.id, DoNotSendAddress.email == ContactEmail.email),
        )
        .where(
            ContactEmail.contact_id == Contact.id,
            DoNotSendAddress.reason == DoNotSendReason.OPTED_OUT,
        )
    )


def _render(
    session: Session,
    base: Select[tuple[Contact]],
    *,
    user: User,
    preset: ExportPreset,
    output_format: ExportFormat,
    headerless: bool,
    today: date,
    cell: Callable[[str], str],
    listed: Collection[str] = frozenset(),
) -> Iterator[str]:
    """:func:`export_stream`'s body, once everything that can fail early has.

    ``cell`` transforms each CSV cell after rendering; the other formats never see it.
    """
    contacts = _iter_contacts(session, base)
    if preset == "macos-contacts":  # vCard only; export_stream refused any other format
        yield from _macos_contacts(user, contacts)
        return
    columns = _COLUMN_PRESETS[preset]
    if preset == "campaign-audience":
        columns = _campaign_audience_columns(listed)
    if columns is None:  # "full"
        if output_format == "csv":
            yield from _full_csv(contacts, headerless=headerless, cell=cell)
        elif output_format == "json":
            yield from _json_stream(_full_row(contact) for contact in contacts)
        else:
            for contact in contacts:
                yield _full_vcard(contact)
        return
    if preset in _REIMPORTABLE_PRESETS:
        contacts = (c for c in contacts if _is_identified(columns, c, today))
    if output_format == "csv":
        yield from _columns_csv(columns, contacts, headerless=headerless, today=today, cell=cell)
    elif output_format == "json":
        yield from _json_stream(_columns_json(columns, contacts, today=today))
    else:
        for contact in contacts:
            yield _vcard_for(columns, contact, today=today)
