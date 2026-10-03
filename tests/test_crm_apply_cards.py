"""Contacts read off a connections-page card, marked needs review (#184, P2-08).

When the connections sync falls back to reading the page, a card whose slug no
contact holds becomes one contact marked needs review: the slug, the card's name
and headline at the lowest provenance, and no URN. A person confirms or rejects
it, or a later Voyager sync attaches a URN and confirms it. Until then it is never
enriched, enrolled, or aged. ``tests/test_crm_apply.py`` holds the sighting-only
rules for a card whose slug a contact already holds.

Every person here is invented.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker
from voyager_pages import PEOPLE, Person

from netkeeper.crm import apply as mapping
from netkeeper.crm import contacts as contacts_service
from netkeeper.crm.identity import IncomingContact, Matched, merge, resolve
from netkeeper.crm.identity import apply as identity_apply
from netkeeper.db import session_scope
from netkeeper.linkedin.connections import ConnectionsPage, SyncMode
from netkeeper.linkedin.voyager import ConnectionSummary
from netkeeper.models import Contact, ContactAlias, ContactSource, User
from netkeeper.scoping import scoped
from netkeeper.services import enrich_plan

NOW = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=7)


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def _card(
    slug: str, first: str = "Priya", last: str = "Okafor", headline: str | None = None
) -> ConnectionSummary:
    return ConnectionSummary(
        urn=None,
        public_id=slug,
        first_name=first,
        last_name=last,
        headline=headline,
        connected_at=None,
    )


def _page(connections: Sequence[ConnectionSummary], *, at: datetime = NOW) -> ConnectionsPage:
    return ConnectionsPage(
        mode=SyncMode.FULL,
        number=0,
        start=0,
        total=0,
        connections=tuple(connections),
        observed_at=at,
    )


def _dom(person: Person) -> ConnectionSummary:
    return _card(person.slug, person.first, person.last, person.headline)


def _voyager(person: Person) -> ConnectionSummary:
    return ConnectionSummary(
        urn=person.urn,
        public_id=person.slug,
        first_name=person.first,
        last_name=person.last,
        headline=person.headline,
        connected_at=datetime.fromtimestamp(1_700_000_000, tz=UTC),
    )


def _voyager_page(people: Sequence[Person], *, at: datetime = LATER) -> ConnectionsPage:
    return ConnectionsPage(
        mode=SyncMode.FULL,
        number=0,
        start=0,
        total=len(people),
        connections=tuple(_voyager(p) for p in people),
        observed_at=at,
    )


def _all(session: Session, user: User) -> list[Contact]:
    return list(session.scalars(scoped(user, Contact).order_by(Contact.id)))


def _by_slug(session: Session, user: User, slug: str) -> Contact:
    return session.scalars(scoped(user, Contact).where(Contact.li_public_id == slug)).one()


# --- creating one, and only one ----------------------------------------------------------


def test_a_repeat_sighting_never_duplicates_the_card_contact(writer: Session, user: User) -> None:
    """The second page, a case-different read, a percent-encoded one, and the same
    card twice on one page all find the contact the first card created."""
    priya = PEOPLE[0]
    first = mapping.apply_page(writer, user, _page([_dom(priya), _dom(priya)]))
    assert first.cards_created == 1

    again = mapping.apply_page(
        writer,
        user,
        _page(
            [
                _dom(priya),
                _card(priya.slug.upper()),
                _card(priya.slug.replace("-", "%2D")),
            ],
            at=LATER,
        ),
    )

    assert again.cards_created == 0
    assert again.sightings == 3
    assert len(_all(writer, user)) == 1
    assert _all(writer, user)[0].needs_review_at == NOW  # the first sighting's, kept


def test_a_repeat_sighting_resets_the_miss_count_like_any_sighting(
    writer: Session, user: User
) -> None:
    mapping.apply_page(writer, user, _page([_dom(PEOPLE[0])]))
    contact = _by_slug(writer, user, PEOPLE[0].slug)
    contact.li_missing_count = 1
    writer.flush()

    mapping.apply_page(writer, user, _page([_dom(PEOPLE[0])], at=LATER))

    assert contact.li_missing_count == 0


def test_an_old_slug_another_contact_held_creates_nothing(writer: Session, user: User) -> None:
    """A slug that is some contact's alias may still be that person's old link; a
    second contact under it would be the duplicate #173's review warned about."""
    holder = factories.make_contact(writer, user)
    holder.aliases.append(
        ContactAlias(user_id=user.id, li_public_id="old-vanity", source=ContactSource.SYNC)
    )
    writer.flush()

    counts = mapping.apply_page(writer, user, _page([_card("old-vanity")]))

    assert counts.cards_created == 0
    assert len(_all(writer, user)) == 1


