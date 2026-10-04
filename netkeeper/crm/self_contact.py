"""The self contact: your own details, held as a contact (#342, #320).

A template names contact fields only. A test send renders it with the self
contact, so ``{{ first_name }}`` there is your own first name, exactly as it is a
contact's in a real send, and goes to the campaign mailbox's own address.

One per user (``contacts.is_self``, with a partial unique index). It is never
part of the network: every query that lists, counts, searches, matches, exports,
merges, enrolls or enriches contacts goes through
:func:`netkeeper.scoping.scoped_contacts`, which leaves it out. Only this module
reaches it, and it edits only the fields a template can name.

It is created on the first start after migration 0034 (:func:`ensure_self_contact`,
called where the local user is ensured), seeded from a deprecated ``[me]`` section
the config may still hold (:class:`netkeeper.config.LegacyMe`): the name is split
into first and last at the first space, the city becomes the location, and the
website becomes a link. After that the config is never read for it again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from netkeeper.config import LegacyMe
from netkeeper.db import is_writer
from netkeeper.models import Contact, ContactLink, ContactSource, LinkKind, User
from netkeeper.scoping import scoped

log = logging.getLogger(__name__)

SELF_FIELDS: Final = ("first_name", "last_name", "current_company", "current_title", "location")
"""The self contact's fields you can edit: the ones a template's contact fields read."""

_MAX_LENGTHS: Final = {
    "first_name": 200,
    "last_name": 200,
    "current_company": 300,
    "current_title": 300,
    "location": 300,
}


class InvalidSelfValue(ValueError):
    """A value the self contact cannot hold: too long, or a field it does not have."""


@dataclass(frozen=True, slots=True)
class SelfDetails:
    """The self contact's editable fields. Empty means no value."""

    first_name: str = ""
    last_name: str = ""
    current_company: str = ""
    current_title: str = ""
    location: str = ""

    @classmethod
    def of(cls, contact: Contact | None) -> SelfDetails:
        if contact is None:
            return cls()
        return cls(
            first_name=contact.first_name,
            last_name=contact.last_name,
            current_company=contact.current_company or "",
            current_title=contact.current_title or "",
            location=contact.location or "",
        )


def get_self_contact(session: Session, user: User) -> Contact | None:
    """The user's self contact, or None before it is first created. Reads only."""
    return session.scalars(
        scoped(user, Contact)
        .where(Contact.is_self.is_(True))
        .execution_options(populate_existing=True)
    ).one_or_none()


def ensure_self_contact(session: Session, user: User, *, legacy: LegacyMe | None = None) -> Contact:
    """The user's self contact, created on first use, seeded from ``legacy`` when given.

    Idempotent: once it exists, ``legacy`` is ignored. Needs a writer session.
    Two writers racing to create it both get the one row: the partial unique index
    refuses the second insert, which then reads the first.
    """
    existing = get_self_contact(session, user)
    if existing is not None:
        return existing
    _require_writer(session)
    first, last = _split_name(legacy.name if legacy is not None else "")
    contact = Contact(
        user_id=user.id,
        is_self=True,
        first_name=first[: _MAX_LENGTHS["first_name"]],
        last_name=last[: _MAX_LENGTHS["last_name"]],
        location=(legacy.city[: _MAX_LENGTHS["location"]] or None) if legacy else None,
        source=ContactSource.MANUAL,
    )
    try:
        with session.begin_nested():
            session.add(contact)
            session.flush()
            if legacy is not None and legacy.website:
                session.add(
                    ContactLink(
                        user_id=user.id,
                        contact_id=contact.id,
                        url=legacy.website[:2000],
                        kind=LinkKind.WEBSITE,
                    )
                )
                session.flush()
    except IntegrityError:
        found = get_self_contact(session, user)
        if found is None:
            raise
        return found
    seeded = legacy is not None and any((legacy.name, legacy.city, legacy.website))
    log.info(
        "user %d: created the self contact%s", user.id, " from [me] in the config" if seeded else ""
    )
    return contact


def update_self_contact(session: Session, user: User, changes: dict[str, str | None]) -> Contact:
    """Set the self contact's fields in ``changes`` (keys from :data:`SELF_FIELDS`),
    creating it first if it does not exist. None or blank clears a field."""
    unknown = sorted(set(changes) - set(SELF_FIELDS))
    if unknown:
        raise InvalidSelfValue(f"the self contact has no field {', '.join(unknown)}")
    cleaned: dict[str, str] = {}
    for name, value in changes.items():
        text = (value or "").strip()
        if len(text) > _MAX_LENGTHS[name]:
            raise InvalidSelfValue(f"{name} is longer than {_MAX_LENGTHS[name]} characters")
        cleaned[name] = text
    contact = ensure_self_contact(session, user)
    for name, text in cleaned.items():
        if name in ("first_name", "last_name"):
            setattr(contact, name, text)
        else:
            setattr(contact, name, text or None)
    if "first_name" in cleaned:
        # The self contact has no separate preferred name; the merge field reads it.
        contact.preferred_name = cleaned["first_name"]
    session.flush()
    return contact


def _split_name(name: str) -> tuple[str, str]:
    """``"Ada B. Lovelace"`` is first ``Ada``, last ``B. Lovelace``. A rough split, for the
    one-time seed only: you can correct it under Settings."""
    first, _, last = " ".join(name.split()).partition(" ")
    return first, last


def _require_writer(session: Session) -> None:
    if not is_writer(session):
        raise RuntimeError(
            "creating the self contact reads, then writes: "
            "open the session with session_scope(factory, write=True)"
        )
