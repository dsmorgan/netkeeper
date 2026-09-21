"""The contacts service behind the contacts API (spec 10.1, 14.1; item P1-05).

The paged query the Contacts table runs, the quick search, one contact in full,
a person's edits to it and to its emails, phones, and links, archive and merge,
and the bulk actions that apply to a filter. Every list statement comes from the
filter language (:mod:`netkeeper.crm.filters`); the quick search is a filter
tree too (:func:`search_tree`), so there is one query builder.

Edits follow spec 10.5. A LinkedIn field a person edits goes through
:func:`netkeeper.crm.provenance.set_manual_field`, so the override sticks until
:func:`revert_field` puts the synced value back (CP1, #28). The fields a person
owns (``preferred_name``, ``notes``, ``met``) go the same way; ``do_not_contact``
and its reason carry no provenance and are written directly. A slug edit is the
one edit with a check in front of it: another contact holding the slug is a
:class:`Conflict`, and ``li_url`` follows the slug, as it does for an import.

Merged-away contacts (spec 8.2): a read of a merged-away id resolves to the
survivor and says which id was asked for (:func:`get_contact`); a write to one
is refused with :class:`Merged`, which names the survivor, because an edit to a
row nothing will show again is a lost edit. This is the convention every
per-contact route should follow; P1-10's routes adopt it in a follow-up.

Transactions belong to the caller. Nothing here commits. Every writer reads
before it writes, so it needs a writer session (``session_scope(factory,
write=True)``, or a non-GET request's session).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, Literal, cast

from sqlalchemy import ColumnElement, CursorResult, Select, Update, func, select
from sqlalchemy.orm import Session, selectinload

from netkeeper.crm.filters import (
    FilterTree,
    SortKey,
    apply_sort,
    compile_count,
    compile_filter,
    compile_update,
    describe,
    paginate,
    parse_filter,
)
from netkeeper.crm.identity import (
    IncomingEmail,
    IncomingLink,
    IncomingPhone,
    merge,
    phone_key,
    resolve_survivor,
)
from netkeeper.crm.provenance import (
    PERSON_OWNED_FIELDS,
    PROVENANCE_FIELDS,
    revert_to_synced,
    set_manual_field,
    set_met,
)
from netkeeper.db import is_writer
from netkeeper.models import (
    Contact,
    ContactAlias,
    ContactEmail,
    ContactLink,
    ContactMet,
    ContactPhone,
    ContactSnapshot,
    ContactSource,
    ContactTag,
    EmailKind,
    EmailStatus,
    LinkKind,
    MetSource,
    PhoneKind,
    TagSource,
    User,
    linkedin_profile_url,
    normalize_public_id,
)
from netkeeper.models.base import utcnow
from netkeeper.scoping import get_scoped, scoped, scoped_count, scoped_update

log = logging.getLogger(__name__)

# Columns a person may change through update_contact(). li_urn is LinkedIn's
# identifier and only the sync, an import, or a merge writes it; li_url follows
# li_public_id, so it is not edited on its own.
EDITABLE_PROVENANCE_FIELDS: Final[frozenset[str]] = PROVENANCE_FIELDS - {"li_urn", "li_url"}
DIRECT_FIELDS: Final[frozenset[str]] = frozenset({"do_not_contact", "do_not_contact_reason"})
EDITABLE_FIELDS: Final[frozenset[str]] = (
    EDITABLE_PROVENANCE_FIELDS | PERSON_OWNED_FIELDS | DIRECT_FIELDS
)
# Names are '' when unknown, never NULL (CP1 decision 3).
_EMPTY_IS_BLANK: Final[frozenset[str]] = frozenset({"first_name", "last_name", "preferred_name"})

# What the quick search looks in, with ``email_contains`` on top.
SEARCH_FIELDS: Final[tuple[str, ...]] = (
    "first_name",
    "last_name",
    "preferred_name",
    "current_company",
    "current_title",
    "headline",
)
SEARCH_SORT: Final[tuple[SortKey, ...]] = (SortKey(field="last_name"), SortKey(field="first_name"))

SNAPSHOTS_IN_DETAIL: Final[int] = 5

BulkAction = Literal["set_met", "archive", "unarchive", "set_do_not_contact"]


# --- errors -----------------------------------------------------------------


class NotFound(LookupError):
    """The contact, or the child row, is not one of the user's. Never says whose it is."""