def test_a_rejected_card_is_not_asked_about_again(writer: Session, user: User) -> None:
    """Rejecting archives the contact and keeps its slug, so the same card on the
    next fallback run is a sighting of it, not a new contact."""
    mapping.apply_page(writer, user, _page([_dom(PEOPLE[0])]))
    contact = _by_slug(writer, user, PEOPLE[0].slug)
    contacts_service.reject_contact(writer, user, contact.id)

    counts = mapping.apply_page(writer, user, _page([_dom(PEOPLE[0])], at=LATER))

    assert counts.cards_created == 0
    assert len(_all(writer, user)) == 1


@pytest.mark.parametrize("slug", ["two words", "a%2Fb", "x" * 101, "tab%09here"])
def test_a_slug_linkedin_would_not_route_creates_nothing(
    writer: Session, user: User, slug: str
) -> None:
    counts = mapping.apply_page(writer, user, _page([_card(slug)]))
    assert counts.cards_created == 0
    assert _all(writer, user) == []


def test_another_users_slug_is_unknown_to_this_user(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    mapping.apply_page(writer, other, _page([_dom(PEOPLE[0])]))

    counts = mapping.apply_page(writer, user, _page([_dom(PEOPLE[0])]))

    assert counts.cards_created == 1
    assert len(_all(writer, user)) == 1
    assert len(_all(writer, other)) == 1


# --- what the card's name may be ------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "last", "headline"),
    [
        ("View", "Priya Okafor's profile", None),  # the link's aria-label
        ("Priya", "Okafor Data engineer at Fictional", "Data engineer at Fictional"),
        ("Priya", "OkaforData engineer", "Data engineer"),  # text nodes run together
        # The run-on join after any word character, in any case (#186).
        ("Jane", "Doe, MBAData engineer", "Data engineer"),  # after a capital
        ("Jane", "DOEData engineer", "Data engineer"),
        ("王", "小明Engineer", "Engineer"),  # after CJK
        ("Jane", "DoeData Engineer", "Data engineer"),  # the headline in another case
        ("Jane", "DoeiOS developer", "iOS developer"),  # a headline that starts lowercase
        ("Jane", "Doe2Data engineer", "Data engineer"),  # after a digit
        ("Priya", "Okafor 2nd degree connection", None),
        ("Priya", "Okafor · Data engineer", None),
        ("Priya", "Okafor | Fictional Robotics", None),
        ("Priya", "Okafor - Data engineer", None),
        ("Priya", "Okafor at Fictional", None),
        ("Priya", "Okafor\u2028Data", None),  # a line separator the extractor let through
        ("Priya", "Okafor\x00", None),
        ("Priya", "Okafor https://example.test", None),
        ("Priya", "okafor@example.test", None),
        ("A", "B C D E F G", None),  # seven words
        ("Member\u2019s", "name Jane Doe", None),  # LinkedIn's visually hidden label
        ("Member's", "name Jane Doe", None),
        ("Jane", "Doe Status is online", None),
        ("Member\u2019s", "nameJane DoeMember\u2019s occupationEngineer", None),
        ("Jane", "Doe Occupation", None),
        ("P" * 60, "O" * 60, None),  # 121 characters
        ("", "", None),
    ],
)
def test_a_card_name_that_is_not_a_name_is_refused(
    first: str, last: str, headline: str | None
) -> None:
    assert mapping.card_name(first, last, headline) == ("", "")


