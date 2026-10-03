"""Identity resolution and merge (spec 8.2), applying rows under per-field provenance (spec 10.5).

Importers (P1-03, P1-04) and the LinkedIn sync (P2) turn each row they read into
an :class:`IncomingContact`, ask :func:`resolve` which contact it is, and
:func:`apply` it to that contact or to a new one. A row that resolves to a
:class:`Candidate` waits for a person's :class:`Decision` before it is applied.
:func:`merge` folds one contact into another when a person decides two rows are
the same person.

Resolution order (spec 8.2): ``li_urn``; ``li_public_id`` (the current slug, or
an old one in ``contact_aliases``); any email; then first name, last name, and
current company together, which only ever produces a candidate. Steps 1 and 2
are one lookup on the LinkedIn identity: when the URN and the slug point at
different contacts, or the contact the slug finds carries a different URN, the
row is a candidate too, never a silent match. Within a step, more than one
contact (two sharing an email) is a candidate as well. A contact merged away
resolves to its survivor.

Transactions belong to the caller. Nothing here commits. Every function reads
before it writes (:func:`resolve` too: a public-id match adopts the incoming
URN, spec 8.2 step 2), so the session must be a writer:
``session_scope(factory, write=True)``, or a non-GET request's session.
:func:`merge` flushes between its steps so the unique constraints on ``li_urn``
and ``li_public_id`` never see the value on both rows at once; those flushes
are still inside the caller's transaction, and a rollback undoes them all.

The candidate rule rests on one assumption: a URN is a stable identifier for one
LinkedIn profile. So two rows with different URNs are two people, and a row
whose URN matches nobody is not the contact a slug or an address finds when that
contact carries a URN of its own. URNs are compared as stored (trimmed,
case-sensitive), and nothing here derives a URN from any other field. Should
LinkedIn change the URN scheme (``urn:li:fsd_profile/...`` today), every stored
URN would need a one-off re-normalization before the next sync; no such path
exists today.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final, Literal, assert_never
from urllib.parse import unquote, urlsplit

from sqlalchemy import func
from sqlalchemy.orm import Session

from netkeeper.crm import do_not_send
from netkeeper.crm.provenance import PROVENANCE_ORDER, may_overwrite, record_synced_value
from netkeeper.db import is_writer
from netkeeper.models import (
    Contact,
    ContactAlias,
    ContactChild,
    ContactEmail,
    ContactLink,
    ContactMet,
    ContactPhone,
    ContactPosition,
    ContactSnapshot,
    ContactSource,
    ContactTag,
    ContactTagSuppression,
    EmailKind,
    EmailStatus,
    Enrollment,
    EnrollmentStatus,
    LinkKind,
    ListMember,
    Message,
    MessageStatus,
    MetSource,
    PhoneKind,
    TagSource,
    User,
    linkedin_profile_url,
    normalize_email,
    normalize_public_id,
)
from netkeeper.models.base import utcnow
from netkeeper.scoping import get_scoped, scoped

log = logging.getLogger(__name__)

# A change to any of these on an existing contact writes a contact_snapshot (spec 8.1, 9.8).
JOB_FIELDS: Final[tuple[str, ...]] = ("headline", "current_title", "current_company", "location")
# Of those, the ones whose change is a position change: the dashboard's "changed
# jobs" (spec 9.8, #286). A first fill from empty is not one, and neither is a
# value only an import or a person wrote: a job change is an enrichment
# replacing what an earlier enrichment recorded (#323).
POSITION_FIELDS: Final[tuple[str, ...]] = ("current_title", "current_company")

# More decided wins in a merge: a person who said "met" is not un-met by a duplicate.
MET_RANK: Final[dict[ContactMet, int]] = {
    ContactMet.MET: 3,
    ContactMet.NOT_MET: 2,
    ContactMet.SKIP: 1,
    ContactMet.UNKNOWN: 0,
}

NOTES_SEPARATOR: Final[str] = "\n\n---\n\n"

_PROFILE_PATH = re.compile(r"^/in/([^/]+)/?$")
_NOT_DIGITS = re.compile(r"\D+")

PositionKey = tuple[str | None, str | None, date | None]


# --- normalization ----------------------------------------------------------


def public_id_from_url(url: str | None) -> str | None:
    """The ``/in/`` slug of a LinkedIn profile URL, lowercased and URL-decoded, or None.

    Accepts ``https://www.linkedin.com/in/<slug>/``, the same without a scheme
    or ``www.``, a country subdomain, and a trailing query string or fragment.
    Anything that is not a profile URL on linkedin.com (an old ``/pub/`` URL, a
    company page, another site) gives None. Never derives anything from a URN.
    """
    if url is None:
        return None
    candidate = url.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlsplit(candidate)
    host = parsed.hostname or ""
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    match = _PROFILE_PATH.match(parsed.path)
    if match is None:
        return None
    return normalize_public_id(unquote(match.group(1)))


def phone_key(number: str) -> str:
    """The digits of a phone number and nothing else: the natural key of ``contact_phones``.

    No country-code inference: ``+1 555 0100`` and ``555-0100`` are different keys.
    """
    return _NOT_DIGITS.sub("", number)


def position_key(company: str | None, title: str | None, started_on: date | None) -> PositionKey:
    """The natural key of ``contact_positions``: company and title case-folded, plus the start."""
    return (_fold(company), _fold(title), started_on)


#: The only schemes a stored link may name.
LINK_SCHEMES: Final = frozenset({"http", "https"})

#: A URL's scheme, as a browser reads one: a letter, then letters, digits, ``+``, ``-``,
#: or ``.``, then ``:``.
_SCHEME: Final = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):")

#: What a browser's URL parser drops before it reads a scheme: leading and trailing C0
#: controls and spaces, and every tab and newline anywhere (``java\tscript:``).
_URL_STRIPPED: Final = "".join(chr(n) for n in range(0x21))
_URL_REMOVED: Final = str.maketrans("", "", "\t\n\r")


def link_scheme(url: str) -> str | None:
    """The scheme a browser would read off ``url``, lower-cased, or ``None`` for none."""
    match = _SCHEME.match(url.strip(_URL_STRIPPED).translate(_URL_REMOVED))
    return match.group(1).lower() if match is not None else None


def _clean(value: str | None) -> str | None:
    """Trim; empty becomes None, which every incoming field reads as "not provided"."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _fold(value: str | None) -> str | None:
    cleaned = _clean(value)
    return cleaned.casefold() if cleaned is not None else None


def _unique[T, K](items: Iterable[T], key: Callable[[T], K]) -> tuple[T, ...]:
    """``items`` with later duplicates by ``key`` dropped, order kept."""
    seen: set[K] = set()
    kept: list[T] = []
    for item in items:
        k = key(item)
        if k not in seen:
            seen.add(k)
            kept.append(item)
    return tuple(kept)


# --- the incoming shape -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class IncomingEmail:
    """An address as a source reports it. Stored lowercased and trimmed; empty is an error."""

    email: str
    kind: EmailKind = EmailKind.OTHER
    is_primary: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "email", normalize_email(self.email))
        object.__setattr__(self, "kind", EmailKind(self.kind))


