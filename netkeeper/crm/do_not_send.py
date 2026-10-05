"""The do-not-send list: email addresses no campaign sends to (#238, Part B).

An address lands here when a message to it bounces, when a person marks it
``bounced`` or ``invalid`` on a contact, when its owner asks to unsubscribe, or
when a person adds it. The campaign guards check the list at enrollment and at
every step fire (spec 11.9), whichever contact holds the address by then.

The list is keyed by the address, normalized as ``contact_emails.email`` is:
trimmed and lowercased, nothing else. A ``+tag`` is part of the address, so
``name+nk1@example.com``, ``name+nk2@example.com`` and ``name@example.com`` are
three addresses, and so are ``first.last@`` and ``firstlast@`` (maintainer's
decision on #238). Nothing here folds them.

An entry is never removed by anything but :func:`remove`, a person's explicit
action. Merging contacts, removing an address from a contact, deleting a contact,
and marking an address ``ok`` again on a contact all leave it in place.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable

from sqlalchemy.orm import Session

from netkeeper.db import is_writer
from netkeeper.models import (
    DO_NOT_SEND_RANK,
    DoNotSendAddress,
    DoNotSendReason,
    EmailStatus,
    User,
    normalize_email,
    single_address,
)
from netkeeper.scoping import get_scoped, scoped

log = logging.getLogger(__name__)

REASON_FOR_STATUS: dict[EmailStatus, DoNotSendReason] = {
    EmailStatus.BOUNCED: DoNotSendReason.BOUNCED,
    EmailStatus.INVALID: DoNotSendReason.INVALID,
}
"""The entry an address status puts on the list. ``ok`` puts none, and takes none off."""


class NotFound(LookupError):
    """The entry is not one of the user's. Never says whose it is."""


def _require_writer(session: Session) -> None:
    if not is_writer(session):
        raise RuntimeError("the do-not-send list is changed only in a writer session")


def add(
    session: Session,
    user: User,
    email: str,
    reason: DoNotSendReason,
    *,
    contact_id: int | None = None,
) -> DoNotSendAddress:
    """Put ``email`` on the list, or keep the stronger reason when it is there already.

    A bounce always sets the entry's ``bounced`` flag, even under a stronger reason.

    An entry already there keeps its contact. ``ValueError`` for an empty address.
    The address is taken as found (a bounce notice's, a contact's): only what a person
    types is held to one bare address, by :func:`add_by_hand`. Needs a writer session.
    """
    _require_writer(session)
    address = normalize_email(email)
    entry = session.scalars(
        scoped(user, DoNotSendAddress).where(DoNotSendAddress.email == address)
    ).one_or_none()
    if entry is None:
        entry = DoNotSendAddress(
            user_id=user.id, email=address, reason=reason, bounced=False, contact_id=contact_id
        )
        session.add(entry)
        log.info("do-not-send: entry added (%s)", reason.value)
    elif DO_NOT_SEND_RANK[reason] > DO_NOT_SEND_RANK[entry.reason]:
        entry.reason = reason
    if reason is DoNotSendReason.BOUNCED:
        entry.bounced = True
    if entry.contact_id is None:
        entry.contact_id = contact_id
    session.flush()
    return entry


def add_by_hand(session: Session, user: User, email: str) -> DoNotSendAddress:
    """A person puts ``email`` on the list (``manual``). ``ValueError`` for anything but one
    bare address. Needs a writer session."""
    return add(session, user, single_address(normalize_email(email)), DoNotSendReason.MANUAL)


def add_for_status(
    session: Session, user: User, email: str, status: EmailStatus, *, contact_id: int
) -> DoNotSendAddress | None:
    """List an address for the status a contact now holds it with; ``None`` for ``ok``."""
    reason = REASON_FOR_STATUS.get(status)
    if reason is None:
        return None
    return add(session, user, email, reason, contact_id=contact_id)


def remove(session: Session, user: User, entry_id: int) -> DoNotSendAddress:
    """Take an entry off the list: a person's explicit action. :class:`NotFound` for one
    that is not the user's. Returns the removed entry. Needs a writer session.

    The whole entry goes, its ``bounced`` flag with it: callers that ask a person
    first say so when the entry's reason is not ``bounced`` but the flag is
    (:func:`also_bounced`).

    A contact that still holds the address as ``bounced`` or ``invalid`` keeps that
    status: the guards still refuse it on that contact, and on any other contact with
    the same address (``address_bounced_elsewhere``), until it is marked ``ok`` there.
    A merge of that contact lists the address again.
    """
    _require_writer(session)
    entry = get(session, user, entry_id)
    if entry is None:
        raise NotFound("no such do-not-send entry")
    session.delete(entry)
    session.flush()
    log.info("do-not-send: entry %d removed", entry_id)
    return entry


def get(session: Session, user: User, entry_id: int) -> DoNotSendAddress | None:
    """The user's entry with this id, if there is one."""
    return get_scoped(session, user, DoNotSendAddress, entry_id)


def also_bounced(entry: DoNotSendAddress) -> bool:
    """Whether removing ``entry`` also clears a bounce its reason does not show."""
    return entry.bounced and entry.reason is not DoNotSendReason.BOUNCED


def find(session: Session, user: User, email: str) -> DoNotSendAddress | None:
    """The entry for ``email``, normalized, if there is one."""
    return session.scalars(
        scoped(user, DoNotSendAddress).where(DoNotSendAddress.email == normalize_email(email))
    ).one_or_none()


def entries(session: Session, user: User) -> list[DoNotSendAddress]:
    """Every entry of the user's, newest first."""
    return list(
        session.scalars(
            scoped(user, DoNotSendAddress).order_by(
                DoNotSendAddress.created_at.desc(), DoNotSendAddress.id.desc()
            )
        )
    )


def reasons(session: Session, user: User, addresses: Iterable[str]) -> dict[str, DoNotSendReason]:
    """The reason each of ``addresses`` that is on the list is there, keyed by address as
    stored. ``addresses`` must already be normalized, as ``contact_emails.email`` is."""
    wanted: Collection[str] = sorted(set(addresses))
    if not wanted:
        return {}
    rows = session.execute(
        scoped(user, DoNotSendAddress)
        .with_only_columns(DoNotSendAddress.email, DoNotSendAddress.reason)
        .where(DoNotSendAddress.email.in_(wanted))
    )
    return {email: reason for email, reason in rows}
