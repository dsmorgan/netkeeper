"""The last few contacts a LinkedIn run touched, and what happened to each (#324).

The dashboard's live run panel lists them: a name, an outcome, a link to the
contact. Nothing else records it per contact -- a run's ``progress_json`` and
``counts_json`` hold counts only, and an enrichment plan's ``completed`` list
says a contact is done, not how it went -- so the runners record it here as they
write, in the same transaction as the write itself.

**Bounded, and no migration.** One ``settings_kv`` key per LinkedIn account
holds the account's newest run's last :data:`RECENT_CONTACTS_MAX` contacts, as
contact ids and outcome words, never a name, slug, or URN. A run's first
record replaces the previous run's, so the key never grows past one run's
handful, and an older run answers an empty list.

**What counts as touched.** An enrichment visit, whatever came of it (the
:class:`~netkeeper.crm.apply.HarvestResult` value). For a connections sync, a
contact the sync added (``added``) or confirmed by LinkedIn's id
(``confirmed``): a sync sees every connection on every page, so "seen" would
list whoever happened to be on the last page, and tell a person nothing.

Writers need a writer session; transactions belong to the caller.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy.orm import Session

from netkeeper.db import is_writer
from netkeeper.models import Contact, SyncRun, User
from netkeeper.scoping import scoped_contacts
from netkeeper.services.settings_kv import get_setting, set_setting

log = logging.getLogger(__name__)

#: How many of a run's touched contacts are kept: "the last few".
RECENT_CONTACTS_MAX: Final = 10

#: A connections sync's two outcomes. Enrichment's are ``HarvestResult`` values.
ADDED: Final = "added"
CONFIRMED: Final = "confirmed"

#: Each outcome word in the words a person reads.
OUTCOME_TEXT: Final[Mapping[str, str]] = {
    "applied": "profile read and saved",
    "not_found": "profile not found",
    "gone": "profile gone; marked disconnected",
    "mismatch": "another person's profile; nothing saved",
    "conflict": "profile address belongs to another contact; nothing saved",
    "missing": "no longer one of your contacts",
    "unreadable": "profile could not be read",
    ADDED: "new connection added",
    CONFIRMED: "confirmed by LinkedIn",
}


@dataclass(frozen=True, slots=True)
class TouchedContact:
    """One touched contact, as the API shows it, newest first."""

    contact_id: int
    first_name: str | None
    last_name: str | None
    outcome: str
    outcome_text: str


def _key(account_id: int) -> str:
    return f"linkedin.runs.{account_id}.recent_contacts"


def record(
    session: Session,
    user: User,
    account_id: int,
    run_id: int,
    touched: Iterable[tuple[int, str]],
) -> None:
    """Add ``touched`` (contact id, outcome), oldest first, to run ``run_id``'s record.

    Keeps the newest :data:`RECENT_CONTACTS_MAX`. A record of another run is
    replaced, so the key only ever holds the account's newest run.
    """
    if not is_writer(session):
        raise RuntimeError("run_contacts.record needs a writer session")
    added = [(int(contact_id), str(outcome)) for contact_id, outcome in touched]
    if not added:
        return
    stored = _stored(session, user, account_id)
    items = stored[1] if stored is not None and stored[0] == run_id else []
    kept = [*items, *added][-RECENT_CONTACTS_MAX:]
    set_setting(
        session,
        user,
        _key(account_id),
        {"run_id": run_id, "items": [[contact_id, outcome] for contact_id, outcome in kept]},
    )


def in_page_order(
    session: Session,
    user: User,
    touched: Mapping[int, str],
    urns: Sequence[str | None],
) -> list[tuple[int, str]]:
    """``touched`` (contact id to outcome) in the order a sync's page listed them.

    A page lists connections by URN; each touched contact is placed where its URN
    sits on the page, and one whose URN the page does not hold goes last, by id.
    Read-only.
    """
    if not touched:
        return []
    position = {urn: index for index, urn in enumerate(urns) if urn is not None}
    held = {
        contact.id: contact.li_urn
        for contact in session.scalars(scoped_contacts(user).where(Contact.id.in_(touched)))
    }

    def place(contact_id: int) -> tuple[int, int]:
        urn = held.get(contact_id)
        return (position.get(urn, len(position)) if urn is not None else len(position), contact_id)

    return [(contact_id, touched[contact_id]) for contact_id in sorted(touched, key=place)]


def recent(session: Session, user: User, run: SyncRun) -> list[TouchedContact]:
    """The last few contacts ``run`` touched, newest first, with their names. Read-only.

    Empty for a run that touched nobody yet, or whose record a newer run replaced.
    A contact deleted since is left out.
    """
    stored = _stored(session, user, run.linkedin_account_id)
    if stored is None or stored[0] != run.id:
        return []
    items = list(reversed(stored[1]))
    ids = {contact_id for contact_id, _ in items}
    names = {
        contact.id: contact
        for contact in session.scalars(scoped_contacts(user).where(Contact.id.in_(ids)))
    }
    return [
        TouchedContact(
            contact_id=contact_id,
            first_name=names[contact_id].first_name,
            last_name=names[contact_id].last_name,
            outcome=outcome,
            outcome_text=OUTCOME_TEXT.get(outcome, outcome),
        )
        for contact_id, outcome in items
        if contact_id in names
    ]


def _stored(
    session: Session, user: User, account_id: int
) -> tuple[int, list[tuple[int, str]]] | None:
    raw = get_setting(session, user, _key(account_id))
    if raw is None:
        return None
    if not isinstance(raw, dict):
        log.warning("the recent contacts of account %d are unreadable; ignoring them", account_id)
        return None
    run_id = raw.get("run_id")
    items = raw.get("items")
    if not isinstance(run_id, int) or not isinstance(items, list):
        log.warning("the recent contacts of account %d are unreadable; ignoring them", account_id)
        return None
    kept: list[tuple[int, str]] = []
    for item in items:
        if isinstance(item, list) and len(item) == 2:
            contact_id, outcome = item
            if isinstance(contact_id, int) and isinstance(outcome, str):
                kept.append((contact_id, outcome))
    return run_id, kept