@dataclass(frozen=True, slots=True)
class IncomingPhone:
    """A phone number as a source reports it. ``raw`` is kept; ``number_e164`` is
    derived when ``raw`` is a ``+``-prefixed number of 7 to 15 digits and the
    source gave none. A number with no digits is an error."""

    raw: str
    number_e164: str | None = None
    kind: PhoneKind = PhoneKind.OTHER
    is_primary: bool = False

    def __post_init__(self) -> None:
        raw = self.raw.strip()
        digits = phone_key(raw)
        if not digits:
            raise ValueError("phone number has no digits")
        e164 = _clean(self.number_e164)
        if e164 is None and raw.startswith("+") and 7 <= len(digits) <= 15:
            e164 = f"+{digits}"
        object.__setattr__(self, "raw", raw)
        object.__setattr__(self, "number_e164", e164)
        object.__setattr__(self, "kind", PhoneKind(self.kind))

    @property
    def key(self) -> str:
        return phone_key(self.number_e164 or self.raw)


@dataclass(frozen=True, slots=True)
class IncomingLink:
    """A URL as a source reports it, trimmed; the URL itself is the natural key.

    A URL that names a scheme must name ``http`` or ``https`` (#206 review): a link is
    rendered as an ``href``, and ``javascript:``, ``data:`` and the like would run or
    show something rather than open a site. A URL with no scheme (``example.test``) is
    kept as a source gave it.
    """

    url: str
    kind: LinkKind = LinkKind.OTHER

    def __post_init__(self) -> None:
        url = _clean(self.url)
        if url is None:
            raise ValueError("link url is empty")
        scheme = link_scheme(url)
        if scheme is not None and scheme not in LINK_SCHEMES:
            raise ValueError("a link must be an http or https url")
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "kind", LinkKind(self.kind))


@dataclass(frozen=True, slots=True)
class IncomingPosition:
    """A position as a source reports it. Needs a title or a company.

    ``is_current`` is tri-state: None means the source did not say, and an
    existing row keeps its own value; a new row starts as not current.
    """

    title: str | None = None
    company: str | None = None
    company_urn: str | None = None
    started_on: date | None = None
    ended_on: date | None = None
    is_current: bool | None = None

    def __post_init__(self) -> None:
        title, company = _clean(self.title), _clean(self.company)
        if title is None and company is None:
            raise ValueError("position needs a title or a company")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "company", company)
        object.__setattr__(self, "company_urn", _clean(self.company_urn))

    @property
    def key(self) -> PositionKey:
        return position_key(self.company, self.title, self.started_on)


@dataclass(frozen=True, slots=True)
class IncomingContact:
    """One row from an importer or the sync, normalized.

    Every text field is trimmed and an empty one becomes None, which means "not
    provided": :func:`apply` leaves the contact's value alone. ``li_public_id``
    is lowercased and URL-decoded; when it is missing it is derived from
    ``li_url`` (:func:`public_id_from_url`), and when it is known ``li_url`` is
    the canonical profile URL. A URN is never derived from anything. Children
    are deduplicated by their natural keys, first occurrence kept.
    ``observed_at`` must be timezone-aware and defaults to now.
    """

    source: ContactSource
    observed_at: datetime = field(default_factory=utcnow)
    li_urn: str | None = None
    li_public_id: str | None = None
    li_url: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = None
    current_title: str | None = None
    current_company: str | None = None
    location: str | None = None
    connected_on: date | None = None
    emails: tuple[IncomingEmail, ...] = ()
    phones: tuple[IncomingPhone, ...] = ()
    links: tuple[IncomingLink, ...] = ()
    positions: tuple[IncomingPosition, ...] = ()

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        put = object.__setattr__
        put(self, "source", ContactSource(self.source))
        put(self, "li_urn", _clean(self.li_urn))
        slug = normalize_public_id(unquote(self.li_public_id)) if self.li_public_id else None
        url = _clean(self.li_url)
        if slug is None:
            slug = public_id_from_url(url)
        if slug is not None:
            url = linkedin_profile_url(slug)
        put(self, "li_public_id", slug)
        put(self, "li_url", url)
        for name in ("first_name", "last_name", "headline", "current_title", "current_company"):
            put(self, name, _clean(getattr(self, name)))
        put(self, "location", _clean(self.location))
        put(self, "emails", _unique(self.emails, lambda e: e.email))
        put(self, "phones", _unique(self.phones, lambda p: p.key))
        put(self, "links", _unique(self.links, lambda l: l.url))  # noqa: E741
        put(self, "positions", _unique(self.positions, lambda p: p.key))

    def provided_fields(self) -> dict[str, str | date]:
        """The provenance fields this row carries, in :data:`PROVENANCE_ORDER`."""
        values: dict[str, str | date] = {}
        for name in PROVENANCE_ORDER:
            value: str | date | None = getattr(self, name)
            if value is not None:
                values[name] = value
        return values


# --- resolution results and decisions ---------------------------------------

MatchedBy = Literal["urn", "public_id", "alias", "email"]
CandidateBy = Literal["identity", "email", "name"]


@dataclass(frozen=True, slots=True)
class Matched:
    """The row is this contact. ``by`` names the step that decided it."""

    contact_id: int
    by: MatchedBy


@dataclass(frozen=True, slots=True)
class Candidate:
    """The row may be one of these contacts (ids ascending); a person decides.

    ``identity``: the URN and slug disagree, or the slug's contact has another URN.
    ``email``: several contacts share an address, or the one that does has another URN.
    ``name``: first name, last name, and company match (spec 8.2 step 4).
    """

    contact_ids: tuple[int, ...]
    by: CandidateBy


@dataclass(frozen=True, slots=True)
class New:
    """Nothing matched: the row is a new contact."""


Resolution = Matched | Candidate | New


@dataclass(frozen=True, slots=True)
class MergeInto:
    """Apply the candidate row to this contact. It need not be one of the candidates."""

    contact_id: int


@dataclass(frozen=True, slots=True)
class CreateNew:
    """The candidate row is a new contact after all."""


Decision = MergeInto | CreateNew


# --- resolve ----------------------------------------------------------------


def _require_writer(session: Session) -> None:
    """Fail at once rather than with "database is locked" on the first write (CLAUDE.md)."""
    if not is_writer(session):
        raise RuntimeError(
            "identity operations need a writer session; use session_scope(factory, write=True)"
        )


def resolve(session: Session, user: User, incoming: IncomingContact) -> Resolution:
    """Which of ``user``'s contacts ``incoming`` is (spec 8.2). See the module docstring.

    ``RuntimeError`` when ``session`` is not a writer.
    """
    _require_writer(session)
    resolution = (
        _resolve_identity(session, user, incoming)
        or _resolve_email(session, user, incoming)
        or _resolve_name(session, user, incoming)
    )
    log.debug("resolved incoming row for user %d: %r", user.id, resolution)
    return resolution


def resolve_survivor(session: Session, user: User, contact_id: int) -> Contact:
    """The contact at the end of ``contact_id``'s ``merged_into_id`` chain.

    ``ValueError`` when the id is not one of ``user``'s contacts.
    """
    return _survivor_of(session, user, _owned(session, user, contact_id))


def _owned(session: Session, user: User, contact_id: int) -> Contact:
    contact = get_scoped(session, user, Contact, contact_id)
    if contact is None:
        raise ValueError(f"contact {contact_id} is not one of user {user.id}'s contacts")
    return contact


def _survivor_of(session: Session, user: User, contact: Contact) -> Contact:
    seen = {contact.id}
    while contact.merged_into_id is not None:
        survivor = get_scoped(session, user, Contact, contact.merged_into_id)
        if survivor is None:
            raise RuntimeError(
                f"contact {contact.id} is merged into {contact.merged_into_id}, "
                f"which is not one of user {user.id}'s contacts"
            )
        if survivor.id in seen:
            raise RuntimeError(f"merge chain from contact {contact.id} loops")
        seen.add(survivor.id)
        contact = survivor
    return contact


