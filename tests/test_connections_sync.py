"""netkeeper.services.connections_sync: a whole connections sync against a fake fetch.

This is P2-06's "done when", end to end: a fixture-driven full sync creates and
updates contacts; an incremental sync stops at the first known page; two
misses set ``li_disconnected_at``; a reappearance clears it. And the property
that matters most for a network nobody wants deleted: a full sync that stops
early, for any reason, ages nobody.

The fetch is :class:`voyager_pages.FakeVoyagerFetch`: invented people, served
from memory, no socket. Pages are 40 wide in production; the list here is
ten people, so the runner is driven with the list padded out where a page
boundary has to fall inside it (``_many``).
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker
from voyager_pages import (
    CHECKPOINT,
    LOGGED_OUT,
    PEOPLE,
    THROTTLED,
    UNRECOGNIZED,
    FakeVoyagerFetch,
    Person,
    Scripted,
)

from netkeeper.config import BudgetSettings, LinkedInSettings
from netkeeper.crm import apply as mapping
from netkeeper.db import session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.connections import StopReason, SyncMode, VoyagerConnections
from netkeeper.linkedin.pacing import human_delay
from netkeeper.models import Contact, LinkedInAccount, User
from netkeeper.scoping import scoped
from netkeeper.services import budgets
from netkeeper.services import heat as heat_service
from netkeeper.services.budgets import ActionClass
from netkeeper.services.connections_sync import (
    HeatSkipped,
    SessionFlagged,
    SyncRunReport,
    sync_connections,
)
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import clear_session_flag, session_flag
from netkeeper.services.pacing import profiles

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
SETTINGS = LinkedInSettings()


@pytest.fixture
def user_id(session_factory: sessionmaker[Session]) -> int:
    with session_scope(session_factory, write=True) as session:
        return factories.make_user(session).id


def _new_user(factory: sessionmaker[Session]) -> int:
    with session_scope(factory, write=True) as session:
        return factories.make_user(session).id


def _many(count: int) -> list[Person]:
    """``count`` invented people, the ten named ones first, the rest numbered."""
    extra = [
        Person(200 + i, f"Given{i}", f"Family{i}", f"Role {i} at Invented Firm {i % 7}")
        for i in range(max(count - len(PEOPLE), 0))
    ]
    return [*PEOPLE, *extra][:count]


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


async def _sync(
    factory: sessionmaker[Session],
    user_id: int,
    fetch: FakeVoyagerFetch,
    mode: SyncMode = SyncMode.FULL,
    *,
    settings: LinkedInSettings = SETTINGS,
    at: datetime = NOW,
    sleeps: Sleeps | None = None,
    rng: random.Random | None = None,
) -> SyncRunReport:
    return await sync_connections(
        factory,
        user_id,
        mode,
        VoyagerConnections(fetch),
        settings=settings,
        clock=Clock(at),
        sleep=sleeps or Sleeps(),
        rng=rng,
    )


def _contacts(factory: sessionmaker[Session], user_id: int) -> dict[str, Contact]:
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        rows = session.scalars(scoped(user, Contact)).all()
        session.expunge_all()
    return {row.li_urn: row for row in rows if row.li_urn is not None}


def _spent(factory: sessionmaker[Session], user_id: int, account_id: int, at: datetime) -> int:
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        return budgets.status(
            session,
            user,
            account_id,
            ActionClass.CONNECTION_PAGES,
            now=at,
            settings=SETTINGS.budget,
        ).day.count


# --- done when: a fixture-driven full sync creates and updates contacts --------------


async def test_a_full_sync_creates_every_connection(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    people = _many(45)  # two pages at 40
    fetch = FakeVoyagerFetch(people)

    report = await _sync(session_factory, user_id, fetch)

    assert fetch.starts == [0, 40]
    assert report.result.reason is StopReason.END_OF_LIST and report.result.complete
    assert (report.pages.seen, report.pages.created, report.pages.updated) == (45, 45, 0)
    contacts = _contacts(session_factory, user_id)
    assert set(contacts) == {p.urn for p in people}
    assert contacts[PEOPLE[1].urn].headline == "Head of design at Acme Testing Group"
    assert report.aging is not None and report.aging.missed == 0


async def test_a_second_full_sync_updates_instead_of_duplicating(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    people = list(PEOPLE)
    await _sync(session_factory, user_id, FakeVoyagerFetch(people))
    people[4] = replace(people[4], headline="Chief of staff at Imaginary Analytics")

    report = await _sync(
        session_factory, user_id, FakeVoyagerFetch(people), at=NOW + timedelta(days=7)
    )

    assert (report.pages.created, report.pages.updated) == (0, 10)
    contacts = _contacts(session_factory, user_id)
    assert len(contacts) == 10
    assert contacts[PEOPLE[4].urn].headline == "Chief of staff at Imaginary Analytics"


async def test_the_run_creates_the_users_account_and_keys_the_budget_by_it(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    fetch = FakeVoyagerFetch(_many(85))  # three pages

    report = await _sync(session_factory, user_id, fetch)

    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        accounts = session.scalars(scoped(user, LinkedInAccount)).all()
        assert [a.id for a in accounts] == [report.account_id]
    assert _spent(session_factory, user_id, report.account_id, NOW) == 3


# --- done when: incremental stops at the first known page -------------------------------


async def test_incremental_stops_at_the_first_page_of_known_connections(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    people = _many(120)
    await _sync(session_factory, user_id, FakeVoyagerFetch(people[3:]))  # the 117 older ones
    newest_first = FakeVoyagerFetch(people)  # three new people on top

    report = await _sync(
        session_factory, user_id, newest_first, SyncMode.INCREMENTAL, at=NOW + timedelta(days=1)
    )

    # Page 0 carries the three new people; page 1 (offsets 40-79) is all known.
    assert newest_first.starts == [0, 40]
    assert report.result.reason is StopReason.CAUGHT_UP
    assert report.pages.created == 3
    assert report.aging is None
    assert set(_contacts(session_factory, user_id)) == {p.urn for p in people}


async def test_incremental_ages_nobody_even_when_someone_is_gone(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    people = list(PEOPLE)
    await _sync(session_factory, user_id, FakeVoyagerFetch(people))
    gone = people.pop(5)

    for day in (1, 2, 3):
        await _sync(
            session_factory,
            user_id,
            FakeVoyagerFetch(people),
            SyncMode.INCREMENTAL,
            at=NOW + timedelta(days=day),
        )

    contact = _contacts(session_factory, user_id)[gone.urn]
    assert (contact.li_missing_count, contact.li_disconnected_at) == (0, None)


# --- done when: two misses set li_disconnected_at; a reappearance clears it -------------


async def test_two_full_sync_misses_disconnect_and_a_reappearance_reconnects(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    people = list(PEOPLE)
    await _sync(session_factory, user_id, FakeVoyagerFetch(people))
    gone = people.pop(3)
    week1, week2, week3 = (NOW + timedelta(days=7 * n) for n in (1, 2, 3))

    await _sync(session_factory, user_id, FakeVoyagerFetch(people), at=week1)
    after_one = _contacts(session_factory, user_id)[gone.urn]
    assert (after_one.li_missing_count, after_one.li_disconnected_at) == (1, None)

    report = await _sync(session_factory, user_id, FakeVoyagerFetch(people), at=week2)
    after_two = _contacts(session_factory, user_id)[gone.urn]
    assert after_two.li_missing_count == 2
    assert after_two.li_disconnected_at == week2
    assert report.aging is not None and report.aging.disconnected == 1
    assert len(_contacts(session_factory, user_id)) == 10  # nothing deleted

    people.insert(0, gone)  # they reconnect: newest first
    report = await _sync(
        session_factory, user_id, FakeVoyagerFetch(people), SyncMode.INCREMENTAL, at=week3
    )
    back = _contacts(session_factory, user_id)[gone.urn]
    assert (back.li_missing_count, back.li_disconnected_at) == (0, None)
    assert report.pages.reconnected == 1


# --- an aborted full sync ages nobody ----------------------------------------------------


async def _one_miss_already(factory: sessionmaker[Session], user_id: int) -> Person:
    """Everyone synced, then one full sync without ``gone``: they sit at one miss."""
    people = _many(100)
    await _sync(factory, user_id, FakeVoyagerFetch(people))
    gone = people[-1]
    await _sync(factory, user_id, FakeVoyagerFetch(people[:-1]), at=NOW + timedelta(days=7))
    assert _contacts(factory, user_id)[gone.urn].li_missing_count == 1
    return gone


@pytest.mark.parametrize(
    "scripted",
    [
        pytest.param(THROTTLED, id="throttled"),
        pytest.param(CHECKPOINT, id="checkpoint"),
        pytest.param(LOGGED_OUT, id="logged-out"),
        pytest.param(UNRECOGNIZED, id="route-changed"),
        pytest.param(Scripted(404, "{}"), id="not-found"),
    ],
)
async def test_a_full_sync_stopped_by_a_response_ages_nobody(
    session_factory: sessionmaker[Session], user_id: int, scripted: Scripted
) -> None:
    """The last page fails: 80 of the 99 seen, and the other 19 are not gone."""
    gone = await _one_miss_already(session_factory, user_id)
    people = _many(99)
    fetch = FakeVoyagerFetch(people, script={2: scripted})

    report = await _sync(session_factory, user_id, fetch, at=NOW + timedelta(days=14))

    assert fetch.starts == [0, 40, 80]
    assert not report.result.complete
    assert report.aging is None
    contacts = _contacts(session_factory, user_id)
    assert (contacts[gone.urn].li_missing_count, contacts[gone.urn].li_disconnected_at) == (1, None)
    assert all(contacts[p.urn].li_missing_count == 0 for p in people[80:])


async def test_a_full_sync_stopped_by_the_budget_ages_nobody(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    gone = await _one_miss_already(session_factory, user_id)
    tight = replace(SETTINGS, budget=BudgetSettings(connection_pages_per_day=2))
    fetch = FakeVoyagerFetch(_many(99))

    report = await _sync(
        session_factory, user_id, fetch, settings=tight, at=NOW + timedelta(days=21)
    )

    assert fetch.starts == [0, 40]
    assert report.result.reason is StopReason.PAGE_BUDGET
    assert report.aging is None
    assert _contacts(session_factory, user_id)[gone.urn].li_disconnected_at is None


async def test_a_full_sync_whose_mapping_fails_ages_nobody(
    session_factory: sessionmaker[Session], user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = await _one_miss_already(session_factory, user_id)
    real = mapping.apply_page
    calls = 0

    def failing(*args: object, **kwargs: object) -> mapping.PageCounts:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("disk full")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mapping, "apply_page", failing)
    with pytest.raises(RuntimeError, match="disk full"):
        await _sync(
            session_factory, user_id, FakeVoyagerFetch(_many(99)), at=NOW + timedelta(days=14)
        )

    assert _contacts(session_factory, user_id)[gone.urn].li_disconnected_at is None


async def test_the_same_complete_sync_does_age_them(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """The control for the three above: nothing fails, and the second miss lands."""
    gone = await _one_miss_already(session_factory, user_id)

    await _sync(session_factory, user_id, FakeVoyagerFetch(_many(99)), at=NOW + timedelta(days=14))

    assert _contacts(session_factory, user_id)[gone.urn].li_disconnected_at is not None


# --- what the stopping response does ----------------------------------------------------


@pytest.mark.parametrize(
    ("scripted", "heat", "flag"),
    [
        pytest.param(THROTTLED, True, None, id="throttled"),
        pytest.param(CHECKPOINT, True, Outcome.CHECKPOINT, id="checkpoint"),
        pytest.param(LOGGED_OUT, False, Outcome.LOGGED_OUT, id="logged-out"),
        pytest.param(UNRECOGNIZED, False, None, id="route-changed"),
    ],
)
async def test_the_stopping_response_raises_heat_and_the_flag_as_spec_9_7_says(
    session_factory: sessionmaker[Session],
    user_id: int,
    scripted: Scripted,
    heat: bool,
    flag: Outcome | None,
) -> None:
    fetch = FakeVoyagerFetch(list(PEOPLE), script={0: scripted})

    report = await _sync(session_factory, user_id, fetch)

    assert len(fetch.requests) == 1  # never retried
    assert (report.heat_raised, report.session_flagged) == (heat, flag is not None)
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        score = heat_service.read(session, user, report.account_id, now=NOW, settings=SETTINGS.heat)
        stored = session_flag(session, user)
    assert (score > 0) is heat
    assert (stored.outcome if stored is not None else None) is flag
    if stored is not None:
        # The path only: the ctx token in the checkpoint url is never stored.
        expected = {
            Outcome.CHECKPOINT: "/checkpoint/challenge/AgFAKE",
            Outcome.LOGGED_OUT: "/authwall",
        }
        assert stored.url == expected[stored.outcome]


async def test_heat_over_the_skip_threshold_stops_the_run_before_anything_is_fetched(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    for _ in range(3):  # three throttles: 3.0 against a threshold of 2.5
        await _sync(session_factory, user_id, FakeVoyagerFetch(list(PEOPLE), script={0: THROTTLED}))
    fetch = FakeVoyagerFetch(list(PEOPLE))

    with pytest.raises(HeatSkipped):
        await _sync(session_factory, user_id, fetch)

    assert fetch.requests == []


async def test_warm_heat_shrinks_the_run_and_stretches_the_pauses(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """One throttle: multiplier 2.0, so the 4 pages left of the day's 5 become 2."""
    settings = replace(SETTINGS, budget=BudgetSettings(connection_pages_per_day=5))
    await _sync(
        session_factory,
        user_id,
        FakeVoyagerFetch(_many(400), script={0: THROTTLED}),
        settings=settings,
    )
    cold, warm = Sleeps(), Sleeps()
    other = _new_user(session_factory)

    warm_fetch = FakeVoyagerFetch(_many(400))
    await _sync(
        session_factory,
        user_id,
        warm_fetch,
        settings=settings,
        sleeps=warm,
        rng=random.Random(7),
    )
    cold_fetch = FakeVoyagerFetch(_many(400))
    await _sync(
        session_factory,
        other,
        cold_fetch,
        settings=settings,
        sleeps=cold,
        rng=random.Random(7),
    )

    # Cold: 5 left, 5 pages. Warm: 4 left (one spent on the throttle) at 2.0: 2 pages.
    assert len(cold_fetch.requests) == 5
    assert len(warm_fetch.requests) == 2
    assert len(cold.waits) == 4 and len(warm.waits) == 1
    # Same seed, same first draw: the warm wait is that draw at twice the median.
    delay = profiles(settings.pacing).delay

    def first_wait(median: float) -> float:
        return human_delay(
            random.Random(7),
            median=median,
            sigma=delay.sigma,
            tail_p=delay.tail_p,
            tail_range=delay.tail_range,
        )

    assert cold.waits[0] == pytest.approx(first_wait(delay.median))
    assert warm.waits[0] == pytest.approx(first_wait(delay.median * 2.0))
    assert warm.waits[0] > cold.waits[0]