class Merged(Exception):
    """A write reached a merged-away contact; ``survivor_id`` is where it went."""

    def __init__(self, contact_id: int, survivor_id: int) -> None:
        self.contact_id = contact_id
        self.survivor_id = survivor_id
        super().__init__(f"contact {contact_id} is merged into {survivor_id}")


class Conflict(ValueError):
    """The write would collide with another row, or there is nothing to revert to."""


class CountMismatch(Exception):
    """A bulk selection counts ``actual`` rows, not the ``expected`` the person confirmed."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"selection counts {actual} contacts, not the {expected} confirmed")


# --- reading ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Page:
    """One page of contacts, how many match in all, and a reading of the selection."""

    contacts: list[Contact]
    total: int
    describe: str


def query(
    session: Session,
    user: User,
    tree: FilterTree,
    sort: Sequence[SortKey] = (),
    *,
    limit: int,
    offset: int = 0,
    now: datetime | None = None,
) -> Page:
    """The contacts ``tree`` selects for ``user``, sorted by ``sort``, one page at a time.

    Emails and phones come loaded, so a row's primary address and number cost
    no query each. :class:`~netkeeper.crm.filters.FilterError` for a tree that
    does not compile (a placeholder predicate); ``ValueError`` for a bad page.
    """
    total = session.scalar(compile_count(user, tree, session=session, now=now)) or 0
    statement = paginate(
        apply_sort(compile_filter(user, tree, session=session, now=now), sort),
        limit=limit,
        offset=offset,
    ).options(selectinload(Contact.emails), selectinload(Contact.phones))
    contacts = list(session.scalars(statement))
    return Page(contacts, total, describe(tree))


def search_tree(q: str) -> FilterTree:
    """The filter behind the quick search: ``q`` as a substring of any name, company,
    title, headline, or email, case-insensitive. Blank ``q`` selects every contact."""
    text = q.strip()
    if not text:
        return FilterTree()
    children: list[dict[str, Any]] = [
        {"op": "contains", "field": field, "value": text} for field in SEARCH_FIELDS
    ]
    children.append({"op": "email_contains", "value": text})
    return parse_filter({"where": {"op": "or", "children": children}})


def search(session: Session, user: User, q: str, *, limit: int, offset: int = 0) -> Page:
    """The quick search: :func:`search_tree` sorted by last name, then first name."""
    text = q.strip()
    page = query(session, user, search_tree(text), SEARCH_SORT, limit=limit, offset=offset)
    reading = f'matching "{text}"' if text else "all contacts"
    return Page(page.contacts, page.total, reading)


@dataclass(frozen=True, slots=True)
class ContactStats:
    """Triage progress and counts over ``user``'s *live* contacts, for ``netkeeper contacts stats``.

    ``total``, ``met``, ``not_met``, ``skipped``, and ``untriaged`` share the
    live-rows baseline :func:`netkeeper.crm.triage.progress` counts from
    (``archived_at IS NULL AND merged_into_id IS NULL``): the four
    :class:`~netkeeper.models.ContactMet` states always add up to ``total``,
    and ``total`` is the same number the triage queue would call "how many
    contacts". A merged-away contact keeps whatever ``met`` it had at the
    merge, so counting it too would double-count one person under two rows;
    excluding it here is what keeps a merge from inflating ``met``.

    ``archived`` and ``merged_away`` are *not* part of ``total`` — they count
    the rows outside the live set, one line each for the two ways a contact
    leaves it. ``with_email``, ``with_phone``, ``tagged``, and ``tagged_by_rule``
    are also over the live set, so every number in the table is a count out of
    the same ``total``.

    ``tagged`` is "this contact carries a tag, from any source"; ``tagged_by_rule``
    is "an auto-tag rule put one there" (:attr:`~netkeeper.models.TagSource.RULE`
    only, not ``manual`` or ``llm``) — a dashboard that wants to say how many
    contacts netkeeper tagged on its own needs the second number, and neither is
    inferable from the other: a contact a rule tagged and a person then tagged by
    hand too still counts in both.
    """

    total: int
    met: int
    not_met: int
    skipped: int
    untriaged: int
    archived: int
    merged_away: int
    with_email: int
    with_phone: int
    tagged: int
    tagged_by_rule: int


def _has_child(user: User, model: type[ContactEmail] | type[ContactPhone]) -> ColumnElement[bool]:
    """A correlated ``EXISTS`` for one of ``user``'s child rows on the outer ``Contact``.

    Module level, not a closure inside :func:`contact_stats`, so a test can compile
    it on its own and check the SQL text names ``user`` the same way
    ``tests/test_filters.py``'s subquery-scoping test does for the filter
    language: the runtime scope guard does not reach a
    correlated subquery (it only inspects the outer statement), so this
    ``model.user_id == user.id`` term is what actually keeps ``with_email`` and
    ``with_phone`` to one user, not a pattern the guard would catch if it were
    ever dropped.
    """
    return (
        select(model.id)
        .where(model.contact_id == Contact.id, model.user_id == user.id)
        .correlate(Contact)
        .exists()
    )


def _has_tag(user: User, *, source: TagSource | None = None) -> ColumnElement[bool]:
    """The same correlated ``EXISTS``, over ``user``'s own :class:`ContactTag` rows.

    ``source`` narrows it to one :class:`~netkeeper.models.TagSource` (``rule``,
    for ``tagged_by_rule``); omitted, it is "any tag from anywhere", ``tagged``'s
    question.
    """
    clauses: list[ColumnElement[bool]] = [
        ContactTag.contact_id == Contact.id,
        ContactTag.user_id == user.id,
    ]
    if source is not None:
        clauses.append(ContactTag.source == source)
    return select(ContactTag.id).where(*clauses).correlate(Contact).exists()


def contact_stats(session: Session, user: User) -> ContactStats:
    """Counts for ``user``'s contacts, for ``netkeeper contacts stats``.

    The four :class:`~netkeeper.models.ContactMet` counts and ``total`` match
    :func:`netkeeper.crm.triage.progress` exactly (same live-rows predicate),
    so the CLI and the triage queue can never quietly disagree on what "how
    many contacts" means.
    """
    live = (Contact.archived_at.is_(None), Contact.merged_into_id.is_(None))

    def count(*clauses: ColumnElement[bool]) -> int:
        statement = scoped_count(user, Contact)
        if clauses:
            statement = statement.where(*clauses)
        return session.scalar(statement) or 0

    return ContactStats(
        total=count(*live),
        met=count(*live, Contact.met == ContactMet.MET),
        not_met=count(*live, Contact.met == ContactMet.NOT_MET),
        skipped=count(*live, Contact.met == ContactMet.SKIP),
        untriaged=count(*live, Contact.met == ContactMet.UNKNOWN),
        archived=count(Contact.archived_at.is_not(None)),
        merged_away=count(Contact.merged_into_id.is_not(None)),
        with_email=count(*live, _has_child(user, ContactEmail)),
        with_phone=count(*live, _has_child(user, ContactPhone)),
        tagged=count(*live, _has_tag(user)),
        tagged_by_rule=count(*live, _has_tag(user, source=TagSource.RULE)),
    )


def get_contact(session: Session, user: User, contact_id: int) -> tuple[Contact, int | None]:
    """One of ``user``'s contacts, following a merge to its survivor.

    Returns the contact and, when ``contact_id`` was merged away, that id, so
    the caller can say the answer stands for it. :class:`NotFound` when the id
    is not ``user``'s.
    """
    try:
        contact = resolve_survivor(session, user, contact_id)
    except ValueError:
        raise NotFound("no such contact") from None
    return contact, (contact_id if contact.id != contact_id else None)


def latest_snapshots(
    session: Session, user: User, contact: Contact, *, limit: int = SNAPSHOTS_IN_DETAIL
) -> list[ContactSnapshot]:
    """The newest ``limit`` snapshots of ``contact``, newest first."""
    statement = (
        scoped(user, ContactSnapshot)
        .where(ContactSnapshot.contact_id == contact.id)
        .order_by(ContactSnapshot.observed_at.desc(), ContactSnapshot.id.desc())
        .limit(limit)
    )
    return list(session.scalars(statement))


def live_contact(session: Session, user: User, contact_id: int) -> Contact:
    """One of ``user``'s contacts, for writing: :class:`NotFound`, or :class:`Merged`
    when the contact was merged away (the survivor is named)."""
    contact = get_scoped(session, user, Contact, contact_id)
    if contact is None:
        raise NotFound("no such contact")
    if contact.merged_into_id is not None:
        raise Merged(contact.id, resolve_survivor(session, user, contact.id).id)
    return contact


# --- editing one contact ----------------------------------------------------


def update_contact(
    session: Session, user: User, contact_id: int, changes: Mapping[str, Any]
) -> Contact:
    """Apply ``changes`` (column to value) to one of ``user``'s contacts as their own edits.

    A LinkedIn field takes the value through ``set_manual_field`` and sticks;
    ``None`` clears it (``''`` for a name) and sticks too. ``li_public_id`` is
    normalized, refused with :class:`Conflict` when another contact holds it,
    and carries ``li_url`` with it; an alias another contact keeps for the new
    slug is dropped, as an import drops it. ``met`` stamps ``triaged_at``.
    ``do_not_contact`` and its reason are written as given. ``ValueError`` for a
    column that is not editable; :class:`NotFound`; :class:`Merged`;
    ``RuntimeError`` for a session that is not a writer.
    """
    _require_writer(session)
    unknown = sorted(set(changes) - EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"not editable: {', '.join(unknown)}")
    contact = live_contact(session, user, contact_id)
    for field, value in changes.items():
        if field == "li_public_id":
            _set_public_id(session, user, contact, value)
        elif field == "met":
            # The person's own answer, which takes a contact a batch decided
            # out of the review queue (spec 10.2).
            set_met(contact, ContactMet(value), source=MetSource.MANUAL)
            contact.triaged_at = utcnow()
        elif field == "do_not_contact":
            contact.do_not_contact = bool(value)
        elif field == "do_not_contact_reason":
            contact.do_not_contact_reason = _text(value)
        elif field == "connected_on":
            set_manual_field(contact, field, _date(value))
        elif field == "notes":
            set_manual_field(contact, field, value if value is None else str(value))
        else:
            cleaned = _text(value)
            if cleaned is None and field in _EMPTY_IS_BLANK:
                cleaned = ""
            set_manual_field(contact, field, cleaned)
    session.flush()
    log.debug("updated contact %d for user %d: %s", contact.id, user.id, ", ".join(changes))
    return contact


def _set_public_id(session: Session, user: User, contact: Contact, value: object) -> None:
    slug = normalize_public_id(value if isinstance(value, str) else None)
    if slug == contact.li_public_id:
        return
    if slug is not None:
        holder = session.scalars(
            scoped(user, Contact)
            .where(Contact.li_public_id == slug, Contact.id != contact.id)
            .limit(1)
        ).first()
        if holder is not None:
            raise Conflict(f"li_public_id {slug!r} already belongs to contact {holder.id}")
        stale = scoped(user, ContactAlias).where(ContactAlias.li_public_id == slug)
        for alias in session.scalars(stale):
            alias.contact.aliases.remove(alias)
    set_manual_field(contact, "li_public_id", slug)
    set_manual_field(contact, "li_url", linkedin_profile_url(slug) if slug else None)


def revert_field(session: Session, user: User, contact_id: int, field: str) -> Contact:
    """Put ``field`` back to what the automated sources last reported (spec 10.5).

    :class:`Conflict` when nothing was ever synced for it, or when the synced
    URN or slug now belongs to another contact. Reverting ``li_public_id``
    reverts ``li_url`` with it when that has a synced value, since the two go
    together. ``ValueError`` for a field without provenance; :class:`NotFound`;
    :class:`Merged`; ``RuntimeError`` for a session that is not a writer.
    """
    _require_writer(session)
    if field not in PROVENANCE_FIELDS:
        raise ValueError(f"{field!r} carries no provenance")
    contact = live_contact(session, user, contact_id)
    synced = (contact.synced_values or {}).get(field)
    if synced is None:
        raise Conflict(f"{field} has no synced value to revert to")
    if field in ("li_urn", "li_public_id") and synced["value"] is not None:
        column = Contact.li_urn if field == "li_urn" else Contact.li_public_id
        holder = session.scalars(
            scoped(user, Contact)
            .where(column == synced["value"], Contact.id != contact.id)
            .limit(1)
        ).first()
        if holder is not None:
            raise Conflict(
                f"the synced {field} {synced['value']!r} now belongs to contact {holder.id}"
            )
    revert_to_synced(contact, field)
    if field == "li_public_id" and "li_url" in (contact.synced_values or {}):
        revert_to_synced(contact, "li_url")
    session.flush()
    log.debug("reverted %s on contact %d for user %d", field, contact.id, user.id)
    return contact


def archive_contact(session: Session, user: User, contact_id: int) -> Contact:
    """Archive; a contact already archived keeps its ``archived_at``."""
    _require_writer(session)
    contact = live_contact(session, user, contact_id)
    if contact.archived_at is None:
        contact.archived_at = utcnow()
        session.flush()
    return contact


def unarchive_contact(session: Session, user: User, contact_id: int) -> Contact:
    _require_writer(session)
    contact = live_contact(session, user, contact_id)
    if contact.archived_at is not None:
        contact.archived_at = None
        session.flush()
    return contact


def merge_contacts(session: Session, user: User, survivor_id: int, loser_id: int) -> Contact:
    """Fold ``loser_id`` into ``survivor_id`` (:func:`netkeeper.crm.identity.merge`).

    The survivor must be live. :func:`merge` would happily resolve a merged-away
    survivor id to its own survivor and fold the loser into that, but the caller
    asked to merge into a row nothing will show again, so they get the same
    :class:`Merged` every other write to one gets and can retry against the id
    it names. A loser already merged away is the other case and stays a
    :class:`Conflict`: it names where the loser went, which is a different
    sentence. :class:`NotFound` when either id is not ``user``'s;
    :class:`Conflict` for the same id twice.
    """
    _require_writer(session)
    live_contact(session, user, survivor_id)
    if get_scoped(session, user, Contact, loser_id) is None:
        raise NotFound("no such contact")
    try:
        return merge(session, user, survivor_id, loser_id)
    except ValueError as exc:
        raise Conflict(str(exc)) from exc


# --- bulk actions -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Selection:
    """What a bulk action applies to: a filter, or explicit ids. Exactly one is set."""

    tree: FilterTree | None = None
    ids: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if (self.tree is None) == (self.ids is None):
            raise ValueError("a selection is a filter or ids, not both or neither")


def describe_selection(selection: Selection) -> str:
    """The selection in words, for the confirmation dialog's sentence."""
    if selection.tree is not None:
        return describe(selection.tree)
    assert selection.ids is not None
    count = len(set(selection.ids))
    return f"{count} contact{'' if count == 1 else 's'} chosen by hand"


