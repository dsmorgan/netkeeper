"""Map the extractor's results onto contacts, and run the edge lifecycle (spec 9.8, 9.10).

The core side of the boundary. :mod:`netkeeper.linkedin.connections` reads the
list and hands back :class:`~netkeeper.linkedin.connections.ConnectionsPage`
values, and :mod:`netkeeper.linkedin.enrich` visits profiles and hands back
:class:`~netkeeper.linkedin.enrich.ProfileHarvest` values, without touching the
database; this is the one module that turns them into rows (spec 9.10: "the
core's ``crm/apply.py`` maps them onto contacts, snapshots, and messages inside
a session"). ``tests/test_extractor_boundary.py`` fails if any other module
maps an extractor result onto a table. The harvest half is
:func:`apply_harvest`; see its docstring for how a profile is matched to the
contact it was visited for, and why a harvest never takes a value away.

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

import enum
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Final
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from netkeeper.crm.identity import (
    Candidate,
    IncomingContact,
    IncomingEmail,
    IncomingLink,
    IncomingPhone,
    IncomingPosition,
    Matched,
    New,
    apply,
    resolve,
    resolve_survivor,
)
from netkeeper.db import is_writer
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.connections import ConnectionsPage
from netkeeper.linkedin.enrich import ProfileHarvest
from netkeeper.linkedin.voyager import (
    ConnectionSummary,
    ContactInfo,
    PositionEntry,
    ProfileDetails,
)
from netkeeper.models import (
    Contact,
    ContactSource,
    EmailKind,
    Interaction,
    InteractionKind,
    LinkKind,
    User,
    normalize_public_id,
)
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


# --- profile harvests (spec 9.4 enrichment, 9.8's NotFound streak) -------------------

#: Spec 9.8: "A NotFound streak of 3 across at least 14 days marks the profile as
#: gone." A deactivated profile looks exactly like a deleted one and often comes
#: back, so one visit that finds nothing proves nothing, and neither do three in a
#: row on consecutive days.
NOT_FOUND_GONE_AFTER: Final = 3
NOT_FOUND_GONE_SPAN: Final = timedelta(days=14)

#: The link a harvested Twitter handle is stored as. A handle is not a url, and
#: ``contact_links`` holds urls; this is the one place the two meet.
TWITTER_URL: Final = "https://twitter.com/{handle}"


class HarvestResult(enum.StrEnum):
    """What :func:`apply_harvest` did with one harvest."""

    APPLIED = "applied"
    """The profile was the contact's; its fields and children were written."""

    NOT_FOUND = "not_found"
    """No profile: one more on the contact's NotFound streak."""

    GONE = "gone"
    """No profile, and the streak reached spec 9.8's bar: marked gone."""

    MISMATCH = "mismatch"
    """The profile found is not the contact's (another URN); nothing was written."""

    CONFLICT = "conflict"
    """The profile's slug belongs to another contact; nothing was written."""

    MISSING = "missing"
    """The contact is no longer one of the user's; nothing was written."""

    UNREADABLE = "unreadable"
    """The profile answered in a shape the parser could not read; only the attempt
    time was written."""


@dataclass(slots=True)
class HarvestCounts:
    """What mapping harvests did, summed over a run, one count per :class:`HarvestResult`.

    ``snapshots`` counts the ``contact_snapshot`` rows the applied harvests wrote.
    """

    applied: int = 0
    not_found: int = 0
    gone: int = 0
    mismatch: int = 0
    conflict: int = 0
    missing: int = 0
    unreadable: int = 0
    snapshots: int = 0

    def add(self, result: HarvestResult) -> None:
        setattr(self, result.value, getattr(self, result.value) + 1)