async def test_pages_are_paced_with_human_like_waits(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    sleeps = Sleeps()
    await _sync(session_factory, user_id, FakeVoyagerFetch(_many(150)), sleeps=sleeps)
    assert len(sleeps.waits) == 3  # four pages (40, 40, 40, 30), a wait between each pair
    assert all(wait > 0 for wait in sleeps.waits)
    assert len(set(sleeps.waits)) == 3  # drawn, not a fixed interval


async def test_a_spent_budget_fetches_nothing(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    settings = replace(SETTINGS, budget=BudgetSettings(connection_pages_per_day=2))
    await _sync(session_factory, user_id, FakeVoyagerFetch(_many(200)), settings=settings)
    fetch = FakeVoyagerFetch(_many(200))

    report = await _sync(session_factory, user_id, fetch, settings=settings)

    assert fetch.requests == []
    assert report.result.reason is StopReason.PAGE_BUDGET


async def test_the_budget_is_spent_before_the_fetch_not_after(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """A page that fails still cost a unit: the unit is the attempt (spec 9.6)."""
    fetch = FakeVoyagerFetch(_many(100), script={1: THROTTLED})
    report = await _sync(session_factory, user_id, fetch)
    assert _spent(session_factory, user_id, report.account_id, NOW) == 2


async def test_budget_spent_elsewhere_mid_run_stops_the_next_page(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """The per-page gate, not only the up-front page budget, enforces the day's limit.

    Another run spends the rest of the day's pages while this one pauses; the
    next page is refused before it is fetched.
    """
    settings = replace(SETTINGS, budget=BudgetSettings(connection_pages_per_day=5))

    class SpendingElsewhere(Sleeps):
        async def __call__(self, seconds: float) -> None:
            await super().__call__(seconds)
            with session_scope(session_factory, write=True) as session:
                user = session.get(User, user_id)
                assert user is not None
                for _ in range(5):
                    try:
                        budgets.consume(
                            session,
                            user,
                            report_account,
                            ActionClass.CONNECTION_PAGES,
                            now=NOW,
                            settings=settings.budget,
                        )
                    except budgets.BudgetExceeded:
                        break

    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        report_account = ensure_account(session, user).id
    fetch = FakeVoyagerFetch(_many(400))

    report = await _sync(
        session_factory, user_id, fetch, settings=settings, sleeps=SpendingElsewhere()
    )

    assert len(fetch.requests) == 1
    assert report.result.reason is StopReason.BUDGET
    assert report.aging is None


# --- a lying paging.total ages nobody it did not see -----------------------------------


@pytest.mark.parametrize("lie", [0, 40], ids=["total-0", "total-40"])
async def test_a_lying_total_never_disconnects_people_who_are_still_there(
    session_factory: sessionmaker[Session], user_id: int, lie: int
) -> None:
    """The review's attack: 100 people served honestly under a false total, twice."""
    people = _many(100)
    await _sync(session_factory, user_id, FakeVoyagerFetch(people))

    for week in (1, 2):
        await _sync(
            session_factory,
            user_id,
            FakeVoyagerFetch(people, total=lie),
            at=NOW + timedelta(days=7 * week),
        )

    contacts = _contacts(session_factory, user_id)
    assert all(c.li_disconnected_at is None for c in contacts.values())
    assert all(c.li_missing_count == 0 for c in contacts.values())


async def test_a_urn_scheme_change_disconnects_nobody(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """The review's attack: every profile comes back under a new URN, same slug.

    Each row resolves to a candidate (the slug's contact carries another URN),
    nothing is written, and every stored contact looks missing. Aging refuses.
    """
    people = _many(100)
    await _sync(session_factory, user_id, FakeVoyagerFetch(people))
    renumbered = [replace(p, urn_prefix="ACoAANEW") for p in people]

    reports = [
        await _sync(
            session_factory,
            user_id,
            FakeVoyagerFetch(renumbered),
            at=NOW + timedelta(days=7 * week),
        )
        for week in (1, 2)
    ]

    assert all(r.pages.needs_review == 100 for r in reports)
    assert all(r.aging is not None and r.aging.refused is not None for r in reports)
    contacts = _contacts(session_factory, user_id)
    assert len(contacts) == 100
    assert all((c.li_missing_count, c.li_disconnected_at) == (0, None) for c in contacts.values())


@pytest.mark.parametrize("scripted", [CHECKPOINT, LOGGED_OUT], ids=["checkpoint", "logged-out"])
async def test_no_run_starts_while_the_session_is_flagged(
    session_factory: sessionmaker[Session], user_id: int, scripted: Scripted
) -> None:
    """The review's attack: a run after a checkpoint fetched anyway, a retry one run later."""
    await _sync(session_factory, user_id, FakeVoyagerFetch(list(PEOPLE), script={0: scripted}))
    later = FakeVoyagerFetch(list(PEOPLE))

    with pytest.raises(SessionFlagged):
        await _sync(session_factory, user_id, later, at=NOW + timedelta(days=1))
    assert later.requests == []

    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        assert clear_session_flag(session, user)
    await _sync(session_factory, user_id, later, at=NOW + timedelta(days=2))
    assert later.requests != []


async def test_a_partial_urn_change_disconnects_nobody_it_holds_for_review(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """The re-review's attack: 16 of 200 (8%) come back under a new URN, same slug.

    Each of the 16 resolves to a candidate: under both limits, so aging runs.
    They are still connections, and they are the rows waiting for a person.
    """
    people = _many(200)
    await _sync(session_factory, user_id, FakeVoyagerFetch(people))
    changed = {p.urn for p in people[100:116]}
    renumbered = [replace(p, urn_prefix="ACoAANEW") if p.urn in changed else p for p in people]

    reports = [
        await _sync(
            session_factory,
            user_id,
            FakeVoyagerFetch(renumbered),
            at=NOW + timedelta(days=7 * week),
        )
        for week in (1, 2)
    ]

    assert all(r.pages.needs_review == 16 for r in reports)
    assert all(r.aging is not None and r.aging.refused is None for r in reports)
    assert all(r.aging is not None and r.aging.missed == 0 for r in reports)
    contacts = _contacts(session_factory, user_id)
    assert all((contacts[urn].li_missing_count, contacts[urn].li_disconnected_at) == (0, None)
               for urn in changed)  # fmt: skip
