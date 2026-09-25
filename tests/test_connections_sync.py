"""netkeeper.services.connections_sync: a whole connections sync against a fake source.

This is P2-06's "done when", end to end: a fixture-driven full sync creates and
updates contacts; an incremental sync stops at the first known page; two
misses set ``li_disconnected_at``; a reappearance clears it. And the property
that matters most for a network nobody wants deleted: a full sync that stops
early, for any reason, ages nobody.

The source is :class:`voyager_pages.FakeConnectionsSource`: invented people, served
from memory, no socket, no request shape of its own (#189 item 4 -- this replaced
driving these tests through ``VoyagerConnections`` over a fake Voyager transport,
retired along with the in-page connections endpoint it read). Pages are 40 wide in
production; the list here is ten people, so the runner is driven with the list
padded out where a page boundary has to fall inside it (``_many``).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
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
    FakeConnectionsSource,
    Person,
    Scripted,
)

from netkeeper.config import BudgetSettings, LinkedInSettings, Settings
from netkeeper.crm import apply as mapping
from netkeeper.db import session_scope
from netkeeper.linkedin.browser import BrowserUnavailable
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.connections import (
    AnswerLost,
    ConnectionsPage,
    ConnectionsSource,
    LostAnswer,
    SourcePage,
    StopReason,
    SyncMode,
    SyncResult,
)
from netkeeper.linkedin.observe import ObservationFailed
from netkeeper.linkedin.pacing import human_delay
from netkeeper.linkedin.voyager import ConnectionSummary
from netkeeper.models import (
    Contact,
    ContactAlias,
    LinkedInAccount,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    User,
)
from netkeeper.scoping import scoped
from netkeeper.services import budgets, route_breaker, runs
from netkeeper.services import heat as heat_service
from netkeeper.services.budgets import ActionClass
from netkeeper.services.connections_sync import (
    CANCEL_SLICE_S,
    HeatSkipped,
    SessionFlagged,
    SyncRunReport,
    sync_connections,
)
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import clear_session_flag, session_flag
from netkeeper.services.pacing import profiles
from netkeeper.services.posture import posture

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
    """Records every sleep. The wait between two pages is sliced (``CANCEL_SLICE_S``)
    so a cancel lands inside it; :attr:`gaps` puts each wait back together."""

    def __init__(self) -> None:
        self.slices: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slices.append(seconds)

    @property
    def waits(self) -> list[float]:
        """The waits between pages: runs of full slices ended by a shorter one."""
        gaps: list[float] = []
        current = 0.0
        for piece in self.slices:
            current += piece
            if piece < CANCEL_SLICE_S:
                gaps.append(current)
                current = 0.0
        if current:
            gaps.append(current)
        return gaps


async def _sync(
    factory: sessionmaker[Session],
    user_id: int,
    fetch: ConnectionsSource,
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
        fetch,
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
    fetch = FakeConnectionsSource(people)

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
    await _sync(session_factory, user_id, FakeConnectionsSource(people))
    people[4] = replace(people[4], headline="Chief of staff at Imaginary Analytics")

    report = await _sync(
        session_factory, user_id, FakeConnectionsSource(people), at=NOW + timedelta(days=7)
    )

    assert (report.pages.created, report.pages.updated) == (0, 10)
    contacts = _contacts(session_factory, user_id)
    assert len(contacts) == 10
    assert contacts[PEOPLE[4].urn].headline == "Chief of staff at Imaginary Analytics"


async def test_the_run_creates_the_users_account_and_keys_the_budget_by_it(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    fetch = FakeConnectionsSource(_many(85))  # three pages

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
    await _sync(session_factory, user_id, FakeConnectionsSource(people[3:]))  # the 117 older ones
    newest_first = FakeConnectionsSource(people)  # three new people on top

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
    await _sync(session_factory, user_id, FakeConnectionsSource(people))
    gone = people.pop(5)

    for day in (1, 2, 3):
        await _sync(
            session_factory,
            user_id,
            FakeConnectionsSource(people),
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
    await _sync(session_factory, user_id, FakeConnectionsSource(people))
    gone = people.pop(3)
    week1, week2, week3 = (NOW + timedelta(days=7 * n) for n in (1, 2, 3))

    await _sync(session_factory, user_id, FakeConnectionsSource(people), at=week1)
    after_one = _contacts(session_factory, user_id)[gone.urn]
    assert (after_one.li_missing_count, after_one.li_disconnected_at) == (1, None)

    report = await _sync(session_factory, user_id, FakeConnectionsSource(people), at=week2)
    after_two = _contacts(session_factory, user_id)[gone.urn]
    assert after_two.li_missing_count == 2
    assert after_two.li_disconnected_at == week2
    assert report.aging is not None and report.aging.disconnected == 1
    assert len(_contacts(session_factory, user_id)) == 10  # nothing deleted

    people.insert(0, gone)  # they reconnect: newest first
    report = await _sync(
        session_factory, user_id, FakeConnectionsSource(people), SyncMode.INCREMENTAL, at=week3
    )
    back = _contacts(session_factory, user_id)[gone.urn]
    assert (back.li_missing_count, back.li_disconnected_at) == (0, None)
    assert report.pages.reconnected == 1


# --- an aborted full sync ages nobody ----------------------------------------------------


async def _one_miss_already(factory: sessionmaker[Session], user_id: int) -> Person:
    """Everyone synced, then one full sync without ``gone``: they sit at one miss."""
    people = _many(100)
    await _sync(factory, user_id, FakeConnectionsSource(people))
    gone = people[-1]
    await _sync(factory, user_id, FakeConnectionsSource(people[:-1]), at=NOW + timedelta(days=7))
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
    fetch = FakeConnectionsSource(people, script={2: scripted})

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
    fetch = FakeConnectionsSource(_many(99))

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
            session_factory, user_id, FakeConnectionsSource(_many(99)), at=NOW + timedelta(days=14)
        )

    assert _contacts(session_factory, user_id)[gone.urn].li_disconnected_at is None


async def test_the_same_complete_sync_does_age_them(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """The control for the three above: nothing fails, and the second miss lands."""
    gone = await _one_miss_already(session_factory, user_id)

    await _sync(
        session_factory, user_id, FakeConnectionsSource(_many(99)), at=NOW + timedelta(days=14)
    )

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
    fetch = FakeConnectionsSource(list(PEOPLE), script={0: scripted})

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
        await _sync(
            session_factory, user_id, FakeConnectionsSource(list(PEOPLE), script={0: THROTTLED})
        )
    fetch = FakeConnectionsSource(list(PEOPLE))

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
        FakeConnectionsSource(_many(400), script={0: THROTTLED}),
        settings=settings,
    )
    cold, warm = Sleeps(), Sleeps()
    other = _new_user(session_factory)

    warm_fetch = FakeConnectionsSource(_many(400))
    await _sync(
        session_factory,
        user_id,
        warm_fetch,
        settings=settings,
        sleeps=warm,
        rng=random.Random(7),
    )
    cold_fetch = FakeConnectionsSource(_many(400))
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
    await _sync(session_factory, user_id, FakeConnectionsSource(_many(150)), sleeps=sleeps)
    assert len(sleeps.waits) == 3  # four pages (40, 40, 40, 30), a wait between each pair
    assert all(wait > 0 for wait in sleeps.waits)
    assert len(set(sleeps.waits)) == 3  # drawn, not a fixed interval


async def test_a_spent_budget_fetches_nothing(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    settings = replace(SETTINGS, budget=BudgetSettings(connection_pages_per_day=2))
    await _sync(session_factory, user_id, FakeConnectionsSource(_many(200)), settings=settings)
    fetch = FakeConnectionsSource(_many(200))

    report = await _sync(session_factory, user_id, fetch, settings=settings)

    assert fetch.requests == []
    assert report.result.reason is StopReason.PAGE_BUDGET


async def test_the_budget_is_spent_before_the_fetch_not_after(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """A page that fails still cost a unit: the unit is the attempt (spec 9.6)."""
    fetch = FakeConnectionsSource(_many(100), script={1: THROTTLED})
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
    fetch = FakeConnectionsSource(_many(400))

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
    await _sync(session_factory, user_id, FakeConnectionsSource(people))

    for week in (1, 2):
        await _sync(
            session_factory,
            user_id,
            FakeConnectionsSource(people, total=lie),
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
    await _sync(session_factory, user_id, FakeConnectionsSource(people))
    renumbered = [replace(p, urn_prefix="ACoAANEW") for p in people]

    reports = [
        await _sync(
            session_factory,
            user_id,
            FakeConnectionsSource(renumbered),
            at=NOW + timedelta(days=7 * week),
        )
        for week in (1, 2)
    ]

    assert all(r.pages.needs_review == 100 for r in reports)
    assert all(r.aging is not None and r.aging.refused is not None for r in reports)
    contacts = _contacts(session_factory, user_id)
    assert len(contacts) == 100
    assert all((c.li_missing_count, c.li_disconnected_at) == (0, None) for c in contacts.values())


@pytest.mark.parametrize("size", [1, 2, 3, 5, 10])
async def test_a_sync_that_replaces_a_small_network_outright_disconnects_nobody(
    session_factory: sessionmaker[Session], user_id: int, size: int
) -> None:
    """#169 A, through the runner: every URN *and* slug changes at once.

    Unlike a URN scheme change, nothing resolves to a candidate: every row is
    new. The runner has to hand ``age_unseen`` the rows its own pages created,
    or a doubled network lets exactly half of it age. The second week is the
    regression persisting: the replacement rows now exist, so it is the half
    rule, not the created rows, that refuses it.
    """
    people = _many(size)
    await _sync(session_factory, user_id, FakeConnectionsSource(people))
    replaced = [
        Person(900 + i, f"Other{i}", f"Stranger{i}", None, urn_prefix="ACoAANEW")
        for i in range(size)
    ]

    reports = [
        await _sync(
            session_factory,
            user_id,
            FakeConnectionsSource(replaced),
            at=NOW + timedelta(days=7 * week),
        )
        for week in (1, 2)
    ]

    assert reports[0].pages.created == size
    assert all(r.result.complete for r in reports)
    assert all(r.aging is not None and r.aging.refused is not None for r in reports)
    contacts = _contacts(session_factory, user_id)
    assert all(
        (contacts[p.urn].li_missing_count, contacts[p.urn].li_disconnected_at) == (0, None)
        for p in people
    )


async def test_the_runner_measures_aging_against_the_contacts_it_found(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """Ten stored; a sync sees five of them and six new people.

    Five of the ten stored would miss: half, refused. Counted over the sixteen
    rows that exist once the pages are written, five would be under half and
    go ahead, which is what a runner that forgot the rows it created would do.
    """
    people = _many(10)
    await _sync(session_factory, user_id, FakeConnectionsSource(people))
    newcomers = [
        Person(900 + i, f"Other{i}", f"Stranger{i}", None, urn_prefix="ACoAANEW") for i in range(6)
    ]

    report = await _sync(
        session_factory,
        user_id,
        FakeConnectionsSource([*newcomers, *people[5:]]),
        at=NOW + timedelta(days=7),
    )

    assert report.result.complete and report.pages.created == 6
    assert report.aging is not None and report.aging.refused is not None
    assert "5 of 10" in report.aging.refused
    contacts = _contacts(session_factory, user_id)
    assert all(contacts[p.urn].li_missing_count == 0 for p in people[:5])


@pytest.mark.parametrize("scripted", [CHECKPOINT, LOGGED_OUT], ids=["checkpoint", "logged-out"])
async def test_no_run_starts_while_the_session_is_flagged(
    session_factory: sessionmaker[Session], user_id: int, scripted: Scripted
) -> None:
    """The review's attack: a run after a checkpoint fetched anyway, a retry one run later."""
    await _sync(session_factory, user_id, FakeConnectionsSource(list(PEOPLE), script={0: scripted}))
    later = FakeConnectionsSource(list(PEOPLE))

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
    await _sync(session_factory, user_id, FakeConnectionsSource(people))
    changed = {p.urn for p in people[100:116]}
    renumbered = [replace(p, urn_prefix="ACoAANEW") if p.urn in changed else p for p in people]

    reports = [
        await _sync(
            session_factory,
            user_id,
            FakeConnectionsSource(renumbered),
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


async def test_a_candidate_matched_by_an_old_slug_is_held_not_aged(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """New URN, and a slug that is only one of the contact's old aliases.

    Neither the URN nor the current slug was seen, so only the review hold
    keeps this contact from aging while a person decides.
    """
    people = _many(100)
    await _sync(session_factory, user_id, FakeConnectionsSource(people))
    target = people[50]
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        contact = session.scalars(scoped(user, Contact).where(Contact.li_urn == target.urn)).one()
        contact.aliases.append(
            ContactAlias(user_id=user.id, li_public_id="an-old-vanity-slug", observed_at=NOW)
        )
    served = list(people)
    served[50] = replace(target, urn_prefix="ACoAANEW", public_id="an-old-vanity-slug")

    reports = [
        await _sync(
            session_factory,
            user_id,
            FakeConnectionsSource(served),
            at=NOW + timedelta(days=7 * week),
        )
        for week in (1, 2)
    ]

    assert all(r.pages.needs_review == 1 for r in reports)
    assert all(r.aging is not None and r.aging.missed == 0 for r in reports)
    held = _contacts(session_factory, user_id)[target.urn]
    assert (held.li_missing_count, held.li_disconnected_at) == (0, None)


# --- P2-10: the run row, cancel, and a refused aging said out loud (#169 E) --------------


def _run_row(factory: sessionmaker[Session], user_id: int, run_id: int) -> SyncRun:
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        run = runs.get_run(session, user, run_id)
        session.expunge(run)
        return run


async def test_a_refused_aging_is_on_the_run_and_in_posture(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    people = _many(10)
    first = await _sync(session_factory, user_id, FakeConnectionsSource(people))
    newcomers = [
        Person(900 + i, f"Other{i}", f"Stranger{i}", None, urn_prefix="ACoAANEW") for i in range(6)
    ]
    later = NOW + timedelta(days=7)
    report = await _sync(
        session_factory, user_id, FakeConnectionsSource([*newcomers, *people[5:]]), at=later
    )

    assert report.aging is not None and report.aging.refused is not None
    run = _run_row(session_factory, user_id, report.run_id)
    assert (run.status, run.stop_reason) == (SyncRunStatus.COMPLETED, "end_of_list")
    assert run.counts_json is not None
    assert run.counts_json["aging"]["refused"] == report.aging.refused
    assert run.notes == f"aged nobody: {report.aging.refused}"
    assert runs.view(run).aging_refused == report.aging.refused
    assert _run_row(session_factory, user_id, first.run_id).notes is None

    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        found = posture(session, user, report.account_id, now=later, settings=Settings())
    (aging,) = [p for p in found.protections if p.name == "network aging"]
    assert aging.warnings and report.aging.refused in aging.warnings[0]
    assert f"run {report.run_id}" in aging.warnings[0]


async def test_a_full_sync_that_aged_as_usual_does_not_warn(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    report = await _sync(session_factory, user_id, FakeConnectionsSource(_many(10)))
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        found = posture(session, user, report.account_id, now=NOW, settings=Settings())
    (aging,) = [p for p in found.protections if p.name == "network aging"]
    assert aging.warnings == () and "aged as usual" in aging.value


class CancelsOnFirstWait(Sleeps):
    """Asks the running run to stop during the first wait between two pages."""

    def __init__(self, factory: sessionmaker[Session], user_id: int) -> None:
        super().__init__()
        self.factory = factory
        self.user_id = user_id

    async def __call__(self, seconds: float) -> None:
        if not self.slices:
            with session_scope(self.factory, write=True) as session:
                user = session.get(User, self.user_id)
                assert user is not None
                running = runs.running_run(session, user, ensure_account(session, user).id)
                assert running is not None
                runs.request_cancel(session, user, running.id, now=NOW)
        await super().__call__(seconds)


async def test_a_cancel_inside_the_wait_stops_before_the_next_page(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    fetch = FakeConnectionsSource(_many(200))
    sleeps = CancelsOnFirstWait(session_factory, user_id)

    report = await _sync(session_factory, user_id, fetch, sleeps=sleeps)

    assert fetch.starts == [0]
    assert sleeps.slices == [CANCEL_SLICE_S]  # one slice, then the flag was read
    assert report.cancelled and not report.result.complete and report.aging is None
    run = _run_row(session_factory, user_id, report.run_id)
    assert (run.status, run.stop_reason) == (SyncRunStatus.ABORTED, "cancelled")
    assert run.counts_json is not None and run.counts_json["pages"] == 1
    assert len(_contacts(session_factory, user_id)) == 40  # what it read is kept


async def test_a_cancel_before_the_first_page_fetches_nothing(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        run = runs.create_run(
            session, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
        runs.request_cancel(session, user, run.id, now=NOW)
        run_id = run.id
    fetch = FakeConnectionsSource(_many(50))

    report = await sync_connections(
        session_factory,
        user_id,
        SyncMode.FULL,
        fetch,
        settings=SETTINGS,
        run_id=run_id,
        clock=Clock(NOW),
        sleep=Sleeps(),
    )

    assert fetch.requests == [] and report.cancelled
    assert _spent(session_factory, user_id, report.account_id, NOW) == 0
    assert _run_row(session_factory, user_id, run_id).stop_reason == "cancelled"


async def test_an_incremental_sync_that_caught_up_is_completed(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    people = _many(50)
    await _sync(session_factory, user_id, FakeConnectionsSource(people))
    report = await _sync(
        session_factory, user_id, FakeConnectionsSource(people), SyncMode.INCREMENTAL
    )
    run = _run_row(session_factory, user_id, report.run_id)
    assert run.kind is SyncRunKind.CONNECTIONS_INCREMENTAL
    assert (run.status, run.stop_reason) == (SyncRunStatus.COMPLETED, "caught_up")


async def test_a_throttled_page_is_recorded_as_the_outcome(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    report = await _sync(
        session_factory, user_id, FakeConnectionsSource(_many(100), script={1: THROTTLED})
    )
    run = _run_row(session_factory, user_id, report.run_id)
    assert (run.status, run.stop_reason) == (SyncRunStatus.ABORTED, "throttled")
    assert run.counts_json is not None and run.counts_json["heat_raised"] is True


# --- #189 item 1: the route-changed breaker, driven through a real sync ---------


def _breaker_tripped(factory: sessionmaker[Session], user_id: int, account_id: int) -> bool:
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        return route_breaker.tripped(session, user, account_id)


def _breaker_count(factory: sessionmaker[Session], user_id: int, account_id: int) -> int:
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        return route_breaker.state(session, user, account_id).count


async def test_two_route_changed_runs_in_a_row_trip_the_breaker(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    account_id = 0
    for day in range(route_breaker.THRESHOLD):
        report = await _sync(
            session_factory,
            user_id,
            FakeConnectionsSource(list(PEOPLE), script={0: UNRECOGNIZED}),
            at=NOW + timedelta(days=day),
        )
        assert report.result.outcome is Outcome.ROUTE_CHANGED
        account_id = report.account_id

    assert _breaker_count(session_factory, user_id, account_id) == route_breaker.THRESHOLD
    assert _breaker_tripped(session_factory, user_id, account_id)


async def test_a_success_in_between_resets_the_breakers_count(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    first = await _sync(
        session_factory,
        user_id,
        FakeConnectionsSource(list(PEOPLE), script={0: UNRECOGNIZED}),
    )
    assert _breaker_count(session_factory, user_id, first.account_id) == 1

    await _sync(
        session_factory, user_id, FakeConnectionsSource(list(PEOPLE)), at=NOW + timedelta(days=1)
    )
    assert _breaker_count(session_factory, user_id, first.account_id) == 0

    await _sync(
        session_factory,
        user_id,
        FakeConnectionsSource(list(PEOPLE), script={0: UNRECOGNIZED}),
        at=NOW + timedelta(days=2),
    )
    # Never two in a row: the success in between kept it from tripping.
    assert _breaker_count(session_factory, user_id, first.account_id) == 1
    assert not _breaker_tripped(session_factory, user_id, first.account_id)


async def test_a_manual_success_clears_a_tripped_breaker(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """Manual runs are never refused for the breaker (#189 item 1): this is how a
    person checks whether the wall is still there, and a manual run that
    succeeds clears it without `reset-breaker`."""
    account_id = 0
    for day in range(route_breaker.THRESHOLD):
        report = await _sync(
            session_factory,
            user_id,
            FakeConnectionsSource(list(PEOPLE), script={0: UNRECOGNIZED}),
            at=NOW + timedelta(days=day),
        )
        account_id = report.account_id
    assert _breaker_tripped(session_factory, user_id, account_id)

    report = await _sync(
        session_factory,
        user_id,
        FakeConnectionsSource(list(PEOPLE)),
        at=NOW + timedelta(days=10),
    )

    assert report.result.reason is StopReason.END_OF_LIST  # the manual run reached a natural end
    assert not _breaker_tripped(session_factory, user_id, account_id)
    assert _breaker_count(session_factory, user_id, account_id) == 0


async def test_a_cancelled_run_does_not_move_the_breaker(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """A cancel says nothing about whether the route is readable."""
    report = await _sync(
        session_factory,
        user_id,
        FakeConnectionsSource(list(PEOPLE), script={0: UNRECOGNIZED}),
    )
    assert _breaker_count(session_factory, user_id, report.account_id) == 1

    sleeps = CancelsOnFirstWait(session_factory, user_id)
    cancelled = await _sync(
        session_factory,
        user_id,
        FakeConnectionsSource(_many(200)),
        at=NOW + timedelta(days=1),
        sleeps=sleeps,
    )

    assert cancelled.cancelled and not cancelled.result.complete
    assert _breaker_count(session_factory, user_id, report.account_id) == 1


async def test_a_tripped_breaker_shows_in_posture(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    account_id = 0
    for day in range(route_breaker.THRESHOLD):
        report = await _sync(
            session_factory,
            user_id,
            FakeConnectionsSource(list(PEOPLE), script={0: UNRECOGNIZED}),
            at=NOW + timedelta(days=day),
        )
        account_id = report.account_id

    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        found = posture(session, user, account_id, now=NOW, settings=Settings())
    (breaker,) = [p for p in found.protections if p.name == "route-changed breaker"]
    assert breaker.warnings
    assert str(route_breaker.THRESHOLD) in breaker.warnings[0]
    assert "reset-breaker" in breaker.warnings[0]


async def test_a_clear_breaker_shows_no_warning_in_posture(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    report = await _sync(session_factory, user_id, FakeConnectionsSource(list(PEOPLE)))
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        found = posture(session, user, report.account_id, now=NOW, settings=Settings())
    (breaker,) = [p for p in found.protections if p.name == "route-changed breaker"]
    assert breaker.warnings == ()


@dataclass(slots=True)
class _RaisingSource:
    """A ``ConnectionsSource`` whose first page raises ``error`` instead of answering."""

    error: Exception
    endpoint: str = "raising"

    async def fetch_page(self, *, start: int, count: int) -> SourcePage:
        raise self.error


async def test_two_observation_failed_runs_in_a_row_trip_the_breaker(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """#191 review F1: the observation mechanism failing to read the page (a body too
    large to keep, too many responses left unread) is the same signal as
    route_changed for the breaker. The run ends by exception, never reaching
    sync_connections's own record() call, so this must be recorded from a
    dedicated except clause or a wall whose body cannot be kept would let a
    scheduled sync retry it forever uncounted."""
    account_id = 0
    for day in range(route_breaker.THRESHOLD):
        with pytest.raises(ObservationFailed):
            await _sync(
                session_factory,
                user_id,
                _RaisingSource(ObservationFailed("an answer of the page could not be kept")),
                at=NOW + timedelta(days=day),
            )
        with session_scope(session_factory, write=True) as session:
            user = session.get(User, user_id)
            assert user is not None
            account_id = ensure_account(session, user).id

    assert _breaker_tripped(session_factory, user_id, account_id)


async def test_a_browser_unavailable_run_does_not_move_the_breaker(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """#191 review F1's decision: BrowserUnavailable ("the tab was replaced mid-read")
    is the local browser breaking, not a signal about whether the connections
    route is readable -- it already gets its own RETRY_LATER handling at the
    worker (spec 9.9). It never counts toward the breaker, not even repeated,
    because a repeat says nothing more about the route than the first one did."""
    report = await _sync(
        session_factory, user_id, FakeConnectionsSource(list(PEOPLE), script={0: UNRECOGNIZED})
    )
    assert _breaker_count(session_factory, user_id, report.account_id) == 1

    for day in (1, 2, 3):
        with pytest.raises(BrowserUnavailable):
            await _sync(
                session_factory,
                user_id,
                _RaisingSource(BrowserUnavailable("the run's tab was replaced mid-read")),
                at=NOW + timedelta(days=day),
            )

    assert _breaker_count(session_factory, user_id, report.account_id) == 1
    assert not _breaker_tripped(session_factory, user_id, report.account_id)


@pytest.mark.parametrize(
    "scripted", [THROTTLED, CHECKPOINT, LOGGED_OUT], ids=["throttled", "checkpoint", "logged-out"]
)
async def test_a_non_route_changed_stop_does_not_move_the_breaker(
    session_factory: sessionmaker[Session], user_id: int, scripted: Scripted
) -> None:
    """#191 review F2: throttled, checkpoint, and logged-out are different signals
    (they already raise heat / flag the session) and must not also count here."""
    report = await _sync(
        session_factory, user_id, FakeConnectionsSource(list(PEOPLE), script={0: scripted})
    )
    assert _breaker_count(session_factory, user_id, report.account_id) == 0


async def test_a_route_changed_stop_after_some_pages_still_trips_the_breaker(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """#191 review F3: route_changed counts wherever in the run it happens, not only
    when it is the very first page (``result.pages == 0``)."""
    report = await _sync(
        session_factory, user_id, FakeConnectionsSource(_many(100), script={1: UNRECOGNIZED})
    )
    assert report.result.pages == 1  # one page read before the route_changed stop
    assert report.result.outcome is Outcome.ROUTE_CHANGED
    assert _breaker_count(session_factory, user_id, report.account_id) == 1


async def test_a_caught_up_incremental_success_clears_the_breaker(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """#191 review F5: CAUGHT_UP is a natural end too -- an incremental sync stopping
    because every URN on its first page is already known -- and must reset the
    streak the same as END_OF_LIST. Fifty people (more than one page) so the first
    page is full: a short first page would end the list before CAUGHT_UP is even
    checked, proving nothing about this specific natural end."""
    people = _many(50)
    first = await _sync(session_factory, user_id, FakeConnectionsSource(people))
    for day in range(route_breaker.THRESHOLD):
        await _sync(
            session_factory,
            user_id,
            FakeConnectionsSource(people, script={0: UNRECOGNIZED}),
            at=NOW + timedelta(days=day + 1),
        )
    assert _breaker_tripped(session_factory, user_id, first.account_id)

    report = await _sync(
        session_factory,
        user_id,
        FakeConnectionsSource(people),
        SyncMode.INCREMENTAL,
        at=NOW + timedelta(days=10),
    )

    assert report.result.reason is StopReason.CAUGHT_UP
    assert not _breaker_tripped(session_factory, user_id, first.account_id)
    assert _breaker_count(session_factory, user_id, first.account_id) == 0


# --- #184: contacts read off connections-page cards ----------------------------------


async def test_card_contacts_a_sync_confirms_do_not_dilute_its_aging_limits(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """Two real connections; an earlier fallback run left ten card contacts. A
    complete sync confirms all ten by URN and misses one of the two: half of the
    contacts that were connections before it ran, refused. Counting the ten it
    confirmed among those would make it one of twelve, and age a real person."""
    real = _many(2)
    await _sync(session_factory, user_id, FakeConnectionsSource(real))
    cards = [
        Person(700 + i, f"Card{i}", f"Person{i}", f"Role {i}", urn_prefix="ACoAACARD")
        for i in range(10)
    ]
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        mapping.apply_page(
            session,
            user,
            ConnectionsPage(
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
                    for p in cards
                ),
                observed_at=NOW,
            ),
        )

    report = await _sync(
        session_factory,
        user_id,
        FakeConnectionsSource([real[0], *cards]),
        at=NOW + timedelta(days=7),
    )

    assert report.result.complete
    assert report.pages.confirmed_by_urn == 10
    assert report.counts()["confirmed_by_urn"] == 10
    assert report.counts()["cards_created"] == 0
    assert report.aging is not None and report.aging.refused is not None
    contacts = _contacts(session_factory, user_id)
    assert (contacts[real[1].urn].li_missing_count, contacts[real[1].urn].li_disconnected_at) == (
        0,
        None,
    )
    assert all(contacts[p.urn].needs_review_at is None for p in cards)


def test_a_runs_counts_carry_the_cards_it_created() -> None:
    """#184: a fallback run's ``counts_json`` says how many card contacts it made."""
    pages = mapping.PageCounts(cards_created=3, confirmed_by_urn=1)
    report = SyncRunReport(
        account_id=1,
        result=SyncResult(
            mode=SyncMode.FULL,
            reason=StopReason.END_OF_LIST,
            pages=1,
            seen_urns=frozenset(),
            total=0,
            source_switched=True,
        ),
        pages=pages,
    )
    counts = report.counts()
    assert (counts["cards_created"], counts["confirmed_by_urn"]) == (3, 1)
    assert counts["complete"] is False


# --- #197: a lost answer is a safe incomplete stop ------------------------------------------

_LOST_CAUSE = "Error (no resource)"


@dataclass
class _LosingSource(FakeConnectionsSource):
    """Honest pages until page ``lose_at``, which raises ``AnswerLost`` for its start."""

    lose_at: int = 2

    async def fetch_page(self, *, start: int, count: int) -> SourcePage:
        if len(self.requests) == self.lose_at:
            self.requests.append((start, count))
            raise AnswerLost(
                LostAnswer(start=start, cause=_LOST_CAUSE, ending="the page moved past it")
            )
        return await super().fetch_page(start=start, count=count)


async def test_a_lost_answer_ends_the_run_aborted_and_says_which_start(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    gone = await _one_miss_already(session_factory, user_id)
    people = _many(99)
    fetch = _LosingSource(people, lose_at=2)

    report = await _sync(session_factory, user_id, fetch, at=NOW + timedelta(days=14))

    assert fetch.starts == [0, 40, 80]
    assert report.result.reason is StopReason.ANSWER_LOST and not report.result.complete
    assert report.aging is None and not report.heat_raised and not report.session_flagged
    contacts = _contacts(session_factory, user_id)
    assert (contacts[gone.urn].li_missing_count, contacts[gone.urn].li_disconnected_at) == (1, None)
    assert all(contacts[p.urn].li_missing_count == 0 for p in people[:80])

    run = _run_row(session_factory, user_id, report.run_id)
    assert (run.status, run.stop_reason, run.error) == (
        SyncRunStatus.ABORTED,
        "answer_lost",
        None,
    )
    assert run.notes == (
        "stopped incomplete: the page's answer for start 80 could not be read"
        " (Error (no resource)); the page moved past it."
    )
    assert run.counts_json is not None
    assert run.counts_json["lost"] == {"start": 80, "cause": _LOST_CAUSE}
    assert run.counts_json["complete"] is False and run.counts_json["outcome"] is None
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        assert session_flag(session, user) is None
        assert heat_service.state(session, user, report.account_id) is None


async def test_a_lost_answer_never_moves_the_breaker(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    """A lost answer is not a changed route: it neither extends the streak (however
    many in a row) nor clears it (it is not a natural end either)."""
    first = await _sync(session_factory, user_id, _LosingSource(_many(99), lose_at=0))
    assert _breaker_count(session_factory, user_id, first.account_id) == 0

    await _sync(
        session_factory,
        user_id,
        FakeConnectionsSource(list(PEOPLE), script={0: UNRECOGNIZED}),
        at=NOW + timedelta(days=1),
    )
    for day in (2, 3, 4):
        report = await _sync(
            session_factory,
            user_id,
            _LosingSource(_many(99), lose_at=1),
            at=NOW + timedelta(days=day),
        )
        assert report.result.reason is StopReason.ANSWER_LOST
    assert _breaker_count(session_factory, user_id, first.account_id) == 1
    assert not _breaker_tripped(session_factory, user_id, first.account_id)


async def test_a_run_without_a_lost_answer_records_none(
    session_factory: sessionmaker[Session], user_id: int
) -> None:
    report = await _sync(session_factory, user_id, FakeConnectionsSource(list(PEOPLE)))
    run = _run_row(session_factory, user_id, report.run_id)
    assert run.counts_json is not None and run.counts_json["lost"] is None
    assert run.notes is None