@pytest.mark.parametrize(
    ("first", "last"),
    [
        ("Priya", "Okafor"),
        ("Mary", "Ann Smith"),
        ("Jean-Luc", "O'Brien"),
        ("Maria", "de la Cruz Gomez"),
        ("Zoë", "Ångström"),
        ("Priya", ""),
        ("Maria José", "de la Cruz Gómez"),  # six words: the most allowed
        ("Pegah", "Ra\u200cfiei"),  # ZWNJ, as Persian names carry it
        ("Anand", "Kum\u200dar"),  # ZWJ
    ],
)
def test_a_plain_card_name_is_kept_as_the_card_gave_it(first: str, last: str) -> None:
    assert mapping.card_name(first, last, "Data engineer at Fictional") == (first, last)


def test_a_headline_is_matched_as_words_not_letters() -> None:
    """A headline of "Ann" is not inside "Anna Karenina"; it is inside "Jo Ann Smith"."""
    assert mapping.card_name("Anna", "Karenina", "Ann") == ("Anna", "Karenina")
    assert mapping.card_name("Jo", "Ann Smith", "Ann") == ("", "")


@pytest.mark.parametrize(
    ("first", "last", "headline"),
    [
        ("Anna", "Karenina", "Ann"),  # the headline runs on into a letter
        ("Jane", "Doe", "Data engineer"),
        ("Jane", "Doe", "Doe Industries"),  # the name is inside the headline, not the reverse
        ("王", "小明", "Engineer"),
        ("Jane", "Doe", "iOS developer"),
        ("Jane", "Doe", ""),
        ("Jane", "Doe", "   "),
    ],
)
def test_the_run_on_check_keeps_a_name_without_its_headline(
    first: str, last: str, headline: str
) -> None:
    """The open left side of the run-on check still needs the headline to end the word."""
    assert mapping.card_name(first, last, headline) == (first, last)


def test_direction_marks_are_stripped_not_refused() -> None:
    assert mapping.card_name("\u200fPriya", "Okafor\u200e", None) == ("Priya", "Okafor")


def test_a_card_with_no_usable_name_is_named_by_its_slug(writer: Session, user: User) -> None:
    card = _card("priya-okafor-1", "View", "Priya Okafor's profile")
    mapping.apply_page(writer, user, _page([card]))

    contact = _by_slug(writer, user, "priya-okafor-1")
    assert (contact.first_name, contact.last_name, contact.preferred_name) == (
        "priya-okafor-1",
        "",
        "priya-okafor-1",
    )


@pytest.mark.parametrize("headline", ["line one\nline two", "x" * 501, "   "])
def test_a_card_headline_that_is_not_one_is_left_out(
    writer: Session, user: User, headline: str
) -> None:
    mapping.apply_page(writer, user, _page([_card("priya-h", headline=headline)]))
    assert _by_slug(writer, user, "priya-h").headline is None


# --- the lowest provenance ------------------------------------------------------------


@pytest.mark.parametrize("source", [ContactSource.CSV, ContactSource.ARCHIVE])
def test_any_source_that_names_the_person_replaces_the_cards_text(
    writer: Session, user: User, source: ContactSource
) -> None:
    """Below sync and the archive, and below a CSV import too: every field of a card
    contact is open to them (spec 10.5). An import does not confirm it -- only a
    URN or a person does."""
    mapping.apply_page(writer, user, _page([_card("priya-p", "Pryia", "Okafro", "Wrong")]))
    row = IncomingContact(
        source=source,
        observed_at=LATER,
        li_public_id="priya-p",
        first_name="Priya",
        last_name="Okafor",
        headline="Data engineer at Fictional Robotics Co",
    )
    resolution = resolve(writer, user, row)
    assert isinstance(resolution, Matched)

    contact = identity_apply(writer, user, row, resolution)

    assert (contact.first_name, contact.last_name, contact.headline) == (
        "Priya",
        "Okafor",
        "Data engineer at Fictional Robotics Co",
    )
    assert contact.field_sources["first_name"] == source.value
    assert contact.needs_review_at is not None