def _resolve_identity(session: Session, user: User, incoming: IncomingContact) -> Resolution | None:
    """Steps 1 and 2 together: URN, then current slug, then alias, all agreeing."""
    hits: dict[int, tuple[Contact, MatchedBy]] = {}
    urns: set[str] = set()

    def hit(row: Contact, by: MatchedBy) -> None:
        survivor = _survivor_of(session, user, row)
        hits.setdefault(survivor.id, (survivor, by))
        urns.update(urn for urn in (row.li_urn, survivor.li_urn) if urn is not None)

    if incoming.li_urn is not None:
        by_urn = scoped(user, Contact).where(Contact.li_urn == incoming.li_urn)
        for row in session.scalars(by_urn):
            hit(row, "urn")
    if incoming.li_public_id is not None:
        by_slug = scoped(user, Contact).where(Contact.li_public_id == incoming.li_public_id)
        for row in session.scalars(by_slug):
            hit(row, "public_id")
        by_alias = (
            scoped(user, Contact)
            .join(ContactAlias, ContactAlias.contact_id == Contact.id)
            .where(
                ContactAlias.user_id == user.id,
                ContactAlias.li_public_id == incoming.li_public_id,
            )
            .order_by(Contact.id)
        )
        for row in session.scalars(by_alias):
            hit(row, "alias")
    if not hits:
        return None
    if len(hits) > 1:
        return Candidate(tuple(sorted(hits)), by="identity")
    ((contact, by),) = hits.values()
    if by != "urn" and incoming.li_urn is not None:
        if urns:
            # The URN matched nobody, so a URN on the slug's contact is another identity.
            return Candidate((contact.id,), by="identity")
        # Spec 8.2 step 2: a slug match learns the URN.
        contact.li_urn = incoming.li_urn
        _record(contact, "li_urn", incoming.source)
    return Matched(contact.id, by)


def _resolve_email(session: Session, user: User, incoming: IncomingContact) -> Resolution | None:
    """Step 3: any address, exact after lowercasing."""
    if not incoming.emails:
        return None
    addresses = [email.email for email in incoming.emails]
    statement = (
        scoped(user, Contact)
        .join(ContactEmail, ContactEmail.contact_id == Contact.id)
        .where(ContactEmail.user_id == user.id, ContactEmail.email.in_(addresses))
        .order_by(Contact.id)
    )
    survivors: dict[int, Contact] = {}
    urns: set[str] = set()
    for row in session.scalars(statement):  # a contact repeats per matching address
        survivor = _survivor_of(session, user, row)
        survivors.setdefault(survivor.id, survivor)
        urns.update(urn for urn in (row.li_urn, survivor.li_urn) if urn is not None)
    if not survivors:
        return None
    if len(survivors) > 1:
        return Candidate(tuple(sorted(survivors)), by="email")
    (contact,) = survivors.values()
    if incoming.li_urn is not None and urns:
        # The URN matched nobody, so a URN on the address's contact is another identity.
        return Candidate((contact.id,), by="email")
    return Matched(contact.id, by="email")


def name_company_key(incoming: IncomingContact) -> tuple[str, str, str] | None:
    """The first name, last name, and company :func:`_resolve_name` matches on, case-folded.

    ``None`` when the row does not carry all three, which is exactly when
    :func:`_resolve_name` cannot produce a candidate for it either. Every field
    ``IncomingContact`` holds is already trimmed (``__post_init__``), so lowering
    the case is the only normalization left to do; this is the single place that
    does it, so anything comparing two rows' name and company agrees with what
    resolution itself would find (spec 8.2 step 4).
    """
    if not (incoming.first_name and incoming.last_name and incoming.current_company):
        return None
    return (
        incoming.first_name.lower(),
        incoming.last_name.lower(),
        incoming.current_company.lower(),
    )


def _resolve_name(session: Session, user: User, incoming: IncomingContact) -> Resolution:
    """Step 4: first name, last name, and company, case-insensitive and trimmed; then step 5."""
    key = name_company_key(incoming)
    if key is None:
        return New()
    first, last, company = key
    statement = (
        scoped(user, Contact)
        .where(
            func.lower(func.trim(Contact.first_name)) == first,
            func.lower(func.trim(Contact.last_name)) == last,
            func.lower(func.trim(Contact.current_company)) == company,
        )
        .order_by(Contact.id)
    )
    ids = sorted({_survivor_of(session, user, row).id for row in session.scalars(statement)})
    return Candidate(tuple(ids), by="name") if ids else New()


# --- apply ------------------------------------------------------------------


def apply(
    session: Session,
    user: User,
    incoming: IncomingContact,
    resolution: Resolution,
    *,
    decision: Decision | None = None,
    snapshot: bool = True,
) -> Contact:
    """Write ``incoming`` to the contact ``resolution`` names, or to a new one, and return it.

    A new contact takes ``incoming.source`` as its first source and records it in
    ``field_sources`` for every provided field. An existing contact takes each
    provided field only when :func:`~netkeeper.crm.provenance.may_overwrite`
    allows it; a slug change keeps the old slug in ``contact_aliases``; a change
    to any job field writes a ``contact_snapshot`` of the values before it,
    marked ``position_changed`` when a sync brings a title or company other
    than an earlier sync recorded (#323; see below). Either
    way, a row from any source but ``manual`` also notes every provided field in
    ``synced_values`` with its source and ``observed_at``, whether or not the
    live column took it (unless a newer observation is already noted), so a
    manual override always has a synced value to revert to
    (:func:`~netkeeper.crm.provenance.revert_to_synced`).
    Children are upserted by natural key (email; phone digits; link URL;
    company, title, and start date), never duplicated, with ``source`` and
    ``observed_at`` refreshed unless the row was observed more recently, an
    existing row's ``is_primary`` kept, and a position's ``is_current`` changed
    only when the row says so.

    **What counts as a job change (#323).** ``position_changed`` is set only
    when ``incoming`` comes from the sync and gives a ``current_title`` or
    ``current_company`` different from the non-empty value an earlier sync
    recorded (:func:`_enriched_value`). Only a profile visit (enrichment) gives
    a sync title or company; the connections list gives a name and headline. So
    a contact's first enrichment, which replaces a title or company from the
    archive, a CSV, or a person, never counts, and a later one counts only
    against what an earlier one recorded. A person's override does not hide a
    move: the comparison is with the last synced value, the snapshot is
    written with that value as the job before, and the column keeps the
    person's value.

    ``snapshot=False`` skips the ``contact_snapshot`` a job-field change would
    write: the sync passes it when the values being replaced were only ever read
    off a card on the connections page (a contact still marked needs review,
    #184), which is no job history and must not read as a job change.

    A :class:`Candidate` needs ``decision``; any other resolution refuses one.
    ``ValueError`` for a missing or misplaced decision, for a contact that is not
    ``user``'s, and for a URN or slug that another contact of ``user`` holds
    (merge the two first: nothing here takes an identity away from a contact).
    Every check runs before the first write, so a ``ValueError`` leaves the
    contact exactly as it was: a caller that catches it per row and commits the
    rest commits nothing of that row. ``RuntimeError`` when ``session`` is not a
    writer.
    """
    _require_writer(session)
    match resolution:
        case Matched(contact_id=contact_id):
            _no_decision(decision)
            contact = resolve_survivor(session, user, contact_id)
            return _update(session, user, contact, incoming, snapshot=snapshot)
        case New():
            _no_decision(decision)
            return _create(session, user, incoming)
        case Candidate():
            match decision:
                case None:
                    raise ValueError(
                        "a Candidate resolution needs a decision: "
                        "MergeInto(contact_id) or CreateNew()"
                    )
                case MergeInto(contact_id=contact_id):
                    contact = resolve_survivor(session, user, contact_id)
                    return _update(session, user, contact, incoming, snapshot=snapshot)
                case CreateNew():
                    return _create(session, user, incoming)
                case _:
                    assert_never(decision)
        case _:
            assert_never(resolution)


