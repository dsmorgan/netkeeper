"""netkeeper.crm.apply: connections pages onto contacts, and the edge lifecycle (spec 9.8, 9.10).

Pages here are built directly from :data:`voyager_pages.PEOPLE`, invented
people at invented companies. The runner that feeds these functions from a
real job is exercised end to end in ``tests/test_connections_sync.py``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker
from voyager_pages import PEOPLE, Person

from netkeeper.crm import apply as mapping
from netkeeper.crm.identity import IncomingContact, New
from netkeeper.crm.identity import apply as identity_apply
from netkeeper.crm.provenance import set_manual_field
from netkeeper.db import session_scope
from netkeeper.linkedin.connections import ConnectionsPage, SyncMode
from netkeeper.linkedin.voyager import ConnectionSummary
from netkeeper.models import Contact, ContactSource, User
from netkeeper.scoping import scoped

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=7)


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def _summary(person: Person, *, headline: str | None = None) -> ConnectionSummary:
    return ConnectionSummary(
        urn=person.urn,
        public_id=person.slug,
        first_name=person.first,
        last_name=person.last,
        headline=headline if headline is not None else person.headline,
        connected_at=(
            datetime.fromtimestamp(person.created_ms / 1000, tz=UTC)
            if person.created_ms is not None
            else None
        ),
    )


def _page(
    people: Sequence[Person],
    *,
    at: datetime = NOW,
    mode: SyncMode = SyncMode.FULL,
    headlines: dict[int, str] | None = None,
) -> ConnectionsPage:
    headlines = headlines or {}
    return ConnectionsPage(
        mode=mode,
        number=0,
        start=0,
        total=len(people),
        connections=tuple(_summary(p, headline=headlines.get(p.n)) for p in people),
        observed_at=at,
    )


def _dom_page(people: Sequence[Person], *, at: datetime = NOW) -> ConnectionsPage:
    """A page shaped like P2-08's DOM fallback: every connection has ``urn=None``
    (:class:`ConnectionSummary`'s docstring) and a total of 0 (this module's docstring
    on why a DOM-sourced page never claims a total)."""
    return ConnectionsPage(
        mode=SyncMode.FULL,
        number=0,
        start=0,
        total=0,
        connections=tuple(
            ConnectionSummary(
                urn=None,
                public_id=p.slug,
                first_name=p.first,
                last_name=p.last,
                headline=p.headline,
                connected_at=None,
            )
            for p in people
        ),
        observed_at=at,
    )


def _by_urn(session: Session, user: User, person: Person) -> Contact:
    return session.scalars(scoped(user, Contact).where(Contact.li_urn == person.urn)).one()


def _by_slug(session: Session, user: User, slug: str) -> Contact:
    return session.scalars(scoped(user, Contact).where(Contact.li_public_id == slug)).one()


def _count(session: Session, user: User) -> int:
    return len(session.scalars(scoped(user, Contact)).all())


# --- mapping a page ------------------------------------------------------------------


def test_a_page_creates_a_contact_per_new_connection(writer: Session, user: User) -> None:
    counts = mapping.apply_page(writer, user, _page(PEOPLE[:4]))

    assert (counts.seen, counts.created, counts.updated) == (4, 4, 0)
    priya = _by_urn(writer, user, PEOPLE[0])
    assert (priya.first_name, priya.last_name) == ("Priya", "Okafor")
    assert priya.li_public_id == PEOPLE[0].slug
    assert priya.li_url == f"https://www.linkedin.com/in/{PEOPLE[0].slug}/"
    assert priya.headline == "Data engineer at Fictional Robotics Co"
    assert priya.source is ContactSource.SYNC
    assert priya.field_sources["headline"] == "sync"
    assert _by_urn(writer, user, PEOPLE[2]).headline is None  # Hana has none; not invented


def test_connected_on_is_the_day_in_the_account_owners_zone(writer: Session) -> None:
    """1_700_000_000_000 ms is 22:13 UTC on 14 November 2023: the 15th at UTC+14."""
    ahead = factories.make_user(writer, timezone="Pacific/Kiritimati")
    utc = factories.make_user(writer, timezone="UTC")
    mapping.apply_page(writer, ahead, _page(PEOPLE[:1]))
    mapping.apply_page(writer, utc, _page(PEOPLE[:1]))
    assert _by_urn(writer, ahead, PEOPLE[0]).connected_on == date(2023, 11, 15)
    assert _by_urn(writer, utc, PEOPLE[0]).connected_on == date(2023, 11, 14)


def test_a_connection_without_a_date_leaves_connected_on_alone(writer: Session, user: User) -> None:
    kwame = PEOPLE[7]
    assert kwame.created_ms is None
    mapping.apply_page(writer, user, _page([kwame]))
    assert _by_urn(writer, user, kwame).connected_on is None


def test_a_second_page_updates_rather_than_duplicates(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page(PEOPLE[:3]))
    counts = mapping.apply_page(writer, user, _page(PEOPLE[:3], at=LATER))
    assert (counts.created, counts.updated) == (0, 3)
    assert _count(writer, user) == 3


def test_a_dom_row_for_an_unknown_slug_creates_one_contact_marked_needs_review(
    writer: Session, user: User
) -> None:
    """#184, revising #173's sighting-only rule for this one case: during an API
    outage a new connection shows up only as a card, so a slug nobody holds
    becomes exactly one contact -- the slug, the card's name and headline, no URN,
    and the needs-review mark. Nothing records a source for the card's text: it
    sits below every source that names the person (spec 10.5)."""
    priya = PEOPLE[0]

    counts = mapping.apply_page(writer, user, _dom_page([priya]))

    assert (counts.created, counts.updated, counts.sightings, counts.cards_created) == (0, 0, 1, 1)
    assert counts.created_contact_ids == set()  # not a connection anything may age yet
    contact = _by_slug(writer, user, priya.slug)
    assert _count(writer, user) == 1
    assert contact.li_urn is None
    assert contact.li_url == f"https://www.linkedin.com/in/{priya.slug}/"
    assert (contact.first_name, contact.last_name, contact.preferred_name) == (
        "Priya",
        "Okafor",
        "Priya",
    )
    assert contact.headline == priya.headline
    assert contact.needs_review_at == NOW
    assert contact.field_sources == {}
    assert contact.synced_values == {}
    assert contact.source is ContactSource.SYNC
    assert contact.connected_on is None


def test_a_dom_sourced_page_never_writes_any_field(writer: Session, user: User) -> None:
    """A DOM row for a slug a contact holds is sighting-only (#173 review): it
    never writes a field of that contact -- not the URN, not the name, not the
    headline -- and never creates a second one. S4/S5 of the review found that
    letting a DOM row through identity resolution could overwrite a contact's
    correct Voyager-sourced name with a crude DOM name-split, or a stranger's
    name entirely if the slug had since passed to someone else; sighting-only
    makes both impossible by construction rather than by care."""
    priya = PEOPLE[0]
    mapping.apply_page(writer, user, _page([priya]))
    before = _by_urn(writer, user, priya)
    assert (before.li_urn, before.first_name, before.headline) == (
        priya.urn,
        priya.first,
        priya.headline,
    )

    # A DOM sighting under the same slug, but with a *different* name and
    # headline than what is stored -- if this module wrote anything, it would
    # show up as corruption, not merely as "unchanged".
    impostor_card = ConnectionSummary(
        urn=None,
        public_id=priya.slug,
        first_name="Someone",
        last_name="Else",
        headline="A completely different headline",
        connected_at=None,
    )
    page = ConnectionsPage(
        mode=SyncMode.FULL,
        number=0,
        start=0,
        total=0,
        connections=(impostor_card,),
        observed_at=LATER,
    )
    counts = mapping.apply_page(writer, user, page)

    after = _by_urn(writer, user, priya)
    assert (after.li_urn, after.first_name, after.headline) == (
        priya.urn,
        priya.first,
        priya.headline,
    )
    assert after.field_sources["li_urn"] == "sync"  # still attributed to the sync that set it
    assert counts.sightings == 1
    assert (counts.created, counts.updated) == (0, 0)


def test_an_archive_contact_is_matched_by_slug_and_learns_its_urn(
    writer: Session, user: User
) -> None:
    """The archive has slugs and no URNs; the first sync joins the two (spec 8.2 step 2)."""
    mateo = PEOPLE[1]
    archived = identity_apply(
        writer,
        user,
        IncomingContact(
            source=ContactSource.ARCHIVE,
            observed_at=NOW - timedelta(days=30),
            li_public_id=mateo.slug,
            first_name="Mateo",
            last_name="Lindqvist",
            current_title="Head of Design",
            current_company="Acme Testing Group",
            connected_on=date(2023, 11, 13),
        ),
        New(),
    )
    assert archived.field_sources.get("li_urn") is None

    counts = mapping.apply_page(writer, user, _page([mateo]))

    assert (counts.created, counts.updated) == (0, 1)
    assert _count(writer, user) == 1
    assert archived.li_urn == mateo.urn
    assert archived.headline == mateo.headline
    assert archived.current_company == "Acme Testing Group"  # the sync does not report it
    assert archived.field_sources["connected_on"] == "sync"  # sync outranks archive


def test_a_persons_own_edit_survives_the_sync_and_the_sync_is_still_recorded(
    writer: Session, user: User
) -> None:
    aiko = PEOPLE[4]
    mapping.apply_page(writer, user, _page([aiko]))
    contact = _by_urn(writer, user, aiko)
    set_manual_field(contact, "headline", "Founder (my note: met at the meetup)")

    mapping.apply_page(writer, user, _page([aiko], at=LATER, headlines={aiko.n: "Advisor"}))

    assert contact.headline == "Founder (my note: met at the meetup)"
    assert contact.synced_values["headline"]["value"] == "Advisor"


def test_a_headline_change_writes_a_snapshot_of_what_it_was(writer: Session, user: User) -> None:
    luca, sofia = PEOPLE[5], PEOPLE[6]
    mapping.apply_page(writer, user, _page([luca, sofia]))

    mapping.apply_page(
        writer, user, _page([luca, sofia], at=LATER, headlines={luca.n: "SRE lead at Other Co"})
    )

    changed = _by_urn(writer, user, luca)
    assert changed.headline == "SRE lead at Other Co"
    assert [(s.headline, s.observed_at) for s in changed.snapshots] == [
        ("SRE at Nonexistent Networks", LATER)
    ]
    assert _by_urn(writer, user, sofia).snapshots == []  # unchanged: no snapshot


def test_a_candidate_is_counted_and_left_for_a_person(writer: Session, user: User) -> None:
    """The URN names one contact and the slug another: two people, or one? Not ours to say."""
    ingrid = PEOPLE[8]
    by_urn = factories.make_contact(writer, user, li_urn=ingrid.urn, li_public_id="someone-else")
    by_slug = factories.make_contact(writer, user, li_urn=None, li_public_id=ingrid.slug)
    before = (by_urn.headline, by_slug.headline)

    counts = mapping.apply_page(writer, user, _page([ingrid]))

    assert (counts.needs_review, counts.created, counts.updated) == (1, 0, 0)
    assert (by_urn.headline, by_slug.headline) == before
    assert _count(writer, user) == 2


def test_a_slug_another_contact_holds_is_counted_and_skipped_not_fatal(
    writer: Session, user: User
) -> None:
    """A merged-away contact holds the incoming slug: that row is refused, the page goes on."""
    ravi, priya = PEOPLE[9], PEOPLE[0]
    survivor = factories.make_contact(
        writer,
        user,
        li_urn=ravi.urn,
        li_public_id="ravi-old-slug",
        li_missing_count=1,
        li_disconnected_at=NOW,
    )
    factories.make_contact(
        writer, user, li_urn=None, li_public_id=ravi.slug, merged_into_id=survivor.id
    )

    counts = mapping.apply_page(writer, user, _page([ravi, priya]))

    assert counts.conflicts == 1
    assert counts.created == 1  # Priya, after the refused row
    assert survivor.li_public_id == "ravi-old-slug"
    # The row was refused, but the URN was on the page: Ravi is still a connection.
    assert (survivor.li_missing_count, survivor.li_disconnected_at) == (0, None)
    assert counts.reconnected == 1


def test_mapping_needs_a_writer(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
    with session_scope(session_factory) as reader, pytest.raises(RuntimeError, match="writer"):
        mapping.apply_page(reader, reader.merge(user), _page(PEOPLE[:1]))


def test_known_urns_are_the_contacts_urns_and_only_theirs(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    mapping.apply_page(writer, user, _page(PEOPLE[:3]))
    mapping.apply_page(writer, other, _page(PEOPLE[3:5]))
    factories.make_contact(writer, user, li_urn=None)

    assert mapping.known_urns(writer, user) == {p.urn for p in PEOPLE[:3]}


# --- the edge lifecycle (spec 9.8) ----------------------------------------------------


def _age(session: Session, user: User, seen: Sequence[Person], at: datetime = LATER) -> None:
    mapping.age_unseen(
        session,
        user,
        frozenset(p.urn for p in seen),
        observed_at=at,
        disconnect_after_misses=2,
        created_by_sync=frozenset(),
    )


def test_one_miss_counts_and_does_not_disconnect(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page(PEOPLE[:4]))

    _age(writer, user, PEOPLE[:3])

    tomasz = _by_urn(writer, user, PEOPLE[3])
    assert (tomasz.li_missing_count, tomasz.li_disconnected_at) == (1, None)
    assert all(_by_urn(writer, user, p).li_missing_count == 0 for p in PEOPLE[:3])


def test_two_consecutive_misses_set_li_disconnected_at(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page(PEOPLE[:4]))
    _age(writer, user, PEOPLE[:3], at=NOW + timedelta(days=7))
    _age(writer, user, PEOPLE[:3], at=NOW + timedelta(days=14))

    tomasz = _by_urn(writer, user, PEOPLE[3])
    assert tomasz.li_missing_count == 2
    assert tomasz.li_disconnected_at == NOW + timedelta(days=14)
    assert _count(writer, user) == 4  # nothing deleted


def test_a_third_miss_keeps_the_first_disconnect_time(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page(PEOPLE[:3]))
    for week in (1, 2, 3):
        _age(writer, user, PEOPLE[:2], at=NOW + timedelta(days=7 * week))
    hana = _by_urn(writer, user, PEOPLE[2])
    assert hana.li_missing_count == 3
    assert hana.li_disconnected_at == NOW + timedelta(days=14)


def test_a_reappearance_clears_both(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page(PEOPLE[:4]))
    _age(writer, user, PEOPLE[:3])
    _age(writer, user, PEOPLE[:3])
    assert _by_urn(writer, user, PEOPLE[3]).li_disconnected_at is not None

    counts = mapping.apply_page(writer, user, _page(PEOPLE[3:4], at=LATER))

    tomasz = _by_urn(writer, user, PEOPLE[3])
    assert (tomasz.li_missing_count, tomasz.li_disconnected_at) == (0, None)
    assert counts.reconnected == 1


def test_a_dom_sourced_reappearance_resets_the_miss_count_but_never_reconnects(
    writer: Session, user: User
) -> None:
    """#174 item 4: a DOM page carries no URN, only a slug, and a slug is weaker
    evidence than a URN (spec 9.6) -- being seen by DOM alone resets the miss
    count (it is still evidence the sync should not keep counting misses), but
    it never clears an *existing* disconnect. Only a URN sighting does that;
    see test_a_reappearance_clears_both for the Voyager-sourced case this one
    is deliberately narrower than."""
    mapping.apply_page(writer, user, _page(PEOPLE[:4]))
    _age(writer, user, PEOPLE[:3])
    _age(writer, user, PEOPLE[:3])
    assert _by_urn(writer, user, PEOPLE[3]).li_disconnected_at is not None

    counts = mapping.apply_page(writer, user, _dom_page(PEOPLE[3:4], at=LATER))

    tomasz = _by_urn(writer, user, PEOPLE[3])
    assert tomasz.li_missing_count == 0
    assert tomasz.li_disconnected_at is not None, "a DOM sighting alone must never reconnect"
    assert tomasz.li_urn == PEOPLE[3].urn  # still the real one; the DOM page never touched it
    assert counts.reconnected == 0


# --- #173 review scenarios: a released slug, reused by someone else --------------
#
# LinkedIn lets an account release a vanity url and another claim it (spec 9.6).
# These pin the two things that go wrong if a sync ever trusts a slug alone to
# mean "the same person as last time": wrongly reconnecting someone who was
# actually removed (F1/S1), and -- before the sighting-only design decision --
# wrongly writing a stranger's data onto the wrong contact, or creating a
# duplicate (F2-F4/S3-S5). S2 and S6 round out the slug-matching and
# name-parsing edges the same scenarios surfaced.


def test_S1_a_voyager_sighting_of_a_released_slug_never_reconnects_its_old_holder(
    writer: Session, user: User
) -> None:
    """F1 (HIGH): a Voyager row's *own* slug must never feed _mark_seen's public_id
    matching -- only a DOM sighting's does. Without that restriction, person A
    (a new URN) reported under a slug B (a removed person) used to hold would
    wrongly clear B's disconnect on every ordinary sync that happens to see A."""
    b = dataclasses.replace(PEOPLE[3], public_id="shared-slug")
    mapping.apply_page(writer, user, _page([*PEOPLE[:3], b]))
    _age(writer, user, PEOPLE[:3])
    _age(writer, user, PEOPLE[:3], at=LATER + timedelta(days=7))
    assert _by_urn(writer, user, b).li_disconnected_at is not None

    # A different, unrelated person (a new URN) now holds "shared-slug" and
    # shows up on an ordinary Voyager page. Identity resolution correctly
    # refuses to guess whether this is B under a new URN or somebody else
    # entirely (a URN/slug conflict is a Candidate, left for a person) -- that
    # part is unaffected by this fix. The fix is what happens to B.
    a = dataclasses.replace(PEOPLE[5], public_id="shared-slug")
    counts = mapping.apply_page(writer, user, _page([a], at=LATER + timedelta(days=14)))
    assert counts.needs_review == 1

    b_after = _by_urn(writer, user, b)
    assert b_after.li_disconnected_at is not None, "B (removed) was wrongly reconnected by A's slug"


def test_a_dom_sighting_of_a_reused_slug_during_a_voyager_outage_never_reconnects_the_old_holder(
    writer: Session, user: User
) -> None:
    """#174 item 4, the reviewer's own scenario: unlike S1 above (a Voyager
    row's own slug, fixed by keeping it out of _mark_seen's public_ids match
    at all), this is the DOM path *after* that restriction already applies.
    B is genuinely removed. LinkedIn later hands B's old slug to a different
    person, A. Voyager is down for this run -- the source has fallen back to
    DOM, so every page is DOM-sourced (``urn=None``), and every sighting can
    only ever travel by slug. The DOM fallback reads A's card
    under B's old slug and marks it seen. Before #174 item 4, a DOM sighting
    alone was enough to clear B's disconnect -- the same wrong reconnection
    S1 fixed for a Voyager row's slug, but reachable here purely through DOM,
    since DOM never carries a URN to prefer instead."""
    b = dataclasses.replace(PEOPLE[3], public_id="shared-slug")
    mapping.apply_page(writer, user, _page([*PEOPLE[:3], b]))
    _age(writer, user, PEOPLE[:3])
    _age(writer, user, PEOPLE[:3], at=LATER + timedelta(days=7))
    b_before = _by_urn(writer, user, b)
    assert b_before.li_disconnected_at is not None
    # An in-progress enrichment NotFound streak, unrelated to the connections
    # sync's own disconnect above (spec 9.8's other "gone" path) -- set
    # directly, since nothing about DOM sightings should ever touch it
    # (#176 review L6): a slug-only match resets the miss count only, not
    # this streak either (_mark_seen's docstring).
    b_before.li_not_found_count = 2
    b_before.li_not_found_since = LATER
    writer.flush()

    # A (a different person) now renders under "shared-slug" -- the only kind
    # of page a fallback run produces once it has switched to DOM.
    a_card = ConnectionSummary(
        urn=None,
        public_id="shared-slug",
        first_name="Someone",
        last_name="Else",
        headline="Not B at all",
        connected_at=None,
    )
    outage_page = ConnectionsPage(
        mode=SyncMode.FULL,
        number=0,
        start=0,
        total=0,
        connections=(a_card,),
        observed_at=LATER + timedelta(days=14),
    )
    counts = mapping.apply_page(writer, user, outage_page)
    assert counts.sightings == 1

    b_after = _by_urn(writer, user, b)
    assert b_after.li_disconnected_at is not None, (
        "B (removed) was wrongly reconnected by A's DOM sighting"
    )
    assert b_after.li_not_found_count == 2, (
        "a slug-only sighting must never touch the NotFound streak"
    )
    assert b_after.li_not_found_since == LATER
    assert b_after.li_missing_count == 0, (
        "the miss count still resets -- only the disconnect is protected"
    )


def test_S2_a_dom_sighting_matches_the_stored_slug_case_insensitively(
    writer: Session, user: User
) -> None:
    """F1's normalization half: a DOM-read slug in a different case than the one
    stored (LinkedIn resolves slugs case-insensitively, and so does identity
    resolution's own normalize_public_id -- crm.models.contacts) must still
    match the contact it names and reset its miss count -- #174 item 4 means
    it stops short of reconnecting (see the "resets the miss count but never
    reconnects" test above), but the case-insensitive match itself must still
    happen."""
    mapping.apply_page(writer, user, _page(PEOPLE[:4]))
    _age(writer, user, PEOPLE[:3])
    _age(writer, user, PEOPLE[:3])
    tomasz_before = _by_urn(writer, user, PEOPLE[3])
    assert tomasz_before.li_disconnected_at is not None
    assert tomasz_before.li_missing_count == 2

    upper = dataclasses.replace(PEOPLE[3], public_id=PEOPLE[3].slug.upper())
    mapping.apply_page(writer, user, _dom_page([upper], at=LATER))

    tomasz = _by_urn(writer, user, PEOPLE[3])
    assert tomasz.li_missing_count == 0, (
        "a case-different DOM slug should still match and reset misses"
    )
    assert tomasz.li_disconnected_at is not None, "but never reconnect on its own (#174 item 4)"


def test_S3_a_dom_card_under_a_renamed_slug_waits_for_review_and_never_merges_silently(
    writer: Session, user: User
) -> None:
    """Priya renamed her vanity url and DOM saw the new one before any Voyager sync
    did. Nobody holds the new slug, so the card becomes a contact marked needs
    review (#184) -- it cannot know it is Priya. The Voyager sync that catches up
    names Priya's URN and the new slug at once, which point at two contacts: a
    candidate for a person, never a silent merge, and neither contact changes."""
    mapping.apply_page(writer, user, _page(PEOPLE[:2]))
    renamed = dataclasses.replace(PEOPLE[0], public_id="priya-new-vanity")

    dom_counts = mapping.apply_page(writer, user, _dom_page([renamed], at=LATER))
    assert (dom_counts.created, dom_counts.updated, dom_counts.cards_created) == (0, 0, 1)
    assert _count(writer, user) == 3
    card = _by_slug(writer, user, "priya-new-vanity")
    assert card.needs_review_at is not None

    later = mapping.apply_page(writer, user, _page([renamed], at=LATER + timedelta(days=1)))
    priya = _by_urn(writer, user, renamed)
    assert later.needs_review == 1
    assert later.review_contact_ids == {priya.id, card.id}
    assert later.confirmed_by_urn == 0
    assert _count(writer, user) == 3
    assert priya.li_public_id == PEOPLE[0].slug  # not taken from the card's contact
    assert (card.li_urn, card.merged_into_id) == (None, None)
    assert card.needs_review_at is not None


def test_S4_a_dom_sighting_never_overwrites_a_correct_voyager_name_split(
    writer: Session, user: User
) -> None:
    """S4: a compound first name ("Mary Ann Smith") is stored correctly by
    Voyager's own first/last fields. A DOM card's crude single-split heuristic
    would read it wrong ("Mary" | "Ann Smith") -- sighting-only means that never
    reaches the contact regardless of what the DOM split gets right or wrong."""
    mary = Person(201, "Mary Ann", "Smith", "Engineer", public_id="mary-ann-smith")
    mapping.apply_page(writer, user, _page([mary]))

    card = ConnectionSummary(
        urn=None,
        public_id="mary-ann-smith",
        first_name="Mary",  # what _split_name("Mary Ann Smith") actually produces
        last_name="Ann Smith",
        headline="Engineer",
        connected_at=None,
    )
    page = ConnectionsPage(
        mode=SyncMode.FULL, number=0, start=0, total=0, connections=(card,), observed_at=LATER
    )
    mapping.apply_page(writer, user, page)

    contact = _by_urn(writer, user, mary)
    assert (contact.first_name, contact.last_name) == ("Mary Ann", "Smith")


def test_S5_a_dom_sighting_of_a_reused_slug_never_writes_a_strangers_name(
    writer: Session, user: User
) -> None:
    """S5: the slug "shared-slug" used to belong to B and now (in the DOM's own,
    unauthoritative rendering) shows someone else's name. Sighting-only means
    that never overwrites B's stored name or headline."""
    b = dataclasses.replace(PEOPLE[3], public_id="shared-slug")
    mapping.apply_page(writer, user, _page([b]))

    impostor = ConnectionSummary(
        urn=None,
        public_id="shared-slug",
        first_name="Zed",
        last_name="Other",
        headline="Someone else entirely",
        connected_at=None,
    )
    page = ConnectionsPage(
        mode=SyncMode.FULL, number=0, start=0, total=0, connections=(impostor,), observed_at=LATER
    )
    mapping.apply_page(writer, user, page)

    contact = _by_urn(writer, user, b)
    assert contact.first_name == b.first
    assert contact.headline == b.headline
    assert contact.snapshots == []  # nothing was ever written, so nothing to snapshot


def test_S6_a_garbled_dom_name_is_never_split_into_a_name(writer: Session, user: User) -> None:
    """S6: when a card's name selector misses and falls back to a link's full
    text, the text can carry an embedded headline after blank lines
    ("Priya Okafor\\n\\n  Data engineer at Fictional"). The extractor reports
    that name unknown rather than splitting it (#184), and the contact the card
    creates is named by its slug."""
    # The DOM reader that produced such a row went with #187's review; the row it
    # reported for a garbled name -- both names empty -- is what apply must handle.
    card = ConnectionSummary(
        urn=None,
        public_id="priya-x",
        first_name="",
        last_name="",
        headline=None,
        connected_at=None,
    )
    page = ConnectionsPage(
        mode=SyncMode.FULL, number=0, start=0, total=0, connections=(card,), observed_at=NOW
    )

    mapping.apply_page(writer, user, page)

    contact = _by_slug(writer, user, "priya-x")
    assert (contact.first_name, contact.last_name, contact.preferred_name) == (
        "priya-x",
        "",
        "priya-x",
    )


def test_a_sighting_between_misses_restarts_the_count(writer: Session, user: User) -> None:
    """Consecutive means consecutive: seen once in between, and two misses are one each."""
    mapping.apply_page(writer, user, _page(PEOPLE[:3]))
    _age(writer, user, PEOPLE[:2])
    mapping.apply_page(writer, user, _page(PEOPLE[2:3], mode=SyncMode.INCREMENTAL, at=LATER))
    _age(writer, user, PEOPLE[:2])
    hana = _by_urn(writer, user, PEOPLE[2])
    assert (hana.li_missing_count, hana.li_disconnected_at) == (1, None)


def test_the_threshold_comes_from_the_caller(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page(PEOPLE[:3]))
    for _ in range(2):
        mapping.age_unseen(
            writer,
            user,
            frozenset({PEOPLE[0].urn, PEOPLE[1].urn}),
            observed_at=LATER,
            disconnect_after_misses=3,
            created_by_sync=frozenset(),
        )
    hana = _by_urn(writer, user, PEOPLE[2])
    assert (hana.li_missing_count, hana.li_disconnected_at) == (2, None)


def test_contacts_without_a_urn_or_merged_away_never_age(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page(PEOPLE[:1]))
    csv_only = factories.make_contact(writer, user, li_urn=None, li_public_id=None)
    survivor = _by_urn(writer, user, PEOPLE[0])
    loser = factories.make_contact(writer, user, li_urn="urn:li:fsd_profile:ACoAAFAKEGONE001")
    loser.merged_into_id = survivor.id
    writer.flush()

    _age(writer, user, PEOPLE[:1])
    _age(writer, user, PEOPLE[:1])

    assert (csv_only.li_missing_count, csv_only.li_disconnected_at) == (0, None)
    assert (loser.li_missing_count, loser.li_disconnected_at) == (0, None)


def test_another_users_contacts_are_never_aged(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    mapping.apply_page(writer, user, _page(PEOPLE[:3]))
    mapping.apply_page(writer, other, _page(PEOPLE[3:5]))
    _age(writer, user, PEOPLE[:2])
    _age(writer, user, PEOPLE[:2])
    assert _by_urn(writer, user, PEOPLE[2]).li_missing_count == 2  # aging did run
    assert _missing(writer, other, PEOPLE[3:5]) == [0, 0]


def test_a_sync_that_saw_nobody_ages_nobody(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page(PEOPLE[:3]))

    aging = mapping.age_unseen(
        writer,
        user,
        frozenset(),
        observed_at=LATER,
        disconnect_after_misses=1,
        created_by_sync=frozenset(),
    )

    assert aging.refused == "the full sync saw no connections"
    assert all(_by_urn(writer, user, p).li_missing_count == 0 for p in PEOPLE[:3])


@pytest.mark.parametrize("threshold", [0, -1])
def test_a_threshold_below_one_is_refused(writer: Session, user: User, threshold: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        mapping.age_unseen(
            writer,
            user,
            frozenset({PEOPLE[0].urn}),
            observed_at=LATER,
            disconnect_after_misses=threshold,
            created_by_sync=frozenset(),
        )


# --- a sync that would age too many is a misreading --------------------------------------


def test_the_aging_limits_are_a_tenth_with_a_floor_of_ten() -> None:
    assert mapping.AGING_MAX_SHARE == 0.10
    assert mapping.AGING_FLOOR == 10
    assert mapping.UNMATCHED_MAX_SHARE == 0.10


def _crowd(count: int, prefix: str = "ACoAAFAKE") -> list[Person]:
    return [
        Person(500 + i, f"Given{i}", f"Family{i}", None, urn_prefix=prefix) for i in range(count)
    ]


def _missing(session: Session, user: User, people: Sequence[Person]) -> list[int]:
    return [_by_urn(session, user, p).li_missing_count for p in people]


def test_aging_up_to_the_share_goes_ahead_and_one_more_is_refused(
    writer: Session, user: User
) -> None:
    """200 contacts: 20 may miss (a tenth); 21 is refused and ages nobody."""
    crowd = _crowd(200)
    mapping.apply_page(writer, user, _page(crowd))

    ok = mapping.age_unseen(
        writer, user, frozenset(p.urn for p in crowd[20:]), observed_at=LATER,
        disconnect_after_misses=2, created_by_sync=frozenset(),
    )  # fmt: skip
    assert (ok.missed, ok.refused) == (20, None)

    refused = mapping.age_unseen(
        writer, user, frozenset(p.urn for p in crowd[21:]), observed_at=LATER,
        disconnect_after_misses=2, created_by_sync=frozenset(),
    )  # fmt: skip
    assert refused.refused is not None and "21 of 200" in refused.refused
    assert refused.missed == 0
    assert _missing(writer, user, crowd[:21]) == [1] * 20 + [0]  # the second call touched none


def _age_all_but(
    session: Session, user: User, crowd: Sequence[Person], missing: int, **kwargs: object
) -> mapping.AgingCounts:
    return mapping.age_unseen(
        session,
        user,
        frozenset(p.urn for p in crowd[missing:]),
        observed_at=LATER,
        disconnect_after_misses=2,
        created_by_sync=frozenset(),
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_whole_network_never_misses_at_once(writer: Session, user: User) -> None:
    """The floor of ten must not let a sync wipe out a network of ten, or of one.

    The sync saw someone, just none of them: the refusal is "all of them", which
    is checked before whether the someone matches anybody.
    """
    stranger = _crowd(1, prefix="ACoAANEW")[0].urn
    for size in (1, 5, 10):
        other = factories.make_user(writer)
        crowd = _crowd(size)
        mapping.apply_page(writer, other, _page(crowd))
        aging = mapping.age_unseen(
            writer,
            other,
            frozenset({stranger}),
            observed_at=LATER,
            disconnect_after_misses=1,
            created_by_sync=frozenset(),
        )
        assert aging.refused is not None and f"all {size}" in aging.refused, size
        assert _missing(writer, other, crowd) == [0] * size


def test_half_of_a_small_network_is_refused_and_less_is_not(writer: Session, user: User) -> None:
    crowd = _crowd(10)
    mapping.apply_page(writer, user, _page(crowd))

    five = _age_all_but(writer, user, crowd, 5)
    assert five.refused is not None and "half or more" in five.refused
    assert _missing(writer, user, crowd) == [0] * 10

    four = _age_all_but(writer, user, crowd, 4)
    assert (four.refused, four.missed) == (None, 4)


def test_twenty_contacts_may_lose_nine_but_not_ten(writer: Session, user: User) -> None:
    """At 20 the floor (10) and the half rule (10) meet; half is refused."""
    crowd = _crowd(20)
    mapping.apply_page(writer, user, _page(crowd))
    assert _age_all_but(writer, user, crowd, 10).refused is not None
    assert _age_all_but(writer, user, crowd, 9).refused is None


def test_twenty_two_contacts_may_lose_ten_but_not_eleven(writer: Session, user: User) -> None:
    """Above 20 the floor of ten binds before half does."""
    crowd = _crowd(22)
    mapping.apply_page(writer, user, _page(crowd))
    assert _age_all_but(writer, user, crowd, 11).refused is not None
    assert _age_all_but(writer, user, crowd, 10).refused is None


def test_the_unmatched_floor_applies_only_once_more_than_ten_urns_were_seen(
    writer: Session, user: User
) -> None:
    """Five seen, one a stranger: a tenth of five is 0, so one is too many."""
    crowd = _crowd(5)
    mapping.apply_page(writer, user, _page(crowd))
    stranger = _crowd(1, prefix="ACoAANEW")[0].urn

    few = mapping.age_unseen(
        writer,
        user,
        frozenset({p.urn for p in crowd[1:]} | {stranger}),
        observed_at=LATER,
        disconnect_after_misses=2,
        created_by_sync=frozenset(),
    )
    assert few.refused is not None and "match no contact" in few.refused

    many = _crowd(30)
    mapping.apply_page(writer, user, _page(many))
    ok = mapping.age_unseen(
        writer,
        user,
        frozenset({p.urn for p in [*crowd, *many]} | {stranger}),
        observed_at=LATER,
        disconnect_after_misses=2,
        created_by_sync=frozenset(),
    )
    assert ok.refused is None


def test_the_unmatched_share_is_of_what_was_seen_not_of_what_is_stored(
    writer: Session, user: User
) -> None:
    """200 contacts, all seen, plus strangers: a tenth of 222 seen is 22, of 200 is 20."""
    crowd = _crowd(200)
    mapping.apply_page(writer, user, _page(crowd))
    strangers = [p.urn for p in _crowd(25, prefix="ACoAANEW")]
    everyone = {p.urn for p in crowd}

    def age(extra: int) -> mapping.AgingCounts:
        return mapping.age_unseen(
            writer,
            user,
            frozenset(everyone | set(strangers[:extra])),
            observed_at=LATER,
            disconnect_after_misses=2,
            created_by_sync=frozenset(),
        )

    assert age(22).refused is None
    assert age(23).refused is not None


def test_a_contact_whose_slug_was_seen_does_not_miss(writer: Session, user: User) -> None:
    crowd = _crowd(20)
    mapping.apply_page(writer, user, _page(crowd))
    aging = _age_all_but(writer, user, crowd, 3, seen_public_ids=frozenset({crowd[0].slug.upper()}))
    assert aging.missed == 2
    assert _missing(writer, user, crowd[:3]) == [0, 1, 1]


def test_a_contact_held_for_review_does_not_miss(writer: Session, user: User) -> None:
    crowd = _crowd(20)
    mapping.apply_page(writer, user, _page(crowd))
    held = _by_urn(writer, user, crowd[1]).id
    aging = _age_all_but(writer, user, crowd, 3, held_for_review=frozenset({held}))
    assert aging.missed == 2
    assert _missing(writer, user, crowd[:3]) == [1, 0, 1]


def test_a_candidate_page_records_who_is_held_for_review(writer: Session, user: User) -> None:
    ingrid = PEOPLE[8]
    by_urn = factories.make_contact(writer, user, li_urn=ingrid.urn, li_public_id="someone-else")
    by_slug = factories.make_contact(writer, user, li_urn=None, li_public_id=ingrid.slug)
    counts = mapping.apply_page(writer, user, _page([ingrid]))
    assert counts.review_contact_ids == {by_urn.id, by_slug.id}


def test_seen_urns_that_match_no_contact_refuse_aging_even_when_few_would_miss(
    writer: Session, user: User
) -> None:
    """95 of 100 seen and 30 URNs nobody holds: only 5 would miss, but the URNs disagree."""
    crowd = _crowd(100)
    mapping.apply_page(writer, user, _page(crowd))
    strangers = {p.urn for p in _crowd(30, prefix="ACoAANEW")}

    aging = mapping.age_unseen(
        writer,
        user,
        frozenset({p.urn for p in crowd[5:]} | strangers),
        observed_at=LATER,
        disconnect_after_misses=1,
        created_by_sync=frozenset(),
    )

    assert aging.refused is not None and "match no contact" in aging.refused
    assert _missing(writer, user, crowd[:5]) == [0] * 5


# --- #169: a sync that replaced the network, and slugs as they are stored -----------------


def _strangers(count: int) -> list[Person]:
    """``count`` people who share nothing with :func:`_crowd`: new URNs, new slugs, new names."""
    return [
        Person(700 + i, f"Other{i}", f"Stranger{i}", None, urn_prefix="ACoAANEW")
        for i in range(count)
    ]


@pytest.mark.parametrize("size", [1, 2, 3, 5, 10])
def test_a_sync_that_replaced_the_whole_network_ages_nobody(
    writer: Session, user: User, size: int
) -> None:
    """#169 A: every URN and slug changes at once, as after a parser regression.

    The sync creates ``size`` rows and misses the ``size`` stored ones. Counted
    against all ``2 * size`` rows that can age, that is exactly half, which a
    more-than-half rule let through; counted against the ``size`` that existed
    before the sync, it is all of them.
    """
    old = _crowd(size)
    mapping.apply_page(writer, user, _page(old))
    new = _strangers(size)
    counts = mapping.apply_page(writer, user, _page(new, at=LATER))
    assert counts.created == size and len(counts.created_contact_ids) == size

    aging = mapping.age_unseen(
        writer,
        user,
        frozenset(p.urn for p in new),
        observed_at=LATER,
        disconnect_after_misses=1,
        created_by_sync=frozenset(counts.created_contact_ids),
        seen_public_ids=frozenset(p.slug for p in new),
    )

    assert aging.refused is not None and f"all {size}" in aging.refused
    assert _missing(writer, user, old) == [0] * size
    assert all(_by_urn(writer, user, p).li_disconnected_at is None for p in old)


def test_rows_the_sync_created_do_not_count_toward_what_may_age(
    writer: Session, user: User
) -> None:
    """Ten stored, six new: four missing goes ahead and five, half of the ten, does not.

    Counted over all sixteen, five missing would be under half and go ahead.
    """
    old = _crowd(10)
    mapping.apply_page(writer, user, _page(old))
    new = _strangers(6)
    counts = mapping.apply_page(writer, user, _page([*old[4:], *new], at=LATER))
    created = frozenset(counts.created_contact_ids)

    five = mapping.age_unseen(
        writer,
        user,
        frozenset(p.urn for p in [*old[5:], *new]),
        observed_at=LATER,
        disconnect_after_misses=2,
        created_by_sync=created,
    )
    assert five.refused is not None and "5 of 10" in five.refused

    four = mapping.age_unseen(
        writer,
        user,
        frozenset(p.urn for p in [*old[4:], *new]),
        observed_at=LATER,
        disconnect_after_misses=2,
        created_by_sync=created,
    )
    assert (four.refused, four.missed) == (None, 4)


def test_a_seen_slug_counts_as_stored_after_url_decoding(writer: Session, user: User) -> None:
    """#169 D: the list can report a slug percent-encoded; the contact stores it decoded."""
    crowd = _crowd(20)
    mapping.apply_page(writer, user, _page(crowd))
    accented = _by_urn(writer, user, crowd[0])
    accented.li_public_id = "josé-fake-núñez-0500"
    writer.flush()

    aging = _age_all_but(
        writer,
        user,
        crowd,
        2,
        seen_public_ids=frozenset({"Jos%C3%A9-Fake-N%C3%BA%C3%B1ez-0500"}),
    )

    assert aging.missed == 1
    assert _missing(writer, user, crowd[:2]) == [0, 1]
