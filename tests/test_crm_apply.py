"""netkeeper.crm.apply: connections pages onto contacts, and the edge lifecycle (spec 9.8, 9.10).

Pages here are built directly from :data:`voyager_pages.PEOPLE`, invented
people at invented companies. The runner that feeds these functions from a
real job is exercised end to end in ``tests/test_connections_sync.py``.
"""

from __future__ import annotations

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


def _by_urn(session: Session, user: User, person: Person) -> Contact:
    return session.scalars(scoped(user, Contact).where(Contact.li_urn == person.urn)).one()


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
    mapping.apply_page(writer, user, _page(PEOPLE[:2]))
    for week in (1, 2, 3):
        _age(writer, user, PEOPLE[:1], at=NOW + timedelta(days=7 * week))
    mateo = _by_urn(writer, user, PEOPLE[1])
    assert mateo.li_missing_count == 3
    assert mateo.li_disconnected_at == NOW + timedelta(days=14)


def test_a_reappearance_clears_both(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page(PEOPLE[:4]))
    _age(writer, user, PEOPLE[:3])
    _age(writer, user, PEOPLE[:3])
    assert _by_urn(writer, user, PEOPLE[3]).li_disconnected_at is not None

    counts = mapping.apply_page(writer, user, _page(PEOPLE[3:4], at=LATER))

    tomasz = _by_urn(writer, user, PEOPLE[3])
    assert (tomasz.li_missing_count, tomasz.li_disconnected_at) == (0, None)
    assert counts.reconnected == 1


def test_a_sighting_between_misses_restarts_the_count(writer: Session, user: User) -> None:
    """Consecutive means consecutive: seen once in between, and two misses are one each."""
    mapping.apply_page(writer, user, _page(PEOPLE[:2]))
    _age(writer, user, PEOPLE[:1])
    mapping.apply_page(writer, user, _page(PEOPLE[1:2], mode=SyncMode.INCREMENTAL, at=LATER))
    _age(writer, user, PEOPLE[:1])
    mateo = _by_urn(writer, user, PEOPLE[1])
    assert (mateo.li_missing_count, mateo.li_disconnected_at) == (1, None)


def test_the_threshold_comes_from_the_caller(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page(PEOPLE[:2]))
    for _ in range(2):
        mapping.age_unseen(
            writer,
            user,
            frozenset({PEOPLE[0].urn}),
            observed_at=LATER,
            disconnect_after_misses=3,
        )
    assert _by_urn(writer, user, PEOPLE[1]).li_disconnected_at is None


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
    mapping.apply_page(writer, other, _page(PEOPLE[:2]))
    _age(writer, user, PEOPLE[:1])
    _age(writer, user, PEOPLE[:1])
    assert _by_urn(writer, other, PEOPLE[1]).li_missing_count == 0


def test_a_sync_that_saw_nobody_ages_nobody(writer: Session, user: User) -> None:
    mapping.apply_page(writer, user, _page(PEOPLE[:3]))

    aging = mapping.age_unseen(
        writer, user, frozenset(), observed_at=LATER, disconnect_after_misses=1
    )

    assert aging.refused is not None
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
        disconnect_after_misses=2,
    )  # fmt: skip
    assert (ok.missed, ok.refused) == (20, None)

    refused = mapping.age_unseen(
        writer, user, frozenset(p.urn for p in crowd[21:]), observed_at=LATER,
        disconnect_after_misses=2,
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
            writer, other, frozenset({stranger}), observed_at=LATER, disconnect_after_misses=1
        )
        assert aging.refused is not None and f"all {size}" in aging.refused, size
        assert _missing(writer, other, crowd) == [0] * size


def test_more_than_half_of_a_small_network_is_refused_and_half_is_not(
    writer: Session, user: User
) -> None:
    crowd = _crowd(10)
    mapping.apply_page(writer, user, _page(crowd))

    six = _age_all_but(writer, user, crowd, 6)
    assert six.refused is not None and "more than half" in six.refused
    assert _missing(writer, user, crowd) == [0] * 10

    five = _age_all_but(writer, user, crowd, 5)
    assert (five.refused, five.missed) == (None, 5)


def test_twenty_contacts_may_lose_ten_but_not_eleven(writer: Session, user: User) -> None:
    """At 20 the floor (10) and the half rule (10) meet; 11 is refused."""
    crowd = _crowd(20)
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
    )

    assert aging.refused is not None and "match no contact" in aging.refused
    assert _missing(writer, user, crowd[:5]) == [0] * 5