def _no_decision(decision: Decision | None) -> None:
    if decision is not None:
        raise ValueError("a decision applies to a Candidate resolution only")


def _create(session: Session, user: User, incoming: IncomingContact) -> Contact:
    provided = incoming.provided_fields()
    _assert_identities_free(session, user, None, provided)
    contact = Contact(user_id=user.id, source=incoming.source, field_sources={}, synced_values={})
    for name, value in provided.items():
        setattr(contact, name, value)
        contact.field_sources[name] = incoming.source.value
    _record_synced(contact, incoming)
    session.add(contact)
    _upsert_children(user, contact, incoming)
    session.flush()
    log.debug("created contact %d for user %d from %s", contact.id, user.id, incoming.source)
    return contact


def _update(
    session: Session,
    user: User,
    contact: Contact,
    incoming: IncomingContact,
    *,
    snapshot: bool = True,
) -> Contact:
    provided = incoming.provided_fields()
    # Decided once per field, from the state before any write: the rule reads the
    # live value, and an empty field that a lower-ranked row is free to fill would
    # look protected again the moment it held that row's value.
    overwrite = {name: may_overwrite(name, incoming.source, contact) for name in provided}
    writable = {
        name: value
        for name, value in provided.items()
        if overwrite[name] and getattr(contact, name) != value
    }
    # Every check comes before the first setattr: the row applies fully or not at all.
    # A setattr first would be autoflushed by the next check's query, and a caller
    # that catches the ValueError per row would commit that half of the row.
    _assert_identities_free(session, user, contact.id, writable)
    before = {name: getattr(contact, name) for name in JOB_FIELDS}
    # Read before the loop below records the incoming source and value over it.
    enriched_before = {name: _enriched_value(contact, name) for name in POSITION_FIELDS}
    # A snapshot holds the job as LinkedIn last showed it: for a field a person
    # overrode, that is the last synced value, not the person's words (#323).
    snapshot_values = dict(before)
    for name in POSITION_FIELDS:
        if _overridden(contact, name) and enriched_before[name] is not None:
            snapshot_values[name] = enriched_before[name]
    for name, value in provided.items():
        if name in writable:
            old: str | date | None = getattr(contact, name)
            setattr(contact, name, value)
            if name == "li_public_id" and isinstance(value, str):
                _retire_slug(session, user, contact, old=old, new=value, incoming=incoming)
        if overwrite[name]:
            _record(contact, name, incoming.source)
    _record_synced(contact, incoming)
    position_changed = incoming.source is ContactSource.SYNC and any(
        enriched_before[name] not in (None, "")
        and name in provided
        and provided[name] != enriched_before[name]
        for name in POSITION_FIELDS
    )
    if snapshot and (
        position_changed
        or any(
            before[name] not in (None, "") and before[name] != getattr(contact, name)
            for name in JOB_FIELDS
        )
    ):
        contact.snapshots.append(
            ContactSnapshot(
                user_id=user.id,
                source=incoming.source,
                observed_at=incoming.observed_at,
                position_changed=position_changed,
                **snapshot_values,
            )
        )
    _upsert_children(user, contact, incoming)
    session.flush()
    log.debug("updated contact %d for user %d from %s", contact.id, user.id, incoming.source)
    return contact


def _enriched_value(contact: Contact, name: str) -> str | date | None:
    """What an earlier enrichment last recorded for position field ``name``, if anything.

    The live value when the sync wrote it. When a person overrode it, the value
    the sync last reported (``synced_values``), so a manual edit does not hide a
    later move (#323). ``None`` when no enrichment recorded the field: the
    connections list never gives a title or company, and an import's or a
    person's value is not an earlier enrichment.
    """
    recorded = (contact.field_sources or {}).get(name)
    if recorded == ContactSource.SYNC.value:
        value: str | date | None = getattr(contact, name)
        return value
    if _overridden(contact, name):
        entry = (contact.synced_values or {}).get(name)
        if entry is not None and entry["source"] == ContactSource.SYNC.value:
            return entry["value"]
    return None


def _overridden(contact: Contact, name: str) -> bool:
    return (contact.field_sources or {}).get(name) == ContactSource.MANUAL.value


def _record(contact: Contact, name: str, source: ContactSource) -> None:
    if contact.field_sources.get(name) != source.value:
        contact.field_sources[name] = source.value


def _record_synced(contact: Contact, incoming: IncomingContact) -> None:
    """Note every provided field in the ledger, whether or not the column took it."""
    if incoming.source is ContactSource.MANUAL:
        return
    for name, value in incoming.provided_fields().items():
        record_synced_value(
            contact, name, value, source=incoming.source, observed_at=incoming.observed_at
        )


def _assert_identities_free(
    session: Session, user: User, contact_id: int | None, values: dict[str, str | date]
) -> None:
    """``ValueError`` when another contact of ``user`` holds the URN or slug in ``values``.

    Runs before anything is written, so the error leaves the contact untouched.
    """
    for name in ("li_urn", "li_public_id"):
        value = values.get(name)
        if value is None:
            continue
        column = Contact.li_urn if name == "li_urn" else Contact.li_public_id
        statement = scoped(user, Contact).where(column == value)
        if contact_id is not None:
            statement = statement.where(Contact.id != contact_id)
        holder = session.scalars(statement.limit(1)).first()
        if holder is not None:
            raise ValueError(
                f"{name} {value!r} already belongs to contact {holder.id}; "
                "merge the two contacts first"
            )


def _retire_slug(
    session: Session,
    user: User,
    contact: Contact,
    *,
    old: str | date | None,
    new: str,
    incoming: IncomingContact,
) -> None:
    """After a vanity-URL change: the old slug becomes an alias, and no alias equals the new one.

    An alias row for the new slug on any contact is stale (a slug belongs to one
    profile at a time) and is removed; so is one on this contact, which would be
    a rename back.
    """
    if isinstance(old, str):
        _adopt_alias(session, user, contact, old, incoming.source, incoming.observed_at)
    stale = scoped(user, ContactAlias).where(ContactAlias.li_public_id == new)
    for alias in session.scalars(stale):
        alias.contact.aliases.remove(alias)


def _adopt_alias(
    session: Session,
    user: User,
    contact: Contact,
    slug: str,
    source: ContactSource,
    observed_at: datetime,
) -> None:
    """Make ``slug`` an alias of ``contact``; an alias row held elsewhere moves here."""
    existing = session.scalars(
        scoped(user, ContactAlias).where(ContactAlias.li_public_id == slug)
    ).one_or_none()
    if existing is None:
        contact.aliases.append(
            ContactAlias(user_id=user.id, li_public_id=slug, source=source, observed_at=observed_at)
        )
    elif existing.contact_id != contact.id:
        existing.contact = contact
        existing.source = source
        existing.observed_at = observed_at


