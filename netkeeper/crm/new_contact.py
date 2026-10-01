"""Adding one contact by hand (#303): the form on the Contacts page, ``POST /contacts``,
and ``netkeeper contacts add``.

A contact added here goes through the same pipeline a CSV row does, so the two
can never disagree about who is already in the address book. The values are
checked the way the importer checks a cell (:func:`~netkeeper.crm.importer.is_email_address`
for an address, :func:`~netkeeper.crm.identity.public_id_from_url` for a LinkedIn
profile URL), then turned into an :class:`~netkeeper.crm.identity.IncomingContact`
of source ``manual`` and resolved with :func:`~netkeeper.crm.identity.resolve`,
the spec 8.2 order an import follows: LinkedIn identity, then any address, then
first name, last name, and company together.

Where an import would *enrich* a matched contact, a hand-added one never does:
the person meant to add someone new, so a match is refused with
:class:`Duplicate`, which names the contact already there so the form can offer
to open it. A match by name alone is only a candidate in an import, and a
person decides it there too; here it is refused the same way unless the caller
passes ``allow_name_match``, which is the form's "add anyway".

A new contact records ``manual`` as its source and as the provenance of every
field it was given (spec 10.5), and its address is the person's own row. It
then takes the tags and the static list asked for, and the auto-tag rules run
over it, as they run over the contacts an import commit wrote (#64).

Every check runs before the first write. Transactions belong to the caller;
nothing here commits. The session must be a writer.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy.orm import Session

from netkeeper.crm import identity
from netkeeper.crm import lists as list_service
from netkeeper.crm import tags as tag_service
from netkeeper.crm.identity import Candidate, CreateNew, IncomingContact, IncomingEmail, Matched
from netkeeper.crm.importer import is_email_address
from netkeeper.db import is_writer
from netkeeper.models import Contact, ContactSource, ListKind, User, single_address

log = logging.getLogger(__name__)

MatchedBy = Literal["linkedin", "email", "name"]

#: The longest value each field takes: the column's own length (``models/contacts.py``).
MAX_LENGTHS: Final[dict[str, int]] = {
    "first_name": 200,
    "last_name": 200,
    "current_company": 300,
    "current_title": 300,
    "email": 320,
    "li_url": 500,
}

#: The most tags one add may put on the new contact.
MAX_TAGS: Final[int] = 50


@dataclass(frozen=True, slots=True)
class NewContact:
    """What the form, the API, and the CLI take. Empty text is "not given"."""

    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    current_company: str | None = None
    current_title: str | None = None
    li_url: str | None = None
    tag_ids: tuple[int, ...] = ()
    list_id: int | None = None


class Invalid(ValueError):
    """One value the contact cannot be added with. ``field`` names it, as the API does."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


class Duplicate(Exception):
    """The contact is already in the address book.

    ``contact_id`` is the one to open: the first of ``contact_ids`` when several
    match (two contacts sharing an address, or a name match). ``matched_by`` says
    which rule found it; ``archived`` says the contact is archived, which the
    form mentions so an archived match does not read as a phantom.
    """

    def __init__(
        self, contact_ids: Sequence[int], matched_by: MatchedBy, *, archived: bool
    ) -> None:
        self.contact_ids = tuple(contact_ids)
        self.contact_id = self.contact_ids[0]
        self.matched_by = matched_by
        self.archived = archived
        super().__init__(f"already a contact: {self.contact_id} (matched by {matched_by})")


