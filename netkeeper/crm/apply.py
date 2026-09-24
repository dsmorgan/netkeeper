"""Map the extractor's connections pages onto contacts, and run the edge lifecycle (spec 9.8, 9.10).

The core side of the boundary. :mod:`netkeeper.linkedin.connections` reads the
list and hands back :class:`~netkeeper.linkedin.connections.ConnectionsPage`
values without touching the database; this is the one module that turns them
into rows (spec 9.10: "the core's ``crm/apply.py`` maps them onto contacts,
snapshots, and messages inside a session"). ``tests/test_extractor_boundary.py``
fails if any other module maps an extractor result onto a table.

**Each connection** becomes an :class:`~netkeeper.crm.identity.IncomingContact`
with source ``sync`` and goes through identity resolution, exactly as the
archive importer's rows do (:mod:`netkeeper.crm.archive`) one rank lower:
``sync`` beats ``archive`` beats ``csv`` and a person's own edit beats them all
(:mod:`netkeeper.crm.provenance`), so a sync refreshes what an import wrote and
never what a person typed. A headline change writes a ``contact_snapshot`` of
the values before it (:func:`netkeeper.crm.identity.apply`). A row that
resolves to a :class:`~netkeeper.crm.identity.Candidate` is counted and left
for a person, as the archive importer does; guessing would merge two people.
A row whose URN or slug another contact already holds is counted and skipped
rather than failing the page.

**The edge lifecycle (spec 9.8).**

* A contact whose URN appears on a page, in either mode, is a connection:
  ``li_missing_count`` goes to 0 and ``li_disconnected_at`` is cleared. Being
  seen is evidence whichever job saw it, so an incremental sync clears a
  disconnect too; it just never *adds* one.
* :func:`age_unseen` runs once, after a *complete* full sync
  (:attr:`~netkeeper.linkedin.connections.SyncResult.complete`), and gives
  every contact with a URN the run did not see one more miss. At
  ``disconnect_after_misses`` (config, default 2) ``li_disconnected_at`` is set.
  An incremental sync never ages anyone, a full sync that stopped early never
  ages anyone, and nothing is ever deleted.

Which contacts can age: those with a URN, not merged into another. A URN is
what the connections list reports, so a contact without one (a CSV row, an
archive row the sync has not matched yet) has nothing a full sync could have
failed to see. The first full sync gives every archive contact it matches by
slug its URN (spec 8.2 step 2).

Transactions belong to the caller, and every function here reads before it
writes, so the session must be a writer (``session_scope(factory, write=True)``).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from typing import Final
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from netkeeper.crm.identity import Candidate, IncomingContact, Matched, New, apply, resolve
from netkeeper.db import is_writer
from netkeeper.linkedin.connections import ConnectionsPage
from netkeeper.linkedin.voyager import ConnectionSummary
from netkeeper.models import Contact, ContactSource, User, normalize_public_id
from netkeeper.scoping import scoped

log = logging.getLogger(__name__)

#: The most contacts one full sync may age, as a share of the contacts that can age
#: (URN-holding, not merged away), and the floor under that share. A week of real
#: disconnections is a handful of people; a sync that would age a tenth of a network
#: is far likelier to have misread it (a URN scheme change, a list the site served
#: short) than to be right, and aging is the step that ends in ``li_disconnected_at``.
#: The floor keeps a small network's ordinary week from tripping the share: 3 of 20
#: is 15%, and still normal. :func:`age_unseen` refuses above ``max(floor, share)``.
AGING_MAX_SHARE: Final = 0.10
AGING_FLOOR: Final = 10

#: The same limit on the other side: of the URNs a sync saw, how many may match no
#: stored contact. After the pages are applied every seen URN is on a contact unless
#: identity resolution refused it (a candidate, a conflict), so many unmatched URNs
#: mean the sync's URNs and the stored ones no longer name the same people -- which is
#: exactly when the stored ones would all look missing. The share is of the URNs
#: *seen*, and :data:`AGING_FLOOR` applies to it only once more than that many were
#: seen: with five seen, one stranger is already too many.
UNMATCHED_MAX_SHARE: Final = 0.10


@dataclass(slots=True)
class PageCounts:
    """What mapping pages did, summed over a run.

    ``seen`` is every connection on the pages; it splits into ``created``,
    ``updated``, ``needs_review`` (a candidate, left for a person) and
    ``conflicts`` (a URN or slug another contact holds). ``reconnected`` counts
    contacts whose ``li_disconnected_at`` a sighting cleared. ``review_contact_ids``
    is every contact a candidate row named: someone the sync may have seen under
    another identity, which :func:`age_unseen` must not age while a person decides.
    ``created_contact_ids`` is every contact these pages created: :func:`age_unseen`
    measures its limits against the contacts that existed *before* the sync, and
    these did not (#169).
    """

    seen: int = 0
    created: int = 0
    updated: int = 0
    needs_review: int = 0
    conflicts: int = 0
    reconnected: int = 0
    review_contact_ids: set[int] = field(default_factory=set)
    created_contact_ids: set[int] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class AgingCounts:
    """What :func:`age_unseen` did. ``refused`` says it declined to age anyone, and why."""

    missed: int = 0
    disconnected: int = 0
    refused: str | None = None


def known_urns(session: Session, user: User) -> frozenset[str]:
    """Every URN ``user``'s contacts carry: what an incremental sync may stop on. Read-only."""
    statement = (
        scoped(user, Contact).with_only_columns(Contact.li_urn).where(Contact.li_urn.is_not(None))
    )
    return frozenset(urn for urn in session.scalars(statement) if urn is not None)


def apply_page(
    session: Session, user: User, page: ConnectionsPage, counts: PageCounts | None = None
) -> PageCounts:
    """Write every connection on ``page`` to ``user``'s contacts and mark each one seen.

    Returns ``counts`` (a new one when none is given) with this page added.
    Nothing is committed.
    """
    _require_writer(session)
    counts = PageCounts() if counts is None else counts
    zone = _zone(user)
    for connection in page.connections:
        counts.seen += 1
        incoming = _incoming(connection, page.observed_at, zone)
        resolution = resolve(session, user, incoming)
        match resolution:
            case Candidate(contact_ids=contact_ids):
                counts.needs_review += 1
                counts.review_contact_ids.update(contact_ids)
            case Matched() | New():
                try:
                    written = apply(session, user, incoming, resolution)
                except ValueError:
                    # apply() checks before its first write, so the row is untouched.
                    # The error names the slug; the log does not need it.
                    counts.conflicts += 1
                    log.warning(
                        "connections sync: skipped a connection for user %d whose URN or slug"
                        " another contact holds; merge the two to let it through",
                        user.id,
                    )
                else:
                    if isinstance(resolution, New):
                        counts.created += 1
                        counts.created_contact_ids.add(written.id)
                    else:
                        counts.updated += 1
    counts.reconnected += _mark_seen(session, user, {c.urn for c in page.connections})
    session.flush()
    return counts


def age_unseen(
    session: Session,
    user: User,
    seen_urns: frozenset[str],
    *,
    observed_at: datetime,
    disconnect_after_misses: int,
    created_by_sync: frozenset[int],
    seen_public_ids: frozenset[str] = frozenset(),
    held_for_review: frozenset[int] = frozenset(),
) -> AgingCounts:
    """Give every contact the sync did not see one more miss (spec 9.8).

    Call once, after a complete full sync, with every URN that sync saw, every
    slug it saw (``seen_public_ids``), and every contact a candidate row named
    (``held_for_review``, :attr:`PageCounts.review_contact_ids`). A contact
    counts as seen when its URN, *or* its slug, appeared, or when it is waiting
    for review: a profile that came back under a new URN with the same slug
    resolves to a candidate and is never written, and aging it would disconnect
    exactly the rows a person has been asked to look at. Slugs are compared the
    way they are stored: URL-decoded and lowercased (:func:`normalize_public_id`).
    At ``disconnect_after_misses`` consecutive misses the contact's
    ``li_disconnected_at`` is set to ``observed_at``; one already disconnected
    keeps the time it was first set. Nothing is committed.

    Refuses, ages nobody, logs an error, and says why in
    :attr:`AgingCounts.refused` when the answer looks like a misreading rather
    than a week of disconnections:

    * ``seen_urns`` is empty -- a list that answered with nobody at all;
    * every contact that can age would miss, or half of them or more would;
    * more contacts would miss than ``max(AGING_FLOOR, AGING_MAX_SHARE`` of the
      contacts that can age``)``;
    * more seen URNs match no stored contact than ``UNMATCHED_MAX_SHARE`` of the
      seen URNs, floored at ``AGING_FLOOR`` only once more than ``AGING_FLOOR``
      URNs were seen -- the sync and the database no longer agree on what a URN
      is (a scheme change turns every row into a candidate, and every stored
      contact would then look missing).

    The first rule is what protects a small network: the floor of ten would
    otherwise let a sync wipe out a network of ten.

    ``created_by_sync`` is every contact this sync's own pages created
    (:attr:`PageCounts.created_contact_ids`), and it is required rather than
    defaulted because leaving it out is the bug it fixes (#169 A). The shares
    above are of the contacts that could age *before* this sync ran: a sync that
    replaced a small network outright (every URN and slug changed, as after a
    parser regression) creates as many rows as it misses, and counting those
    rows as ones that "can age" diluted the denominator by exactly the number
    that missed. A row the sync created was seen by it, so it never misses
    either way. The half rule is "half or more", not "more than half", for the
    week *after* such a sync: by then the replacement rows exist, and a
    regression that persists misses exactly half of the doubled network again.
    """
    _require_writer(session)
    if disconnect_after_misses < 1:
        raise ValueError("disconnect_after_misses must be at least 1")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if not seen_urns:
        log.warning("connections sync for user %d saw no connections; aging nobody", user.id)
        return AgingCounts(refused="the full sync saw no connections")
    statement = scoped(user, Contact).where(
        Contact.li_urn.is_not(None), Contact.merged_into_id.is_(None)
    )
    ageable = list(session.scalars(statement))
    stored = {contact.li_urn for contact in ageable}
    before = [contact for contact in ageable if contact.id not in created_by_sync]
    slugs = {
        slug
        for slug in (normalize_public_id(unquote(raw)) for raw in seen_public_ids)
        if slug is not None
    }
    unseen = [
        contact
        for contact in before
        if contact.li_urn not in seen_urns
        and contact.li_public_id not in slugs
        and contact.id not in held_for_review
    ]
    refusal = _implausible(
        missed=len(unseen),
        can_age=len(before),
        unmatched=len(seen_urns - stored),
        seen=len(seen_urns),
    )
    if refusal is not None:
        log.error(
            "connections sync for user %d: aging nobody, %s (%d of %d would miss, %d of %d seen"
            " URNs match no contact)",
            user.id,
            refusal,
            len(unseen),
            len(before),
            len(seen_urns - stored),
            len(seen_urns),
        )
        return AgingCounts(refused=refusal)
    missed = disconnected = 0
    for contact in unseen:
        missed += 1
        contact.li_missing_count += 1
        if (
            contact.li_missing_count >= disconnect_after_misses
            and contact.li_disconnected_at is None
        ):
            contact.li_disconnected_at = observed_at
            disconnected += 1
    session.flush()
    log.info(
        "connections sync for user %d: %d contacts missed, %d newly disconnected",
        user.id,
        missed,
        disconnected,
    )
    return AgingCounts(missed=missed, disconnected=disconnected)


def _limit(share: float, of: int) -> int:
    return max(AGING_FLOOR, math.floor(share * of))


def _implausible(*, missed: int, can_age: int, unmatched: int, seen: int) -> str | None:
    """Why aging this sync would be trusting a misreading, or None when it looks real."""
    if missed and missed == can_age:
        return f"all {can_age} contacts that can age would miss this sync"
    if missed and missed * 2 >= can_age:
        return f"{missed} of {can_age} contacts would miss this sync, half or more"
    if missed > _limit(AGING_MAX_SHARE, can_age):
        return (
            f"{missed} of {can_age} contacts would miss this sync, more than the"
            f" {_limit(AGING_MAX_SHARE, can_age)} one sync may age"
        )
    unmatched_limit = (
        _limit(UNMATCHED_MAX_SHARE, seen)
        if seen > AGING_FLOOR
        else math.floor(UNMATCHED_MAX_SHARE * seen)
    )
    if unmatched > unmatched_limit:
        return (
            f"{unmatched} of the {seen} URNs this sync saw match no contact; the"
            " stored URNs and LinkedIn's may no longer name the same people"
        )
    return None


def _mark_seen(session: Session, user: User, urns: set[str]) -> int:
    """Clear the miss count and any disconnect on every contact holding one of ``urns``.

    Returns how many had been disconnected. Runs after the page's rows are
    written, so a contact the page just matched by slug has its URN by now.
    """
    if not urns:
        return 0
    statement = scoped(user, Contact).where(Contact.li_urn.in_(sorted(urns)))
    reconnected = 0
    for contact in session.scalars(statement):
        if contact.li_disconnected_at is not None:
            contact.li_disconnected_at = None
            reconnected += 1
        if contact.li_missing_count != 0:
            contact.li_missing_count = 0
    return reconnected


def _incoming(
    connection: ConnectionSummary, observed_at: datetime, zone: tzinfo
) -> IncomingContact:
    connected_on: date | None = None
    if connection.connected_at is not None:
        # The day LinkedIn shows, which is the day in the account owner's zone.
        connected_on = connection.connected_at.astimezone(zone).date()
    return IncomingContact(
        source=ContactSource.SYNC,
        observed_at=observed_at,
        li_urn=connection.urn,
        li_public_id=connection.public_id,
        first_name=connection.first_name,
        last_name=connection.last_name,
        headline=connection.headline,
        connected_on=connected_on,
    )


def _zone(user: User) -> tzinfo:
    try:
        return ZoneInfo(user.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _require_writer(session: Session) -> None:
    if not is_writer(session):
        raise RuntimeError(
            "mapping extractor results needs a writer session;"
            " use session_scope(factory, write=True)"
        )