def test_a_person_edit_of_a_card_contact_sticks_like_any_edit(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page([_dom(PEOPLE[0])]))
    contact = _by_slug(writer, user, PEOPLE[0].slug)
    contacts_service.update_contact(writer, user, contact.id, {"headline": "Mine"})

    mapping.apply_page(writer, user, _voyager_page([PEOPLE[0]]))

    assert contact.headline == "Mine"
    assert contact.li_urn == PEOPLE[0].urn


# --- a later Voyager sync -------------------------------------------------------------


def test_a_voyager_row_attaches_its_urn_upgrades_the_fields_and_confirms(
    writer: Session, user: User
) -> None:
    """The decision on record (#184): a URN from LinkedIn's own API is the
    confirmation the mark waits for, so attaching one clears it."""
    priya = PEOPLE[0]
    card = _card(priya.slug, "Priya", "Okafor", "Data eng (card)")
    mapping.apply_page(writer, user, _page([card]))
    contact = _by_slug(writer, user, priya.slug)

    counts = mapping.apply_page(writer, user, _voyager_page([priya]))

    assert (counts.created, counts.updated, counts.confirmed_by_urn) == (0, 1, 1)
    assert counts.confirmed_contact_ids == {contact.id}
    assert contact.id in counts.new_connection_ids
    assert len(_all(writer, user)) == 1
    assert contact.li_urn == priya.urn
    assert contact.needs_review_at is None
    assert contact.headline == priya.headline
    assert contact.connected_on is not None
    assert contact.field_sources["headline"] == "sync"
    assert contact.field_sources["li_urn"] == "sync"
    # A card is no job history: replacing its headline is not a job change.
    assert contact.snapshots == []


def test_a_urn_never_keeps_a_card_headline_its_row_did_not_confirm(
    writer: Session, user: User
) -> None:
    """The card showed A; by the time Voyager answers, the slug is B's, and B has no
    headline. The URN confirms B, so nothing of A's card may stay on B's contact."""
    mapping.apply_page(writer, user, _page([_card("moved-slug", "Ann", "Card", "A's job at A Co")]))
    contact = _by_slug(writer, user, "moved-slug")
    b = Person(401, "Bea", "Other", None, public_id="moved-slug")

    mapping.apply_page(writer, user, _voyager_page([b]))

    assert (contact.first_name, contact.last_name, contact.headline) == ("Bea", "Other", None)
    assert contact.needs_review_at is None


def test_a_confirmed_contacts_headline_change_still_writes_a_snapshot(
    writer: Session, user: User
) -> None:
    """Only a contact still waiting skips the snapshot."""
    priya = PEOPLE[0]
    mapping.apply_page(writer, user, _page([_card(priya.slug, headline="Card headline")]))
    contact = _by_slug(writer, user, priya.slug)
    contacts_service.confirm_contact(writer, user, contact.id)

    mapping.apply_page(writer, user, _voyager_page([priya]))

    assert [s.headline for s in contact.snapshots] == ["Card headline"]


def test_a_preferred_name_that_was_only_the_cards_follows_the_real_first_name(
    writer: Session, user: User
) -> None:
    mapping.apply_page(writer, user, _page([_card(PEOPLE[0].slug, "View", "her profile")]))
    contact = _by_slug(writer, user, PEOPLE[0].slug)
    assert contact.preferred_name == PEOPLE[0].slug

    mapping.apply_page(writer, user, _voyager_page([PEOPLE[0]]))

    assert (contact.first_name, contact.preferred_name) == ("Priya", "Priya")


def test_a_sync_rename_never_moves_a_real_contacts_preferred_name(
    writer: Session, user: User
) -> None:
    """Only a preferred name that was a card's default follows the first name."""
    priya = dataclasses.replace(PEOPLE[0], public_id="priya-okafor-real")
    mapping.apply_page(writer, user, _voyager_page([priya], at=NOW))
    renamed = dataclasses.replace(priya, first="Pria")

    mapping.apply_page(writer, user, _voyager_page([renamed]))

    contact = _by_slug(writer, user, "priya-okafor-real")
    assert (contact.first_name, contact.preferred_name) == ("Pria", "Priya")