def apply_harvest(
    session: Session, user: User, harvest: ProfileHarvest, counts: HarvestCounts | None = None
) -> HarvestResult:
    """Write one profile visit's harvest to the contact it was visited for (spec 9.4 step 4).

    **Whose profile it is.** The job visits a slug, and a slug is a vanity url
    a person can give up and another can claim. So the harvest is written only
    when the profile's URN is the one the contact already holds: enrichment
    visits only contacts a sync has seen, which all have one. A profile under
    another URN is :attr:`HarvestResult.MISMATCH` and writes nothing but the
    attempt time (below). A merged
    contact stands for its survivor. A slug change under the same URN is a
    vanity-url rename, and goes through identity resolution's usual path: the
    old slug becomes an alias. A new slug another contact holds is
    :attr:`HarvestResult.CONFLICT` and writes nothing but the attempt time, as a
    connections page writes nothing of the same row.

    **Every visit is an attempt.** Whatever the visit found, the contact's
    ``li_enrich_attempted_at`` becomes its ``observed_at``, and the planner waits
    a week after a visit that wrote nothing
    (:data:`~netkeeper.services.enrich_plan.ENRICH_RETRY_AFTER`): without it, a
    contact whose slug a CSV duplicate holds would cost a profile visit every
    day until someone merged the two. An unreadable harvest
    (:attr:`HarvestResult.UNREADABLE`) is recorded the same way.

    **Nothing known is taken away.** The harvest becomes an
    :class:`~netkeeper.crm.identity.IncomingContact` with source ``sync``, and
    an incoming field that is absent is "not provided": a profile without a
    headline, a location, or a current position leaves the stored one. Child
    rows (emails, phones, links, positions) are upserted by natural key and
    never removed, and a position's end date is only ever filled in. A field a
    person edited stays theirs (spec 10.5). A change to the headline, title,
    company, or location writes a ``contact_snapshot`` of the values before it.
    Education has no table (spec 8.1) and is not stored.

    **NotFound.** A harvest that found no profile adds to the contact's streak;
    at :data:`NOT_FOUND_GONE_AFTER` across at least :data:`NOT_FOUND_GONE_SPAN`
    the contact gets ``li_disconnected_at`` and a note on its timeline (spec
    9.8), and the streak starts over, so marking gone once is not marking gone
    again on each later visit. An applied harvest ends the streak, and so does a
    sync that sees the contact (:func:`_mark_seen`).

    ``last_enriched_at`` is set, and ``enrich_priority`` cleared, only by an
    applied harvest. An applied harvest does **not** clear ``li_disconnected_at``:
    a profile that can be looked up is not proof of a connection, and only a
    sync that sees the contact in the connections list clears it. Nothing is
    committed; the session must be a writer.
    """
    _require_writer(session)
    counts = HarvestCounts() if counts is None else counts
    try:
        contact = resolve_survivor(session, user, harvest.contact_ref)
    except ValueError:
        log.warning("enrichment: contact %d is not one of user %d's", harvest.contact_ref, user.id)
        counts.add(HarvestResult.MISSING)
        return HarvestResult.MISSING
    contact.li_enrich_attempted_at = harvest.observed_at
    if harvest.outcome is Outcome.NOT_FOUND:
        result = _record_not_found(session, user, contact, harvest.observed_at)
        counts.add(result)
        return result
    if harvest.outcome is Outcome.ROUTE_CHANGED:
        session.flush()
        counts.add(HarvestResult.UNREADABLE)
        return HarvestResult.UNREADABLE
    details, info = harvest.details, harvest.contact_info
    assert details is not None and info is not None  # ProfileHarvest's own invariant
    if contact.li_urn is None or details.urn != contact.li_urn:
        log.warning(
            "enrichment: the profile visited for contact %d of user %d is not theirs"
            " (%s URN); nothing written",
            contact.id,
            user.id,
            "no stored" if contact.li_urn is None else "another",
        )
        session.flush()
        counts.add(HarvestResult.MISMATCH)
        return HarvestResult.MISMATCH
    incoming = _harvested(details, info, harvest.observed_at)
    before = len(contact.snapshots)
    try:
        apply(session, user, incoming, Matched(contact.id, "urn"))
    except ValueError:
        # apply() checks before its first write, so the contact is untouched.
        # The error names the slug; the log does not need it.
        log.warning(
            "enrichment: the profile visited for contact %d of user %d carries a slug another"
            " contact holds; merge the two to let it through",
            contact.id,
            user.id,
        )
        session.flush()
        counts.add(HarvestResult.CONFLICT)
        return HarvestResult.CONFLICT
    counts.snapshots += len(contact.snapshots) - before
    contact.last_enriched_at = harvest.observed_at
    contact.enrich_priority = 0
    contact.li_not_found_count = 0
    contact.li_not_found_since = None
    contact.li_not_found_at = None
    session.flush()
    counts.add(HarvestResult.APPLIED)
    return HarvestResult.APPLIED