def count_selection(
    session: Session, user: User, selection: Selection, *, now: datetime | None = None
) -> int:
    """How many of ``user``'s contacts ``selection`` names right now."""
    if selection.tree is not None:
        return session.scalar(compile_count(user, selection.tree, session=session, now=now)) or 0
    assert selection.ids is not None
    statement: Select[tuple[int]] = scoped_count(user, Contact).where(
        Contact.id.in_(selection.ids), Contact.merged_into_id.is_(None)
    )
    return session.scalar(statement) or 0


def bulk_update(
    session: Session,
    user: User,
    selection: Selection,
    action: BulkAction,
    *,
    value: ContactMet | bool | None = None,
    reason: str | None = None,
    expected_count: int,
    now: datetime | None = None,
) -> int:
    """Apply ``action`` to ``selection`` in one statement; returns the rows written.

    The selection is counted first and :class:`CountMismatch` refuses the
    action when the count is not ``expected_count``, the number the person
    confirmed (the API takes that number from a signed token,
    :mod:`netkeeper.crm.confirmation`). ``set_met`` takes a :class:`ContactMet` ``value`` and stamps
    ``triaged_at``; ``set_do_not_contact`` a boolean ``value`` with ``reason``
    kept only when true; ``archive`` and ``unarchive`` take none. Runs as a bulk
    ``UPDATE`` without session synchronization, then expires every loaded
    object, so the session reads the new values back. ``ValueError`` for a
    value that does not fit the action; ``RuntimeError`` for a session that is
    not a writer.
    """
    _require_writer(session)
    # One instant for the count and for the write. A relative window
    # ("last_contacted within 30 days") compiles its own clock every time
    # compile_where() runs, so two resolutions could count one set of rows and
    # update another -- the drift the confirmation token exists to prevent.
    moment = utcnow() if now is None else now
    values = _bulk_values(action, value, reason, moment)
    actual = count_selection(session, user, selection, now=moment)
    if actual != expected_count:
        raise CountMismatch(expected_count, actual)
    statement = _selection_update(session, user, selection, moment).values(**values)
    # Session.execute() is typed as the plain Result; DML gets a CursorResult.
    result = cast(CursorResult[Any], session.execute(statement))
    session.expire_all()
    affected: int = result.rowcount
    log.info("bulk %s on %d contacts of user %d", action, affected, user.id)
    return affected