def test_a_preferred_name_the_person_chose_stays(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page([_card(PEOPLE[0].slug, "Pri", "Okafor")]))
    contact = _by_slug(writer, user, PEOPLE[0].slug)
    contacts_service.update_contact(writer, user, contact.id, {"preferred_name": "Pree"})

    mapping.apply_page(writer, user, _voyager_page([PEOPLE[0]]))

    assert (contact.first_name, contact.preferred_name) == ("Priya", "Pree")


def test_a_voyager_row_whose_urn_is_another_contacts_never_merges_the_card_contact(
    writer: Session, user: User
) -> None:
    """Slug reassigned: the card showed a slug that, by the time Voyager answers,
    it reports for a person netkeeper already holds under a URN. The URN and the
    slug name two contacts: a candidate, so neither is written, the card contact
    keeps its mark and gets no URN, and both are held from aging for review."""
    x = PEOPLE[1]
    mapping.apply_page(writer, user, _voyager_page([x], at=NOW))
    mapping.apply_page(writer, user, _page([_card("shared-slug", "Someone", "Else")]))
    card_contact = _by_slug(writer, user, "shared-slug")
    x_contact = writer.scalars(scoped(user, Contact).where(Contact.li_urn == x.urn)).one()

    moved = dataclasses.replace(x, public_id="shared-slug")
    counts = mapping.apply_page(writer, user, _voyager_page([moved]))

    assert counts.needs_review == 1
    assert counts.confirmed_by_urn == 0
    assert counts.review_contact_ids == {x_contact.id, card_contact.id}
    assert (card_contact.li_urn, card_contact.first_name) == (None, "Someone")
    assert card_contact.needs_review_at is not None
    assert card_contact.merged_into_id is None
    assert x_contact.li_public_id == x.slug


def test_a_urn_reaching_a_marked_contact_by_urn_confirms_it(writer: Session, user: User) -> None:
    """However the unconfirmed contact came by the URN, a sync that sees it confirms it."""
    contact = factories.make_contact(
        writer, user, li_urn=PEOPLE[0].urn, li_public_id=PEOPLE[0].slug, needs_review_at=NOW
    )

    counts = mapping.apply_page(writer, user, _voyager_page([PEOPLE[0]]))

    assert counts.confirmed_by_urn == 1
    assert contact.needs_review_at is None


# --- never aged, never enriched ------------------------------------------------------


def test_an_unconfirmed_contact_is_never_aged_nor_counted(writer: Session, user: User) -> None:
    """Even one holding a URN: it is not a connection anything has confirmed."""
    crowd = [dataclasses.replace(PEOPLE[0], n=300 + i, public_id=f"p-{i}") for i in range(20)]
    mapping.apply_page(writer, user, _voyager_page(crowd, at=NOW))
    marked = factories.make_contact(
        writer, user, li_urn="urn:li:fsd_profile:ACoAAFAKEMARK01", needs_review_at=NOW
    )

    for _ in range(3):
        aging = mapping.age_unseen(
            writer,
            user,
            frozenset(p.urn for p in crowd[1:]),
            observed_at=LATER,
            disconnect_after_misses=2,
            created_by_sync=frozenset(),
        )
        assert aging.refused is None

    assert (marked.li_missing_count, marked.li_disconnected_at) == (0, None)


def test_the_unconfirmed_do_not_dilute_the_aging_limits(writer: Session, user: User) -> None:
    """Two real contacts, one missing: half, refused. Unconfirmed contacts with URNs
    beside them must not count toward "can age" and turn that into an aging."""
    mapping.apply_page(writer, user, _voyager_page(PEOPLE[:2], at=NOW))
    for i in range(10):
        factories.make_contact(
            writer, user, li_urn=f"urn:li:fsd_profile:ACoAAFAKEMARK{i:02d}", needs_review_at=NOW
        )

    aging = mapping.age_unseen(
        writer,
        user,
        frozenset(
            {PEOPLE[0].urn} | {f"urn:li:fsd_profile:ACoAAFAKEMARK{i:02d}" for i in range(10)}
        ),
        observed_at=LATER,
        disconnect_after_misses=2,
        created_by_sync=frozenset(),
    )

    assert aging.refused is not None
    assert "1 of 2" in aging.refused