def _upsert_children(user: User, contact: Contact, incoming: IncomingContact) -> None:
    _upsert_emails(user, contact, incoming)
    _upsert_phones(user, contact, incoming)
    _upsert_links(user, contact, incoming)
    _upsert_positions(user, contact, incoming)


def _refresh(row: ContactChild, incoming: IncomingContact) -> bool:
    """Stamp ``row`` with the incoming observation; False when the row was seen more recently."""
    if incoming.observed_at < row.observed_at:
        return False
    row.source = incoming.source
    row.observed_at = incoming.observed_at
    return True


def _upsert_emails(user: User, contact: Contact, incoming: IncomingContact) -> None:
    existing = {row.email: row for row in contact.emails}
    has_primary = any(row.is_primary for row in contact.emails)
    for email in incoming.emails:
        row = existing.get(email.email)
        if row is None:
            row = ContactEmail(
                user_id=user.id,
                email=email.email,
                kind=email.kind,
                is_primary=email.is_primary and not has_primary,
                source=incoming.source,
                observed_at=incoming.observed_at,
            )
            contact.emails.append(row)
            existing[email.email] = row
            has_primary = has_primary or row.is_primary
        elif _refresh(row, incoming) and email.kind is not EmailKind.OTHER:
            row.kind = email.kind


def _upsert_phones(user: User, contact: Contact, incoming: IncomingContact) -> None:
    existing = {phone_key(row.number_e164 or row.raw): row for row in contact.phones}
    has_primary = any(row.is_primary for row in contact.phones)
    for phone in incoming.phones:
        row = existing.get(phone.key)
        if row is None:
            row = ContactPhone(
                user_id=user.id,
                raw=phone.raw,
                number_e164=phone.number_e164,
                kind=phone.kind,
                is_primary=phone.is_primary and not has_primary,
                source=incoming.source,
                observed_at=incoming.observed_at,
            )
            contact.phones.append(row)
            existing[phone.key] = row
            has_primary = has_primary or row.is_primary
        elif _refresh(row, incoming):
            if row.number_e164 is None and phone.number_e164 is not None:
                row.number_e164 = phone.number_e164
            if phone.kind is not PhoneKind.OTHER:
                row.kind = phone.kind


def _upsert_links(user: User, contact: Contact, incoming: IncomingContact) -> None:
    existing = {row.url: row for row in contact.links}
    for link in incoming.links:
        row = existing.get(link.url)
        if row is None:
            row = ContactLink(
                user_id=user.id,
                url=link.url,
                kind=link.kind,
                source=incoming.source,
                observed_at=incoming.observed_at,
            )
            contact.links.append(row)
            existing[link.url] = row
        elif _refresh(row, incoming) and link.kind is not LinkKind.OTHER:
            row.kind = link.kind


def _upsert_positions(user: User, contact: Contact, incoming: IncomingContact) -> None:
    existing = {
        position_key(row.company, row.title, row.started_on): row for row in contact.positions
    }
    for position in incoming.positions:
        row = existing.get(position.key)
        if row is None:
            row = ContactPosition(
                user_id=user.id,
                title=position.title,
                company=position.company,
                company_urn=position.company_urn,
                started_on=position.started_on,
                ended_on=position.ended_on,
                is_current=bool(position.is_current),
                source=incoming.source,
                observed_at=incoming.observed_at,
            )
            contact.positions.append(row)
            existing[position.key] = row
        elif _refresh(row, incoming):
            if row.company_urn is None and position.company_urn is not None:
                row.company_urn = position.company_urn
            if position.ended_on is not None:
                row.ended_on = position.ended_on
            if position.is_current is not None:
                row.is_current = position.is_current


# --- merge ------------------------------------------------------------------


def merge(session: Session, user: User, survivor_id: int, loser_id: int) -> Contact:
    """Fold ``loser_id`` into ``survivor_id`` and return the survivor (spec 8.2).

    Every child row moves to the survivor; emails, phones, links, and positions
    are deduplicated by natural key (the survivor's row stays, the loser's
    duplicate goes; a shared address keeps the worse of the two statuses,
    :data:`EMAIL_STATUS_SEVERITY`), snapshots, aliases, and interactions all move. The
    survivor's empty provenance fields take the loser's values with the loser's
    recorded source, and its ``synced_values`` fill in from the loser's for the
    fields it has none for. Tag assignments and suppressions carry across under
    :func:`~netkeeper.crm.tags.tag_contact`'s precedence, one row per tag; see
    :func:`_merge_tags`. Static-list memberships carry across too, so the
    survivor is in every list the loser was in; see :func:`_merge_list_members`.
    Campaign messages all move, and so do enrollments, one per campaign; see
    :func:`_merge_campaign_rows`.
    The loser's URN and slug move to the survivor when it lacks
    them; otherwise the slug becomes an alias of the survivor and the URN is
    dropped. Both are cleared on the loser, whose ``merged_into_id`` points at
    the survivor. ``met`` takes the more decided value, ``do_not_contact`` is
    true if either was, notes are joined with :data:`NOTES_SEPARATOR`,
    ``last_contacted_at`` is the later one, a customized ``preferred_name`` on
    the loser fills a default one on the survivor, and the survivor stays
    archived only if both were.

    Idempotent: a loser already merged into this survivor (through any chain)
    is a no-op. A survivor that was itself merged away stands for its own
    survivor. ``ValueError`` for the same id twice, a contact that is not
    ``user``'s, or a loser already merged into a different contact.
    ``RuntimeError`` when ``session`` is not a writer.
    """
    _require_writer(session)
    if survivor_id == loser_id:
        raise ValueError("a contact cannot be merged into itself")
    survivor = resolve_survivor(session, user, survivor_id)
    loser = _owned(session, user, loser_id)
    if loser.merged_into_id is not None:
        final = _survivor_of(session, user, loser)
        if final.id != survivor.id:
            raise ValueError(
                f"contact {loser_id} is already merged into {final.id}, not {survivor.id}"
            )
        return survivor
    if survivor.id == loser.id:
        raise ValueError(f"contact {survivor_id} is merged into {loser_id}; nothing to merge")
    # Both rows came through get_scoped, so both are this user's. Everything below
    # moves rows between them without re-filtering each one, so state the invariant
    # rather than assume it: one cross-user row here would leak across users.
    if survivor.user_id != user.id or loser.user_id != user.id:
        raise ValueError(
            f"contacts {survivor.id} and {loser.id} are not both contacts of user {user.id}"
        )
    log.info("merging contact %d into %d for user %d", loser.id, survivor.id, user.id)

    _merge_identity(session, user, survivor, loser)
    _merge_children(survivor, loser)
    for email in survivor.emails:  # already listed when it got the status; made sure of here
        do_not_send.add_for_status(session, user, email.email, email.status, contact_id=survivor.id)
    _merge_tags(session, user, survivor, loser)
    _merge_list_members(session, user, survivor, loser)
    _merge_campaign_rows(session, user, survivor, loser)
    _merge_scalars(survivor, loser)
    _merge_synced_values(survivor, loser)
    loser.merged_into_id = survivor.id
    session.flush()
    return survivor