def _bulk_values(
    action: BulkAction, value: ContactMet | bool | None, reason: str | None, now: datetime
) -> dict[str, Any]:
    match action:
        case "set_met":
            if not isinstance(value, ContactMet):
                raise ValueError("set_met needs a ContactMet value")
            # met_source, so a bulk edit by hand is a decision by hand and
            # leaves the review queue (spec 10.2).
            return {"met": value, "met_source": MetSource.MANUAL, "triaged_at": now}
        case "archive":
            # Keep the stamp a row already carries, as archive_contact() does:
            # archived_at records when the contact left the table, and a row
            # already archived did not leave it again. An ids selection, or a
            # filter with include_archived, reaches such rows. COALESCE rather
            # than narrowing the WHERE, so the rows written still match the
            # count the person confirmed.
            return {"archived_at": func.coalesce(Contact.archived_at, now)}
        case "unarchive":
            return {"archived_at": None}
        case "set_do_not_contact":
            if not isinstance(value, bool):
                raise ValueError("set_do_not_contact needs a boolean value")
            return {
                "do_not_contact": value,
                "do_not_contact_reason": _text(reason) if value else None,
            }


def _selection_update(
    session: Session, user: User, selection: Selection, now: datetime | None
) -> Update:
    if selection.tree is not None:
        return compile_update(user, selection.tree, session=session, now=now)
    assert selection.ids is not None
    # "auto" synchronization would issue its own unscoped SELECT to find the rows
    # (see netkeeper.crm.filters.compile_update); the caller expires the session.
    return (
        scoped_update(user, Contact)
        .where(Contact.id.in_(selection.ids), Contact.merged_into_id.is_(None))
        .execution_options(synchronize_session=False)
    )