def test_an_unconfirmed_contact_is_never_enriched_nor_pinned(writer: Session, user: User) -> None:
    account = 1
    marked = factories.make_contact(writer, user, needs_review_at=NOW)
    plain = factories.make_contact(writer, user)

    plan = enrich_plan.prioritize(writer, user, account, now=LATER, limit=10, stale_days=180)

    assert [contact_id for contact_id, _ in plan] == [plain.id]
    with pytest.raises(enrich_plan.PinError):
        enrich_plan.pin(writer, user, account, marked.id)


# --- merge ---------------------------------------------------------------------------


def test_merging_a_card_contact_into_a_real_one_confirms_and_keeps_its_text_lowest(
    writer: Session, user: User
) -> None:
    real = factories.make_contact(writer, user, headline=None, location=None, field_sources={})
    mapping.apply_page(writer, user, _page([_card("card-slug", "Priya", "Okafor", "Card text")]))
    card = _by_slug(writer, user, "card-slug")

    merge(writer, user, real.id, card.id)

    assert real.needs_review_at is None
    assert real.headline == "Card text"
    assert "headline" not in real.field_sources  # still open to every source


def test_merging_a_real_contact_into_a_card_contact_confirms_it_and_the_real_values_win(
    writer: Session, user: User
) -> None:
    """The person picked the card contact as the survivor; the real contact's name
    and headline still win, with their sync provenance, over the card's text."""
    mapping.apply_page(writer, user, _page([_card("card-slug", "Pri", "Oka", "Card headline")]))
    card = _by_slug(writer, user, "card-slug")
    real = factories.make_contact(
        writer,
        user,
        first_name="Priya",
        last_name="Okafor",
        headline="Data engineer at Fictional Robotics Co",
        field_sources={"first_name": "sync", "last_name": "sync", "headline": "sync"},
    )

    merge(writer, user, card.id, real.id)

    assert card.needs_review_at is None
    assert (card.first_name, card.last_name, card.headline, card.preferred_name) == (
        "Priya",
        "Okafor",
        "Data engineer at Fictional Robotics Co",
        "Priya",
    )
    assert {name: card.field_sources.get(name) for name in ("first_name", "headline")} == {
        "first_name": "sync",
        "headline": "sync",
    }


def test_merging_into_a_card_contact_keeps_a_field_the_person_recorded_on_it(
    writer: Session, user: User
) -> None:
    """r2c of #186: only the card's unrecorded text counts as empty. A name the
    person typed on the card contact before the merge is theirs, and it stays."""
    mapping.apply_page(writer, user, _page([_card("card-slug", "Pri", "Oka", "Card headline")]))
    card = _by_slug(writer, user, "card-slug")
    contacts_service.update_contact(writer, user, card.id, {"first_name": "Priyanka"})
    real = factories.make_contact(
        writer,
        user,
        first_name="Priya",
        last_name="Okafor",
        headline="Data engineer at Fictional Robotics Co",
        field_sources={"first_name": "sync", "last_name": "sync", "headline": "sync"},
    )

    merge(writer, user, card.id, real.id)

    assert card.needs_review_at is None
    assert (card.first_name, card.field_sources["first_name"]) == ("Priyanka", "manual")
    # The unrecorded card text still gives way to the real contact's.
    assert (card.last_name, card.field_sources["last_name"]) == ("Okafor", "sync")
    assert card.headline == "Data engineer at Fictional Robotics Co"


def test_merging_a_headless_real_contact_into_a_card_contact_drops_the_cards_headline(
    writer: Session, user: User
) -> None:
    """#186: the sync drops a card headline the URN row does not replace; so does a merge."""
    mapping.apply_page(writer, user, _page([_card("card-slug", "Pri", "Oka", "Card headline")]))
    card = _by_slug(writer, user, "card-slug")
    real = factories.make_contact(
        writer,
        user,
        first_name="Priya",
        last_name="Okafor",
        headline=None,
        field_sources={"first_name": "sync", "last_name": "sync"},
    )

    merge(writer, user, card.id, real.id)

    assert card.needs_review_at is None
    assert card.headline is None
    assert "headline" not in card.field_sources