def _merge_identity(session: Session, user: User, survivor: Contact, loser: Contact) -> None:
    """Move the loser's URN and slug across, or alias the slug, without the uniques colliding."""
    urn, slug = loser.li_urn, loser.li_public_id
    urn_source, slug_source = _source_of(loser, "li_urn"), _source_of(loser, "li_public_id")
    loser.li_urn = None
    loser.li_public_id = None
    loser.field_sources.pop("li_urn", None)
    loser.field_sources.pop("li_public_id", None)
    session.flush()  # the loser's values are free before the survivor takes them
    if urn is not None:
        if survivor.li_urn is None:
            survivor.li_urn = urn
            survivor.field_sources["li_urn"] = urn_source
        else:
            log.info(
                "contact %d keeps its URN; the URN of merged contact %d is dropped",
                survivor.id,
                loser.id,
            )
    if slug is not None:
        if survivor.li_public_id is None:
            survivor.li_public_id = slug
            survivor.field_sources["li_public_id"] = slug_source
        else:
            _adopt_alias(session, user, survivor, slug, ContactSource(slug_source), utcnow())


def _source_of(contact: Contact, name: str) -> str:
    return contact.field_sources.get(name) or contact.source.value


def _take_source(survivor: Contact, loser: Contact, name: str) -> None:
    """The survivor's record for ``name`` becomes the loser's.

    A loser with no record for the field lends its first source, as ever --
    except a contact still waiting for review (#184), whose unrecorded fields are
    a connections-page card's text written at the lowest provenance there is
    (none: open to every source, spec 10.5). Recording its first source (``sync``)
    for them on the survivor would promote a card's text to sync rank, and a
    later archive import could no longer correct it; the survivor's field is
    left unrecorded instead.
    """
    recorded = loser.field_sources.get(name)
    if recorded is not None:
        survivor.field_sources[name] = recorded
    elif loser.needs_review_at is not None:
        survivor.field_sources.pop(name, None)
    else:
        survivor.field_sources[name] = loser.source.value


EMAIL_STATUS_SEVERITY: Final[Mapping[EmailStatus, int]] = {
    EmailStatus.OK: 0,
    EmailStatus.INVALID: 1,
    EmailStatus.BOUNCED: 2,
}
"""Which status a merged address keeps when both contacts hold it: the higher (#238)."""


def _merge_children(survivor: Contact, loser: Contact) -> None:
    _keep_worse_email_status(survivor.emails, loser.emails)
    _move_keyed(survivor.emails, loser.emails, key=lambda row: row.email)
    _keep_one_primary(survivor.emails)
    _move_keyed(
        survivor.phones, loser.phones, key=lambda row: phone_key(row.number_e164 or row.raw)
    )
    _keep_one_primary(survivor.phones)
    _move_keyed(survivor.links, loser.links, key=lambda row: row.url)
    _move_keyed(
        survivor.positions,
        loser.positions,
        key=lambda row: position_key(row.company, row.title, row.started_on),
    )
    for snapshot in list(loser.snapshots):
        survivor.snapshots.append(snapshot)
    for interaction in list(loser.interactions):
        survivor.interactions.append(interaction)
    for alias in list(loser.aliases):
        if alias.li_public_id == survivor.li_public_id:
            loser.aliases.remove(alias)  # the survivor's own slug is not its alias
        else:
            survivor.aliases.append(alias)


def _keep_worse_email_status(
    target: Sequence[ContactEmail], source: Sequence[ContactEmail]
) -> None:
    """Where both contacts hold an address, the survivor's row takes the worse status.

    :func:`_move_keyed` keeps the survivor's row and drops the loser's, so without
    this a bounce on the loser's copy would be lost and the survivor would be
    sendable at a bounced address (#238).
    """
    theirs = {row.email: row.status for row in source}
    for row in target:
        other = theirs.get(row.email)
        if other is not None and EMAIL_STATUS_SEVERITY[other] > EMAIL_STATUS_SEVERITY[row.status]:
            row.status = other


def _move_keyed[T: ContactChild, K](
    target: list[T], source: list[T], *, key: Callable[[T], K]
) -> None:
    """Re-parent ``source`` rows onto ``target``; a row whose key ``target`` has is dropped."""
    seen = {key(row) for row in target}
    for row in list(source):
        k = key(row)
        if k in seen:
            source.remove(row)
        else:
            seen.add(k)
            target.append(row)


def _keep_one_primary(rows: Sequence[ContactEmail] | Sequence[ContactPhone]) -> None:
    """The survivor's primary (first in the list) stays; a moved primary is demoted."""
    seen = False
    for row in rows:
        if row.is_primary:
            if seen:
                row.is_primary = False
            seen = True


def _merge_tags(session: Session, user: User, survivor: Contact, loser: Contact) -> None:
    """Carry the loser's tag assignments and suppressions across, one row per tag (spec 8.3).

    The precedence is :func:`~netkeeper.crm.tags.tag_contact`'s, read across both
    contacts: a ``manual`` assignment beats a ``rule`` or an ``llm`` one, and on a
    tie the survivor's row stays, because its ``contact_id`` is already right and
    its ``rule_id`` credit is current. A ``rule`` assignment that moves keeps that
    credit, so the next run re-credits it instead of counting it as newly added.
    A ``manual`` assignment that wins over an automatic one leaves the row
    ``manual`` with no rule, exactly as ``tag_contact`` does.

    Suppressions carry across too: one on the loser says the user removed that
    automatic tag from that person, and the person survives the merge. Where a
    suppression on one contact meets an automatic assignment on the other, the
    suppression wins, in both directions:

    - The loser's suppression removes the survivor's ``rule`` or ``llm``
      assignment, which is what :func:`~netkeeper.crm.tags.untag_contact` would
      have done to the survivor; without it a tag the user deliberately removed
      would come back with the merge, which is what the suppression exists to
      prevent.
    - The survivor's suppression drops the loser's ``rule`` or ``llm``
      assignment, because ``tag_contact`` refuses an automatic assignment on a
      contact that suppresses the tag (:class:`~netkeeper.crm.tags.TagSuppressed`).

    A ``manual`` assignment on either contact beats a suppression on either and
    clears it, as ``tag_contact`` with its default source does. So the survivor
    keeps at most one of an assignment and a suppression per tag, the invariant
    those two functions hold on a single contact, and the loser keeps neither.
    """
    mine_tagged = _assignments_of(session, user, survivor.id)
    their_tagged = _assignments_of(session, user, loser.id)
    mine_blocked = _suppressions_of(session, user, survivor.id)
    their_blocked = _suppressions_of(session, user, loser.id)
    for tag_id in sorted({*mine_tagged, *their_tagged, *mine_blocked, *their_blocked}):
        assigned = (mine_tagged.get(tag_id), their_tagged.get(tag_id))
        blocked = (mine_blocked.get(tag_id), their_blocked.get(tag_id))
        if any(row is not None and row.source is TagSource.MANUAL for row in assigned):
            kept = _keep_assignment(session, survivor, *assigned)
            kept.source = TagSource.MANUAL
            kept.rule_id = None
            _drop(session, *blocked)
        elif any(row is not None for row in blocked):
            if any(row is not None for row in assigned):
                log.info(
                    "tag %d stays suppressed for contact %d after the merge of contact %d; "
                    "the automatic assignment is dropped",
                    tag_id,
                    survivor.id,
                    loser.id,
                )
            _drop(session, *assigned)
            _keep_suppression(session, survivor, *blocked)
        else:
            _keep_assignment(session, survivor, *assigned)
    session.flush()
    # _drop deletes rows the loser's collection may still hold, and the survivor's
    # read-only ``tags`` does not see an assignment appended to it; reload both on
    # next access so a caller serializing either in this session reads what is stored.
    for contact in (survivor, loser):
        session.expire(contact, ["tag_assignments", "tags"])