# --- emails, phones, links --------------------------------------------------


def add_email(
    session: Session,
    user: User,
    contact_id: int,
    email: str,
    *,
    kind: EmailKind = EmailKind.OTHER,
    is_primary: bool = False,
    status: EmailStatus = EmailStatus.OK,
) -> ContactEmail:
    """Add an address, lowercased, as the person's own row.

    The first address on a contact is primary whether or not asked; a later one
    asked to be primary demotes the others. :class:`Conflict` for an address the
    contact already has; ``ValueError`` for an empty one.
    """
    _require_writer(session)
    contact = live_contact(session, user, contact_id)
    incoming = IncomingEmail(email, kind, is_primary)
    if any(row.email == incoming.email for row in contact.emails):
        raise Conflict(f"the contact already has {incoming.email}")
    row = ContactEmail(
        user_id=user.id,
        email=incoming.email,
        kind=incoming.kind,
        status=EmailStatus(status),
        source=ContactSource.MANUAL,
        observed_at=utcnow(),
    )
    contact.emails.append(row)
    if incoming.is_primary or not any(other.is_primary for other in contact.emails):
        _make_primary(contact.emails, row)
    session.flush()
    return row


def update_email(
    session: Session, user: User, contact_id: int, email_id: int, changes: Mapping[str, Any]
) -> ContactEmail:
    """Change an address's fields; the row becomes the person's own observation."""
    _require_writer(session)
    contact = live_contact(session, user, contact_id)
    row = _child(contact.emails, email_id, "email")
    if "email" in changes:
        address = IncomingEmail(changes["email"]).email
        if any(other.email == address for other in contact.emails if other is not row):
            raise Conflict(f"the contact already has {address}")
        row.email = address
    if "kind" in changes:
        row.kind = EmailKind(changes["kind"])
    if "status" in changes:
        row.status = EmailStatus(changes["status"])
    _apply_primary(contact.emails, row, changes)
    _touch(row)
    session.flush()
    return row


