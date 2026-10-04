"""Possible duplicates of one contact, for a hint next to a review band (#363).

Read-only and conservative. Nothing here merges, marks, or writes anything: it
names contacts a person may want to merge with this one, and the person decides
(``POST /contacts/{id}/merge``). A hint that is wrong costs a glance; a hint
that is missing costs nothing a merge from the contact page cannot fix, so every
rule leans toward saying less.

Another contact is a possible duplicate only when it is one of the same user's
live contacts (not archived, not merged away) and the names agree: the same last
name and the same given name, where the given name is the first name or a
customized preferred name on either side, compared trimmed and case-insensitive
the way identity resolution compares names
(:func:`netkeeper.crm.identity._resolve_name`). Both sides need a first and a
last name, so a one-word card name never matches half the address book.

On top of the name, ``matched_by`` says what else agrees:

- ``email``: an address on both, exact (addresses are stored lowercased). A role
  address (:data:`ROLE_LOCAL_PARTS`, such as ``info@`` or ``sales@``) is shared
  by whoever answers it, so it never counts.
- ``phone``: a number on both, by its E.164 form, or by the raw text when
  neither side parsed it.

An address or a number alone, with names that disagree, is no match at all: a
shared office line or a reused address says two people share it, not that they
are one person.

Differing LinkedIn ids count against a match. Two contacts with different URNs
are never a match: a URN is a stable identifier for one LinkedIn profile, so they
are two people, the same assumption identity resolution makes (spec 8.2). Two
with different slugs may be one person whose slug changed before any sync (#186
item 5), or two people who share a name; they are still named, with
``linkedin_ids_differ`` set, and rank below every other match.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import ColumnElement, Select, func, or_
from sqlalchemy.orm import Session

from netkeeper.crm.contacts import NotFound
from netkeeper.crm.identity import resolve_survivor
from netkeeper.models import Contact, ContactEmail, ContactPhone, User
from netkeeper.scoping import scoped

MatchedBy = Literal["name", "email", "phone"]

MATCH_ORDER: Final[tuple[MatchedBy, ...]] = ("email", "phone", "name")
"""The strongest reason first: how ``matched_by`` is ordered, and how matches rank."""

ROLE_LOCAL_PARTS: Final[frozenset[str]] = frozenset(
    {
        "info",
        "hello",
        "sales",
        "office",
        "contact",
        "admin",
        "support",
        "team",
        "hr",
        "jobs",
        "careers",
        "noreply",
        "no-reply",
    }
)
"""Local parts of addresses that belong to a role, not a person: never a match."""

DUPLICATE_LIMIT: Final = 5
"""The most possible duplicates one hint names."""


@dataclass(frozen=True, slots=True)
class PossibleDuplicate:
    """Another contact that may be the same person, and every rule that says so."""

    contact: Contact
    matched_by: tuple[MatchedBy, ...]
    linkedin_ids_differ: bool = False
    """Both carry a LinkedIn slug and the slugs differ: evidence against, so it ranks last."""


def possible_duplicates(
    session: Session, user: User, contact_id: int, *, limit: int = DUPLICATE_LIMIT
) -> tuple[Contact, list[PossibleDuplicate]]:
    """The contact ``contact_id`` stands for, and its possible duplicates, strongest first.

    A merged-away id stands for its survivor, as every read does. Matches rank
    by their strongest rule (:data:`MATCH_ORDER`), then by how many rules hold,
    then by id. :class:`~netkeeper.crm.contacts.NotFound` when the id is not
    ``user``'s. Reads only; any session will do.
    """
    try:
        contact = resolve_survivor(session, user, contact_id)
    except ValueError:
        raise NotFound("no such contact") from None
    reasons: dict[int, set[MatchedBy]] = {}

    def note(ids: Iterable[int], reason: MatchedBy) -> None:
        for other in ids:
            reasons.setdefault(other, set()).add(reason)

    note(_by_name(session, user, contact), "name")
    if not reasons:
        return contact, []  # without a name match nothing else counts
    note(_by_email(session, user, contact), "email")
    note(_by_phone(session, user, contact), "phone")
    if not reasons:
        return contact, []
    others = {
        row.id: row
        for row in session.scalars(
            _live_others(user, contact).where(Contact.id.in_(sorted(reasons)))
        )
    }
    found: list[PossibleDuplicate] = []
    for other_id, matched in reasons.items():
        other = others.get(other_id)
        # The names must agree; an address or a number alone is no match.
        if other is None or "name" not in matched or _different_urns(contact, other):
            continue
        found.append(
            PossibleDuplicate(
                other,
                tuple(r for r in MATCH_ORDER if r in matched),
                linkedin_ids_differ=_slugs_differ(contact, other),
            )
        )
    found.sort(
        key=lambda d: (
            d.linkedin_ids_differ,
            MATCH_ORDER.index(d.matched_by[0]),
            -len(d.matched_by),
            d.contact.id,
        )
    )
    return contact, found[:limit]


def _live_others(user: User, contact: Contact) -> Select[tuple[Contact]]:
    return scoped(user, Contact).where(
        Contact.id != contact.id,
        Contact.merged_into_id.is_(None),
        Contact.archived_at.is_(None),
    )


def _folded(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned.lower() if cleaned else None


def _given_names(contact: Contact) -> set[str]:
    """The first name, and a preferred name that differs from it ("" means "use first")."""
    return {name for name in (_folded(contact.first_name), _folded(contact.preferred_name)) if name}


def _by_name(session: Session, user: User, contact: Contact) -> list[int]:
    last = _folded(contact.last_name)
    given = _given_names(contact)
    if last is None or _folded(contact.first_name) is None:
        return []
    first_column: ColumnElement[str] = func.lower(func.trim(Contact.first_name))
    preferred_column: ColumnElement[str] = func.lower(func.trim(Contact.preferred_name))
    statement = (
        _live_others(user, contact)
        .with_only_columns(Contact.id)
        .where(
            func.lower(func.trim(Contact.last_name)) == last,
            or_(first_column.in_(sorted(given)), preferred_column.in_(sorted(given))),
        )
    )
    return list(session.scalars(statement))


def _by_email(session: Session, user: User, contact: Contact) -> list[int]:
    addresses = sorted({row.email for row in contact.emails if not is_role_address(row.email)})
    if not addresses:
        return []
    statement = (
        scoped(user, ContactEmail)
        .with_only_columns(ContactEmail.contact_id)
        .where(ContactEmail.email.in_(addresses), ContactEmail.contact_id != contact.id)
    )
    return list(session.scalars(statement))


def _by_phone(session: Session, user: User, contact: Contact) -> list[int]:
    numbers = sorted({row.number_e164 for row in contact.phones if row.number_e164})
    raws = sorted({row.raw.strip() for row in contact.phones if not row.number_e164})
    clauses: list[ColumnElement[bool]] = []
    if numbers:
        clauses.append(ContactPhone.number_e164.in_(numbers))
    if raws:
        clauses.append(ContactPhone.number_e164.is_(None) & func.trim(ContactPhone.raw).in_(raws))
    if not clauses:
        return []
    statement = (
        scoped(user, ContactPhone)
        .with_only_columns(ContactPhone.contact_id)
        .where(or_(*clauses), ContactPhone.contact_id != contact.id)
    )
    return list(session.scalars(statement))


def _different_urns(one: Contact, other: Contact) -> bool:
    return one.li_urn is not None and other.li_urn is not None and one.li_urn != other.li_urn


def _slugs_differ(one: Contact, other: Contact) -> bool:
    return (
        one.li_public_id is not None
        and other.li_public_id is not None
        and one.li_public_id != other.li_public_id
    )


def is_role_address(email: str) -> bool:
    """``info@``, ``sales+eu@`` and the like: an address a role answers, not a person."""
    local = email.strip().lower().partition("@")[0].partition("+")[0]
    return local in ROLE_LOCAL_PARTS
