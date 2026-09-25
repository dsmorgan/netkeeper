"""#187's done-when, end to end: a sync read from what the page loads, written to contacts.

:func:`netkeeper.services.connections_sync.sync_connections` -- budgets, heat, the run
row, the mapping, and aging -- over :class:`~netkeeper.linkedin.page_connections.PageConnections`
reading :mod:`flagship_site`'s fake connections page through a real ``BrowserRun``.
Invented people only; nothing opens a socket.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime

import factories
import pytest
from flagship_pages import CardOptions
from flagship_site import FlagshipSite
from run_fakes import fake_provider
from sqlalchemy.orm import Session, sessionmaker
from voyager_pages import PEOPLE, Person

from netkeeper.config import LinkedInSettings
from netkeeper.db import session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.connections import StopReason, SyncMode
from netkeeper.linkedin.page_connections import PageConnections
from netkeeper.models import Contact, ContactSource, SyncRun, SyncRunStatus, User
from netkeeper.scoping import scoped
from netkeeper.services.connections_sync import SyncRunReport, sync_connections

NOW = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)
SETTINGS = LinkedInSettings()


def many(count: int) -> list[Person]:
    extra = [
        Person(400 + i, f"Given{i}", f"Family{i}", f"Role {i} at Invented Firm {i % 5}")
        for i in range(max(count - len(PEOPLE), 0))
    ]
    return [*PEOPLE, *extra][:count]


async def no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def user_id(session_factory: sessionmaker[Session]) -> int:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        # West of Greenwich: a "Connected on" day read as a midnight-UTC instant would
        # land on the day before here.
        user.timezone = "America/Los_Angeles"
        return user.id


async def _sync(
    factory: sessionmaker[Session],
    user_id: int,
    site: FlagshipSite,
    mode: SyncMode = SyncMode.FULL,
) -> SyncRunReport:
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        source = PageConnections(
            run,
            require_newest_first=mode is SyncMode.INCREMENTAL,
            rng=random.Random(3),
            sleep=no_sleep,
            response_wait_s=0.01,
            landing_wait_s=0.05,
        )
        return await sync_connections(
            factory,
            user_id,
            mode,
            source,
            settings=SETTINGS,
            clock=lambda: NOW,
            sleep=no_sleep,
            rng=random.Random(0),
        )


def _contacts(factory: sessionmaker[Session], user_id: int) -> dict[str, Contact]:
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        rows = session.scalars(scoped(user, Contact)).all()
        session.expunge_all()
    return {row.li_public_id or "": row for row in rows}


async def test_a_sync_creates_contacts_with_urn_slug_name_headline_and_connected_on(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    people = many(25)
    report = await _sync(session_factory, user_id, FlagshipSite(people))

    assert report.result.reason is StopReason.END_OF_LIST and report.result.complete
    assert (report.pages.created, report.pages.seen) == (25, 25)
    contacts = _contacts(session_factory, user_id)
    priya = contacts[PEOPLE[0].slug]
    assert priya.li_urn == "urn:li:fsd_profile:ACoAAFAKE0000101"
    assert (priya.first_name, priya.last_name) == ("Priya", "Okafor")
    assert priya.headline == "Data engineer at Fictional Robotics Co"
    # The card says "Connected on November 14, 2023"; that day, not the day before.
    assert priya.connected_on == date(2023, 11, 14)
    assert priya.source is ContactSource.SYNC
    assert contacts[PEOPLE[2].slug].headline is None
    assert contacts[PEOPLE[7].slug].connected_on is None


async def test_an_incremental_sync_stops_at_the_first_page_of_known_urns(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    people = many(120)
    await _sync(session_factory, user_id, FlagshipSite(people))
    newcomers = [Person(900 + i, f"New{i}", f"Person{i}", None) for i in range(5)]
    site = FlagshipSite([*newcomers, *people])

    report = await _sync(session_factory, user_id, site, SyncMode.INCREMENTAL)

    assert report.result.reason is StopReason.CAUGHT_UP
    assert report.result.pages == 2  # 5 new and 35 known, then 40 known
    assert report.pages.created == 5
    assert report.aging is None  # an incremental sync never ages anyone
    assert len(_contacts(session_factory, user_id)) == 125


async def test_an_empty_page_ends_the_list_and_only_then_does_anyone_age(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    people = many(35)
    first = await _sync(session_factory, user_id, FlagshipSite(people))
    assert first.result.complete and first.aging is not None and first.aging.missed == 0

    gone = people[12]
    remaining = [p for p in people if p is not gone]
    for _ in range(2):
        report = await _sync(session_factory, user_id, FlagshipSite(remaining))
        assert report.result.reason is StopReason.END_OF_LIST and report.result.complete
    contacts = _contacts(session_factory, user_id)
    assert contacts[gone.slug].li_disconnected_at is not None
    assert all(contacts[p.slug].li_disconnected_at is None for p in remaining)


async def test_a_run_that_could_not_prove_the_end_ages_nobody(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    people = many(35)
    await _sync(session_factory, user_id, FlagshipSite(people))
    for _ in range(3):
        report = await _sync(session_factory, user_id, FlagshipSite(people[:30], end="stall"))
        assert not report.result.complete and report.aging is None
    assert all(c.li_missing_count == 0 for c in _contacts(session_factory, user_id).values())


async def test_a_changed_payload_stops_the_run_and_writes_no_part_of_its_page(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    people = many(70)
    site = FlagshipSite(people, card_options={52: CardOptions(key_slug="not-the-card-fake-0")})

    report = await _sync(session_factory, user_id, site)

    assert report.result.reason is StopReason.RESPONSE
    assert report.result.outcome is Outcome.ROUTE_CHANGED and report.aging is None
    assert set(_contacts(session_factory, user_id)) == {p.slug for p in people[:40]}
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        (run,) = session.scalars(scoped(user, SyncRun)).all()
        assert (run.status, run.stop_reason) == (SyncRunStatus.ABORTED, "route_changed")


async def test_a_name_an_import_split_elsewhere_is_not_rewritten(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """The card says "Mary Ann Smith"; the archive said "Mary Ann" / "Smith". Same name,
    so the archive's split stays. A name that really changed is written."""
    mary = Person(501, "Mary", "Ann Smith", "Counsel at Invented Firm", public_id="mary-fake-0501")
    renamed = Person(502, "Jo", "Newname", None, public_id="jo-fake-0502")
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        for slug, first, last in (
            ("mary-fake-0501", "Mary Ann", "Smith"),
            ("jo-fake-0502", "Jo", "Oldname"),
        ):
            factories.make_contact(
                session,
                user,
                li_urn=None,
                li_public_id=slug,
                first_name=first,
                last_name=last,
                source=ContactSource.ARCHIVE,
                field_sources={"first_name": "archive", "last_name": "archive"},
            )

    await _sync(session_factory, user_id, FlagshipSite([mary, renamed]))

    contacts = _contacts(session_factory, user_id)
    assert (contacts["mary-fake-0501"].first_name, contacts["mary-fake-0501"].last_name) == (
        "Mary Ann",
        "Smith",
    )
    assert contacts["mary-fake-0501"].li_urn == mary.urn
    assert contacts["jo-fake-0502"].last_name == "Newname"