def delete_email(session: Session, user: User, contact_id: int, email_id: int) -> None:
    """Remove an address; when it was primary, the first remaining one takes over."""
    _require_writer(session)
    contact = live_contact(session, user, contact_id)
    row = _child(contact.emails, email_id, "email")
    _remove(contact.emails, row)
    session.flush()


def add_phone(
    session: Session,
    user: User,
    contact_id: int,
    raw: str,
    *,
    number_e164: str | None = None,
    kind: PhoneKind = PhoneKind.OTHER,
    is_primary: bool = False,
) -> ContactPhone:
    """Add a number as the person's own row; the E.164 form is derived when it can be.

    :class:`Conflict` for a number the contact already has (same digits);
    ``ValueError`` for one with no digits.
    """
    _require_writer(session)
    contact = live_contact(session, user, contact_id)
    incoming = IncomingPhone(raw, number_e164, kind, is_primary)
    if any(phone_key(row.number_e164 or row.raw) == incoming.key for row in contact.phones):
        raise Conflict(f"the contact already has {incoming.raw}")
    row = ContactPhone(
        user_id=user.id,
        raw=incoming.raw,
        number_e164=incoming.number_e164,
        kind=incoming.kind,
        source=ContactSource.MANUAL,
        observed_at=utcnow(),
    )
    contact.phones.append(row)
    if incoming.is_primary or not any(other.is_primary for other in contact.phones):
        _make_primary(contact.phones, row)
    session.flush()
    return row