def _merge_list_members(session: Session, user: User, survivor: Contact, loser: Contact) -> None:
    """Re-point the loser's static-list memberships at the survivor (spec 10.4; #81).

    The rows are the person's place in every "First 100"-style list they were
    put in by hand, and the person is not going anywhere — only one of their
    two records is. Left alone, the row keeps naming the loser, which
    :func:`netkeeper.crm.lists.list_members` skips as merged away, so the list
    quietly comes back one member short and nobody is told.

    A row whose list the survivor is already in is deleted rather than moved:
    ``uq_list_members_user_id_list_id_contact_id`` allows one membership per
    (list, contact), and the survivor's own row is the one to keep, being both
    already correct and already ordered by its own ``added_at``. A moved row
    keeps the ``added_at`` it had, which is when that person joined the list;
    the merge is not a new addition.

    Only static lists have rows here at all — a smart list's members are its
    filter's result, which follows the survivor by itself.
    """
    mine = set(
        session.scalars(
            scoped(user, ListMember)
            .with_only_columns(ListMember.list_id)
            .where(ListMember.contact_id == survivor.id)
        )
    )
    theirs = session.scalars(
        scoped(user, ListMember)
        .where(ListMember.contact_id == loser.id)
        .order_by(ListMember.list_id)
    ).all()
    for row in theirs:
        if row.list_id in mine:
            session.delete(row)
        else:
            row.contact_id = survivor.id
            mine.add(row.list_id)
    if theirs:
        log.debug(
            "moved %d list memberships from contact %d to %d", len(theirs), loser.id, survivor.id
        )
    session.flush()


MERGED_EXIT_REASON: Final = "merged"
"""``exit_reason`` of an enrollment a merge set aside for the survivor's in the same campaign."""

STOP_PRECEDENCE: Final[tuple[EnrollmentStatus, ...]] = (
    EnrollmentStatus.OPTED_OUT,
    EnrollmentStatus.BOUNCED,
    EnrollmentStatus.REPLIED,
    EnrollmentStatus.REMOVED,
)
"""Enrollments the person or the contact ended, strongest first (#242 review).

Of two in one campaign, one of these is kept over one still going, and the
earlier here over the later: an opt-out must survive a merge with a reply.
"""

UNSENT_MESSAGE_STATUSES: Final[frozenset[MessageStatus]] = frozenset(
    {MessageStatus.DRAFTED, MessageStatus.PREFILLED}
)
"""Outbound messages waiting for the person. A merge discards the set-aside enrollment's
(#242 review). A discarded message's Gmail draft stays in Gmail for the person:
netkeeper deletes nothing there (ADR 0003; #273, question 2). Not ``scheduled``: a
scheduled message may be in the sender's hands or out already, so the engine's
reconcile decides it after a search by Message-ID, never a merge (#269)."""

LIVE_AFTER_MERGE: Final[frozenset[EnrollmentStatus]] = frozenset(
    {EnrollmentStatus.PENDING, EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED}
)
"""A combined enrollment in one of these still has steps to send; any other keeps no
unsent message (#247 review)."""

_ENROLLMENT_STATE: Final = (
    "status",
    "current_step",
    "next_action_at",
    "exit_reason",
    "replied_at",
    "channel_ids_json",
)
"""The columns that say where an enrollment is: what the kept one's row carries (#242)."""


def _merge_campaign_rows(session: Session, user: User, survivor: Contact, loser: Contact) -> None:
    """Move the loser's campaign messages and enrollments to the survivor (spec 8.2; #242).

    Every message moves: nothing about a message is unique per contact, and the
    survivor's timeline and the recency guard (spec 11.9) must see all that was
    sent to the person.

    An enrollment moves when the survivor has none in that campaign. When both
    do, ``(campaign_id, contact_id)`` allows one, so the two combine. Whose
    state wins:

    - An enrollment the person or the contact ended (:data:`STOP_PRECEDENCE`)
      wins over one still going, and the earlier in that order over the later.
      A merge must never restart a sequence somebody opted out of, bounced
      from, replied to, or was taken out of, nor let a reply hide an opt-out.
    - Otherwise the one furthest along (the higher ``current_step``).
    - Otherwise a ``completed`` one, then an ``active`` one, and then the
      survivor's own. A completed enrollment has nothing left to send, so a tie
      with it must not restart a sequence, nor let a chained merge through a
      completed middle contact lose a pause (#247 review).

    Whichever wins, the combined enrollment keeps what either one knew:

    - ``current_step`` is the higher of the two, because it holds the messages
      of both, and a resume must not send a step again.
    - A pause holds: if either was ``paused`` and the winner is ``active`` or
      ``pending``, the combined one is ``paused``.
    - ``replied_at`` is the winner's, or else the other's.
    - The other one's unsent messages (:data:`UNSENT_MESSAGE_STATUSES`) are
      ``discarded``, so the combined enrollment never holds two of a step
      waiting for the person. A ``scheduled`` one is left as it is: it may be
      in the sender's hands or out already, and the engine's reconcile
      searches Gmail for it first (#269). When the combined enrollment is
      over (anything but :data:`LIVE_AFTER_MERGE`), the winner's are
      discarded too: nothing is left to send them, and a stale one waiting
      must not read as a step to come (#247 review). A discarded ``drafted``
      message's Gmail draft is left in Gmail for the person (ADR 0003).

    The survivor's row is the one that stays: it takes the combined state and
    every message of both. Moving the loser's row across instead would collide
    with the survivor's on the unique constraint. The loser's row is left on the
    loser, ``removed`` with :data:`MERGED_EXIT_REASON` and no messages, as the
    record that it existed.
    """
    mine = {
        row.campaign_id: row
        for row in session.scalars(
            scoped(user, Enrollment).where(Enrollment.contact_id == survivor.id)
        )
    }
    theirs = session.scalars(
        scoped(user, Enrollment)
        .where(Enrollment.contact_id == loser.id)
        .order_by(Enrollment.campaign_id)
    ).all()
    set_aside: dict[int, int] = {}  # loser's enrollment id -> the survivor's that absorbs it
    outranked: list[int] = []  # the enrollments whose unsent messages are discarded
    for row in theirs:
        kept = mine.get(row.campaign_id)
        if kept is None:
            row.contact_id = survivor.id
            mine[row.campaign_id] = row
            continue
        losers_wins = _enrollment_rank(row, survivor_row=False) > _enrollment_rank(
            kept, survivor_row=True
        )
        winner, other = (row, kept) if losers_wins else (kept, row)
        state = {name: getattr(winner, name) for name in _ENROLLMENT_STATE}
        state["current_step"] = max(
            (step for step in (row.current_step, kept.current_step) if step is not None),
            default=None,
        )
        if (
            state["status"] in (EnrollmentStatus.ACTIVE, EnrollmentStatus.PENDING)
            and other.status is EnrollmentStatus.PAUSED
        ):
            state["status"] = EnrollmentStatus.PAUSED
        state["replied_at"] = winner.replied_at or other.replied_at
        outranked.append(other.id)
        if state["status"] not in LIVE_AFTER_MERGE:
            outranked.append(winner.id)
        for name, value in state.items():
            setattr(kept, name, value)
        row.status = EnrollmentStatus.REMOVED
        row.exit_reason = MERGED_EXIT_REASON
        row.next_action_at = None
        set_aside[row.id] = kept.id
        log.info(
            "contact %d's enrollment %d in campaign %d is combined into contact %d's %d",
            loser.id,
            row.id,
            row.campaign_id,
            survivor.id,
            kept.id,
        )
    session.flush()  # the survivor's enrollments hold before messages point at them
    if outranked:
        # Before the messages move: afterwards both sides' sit on the one enrollment.
        unsent = session.scalars(
            scoped(user, Message).where(
                Message.enrollment_id.in_(outranked),
                Message.status.in_(UNSENT_MESSAGE_STATUSES),
            )
        ).all()
        for message in unsent:
            message.status = MessageStatus.DISCARDED
        if unsent:
            log.info("discarded %d unsent messages of combined enrollments", len(unsent))
    messages = session.scalars(
        scoped(user, Message).where(
            (Message.contact_id == loser.id) | Message.enrollment_id.in_(sorted(set_aside))
        )
    ).all()
    for message in messages:
        if message.contact_id == loser.id:
            message.contact_id = survivor.id
        if message.enrollment_id in set_aside:
            message.enrollment_id = set_aside[message.enrollment_id]
            session.expire(message, ["enrollment"])  # the column moved under the relationship
    if messages:
        log.debug(
            "moved %d campaign messages from contact %d to %d", len(messages), loser.id, survivor.id
        )
    session.flush()