def test_merging_into_a_card_contact_keeps_a_headline_the_person_typed(
    writer: Session, user: User
) -> None:
    mapping.apply_page(writer, user, _page([_card("card-slug", "Pri", "Oka", "Card headline")]))
    card = _by_slug(writer, user, "card-slug")
    contacts_service.update_contact(writer, user, card.id, {"headline": "Typed by hand"})
    real = factories.make_contact(
        writer, user, headline=None, field_sources={"first_name": "sync", "last_name": "sync"}
    )

    merge(writer, user, card.id, real.id)

    assert (card.headline, card.field_sources["headline"]) == ("Typed by hand", "manual")


def test_merging_two_card_contacts_confirms_neither(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page([_card("card-a"), _card("card-b")]))
    a, b = _by_slug(writer, user, "card-a"), _by_slug(writer, user, "card-b")

    merge(writer, user, a.id, b.id)

    assert a.needs_review_at == NOW


def test_merging_two_card_contacts_leaves_both_sides_fields_alone(
    writer: Session, user: User
) -> None:
    """r2d of #186: neither side is confirmed, so neither card's text counts as empty.
    The survivor keeps its own card text, unrecorded, and takes nothing from the
    loser's where it has a value; it takes the loser's card text, unrecorded too,
    only into a field it had empty."""
    mapping.apply_page(
        writer,
        user,
        _page(
            [
                _card("card-a", "Priya", "Okafor", "Headline A"),
                _card("card-b", "Pri", "Oka", "Headline B"),
            ]
        ),
    )
    a, b = _by_slug(writer, user, "card-a"), _by_slug(writer, user, "card-b")
    b.location = "Lagos"  # a card gives no location; a is left without one
    writer.flush()

    merge(writer, user, a.id, b.id)

    assert (a.first_name, a.last_name, a.headline) == ("Priya", "Okafor", "Headline A")
    assert a.location == "Lagos"
    assert not {"first_name", "last_name", "headline", "location"} & set(a.field_sources)
    # The loser keeps its own values; only its identity moved.
    assert (b.first_name, b.last_name, b.headline) == ("Pri", "Oka", "Headline B")
    assert b.li_public_id is None and b.merged_into_id == a.id


# --- confirm and reject -----------------------------------------------------------------


def test_confirm_clears_the_mark_and_is_idempotent(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page([_card("card-c")]))
    contact = _by_slug(writer, user, "card-c")

    contacts_service.confirm_contact(writer, user, contact.id)
    contacts_service.confirm_contact(writer, user, contact.id)

    assert contact.needs_review_at is None
    assert contact.archived_at is None


def test_reject_archives_keeps_the_mark_and_never_deletes(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page([_card("card-r")]))
    contact = _by_slug(writer, user, "card-r")

    contacts_service.reject_contact(writer, user, contact.id)
    first = contact.archived_at
    contacts_service.reject_contact(writer, user, contact.id)

    assert first is not None and contact.archived_at == first
    assert contact.needs_review_at is not None
    contacts_service.unarchive_contact(writer, user, contact.id)
    assert contact.needs_review_at is not None  # still unconfirmed when brought back
    assert len(_all(writer, user)) == 1


def test_reject_refuses_a_contact_that_is_not_waiting(writer: Session, user: User) -> None:
    plain = factories.make_contact(writer, user)
    with pytest.raises(contacts_service.Conflict):
        contacts_service.reject_contact(writer, user, plain.id)
    assert plain.archived_at is None


def test_confirm_and_reject_reach_only_the_users_own_live_contacts(
    writer: Session, user: User
) -> None:
    other = factories.make_user(writer)
    theirs = factories.make_contact(writer, other, needs_review_at=NOW)
    survivor = factories.make_contact(writer, user)
    gone = factories.make_contact(writer, user, needs_review_at=NOW, merged_into_id=survivor.id)
    for act in (contacts_service.confirm_contact, contacts_service.reject_contact):
        with pytest.raises(contacts_service.NotFound):
            act(writer, user, theirs.id)
        with pytest.raises(contacts_service.Merged):
            act(writer, user, gone.id)
    assert theirs.needs_review_at == NOW
    assert gone.needs_review_at == NOW