def update_phone(
    session: Session, user: User, contact_id: int, phone_id: int, changes: Mapping[str, Any]
) -> ContactPhone:
    _require_writer(session)
    contact = live_contact(session, user, contact_id)
    row = _child(contact.phones, phone_id, "phone")
    if "raw" in changes or "number_e164" in changes:
        incoming = IncomingPhone(
            changes.get("raw", row.raw), changes.get("number_e164", row.number_e164)
        )
        if any(
            phone_key(other.number_e164 or other.raw) == incoming.key
            for other in contact.phones
            if other is not row
        ):
            raise Conflict(f"the contact already has {incoming.raw}")
        row.raw = incoming.raw
        row.number_e164 = incoming.number_e164
    if "kind" in changes:
        row.kind = PhoneKind(changes["kind"])
    _apply_primary(contact.phones, row, changes)
    _touch(row)
    session.flush()
    return row


def delete_phone(session: Session, user: User, contact_id: int, phone_id: int) -> None:
    _require_writer(session)
    contact = live_contact(session, user, contact_id)
    row = _child(contact.phones, phone_id, "phone")
    _remove(contact.phones, row)
    session.flush()


def add_link(
    session: Session, user: User, contact_id: int, url: str, *, kind: LinkKind = LinkKind.OTHER
) -> ContactLink:
    """Add a URL as the person's own row. :class:`Conflict` for one the contact has."""
    _require_writer(session)
    contact = live_contact(session, user, contact_id)
    incoming = IncomingLink(url, kind)
    if any(row.url == incoming.url for row in contact.links):
        raise Conflict(f"the contact already has {incoming.url}")
    row = ContactLink(
        user_id=user.id,
        url=incoming.url,
        kind=incoming.kind,
        source=ContactSource.MANUAL,
        observed_at=utcnow(),
    )
    contact.links.append(row)
    session.flush()
    return row