def create_contact(
    session: Session, user: User, new: NewContact, *, allow_name_match: bool = False
) -> Contact:
    """Add one contact to ``user``'s address book and return it.

    :class:`Invalid` for a value that does not hold up, or a tag or list that is
    not ``user``'s (a static list only); :class:`Duplicate` when the person is
    already a contact. ``RuntimeError`` for a session that is not a writer.
    """
    if not is_writer(session):
        raise RuntimeError(
            "adding a contact needs a writer session; use session_scope(factory, write=True)"
        )
    incoming = _incoming(new)
    tag_ids = _check_tags(session, user, new.tag_ids)
    _check_list(session, user, new.list_id)

    resolution = identity.resolve(session, user, incoming)
    decision: CreateNew | None = None
    match resolution:
        case Matched(contact_id=contact_id, by=by):
            raise _duplicate(session, user, (contact_id,), "email" if by == "email" else "linkedin")
        case Candidate(contact_ids=ids, by="name"):
            if not allow_name_match:
                raise _duplicate(session, user, ids, "name")
            decision = CreateNew()
        case Candidate(contact_ids=ids, by=by):
            raise _duplicate(session, user, ids, "email" if by == "email" else "linkedin")
        case _:
            pass

    contact = identity.apply(session, user, incoming, resolution, decision=decision)
    for tag_id in tag_ids:
        tag_service.tag_contact(session, user, contact.id, tag_id)
    if new.list_id is not None:
        list_service.add_members(session, user, new.list_id, [contact.id])
    # As an import commit does (#64): seed the defaults, then the rules over this one contact.
    tag_service.ensure_default_rules(session, user)
    tag_service.run_rules(session, user, [contact.id])
    session.flush()
    log.info("user %d added contact %d by hand", user.id, contact.id)
    return contact


def _incoming(new: NewContact) -> IncomingContact:
    """The values checked and normalized the way an import reads a row."""
    values = {
        "first_name": _clean(new.first_name),
        "last_name": _clean(new.last_name),
        "current_company": _clean(new.current_company),
        "current_title": _clean(new.current_title),
        "email": _clean(new.email),
        "li_url": _clean(new.li_url),
    }
    for name, value in values.items():
        if value is not None and len(value) > MAX_LENGTHS[name]:
            raise Invalid(name, f"is longer than {MAX_LENGTHS[name]} characters")
    if values["first_name"] is None and values["last_name"] is None:
        raise Invalid("first_name", "a contact needs a first name or a last name")

    emails: tuple[IncomingEmail, ...] = ()
    email = values["email"]
    if email is not None:
        address = IncomingEmail(email, is_primary=True).email
        # The importer's check, then one bare address: a campaign sends to it (#269).
        try:
            if not is_email_address(address):
                raise ValueError
            single_address(address)
        except ValueError:
            raise Invalid("email", f"{email!r} is not an email address") from None
        emails = (IncomingEmail(address, is_primary=True),)

    li_url = values["li_url"]
    if li_url is not None and identity.public_id_from_url(li_url) is None:
        raise Invalid(
            "li_url",
            f"{li_url!r} is not a LinkedIn profile URL (https://www.linkedin.com/in/...)",
        )

    return IncomingContact(
        source=ContactSource.MANUAL,
        first_name=values["first_name"],
        last_name=values["last_name"],
        current_company=values["current_company"],
        current_title=values["current_title"],
        li_url=li_url,
        emails=emails,
    )


def _check_tags(session: Session, user: User, tag_ids: Sequence[int]) -> list[int]:
    unique = list(dict.fromkeys(tag_ids))
    if len(unique) > MAX_TAGS:
        raise Invalid("tag_ids", f"at most {MAX_TAGS} tags at once")
    for tag_id in unique:
        try:
            tag_service.get_tag(session, user, tag_id)
        except tag_service.TagNotFound:
            raise Invalid("tag_ids", f"no tag {tag_id}") from None
    return unique


def _check_list(session: Session, user: User, list_id: int | None) -> None:
    if list_id is None:
        return
    try:
        row = list_service.get_list(session, user, list_id)
    except list_service.ListNotFound:
        raise Invalid("list_id", f"no list {list_id}") from None
    if row.kind is not ListKind.STATIC:
        raise Invalid("list_id", f"{row.name!r} is a smart list; only a static list takes members")


def _duplicate(
    session: Session, user: User, contact_ids: Sequence[int], matched_by: MatchedBy
) -> Duplicate:
    first = identity.resolve_survivor(session, user, contact_ids[0])
    return Duplicate(contact_ids, matched_by, archived=first.archived_at is not None)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