def _record_not_found(
    session: Session, user: User, contact: Contact, observed_at: datetime
) -> HarvestResult:
    """One more on ``contact``'s NotFound streak; mark it gone at spec 9.8's bar."""
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if contact.li_not_found_count == 0 or contact.li_not_found_since is None:
        contact.li_not_found_since = observed_at
    contact.li_not_found_count += 1
    contact.li_not_found_at = observed_at
    since = contact.li_not_found_since
    if (
        contact.li_not_found_count >= NOT_FOUND_GONE_AFTER
        and observed_at - since >= NOT_FOUND_GONE_SPAN
        and contact.li_disconnected_at is None
    ):
        contact.li_disconnected_at = observed_at
        contact.interactions.append(
            Interaction(
                user_id=user.id,
                kind=InteractionKind.NOTE,
                at=observed_at,
                summary=(
                    f"LinkedIn profile not found on {contact.li_not_found_count} visits since"
                    f" {since.date().isoformat()}; marked as gone. A profile that comes back"
                    " clears this at the next sync that sees it."
                ),
                source=ContactSource.SYNC,
                observed_at=observed_at,
            )
        )
        # Spec 9.8's bar is three across fourteen days, each time: the streak
        # that marked the contact gone is spent, and a later one starts over.
        contact.li_not_found_count = 0
        contact.li_not_found_since = None
        session.flush()
        log.info("enrichment: contact %d of user %d marked gone", contact.id, user.id)
        return HarvestResult.GONE
    session.flush()
    return HarvestResult.NOT_FOUND


def _harvested(
    details: ProfileDetails, info: ContactInfo, observed_at: datetime
) -> IncomingContact:
    """A harvest as the row identity resolution applies. Absent means not provided."""
    current = next((p for p in details.positions if _is_open(p)), None)
    return IncomingContact(
        source=ContactSource.SYNC,
        observed_at=observed_at,
        li_urn=details.urn,
        li_public_id=details.public_id,
        first_name=details.first_name,
        last_name=details.last_name,
        headline=details.headline,
        current_title=current.title if current is not None else None,
        current_company=current.company if current is not None else None,
        location=details.location,
        emails=_emails(info),
        phones=_phones(info),
        links=_links(info),
        positions=tuple(_position(p) for p in details.positions if p.title or p.company),
    )


def _emails(info: ContactInfo) -> tuple[IncomingEmail, ...]:
    if info.email is None:
        return ()
    try:
        # Primary only when the contact has none yet; an existing primary stays.
        return (IncomingEmail(info.email, kind=EmailKind.OTHER, is_primary=True),)
    except ValueError:
        return ()


def _phones(info: ContactInfo) -> tuple[IncomingPhone, ...]:
    phones: list[IncomingPhone] = []
    for raw in info.phones:
        try:
            phones.append(IncomingPhone(raw))
        except ValueError:
            continue  # no digits: nothing to dial, nothing to key on
    return tuple(phones)


def _links(info: ContactInfo) -> tuple[IncomingLink, ...]:
    links: list[IncomingLink] = []
    for url in info.websites:
        if url.strip():
            links.append(IncomingLink(url, kind=_link_kind(url)))
    for handle in info.twitter_handles:
        cleaned = handle.strip().lstrip("@")
        if cleaned:
            links.append(IncomingLink(TWITTER_URL.format(handle=cleaned), kind=LinkKind.TWITTER))
    return tuple(links)


def _link_kind(url: str) -> LinkKind:
    try:
        host = (urlsplit(url if "://" in url else f"https://{url}").hostname or "").lower()
    except ValueError:
        return LinkKind.WEBSITE
    if host == "github.com" or host.endswith(".github.com"):
        return LinkKind.GITHUB
    return LinkKind.WEBSITE


def _position(entry: PositionEntry) -> IncomingPosition:
    """A position with its dates as the first of the month, as the archive importer stores them."""
    return IncomingPosition(
        title=entry.title,
        company=entry.company,
        started_on=_month(entry.start_year, entry.start_month),
        ended_on=_month(entry.end_year, entry.end_month),
        is_current=_is_open(entry),
    )


def _is_open(entry: PositionEntry) -> bool:
    """A position with no end at all. An end with a month but no year still ended."""
    return entry.end_year is None and entry.end_month is None


def _month(year: int | None, month: int | None) -> date | None:
    if year is None or not 1 <= year <= 9999:
        return None
    return date(year, month if month is not None and 1 <= month <= 12 else 1, 1)


def _mark_seen(session: Session, user: User, urns: set[str]) -> int:
    """Clear the miss count, any disconnect, and any NotFound streak on every contact
    holding one of ``urns``.

    A sighting is evidence the profile is there, so an enrichment NotFound streak
    (spec 9.8) starts over too: without that, a contact marked gone and then seen
    again would be marked gone by its next single NotFound. Returns how many had
    been disconnected. Runs after the page's rows are written, so a contact the
    page just matched by slug has its URN by now.
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
        if contact.li_not_found_count != 0 or contact.li_not_found_since is not None:
            contact.li_not_found_count = 0
            contact.li_not_found_since = None
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