def update_link(
    session: Session, user: User, contact_id: int, link_id: int, changes: Mapping[str, Any]
) -> ContactLink:
    _require_writer(session)
    contact = live_contact(session, user, contact_id)
    row = _child(contact.links, link_id, "link")
    if "url" in changes:
        url = IncomingLink(changes["url"]).url
        if any(other.url == url for other in contact.links if other is not row):
            raise Conflict(f"the contact already has {url}")
        row.url = url
    if "kind" in changes:
        row.kind = LinkKind(changes["kind"])
    _touch(row)
    session.flush()
    return row


def delete_link(session: Session, user: User, contact_id: int, link_id: int) -> None:
    _require_writer(session)
    contact = live_contact(session, user, contact_id)
    row = _child(contact.links, link_id, "link")
    contact.links.remove(row)
    session.flush()


# --- helpers ----------------------------------------------------------------


def _require_writer(session: Session) -> None:
    """Fail at once rather than with "database is locked" on the first write (CLAUDE.md)."""
    if not is_writer(session):
        raise RuntimeError(
            "contact writes need a writer session; use session_scope(factory, write=True)"
        )


def _text(value: object) -> str | None:
    """A text field as stored: trimmed, and empty is ``None``."""
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _date(value: object) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _child[T: ContactEmail | ContactPhone | ContactLink](
    rows: Sequence[T], row_id: int, what: str
) -> T:
    for row in rows:
        if row.id == row_id:
            return row
    raise NotFound(f"no such {what}")


def _make_primary(rows: Sequence[ContactEmail] | Sequence[ContactPhone], chosen: object) -> None:
    for row in rows:
        row.is_primary = row is chosen


def _apply_primary(
    rows: Sequence[ContactEmail] | Sequence[ContactPhone],
    row: ContactEmail | ContactPhone,
    changes: Mapping[str, Any],
) -> None:
    if "is_primary" not in changes:
        return
    if changes["is_primary"]:
        _make_primary(rows, row)
    else:
        row.is_primary = False


def _remove(
    rows: list[ContactEmail] | list[ContactPhone], row: ContactEmail | ContactPhone
) -> None:
    """Drop ``row``; when it was primary, the first remaining row (lowest id) takes over."""
    was_primary = row.is_primary
    rows.remove(row)  # type: ignore[arg-type]  # one homogeneous list, typed as a union of two
    if was_primary and rows:
        _make_primary(rows, rows[0])


def _touch(row: ContactEmail | ContactPhone | ContactLink) -> None:
    """An edit is the person's own observation of the row, now."""
    row.source = ContactSource.MANUAL
    row.observed_at = utcnow()
