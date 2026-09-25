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

**Each connection with a URN** (the connections page's own answers since P2-17,
spec 9.3; Voyager's in-page API before it) becomes an
:class:`~netkeeper.crm.identity.IncomingContact` with source ``sync`` and goes
through identity resolution, exactly as the archive importer's rows do
(:mod:`netkeeper.crm.archive`) one rank lower: ``sync`` beats ``archive`` beats
``csv`` and a person's own edit beats them all
(:mod:`netkeeper.crm.provenance`), so a sync refreshes what an import wrote and
never what a person typed. A headline change writes a ``contact_snapshot`` of
the values before it (:func:`netkeeper.crm.identity.apply`). A row that resolves
to a :class:`~netkeeper.crm.identity.Candidate` is counted and left for a
person, as the archive importer does; guessing would merge two people. A row
whose URN or slug another contact already holds is counted and skipped rather
than failing the page.

**A DOM-sourced connection (P2-08's fallback, no URN) never reaches identity
resolution** -- see :func:`apply_page`'s docstring for the full reasoning (a
slug is not owned by one person forever, spec 9.6) and
the P2-08 DOM reader's docstring (removed by #190; see its PRs) for the scenarios
that found the alternative unsafe. Against a contact that already holds its slug it is
sighting-only: it marks that contact seen and writes no field. A slug no
contact holds creates one contact **marked needs review** (#184), which
nothing enriches, enrolls, or ages until a person confirms it or a Voyager
sync attaches a URN to it.

**The edge lifecycle (spec 9.8).**

* A contact whose URN appears on a page, in either mode, is a connection:
  ``li_missing_count`` goes to 0 and ``li_disconnected_at`` is cleared. A
  contact whose *current* slug a DOM sighting names has ``li_missing_count``
  reset the same way, whether or not it also carries a URN, but never has an
  existing ``li_disconnected_at`` cleared by a slug alone (#174 item 4) -- a
  slug is not owned by one person forever (spec 9.6), so only a URN sighting
  is trusted evidence a disconnect should be undone. Being seen is evidence
  whichever job saw it, so an incremental sync clears a disconnect too when
  it is a URN sighting; it just never *adds* one.
* :func:`age_unseen` runs once, after a *complete* full sync
  (:attr:`~netkeeper.linkedin.connections.SyncResult.complete`), and gives
  every contact with a URN the run did not see one more miss. At
  ``disconnect_after_misses`` (config, default 2) ``li_disconnected_at`` is set.
  An incremental sync never ages anyone, a full sync that stopped early never
  ages anyone, a full sync that ever fell back to DOM never ages anyone
  (:attr:`~netkeeper.linkedin.connections.SyncResult.complete` refuses
  outright once :attr:`~netkeeper.linkedin.connections.SyncResult.source_switched`
  is true), and nothing is ever deleted.

Which contacts can age: those with a URN, not merged into another, and not
waiting for review (#184). A URN is
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
import re
import unicodedata
from dataclasses import dataclass, field, replace
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
    ContactAlias,
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

#: What a name read off a connections-page card may look like before it is stored
#: (#184; the #173 review's F3). A card whose name selector missed falls back to the
#: profile link's own text, which can be "View Jane Doe's profile" (its
#: ``aria-label``) or the name and the occupation run together; anything past these
#: limits is not a name, and the contact is named by its slug instead.
CARD_NAME_MAX_CHARS: Final = 100
CARD_NAME_MAX_WORDS: Final = 6
#: Words and marks that a person's name does not carry and a card's surrounding
#: text does: the link's accessible label, LinkedIn's visually hidden labels
#: ("Member's name", "Member's occupation", "Status is online"; the apostrophe is
#: often U+2019), a url, an address, a digit, and the separators LinkedIn puts
#: between a name and an occupation.
#:
#: Known limit: a name and an occupation run together with no separator and no
#: headline on the card ("Jane DoeSoftware Engineer") passes. Catching it would
#: take camel-case detection, which would refuse McDonald and DeAndre; the
#: needs-review mark is what covers it.
CARD_NAME_JUNK: Final = re.compile(
    r"(?i)\bview\b|\bprofile\b|\bconnection\b|\bmember\W?s\b|\boccupation\b|\bstatus\b"
    r"|https?:|www\.|@|\d|[|\u2022\u00b7\u2013\u2014]|\s-\s|\sat\s"
)
#: Direction marks a rendered name can carry and a stored one never needs.
_DIRECTION_MARKS: Final = str.maketrans("", "", "\u200e\u200f")
#: Joiners that some scripts' names genuinely contain (ZWNJ in Persian, ZWJ in
#: several Indic scripts): format characters, but not junk.
_NAME_JOINERS: Final = frozenset("\u200c\u200d")
#: The longest headline stored from a card; the column holds 500.
CARD_HEADLINE_MAX_CHARS: Final = 500
#: A slug as LinkedIn's own routing allows it: one path segment, no whitespace.
_CARD_SLUG: Final = re.compile(r"[^\s/?#]{1,100}")


@dataclass(slots=True)
class PageCounts:
    """What mapping pages did, summed over a run.

    ``seen`` is every connection on the pages; a Voyager-sourced one (a real
    URN) splits into ``created``, ``updated``, ``needs_review`` (a candidate,
    left for a person) and ``conflicts`` (a URN or slug another contact
    holds). ``sightings`` counts the rest -- DOM-sourced rows (P2-08, no URN),
    which are never resolved or applied at all: see :func:`apply_page`'s
    docstring for why a DOM row is sighting-only against a contact that holds
    its slug (``cards_created``, below, is the other case). ``needs_review``
    is the candidate count, not the needs-review mark #184 puts on a card's
    contact. ``reconnected`` counts
    contacts whose ``li_disconnected_at`` a *URN* sighting cleared -- a DOM
    sighting resets a miss count but never clears an existing disconnect
    (#174 item 4, :func:`_mark_seen`'s docstring), so it never adds to this
    count. ``review_contact_ids`` is every contact a candidate row named:
    someone the sync may have seen under another identity, which
    :func:`age_unseen` must not age while a person decides.
    ``created_contact_ids`` is every contact these pages created: :func:`age_unseen`
    measures its limits against the contacts that existed *before* the sync, and
    these did not (#169).

    ``cards_created`` counts the contacts a DOM row created because no contact
    held its slug, each marked needs review (#184); they are not in
    ``created_contact_ids``, because they are not connections anything can age
    yet. ``confirmed_by_urn`` counts needs-review contacts a Voyager row's URN
    confirmed on these pages, and ``confirmed_contact_ids`` names them: they
    were not real connections before this sync either, so :func:`age_unseen`'s
    "existed before the sync" set leaves them out the way it leaves out
    ``created_contact_ids`` (:attr:`new_connection_ids`).
    """

    seen: int = 0
    created: int = 0
    updated: int = 0
    needs_review: int = 0
    conflicts: int = 0
    sightings: int = 0
    reconnected: int = 0
    review_contact_ids: set[int] = field(default_factory=set)
    created_contact_ids: set[int] = field(default_factory=set)
    cards_created: int = 0
    confirmed_by_urn: int = 0
    confirmed_contact_ids: set[int] = field(default_factory=set)

    @property
    def new_connection_ids(self) -> frozenset[int]:
        """Every contact that became a connection on these pages: :func:`age_unseen`'s
        ``created_by_sync``."""
        return frozenset(self.created_contact_ids | self.confirmed_contact_ids)


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
    """Write every Voyager-sourced connection on ``page`` to ``user``'s contacts, mark
    every connection seen, and turn a DOM-sourced one for a slug nobody holds into a
    contact marked needs review.

    **A DOM row (P2-08's fallback, no URN: :class:`~netkeeper.linkedin.voyager.ConnectionSummary`'s
    docstring) never goes through identity resolution.** Its only identity
    signal is a public-id slug, and a slug is not owned by one person forever
    -- LinkedIn lets an account release a vanity url and another claim it (spec
    9.6). Resolving and applying it the way a Voyager row is resolved would
    risk writing a stranger's name or headline onto the contact who used to
    hold that slug (the P2-08 DOM reader's docstring had the fuller argument, and
    the scenarios that found this; the reader was removed by #190). So a DOM row never
    calls :func:`~netkeeper.crm.identity.resolve` or
    :func:`~netkeeper.crm.identity.apply`, and what it does depends only on
    whether its (normalized) slug is known:

    * **A slug some contact holds, as its slug or as an old one** -- the row is
      sighting-only (#173 review). It contributes the slug to the "mark seen"
      pass below, which can reset a miss count but never clears an existing
      disconnect and never writes a field (#174 item 4: a slug is weaker
      evidence than a URN; see :func:`_mark_seen`'s docstring). An old slug (an
      alias) matches nothing in that pass, as before: it only stops a second
      contact being created under it.
    * **A slug nobody holds** -- during an API outage this is how a new
      connection shows up, so it is not dropped (#184). It creates exactly one
      contact: the slug, the card's name and headline, no URN, and
      ``needs_review_at`` set (:func:`_create_from_card`). The card's text is
      written with no recorded source, the lowest provenance there is, so any
      source that names the person -- a sync, the archive, a CSV import --
      overwrites it (spec 10.5). A later DOM row for the same slug finds this
      contact and is sighting-only, so repeats never duplicate it.

    **A Voyager row that reaches a needs-review contact confirms it.** Identity
    resolution finds it by slug and gives it the row's URN (spec 8.2 step 2);
    once the contact holds the URN the row carries, the mark is cleared,
    because a URN from LinkedIn's own API is the confirmation the mark was
    waiting for (#184). The card's values are replaced under the ordinary
    provenance rules without a ``contact_snapshot`` (a card is no job history);
    a card headline the row does not replace is dropped rather than kept under
    the URN (the slug may have passed to someone else since the card was read);
    and a preferred name that was only ever the card's first name follows the
    new first name. A Voyager row whose URN is another contact's and whose slug
    is the needs-review contact's is a candidate naming both, as any URN and
    slug that disagree are: it is never merged silently.

    Returns ``counts`` (a new one when none is given) with this page added.
    Nothing is committed.
    """
    _require_writer(session)
    counts = PageCounts() if counts is None else counts
    zone = _zone(user)
    urns: set[str] = set()
    sighted_public_ids: set[str] = set()
    for connection in page.connections:
        counts.seen += 1
        if connection.urn is None:
            # DOM row: see the docstring above. normalize_public_id(unquote(...))
            # matches exactly how a Voyager/archive row's own slug is normalized
            # before it is ever stored (crm.identity.IncomingContact), so a
            # differently-cased or percent-encoded DOM read still matches the
            # contact's stored slug (#173 review, F1/S2).
            normalized = normalize_public_id(unquote(connection.public_id))
            if normalized is not None:
                sighted_public_ids.add(normalized)
                if _CARD_SLUG.fullmatch(normalized) and not _slug_known(session, user, normalized):
                    _create_from_card(session, user, connection, normalized, page.observed_at)
                    counts.cards_created += 1
            counts.sightings += 1
            continue
        urns.add(connection.urn)
        incoming = _incoming(connection, page.observed_at, zone)
        resolution = resolve(session, user, incoming)
        match resolution:
            case Candidate(contact_ids=contact_ids):
                counts.needs_review += 1
                counts.review_contact_ids.update(contact_ids)
            case Matched() | New():
                target = (
                    resolve_survivor(session, user, resolution.contact_id)
                    if isinstance(resolution, Matched)
                    else None
                )
                if target is not None:
                    incoming = _keep_split(incoming, target)
                unconfirmed = target is not None and target.needs_review_at is not None
                card_named = target is not None and "first_name" not in target.field_sources
                first_before = target.first_name if target is not None else ""
                preferred_before = target.preferred_name if target is not None else ""
                try:
                    written = apply(session, user, incoming, resolution, snapshot=not unconfirmed)
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
                    if (
                        card_named
                        and preferred_before == first_before
                        and written.first_name != first_before
                    ):
                        # The preferred name was only ever the default (the card's first
                        # name, or the slug standing in for one): it follows the real one.
                        written.preferred_name = written.first_name
                    if unconfirmed and written.li_urn == connection.urn:
                        if "headline" not in written.field_sources:
                            # The row gave no headline, so the card's is still there, and
                            # the URN says who this is, not that the card was theirs: the
                            # slug may have passed to this person since the card was read.
                            written.headline = None
                        written.needs_review_at = None
                        counts.confirmed_by_urn += 1
                        counts.confirmed_contact_ids.add(written.id)
    # F1 of the #173 review: public_ids here is built *only* from DOM sightings,
    # never from a Voyager row's own slug. A Voyager row already carries a real
    # URN, which is the identity signal that clears its own disconnect above --
    # feeding its slug in too let a *different* person now holding that slug
    # (a stale vanity url a sync happens to see) wrongly clear a third, unrelated
    # contact's disconnect, on every ordinary sync (the S1 scenario). Matching a
    # URN-holding contact by a DOM slug is still allowed and still safe: it can
    # only prevent an aging that should not happen, never cause one (#173 review).
    # #174 item 4 narrows this further: a DOM slug match resets the miss count
    # but never clears an existing disconnect -- only a URN sighting does that
    # (see _mark_seen's docstring for the scenario that requires it: a slug
    # reused during a Voyager outage, when DOM is all a fallback run has).
    counts.reconnected += _mark_seen(session, user, urns=urns, public_ids=sighted_public_ids)
    session.flush()
    return counts


def _slug_known(session: Session, user: User, slug: str) -> bool:
    """True when one of ``user``'s contacts holds ``slug``, as its slug or an old one.

    Any contact counts, archived or not: a person who rejected a card's contact
    (which archives it) is not asked about the same card again on the next run.
    """
    held = scoped(user, Contact).with_only_columns(Contact.id).where(Contact.li_public_id == slug)
    if session.scalars(held.limit(1)).first() is not None:
        return True
    alias = (
        scoped(user, ContactAlias)
        .with_only_columns(ContactAlias.id)
        .where(ContactAlias.li_public_id == slug)
    )
    return session.scalars(alias.limit(1)).first() is not None


def _create_from_card(
    session: Session,
    user: User,
    connection: ConnectionSummary,
    slug: str,
    observed_at: datetime,
) -> Contact:
    """One contact from a connections-page card whose slug nobody holds (#184).

    The slug, the card's name and headline (:func:`card_name`,
    :func:`card_headline`), no URN, and ``needs_review_at``. No field records a
    source and nothing goes into ``synced_values``: a card is not a source a
    person could want to revert to, and a field with no recorded source is open
    to every source that names the person (spec 10.5), so the first sync,
    archive, or CSV row to reach the contact replaces what the card said.
    ``source`` is ``sync``, because the connections sync is what found it; the
    mark is what says nobody has confirmed it. Nothing is committed.
    """
    first, last = card_name(connection.first_name, connection.last_name, connection.headline)
    contact = Contact(
        user_id=user.id,
        source=ContactSource.SYNC,
        field_sources={},
        synced_values={},
        li_public_id=slug,
        first_name=first if first else slug,
        last_name=last if first else "",
        headline=card_headline(connection.headline),
        needs_review_at=observed_at,
    )
    session.add(contact)
    session.flush()
    log.info(
        "connections sync: created contact %d for user %d from a connections-page card;"
        " it needs review",
        contact.id,
        user.id,
    )
    return contact


def card_name(first: str, last: str, headline: str | None) -> tuple[str, str]:
    """A card's name as ``(first, last)``, or ``("", "")`` when it is not a name.

    The card's text is the person's name only when the card marked it as one; a
    card whose name selector missed hands over whatever the profile link said
    (the #173 review's F3), such as "View Jane Doe's profile" or the name and the
    occupation run together. Refused: anything with a control character (a line
    break the extractor let through), longer than :data:`CARD_NAME_MAX_CHARS`,
    more than :data:`CARD_NAME_MAX_WORDS` words, anything :data:`CARD_NAME_JUNK`
    finds, and a name that contains the card's own headline (the occupation
    leaked into the link text; :func:`_holds_headline`). Direction marks (LRM,
    RLM) are stripped first; the joiners ZWNJ and ZWJ are kept, since names in
    some scripts contain them. A refused name is never trimmed into shape:
    guessing which part is the name is how a stranger's words end up on a
    contact.
    """
    first = first.translate(_DIRECTION_MARKS).strip()
    last = last.translate(_DIRECTION_MARKS).strip()
    whole = f"{first} {last}".strip()
    if not whole or len(whole) > CARD_NAME_MAX_CHARS:
        return "", ""
    if _has_control(whole) or len(whole.split()) > CARD_NAME_MAX_WORDS:
        return "", ""
    if CARD_NAME_JUNK.search(whole):
        return "", ""
    if headline is not None and _holds_headline(whole, headline.strip()):
        return "", ""
    return first, last


def _holds_headline(name: str, headline: str) -> bool:
    """True when the card's headline is inside its name text: the occupation leaked in.

    Word-bounded, so a headline of "Ann" does not refuse "Anna Karenina". The one
    unbounded case is the join LinkedIn's markup produces when two text nodes run
    together: a lowercase letter directly followed by the headline as the card
    wrote it ("OkaforData engineer").
    """
    if not headline:
        return False
    escaped = re.escape(headline)
    if re.search(rf"(?<!\w){escaped}(?!\w)", name, re.IGNORECASE):
        return True
    return headline[0].isupper() and re.search(rf"(?<=[a-z]){escaped}(?!\w)", name) is not None


def card_headline(headline: str | None) -> str | None:
    """A card's headline, or None when it is empty, carries a control character, or is too long."""
    if headline is None:
        return None
    cleaned = headline.strip()
    if not cleaned or len(cleaned) > CARD_HEADLINE_MAX_CHARS or _has_control(cleaned):
        return None
    return cleaned


def _has_control(text: str) -> bool:
    """A control or format character, or a line or paragraph separator: never in a name.

    ZWNJ and ZWJ are format characters that names in some scripts contain, so they pass.
    """
    return any(
        ch not in _NAME_JOINERS
        and (unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in ("Zl", "Zp"))
        for ch in text
    )


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
    # A contact waiting for review (#184) is not a connection anything has
    # confirmed, so it neither ages nor counts toward the shares below. One can
    # only hold a URN here by a path other than a sync (a merge, an edit): a
    # sync that sees its URN confirms it before this runs.
    statement = scoped(user, Contact).where(
        Contact.li_urn.is_not(None),
        Contact.merged_into_id.is_(None),
        Contact.needs_review_at.is_(None),
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
    Education, a birthday, and an address have no table (spec 8.1) and are not
    stored; neither is the overlay's "Connected since" day, which the connections
    list already gives. An ``Ok`` harvest without contact info (the job clicks
    **Contact info** only on a profile whose id is the contact's, #190) writes
    nothing: a mismatch is :attr:`HarvestResult.MISMATCH`, and a matching one,
    which the job never hands over, is :attr:`HarvestResult.UNREADABLE`.

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
    assert details is not None  # ProfileHarvest's own invariant
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
    if info is None:
        # The job clicks Contact info only on a profile whose id is the contact's, so a
        # harvest without it for a matching URN is not one the job makes. A visit is
        # written whole or not at all: nothing but the attempt.
        log.warning(
            "enrichment: the harvest for contact %d of user %d has no contact info; nothing"
            " written",
            contact.id,
            user.id,
        )
        session.flush()
        counts.add(HarvestResult.UNREADABLE)
        return HarvestResult.UNREADABLE
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
    """The overlay's addresses; the first it shows is the primary candidate.

    Primary only when the contact has none yet; an existing primary stays.
    """
    emails: list[IncomingEmail] = []
    for address in info.emails:
        try:
            emails.append(IncomingEmail(address, kind=EmailKind.OTHER, is_primary=not emails))
        except ValueError:
            continue  # not an address: nothing to key on
    return tuple(emails)


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


def _mark_seen(session: Session, user: User, *, urns: set[str], public_ids: set[str]) -> int:
    """Clear the miss count on every contact holding one of ``urns`` or one of
    ``public_ids``; also clear an existing disconnect and any NotFound streak,
    but only for a ``urns`` match.

    A URN sighting is trusted evidence the profile is there, so it clears
    ``li_disconnected_at``, ``li_missing_count``, and the enrichment NotFound
    streak (spec 9.8) the same as it always has: without that, a contact
    marked gone and then seen again would be marked gone by its next single
    NotFound. Returns how many had been disconnected by a URN sighting. Runs
    after the page's rows are written, so a Voyager connection the page just
    matched by slug has its URN by now, already covered by the ``urns`` branch.

    **A ``public_ids``-only match (a connection observed with no URN -- spec
    9.8's #184 path, see :class:`~netkeeper.linkedin.voyager.ConnectionSummary`'s
    docstring) resets ``li_missing_count`` only, and never clears
    ``li_disconnected_at`` or the NotFound streak (#174 item 4).** A slug is
    not owned by one person forever (spec 9.6), and #173's review already
    restricted which slugs ever reach ``public_ids`` in the first place (see
    below) -- but even a genuine URN-less sighting's own slug is weaker
    evidence than a URN. The scenario that requires this: during an API
    outage, a connection can show up with only a slug for identity, no URN
    (#184). If LinkedIn has since handed a removed contact's old slug to a
    different person, a later URN-less sighting of that person's card would
    otherwise let this function wrongly clear the *old*, actually-disconnected
    contact's ``li_disconnected_at`` -- the same wrong reconnection F1/S1
    fixed for a Voyager row's own slug, but reachable here even without one,
    because a URN-less row never has a URN to prefer instead. Resetting the
    miss count is still safe in the one direction this module allows (it can
    only delay a disconnect that has not happened yet, never undo one that
    already has); clearing an established disconnect is not, so only a URN
    sighting does that.

    **``public_ids`` is normalized-slug evidence from DOM sightings only (#173
    review, F1) -- ``apply_page`` never includes a Voyager row's own slug here.**
    A Voyager row already carries a real URN, the identity signal the ``urns``
    branch reads; feeding its slug in too would let *whoever currently holds
    that slug* affect it, which is wrong the moment that slug has since passed
    to someone else -- a Voyager sighting of a person who happens to be
    reported with a stale vanity url would then wrongly touch a *different*,
    actually-removed contact still recorded under it (the S1 scenario the
    review found: without this restriction, an ordinary sync could silently
    undo a real disconnection every time a released slug resurfaces). Matching
    by slug at all exists for P2-08's DOM fallback, whose pages carry no URN
    (:class:`~netkeeper.linkedin.voyager.ConnectionSummary`'s docstring):
    without it, a contact seen only through DOM would never have its miss
    count cleared, even though spec 9.8 says "being seen is evidence whichever
    job made it" -- item 4 narrows just how far that evidence reaches.
    """
    if not urns and not public_ids:
        return 0
    reconnected = 0
    matched_by_urn: set[int] = set()
    if urns:
        statement = scoped(user, Contact).where(Contact.li_urn.in_(sorted(urns)))
        for contact in session.scalars(statement):
            matched_by_urn.add(contact.id)
            if contact.li_disconnected_at is not None:
                contact.li_disconnected_at = None
                reconnected += 1
            if contact.li_missing_count != 0:
                contact.li_missing_count = 0
            if contact.li_not_found_count != 0 or contact.li_not_found_since is not None:
                contact.li_not_found_count = 0
                contact.li_not_found_since = None
    if public_ids:
        statement = scoped(user, Contact).where(Contact.li_public_id.in_(sorted(public_ids)))
        for contact in session.scalars(statement):
            if contact.id in matched_by_urn:
                continue  # already given the fuller urns-branch treatment above
            # #174 item 4: a slug-only (DOM) sighting resets the miss count but
            # never clears an existing disconnect or NotFound streak -- see the
            # docstring above for the stale-slug-during-an-outage scenario this
            # guards against.
            if contact.li_missing_count != 0:
                contact.li_missing_count = 0
    return reconnected


def _incoming(
    connection: ConnectionSummary, observed_at: datetime, zone: tzinfo
) -> IncomingContact:
    connected_on: date | None = connection.connected_on
    if connected_on is None and connection.connected_at is not None:
        # The day LinkedIn shows, which is the day in the account owner's zone. A
        # source that states the day itself (the flagship-web card) is taken as is.
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


def _keep_split(incoming: IncomingContact, target: Contact) -> IncomingContact:
    """``incoming`` with the contact's own first/last split when both spell the same name.

    The flagship-web card gives one display name, split at its first space
    (:func:`netkeeper.linkedin.flagship.split_display_name`); an archive or a CSV
    import may have split the same name elsewhere ("Mary Ann" / "Smith"). When the
    two join to the same words, the name has not changed, so the contact's split is
    kept rather than rewritten on every sync. A name that did change is written as
    the row gives it.
    """
    joined = " ".join(f"{incoming.first_name or ''} {incoming.last_name or ''}".split())
    stored = " ".join(f"{target.first_name or ''} {target.last_name or ''}".split())
    if not joined or joined != stored:
        return incoming
    if (incoming.first_name, incoming.last_name) == (target.first_name, target.last_name or None):
        return incoming
    return replace(incoming, first_name=target.first_name, last_name=target.last_name or None)


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