_TIE_RANK: Final[Mapping[EnrollmentStatus, int]] = {
    EnrollmentStatus.COMPLETED: 2,
    EnrollmentStatus.ACTIVE: 1,
}
"""At a tied step, completed beats active beats the rest (#247 review)."""


def _enrollment_rank(row: Enrollment, *, survivor_row: bool) -> tuple[int, int, int, bool]:
    """Of two enrollments in one campaign, the higher wins: see :func:`_merge_campaign_rows`."""
    stop = (
        len(STOP_PRECEDENCE) - STOP_PRECEDENCE.index(row.status)
        if row.status in STOP_PRECEDENCE
        else 0
    )
    return (stop, row.current_step or 0, _TIE_RANK.get(row.status, 0), survivor_row)


def _assignments_of(session: Session, user: User, contact_id: int) -> dict[int, ContactTag]:
    """The contact's tag assignments by tag id; one per tag, as the unique constraint has it."""
    rows = session.scalars(scoped(user, ContactTag).where(ContactTag.contact_id == contact_id))
    return {row.tag_id: row for row in rows}


def _suppressions_of(
    session: Session, user: User, contact_id: int
) -> dict[int, ContactTagSuppression]:
    """The contact's tag suppressions by tag id."""
    rows = session.scalars(
        scoped(user, ContactTagSuppression).where(ContactTagSuppression.contact_id == contact_id)
    )
    return {row.tag_id: row for row in rows}


def _keep_assignment(
    session: Session, survivor: Contact, mine: ContactTag | None, theirs: ContactTag | None
) -> ContactTag:
    """Leave the survivor one assignment for the tag: its own, or the loser's moved across."""
    if mine is None:
        assert theirs is not None, "called with no assignment on either contact"
        survivor.tag_assignments.append(theirs)  # source and rule_id come along
        return theirs
    _drop(session, theirs)
    return mine


def _keep_suppression(
    session: Session,
    survivor: Contact,
    mine: ContactTagSuppression | None,
    theirs: ContactTagSuppression | None,
) -> None:
    """Leave the survivor one suppression for the tag: its own, or the loser's moved across."""
    if mine is None:
        assert theirs is not None, "called with no suppression on either contact"
        theirs.contact_id = survivor.id  # no relationship to append to; the FK is the whole row
    else:
        _drop(session, theirs)


def _drop(session: Session, *rows: ContactTag | ContactTagSuppression | None) -> None:
    for row in rows:
        if row is not None:
            session.delete(row)


def _merge_scalars(survivor: Contact, loser: Contact) -> None:
    survivor_default_name = survivor.preferred_name in ("", survivor.first_name)
    loser_custom_name = bool(loser.preferred_name) and loser.preferred_name != loser.first_name
    # #184: a survivor still waiting for review holds a connections-page card's
    # text in its unrecorded fields. Merging a contact that is not waiting into it
    # is the person saying which contact this is, so those fields count as empty
    # and the loser's values win with the loser's sources, as they would have had
    # the person picked the loser as the survivor.
    card_survivor = survivor.needs_review_at is not None and loser.needs_review_at is None
    for name in PROVENANCE_ORDER:
        if name in ("li_urn", "li_public_id"):
            continue  # _merge_identity did these
        mine: str | date | None = getattr(survivor, name)
        theirs: str | date | None = getattr(loser, name)
        open_to_loser = mine in (None, "") or (card_survivor and name not in survivor.field_sources)
        if open_to_loser and theirs not in (None, ""):
            setattr(survivor, name, theirs)
            _take_source(survivor, loser, name)
    if survivor_default_name:
        # "" means "use first_name" on a stored row (the preferred_name validator).
        survivor.preferred_name = loser.preferred_name if loser_custom_name else ""
    if MET_RANK[loser.met] > MET_RANK[survivor.met]:
        # The value carries who decided it with it (spec 10.2): a decision a
        # triage batch made must not read as one the person made by hand just
        # because it moved to the survivor, and it stays up for review there.
        survivor.met = loser.met
        survivor.met_source = loser.met_source
        survivor.triaged_at = loser.triaged_at
    elif (
        loser.met is survivor.met
        and loser.met_source is MetSource.MANUAL
        and loser.triaged_at is not None
        and survivor.met_source is MetSource.AUTOMATIC
    ):
        # The same answer, but one of them is the person's own. Keeping the
        # batch's would throw away a confirmation and leave the survivor in the
        # review queue for a decision that has already been reviewed. Only a
        # triaged loser confirms anything (#331): ``manual`` is also the column
        # default, so an untriaged contact carries it without anyone deciding.
        survivor.met_source = MetSource.MANUAL
    if loser.do_not_contact:
        if not survivor.do_not_contact:
            survivor.do_not_contact = True
            survivor.do_not_contact_reason = loser.do_not_contact_reason
        elif not survivor.do_not_contact_reason:
            survivor.do_not_contact_reason = loser.do_not_contact_reason
    if loser.notes:
        survivor.notes = (
            f"{survivor.notes}{NOTES_SEPARATOR}{loser.notes}" if survivor.notes else loser.notes
        )
    if loser.last_contacted_at is not None and (
        survivor.last_contacted_at is None or loser.last_contacted_at > survivor.last_contacted_at
    ):
        survivor.last_contacted_at = loser.last_contacted_at
    if loser.archived_at is None:
        survivor.archived_at = None
    # #184: a contact read only off a card waits for a person to confirm it. Merging
    # it with a contact that is not waiting is that confirmation (the person said
    # which contact it is); merging two that are both waiting confirms neither.
    if loser.needs_review_at is None:
        survivor.needs_review_at = None


def _merge_synced_values(survivor: Contact, loser: Contact) -> None:
    """The survivor learns what was synced for the loser, where it knows nothing itself."""
    for name, synced in loser.synced_values.items():
        if name not in survivor.synced_values:
            survivor.synced_values[name] = synced.copy()
