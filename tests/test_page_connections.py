"""PageConnections: the connections list from the page's own answers (#187, ADR 0006).

Driven through the real :class:`~netkeeper.linkedin.browser.BrowserRun` and the real
:func:`~netkeeper.linkedin.connections.run_connections_sync` over :mod:`flagship_site`'s
fake page, which "receives" its first screen on navigation and "sends" a pagination
request per scroll. The attacks this file makes on the source, each a test below: can a
changed payload write part of a page or the wrong person? Can a run be complete without
the page proving the end of the list? Can the scroll loop spin forever or overspend its
unit? Can a body reach a log?
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from flagship_pages import CardOptions, pagination_payload
from flagship_site import (
    CHECKPOINT_URL,
    LOGIN_URL,
    LOST_BODY_MESSAGE,
    PAGE_URL,
    SHELL,
    Answer,
    FakeCdpSession,
    FlagshipSite,
    ListeningTab,
    Lost,
)
from run_fakes import fake_provider
from voyager_pages import PEOPLE, Person

from netkeeper.linkedin import page_connections
from netkeeper.linkedin.browser import BrowserRun, BrowserUnavailable, ScrollOutcome
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.connections import (
    AnswerLost,
    ConnectionsPage,
    StopReason,
    SyncJobSpec,
    SyncMode,
    SyncResult,
    run_connections_sync,
)
from netkeeper.linkedin.observe import (
    ObservationFailed,
    ObservationLimits,
    ResponseMatch,
    ResponseRule,
)
from netkeeper.linkedin.pacing import ScrollPlan
from netkeeper.linkedin.page_connections import PageConnections

#: No test here needs more scroll plans than this; a scroll loop that never stops
#: fails at this bound instead of hanging the suite (#198 review, L4).
MAX_PLANS_PER_TEST = 300


@pytest.fixture(autouse=True)
def _bounded_scrolls(monkeypatch: pytest.MonkeyPatch) -> None:
    real = BrowserRun.scroll
    plans = 0

    async def bounded(self: BrowserRun, plan: ScrollPlan, **kwargs: Any) -> ScrollOutcome:
        nonlocal plans
        plans += 1
        assert plans <= MAX_PLANS_PER_TEST, "the scroll never stopped"
        return await real(self, plan, **kwargs)

    monkeypatch.setattr(BrowserRun, "scroll", bounded)


def many(count: int) -> list[Person]:
    """``count`` invented people: the named ten, then numbered ones."""
    extra = [
        Person(300 + i, f"Given{i}", f"Family{i}", f"Role {i} at Invented Firm {i % 5}")
        for i in range(max(count - len(PEOPLE), 0))
    ]
    return [*PEOPLE, *extra][:count]


async def no_sleep(seconds: float) -> None:
    return None


@dataclass
class Gate:
    """The job's gate, counting units: ``allow`` units, then no more."""

    allow: int = 1000
    units: int = 0
    pauses: int = 0

    async def before_page(self, number: int) -> bool:
        if self.units >= self.allow:
            return False
        self.units += 1
        return True

    async def between_pages(self) -> None:
        self.pauses += 1


@dataclass
class Outcomes:
    result: SyncResult
    pages: list[ConnectionsPage] = field(default_factory=list)
    site: FlagshipSite | None = None

    @property
    def urns(self) -> list[str | None]:
        return [c.urn for page in self.pages for c in page.connections]


async def sync(
    site: FlagshipSite,
    mode: SyncMode = SyncMode.FULL,
    *,
    known: Sequence[str] = (),
    gate: Gate | None = None,
    page_budget: int = 100,
    pages_sink: list[ConnectionsPage] | None = None,
    **source_kwargs: object,
) -> Outcomes:
    provider, _ = fake_provider(site)
    pages: list[ConnectionsPage] = [] if pages_sink is None else pages_sink

    async def on_page(page: ConnectionsPage) -> None:
        pages.append(page)

    async with provider.run("account-1") as run:
        source = PageConnections(
            run,
            require_newest_first=mode is SyncMode.INCREMENTAL,
            rng=random.Random(7),
            sleep=no_sleep,
            response_wait_s=0.01,
            landing_wait_s=0.05,
            **source_kwargs,  # type: ignore[arg-type]
        )
        spec = SyncJobSpec(mode=mode, page_budget=page_budget, known_urns=frozenset(known))
        result = await run_connections_sync(
            spec, source, gate or Gate(), on_page=on_page, clock=lambda: datetime.now(UTC)
        )
    return Outcomes(result=result, pages=pages, site=site)


# --- the whole list, and when it is complete -----------------------------------------------


async def test_a_full_sync_reads_the_whole_list_and_is_complete() -> None:
    people = many(95)
    out = await sync(FlagshipSite(people))
    assert out.urns == [p.urn for p in people]
    assert out.result.reason is StopReason.END_OF_LIST and out.result.complete
    assert out.result.max_total == 95 and out.result.pages == 3  # 40 + 40 + 15
    assert out.pages[0].connections[0].public_id == people[0].slug
    # The page asked for each ten-card page once, then the one after the short page of
    # five, which came back empty.
    starts = [int(body.split('"startIndex": ')[1].split(",")[0]) for body in out.site.fetches]  # type: ignore[union-attr]
    assert starts == [*range(10, 100, 10), 95]


async def test_the_first_screen_can_come_from_the_screen_request() -> None:
    people = many(25)
    out = await sync(FlagshipSite(people, landing="screen"))
    assert out.urns == [p.urn for p in people] and out.result.complete


async def test_a_short_last_page_that_asks_for_nothing_ends_the_list() -> None:
    people = many(33)
    out = await sync(FlagshipSite(people, end="short"))
    assert out.urns == [p.urn for p in people] and out.result.complete


async def test_a_full_last_page_that_asks_for_nothing_ends_a_list_the_run_has_all_of() -> None:
    """A network of forty (a multiple of ten): the fourth page is full and asks for no
    fifth. The run has seen as many distinct people as the stated total, so that answer
    is the end, and the run is complete (#188 review, follow-up 1)."""
    people = many(40)
    out = await sync(FlagshipSite(people, end="short"))
    assert out.result.reason is StopReason.END_OF_LIST and out.result.complete
    assert out.urns == [p.urn for p in people]


async def test_a_list_of_exactly_ten_completes_on_the_first_screen() -> None:
    """The whole network is the first screen: ten cards, a total of ten, and no next
    request (#189 item 2). ``_land`` must end the list on its own, without ever
    scrolling -- the same "a full answer with no next request ends a list the run
    has all of" rule as the case above, but here the full answer *is* the first
    screen, so ``_land`` has to apply it directly rather than ``_take_answer``
    applying it to a later pagination answer."""
    people = many(10)
    site = FlagshipSite(people, end="short")
    out = await sync(site)
    assert out.result.reason is StopReason.END_OF_LIST and out.result.complete
    assert out.urns == [p.urn for p in people]
    assert site.fetches == []  # the first screen was the whole list; nothing was scrolled


async def test_a_full_last_page_that_asks_for_nothing_proves_nothing_short_of_the_total() -> None:
    """The same answer when the total says fifty: the list may go on. The page stops
    loading, the source gives up after its idle scrolls, and nobody ages."""
    out = await sync(FlagshipSite(many(40), end="short", total=50))
    assert out.result.reason is StopReason.RESPONSE
    assert out.result.outcome is Outcome.ROUTE_CHANGED and not out.result.complete


async def test_a_full_last_page_ends_the_list_when_hidden_members_are_within_the_slack() -> None:
    """#208: 620 visible under a stated total of 624. The last answer is full and asks
    for no next page; 620 is within completion_slack(624) == 7 of the total, so that
    is the end, and the run completes with a shortfall of 4 (the #204 slack)."""
    people = many(620)
    out = await sync(FlagshipSite(people, end="short", total=624))
    assert out.result.reason is StopReason.END_OF_LIST and out.result.complete
    assert out.result.shortfall == 4
    assert out.urns == [p.urn for p in people]


async def test_a_full_last_page_ends_the_list_at_exactly_the_slack() -> None:
    """The boundary: 40 visible of 45 is exactly completion_slack(45) == 5 short."""
    out = await sync(FlagshipSite(many(40), end="short", total=45))
    assert out.result.reason is StopReason.END_OF_LIST and out.result.complete
    assert out.result.shortfall == 5


async def test_a_full_last_page_one_past_the_slack_proves_nothing() -> None:
    """One more than the slack: 40 visible of 46 is 6 short of a slack of 5. The page
    stops loading, the source gives up, and nobody ages."""
    out = await sync(FlagshipSite(many(40), end="short", total=46))
    assert out.result.reason is StopReason.RESPONSE
    assert out.result.outcome is Outcome.ROUTE_CHANGED and not out.result.complete


async def test_a_large_lists_slack_is_its_own_one_percent_at_the_end_of_the_list() -> None:
    """The slack scales with the total: 620 of 627 is 7 short, completion_slack(627)
    == 7, so the list ends; 620 of 628 is 8 short of the same slack, so it does not.
    A fixed floor of 5 would refuse the first."""
    ends = await sync(FlagshipSite(many(620), end="short", total=627))
    assert ends.result.reason is StopReason.END_OF_LIST and ends.result.complete
    stops = await sync(FlagshipSite(many(620), end="short", total=628))
    assert stops.result.outcome is Outcome.ROUTE_CHANGED and not stops.result.complete


async def test_seeing_the_total_does_not_make_a_stalled_page_the_end() -> None:
    """The last answer asks for a next page the page never requests: however many
    people the run has seen, that is a stall, never the end."""
    out = await sync(FlagshipSite(many(40), end="stall"))
    assert out.result.outcome is Outcome.ROUTE_CHANGED and not out.result.complete


async def test_the_idle_scrolls_are_exactly_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """#188 review, S3: pages 10 and 20 come on the first two scroll plans, then the
    page stops asking. Exactly MAX_IDLE_SCROLLS more plans, then RouteChanged."""
    site = FlagshipSite(many(30), end="stall", answer_plans=lambda n: True)
    _count_plans(monkeypatch, site)
    out = await sync(site)
    assert out.result.outcome is Outcome.ROUTE_CHANGED
    assert site.plans == 2 + page_connections.MAX_IDLE_SCROLLS


async def test_idle_scrolls_add_up_across_a_unit_and_do_not_reset_on_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page that answers only every fourth scroll: three idle, a page, three idle --
    six idle scrolls inside one unit, which is the bound, before the unit fills. A
    counter that reset on each page would let this unit scroll on indefinitely."""
    site = FlagshipSite(many(60), answer_plans=lambda n: n % 4 == 0)
    _count_plans(monkeypatch, site)
    out = await sync(site)
    assert out.result.outcome is Outcome.ROUTE_CHANGED and out.urns == []
    assert site.plans == 7  # plans 1-3 idle, 4 brought page 10, 5-7 idle: six in all


async def test_idle_scrolls_start_over_with_each_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound is per unit: a later unit gets its own six."""
    site = FlagshipSite(many(80), answer_plans=lambda n: n % 2 == 0)
    _count_plans(monkeypatch, site)
    out = await sync(site)
    # One idle scroll before each page: three or four in a unit, eight in the run.
    assert out.result.complete and len(out.urns) == 80
    assert site.plans - len(site.fetches) > page_connections.MAX_IDLE_SCROLLS


def _count_plans(monkeypatch: pytest.MonkeyPatch, site: FlagshipSite) -> None:
    """Tell ``site`` each time ``BrowserRun.scroll`` starts replaying a plan."""
    real = BrowserRun.scroll

    async def counted(self: BrowserRun, plan: ScrollPlan, **kwargs: Any) -> ScrollOutcome:
        tab = site.pages[-1]
        assert isinstance(tab, ListeningTab)
        site.plan_started(tab)
        return await real(self, plan, **kwargs)

    monkeypatch.setattr(BrowserRun, "scroll", counted)


async def test_a_page_that_stops_loading_is_not_the_end_and_the_scroll_is_bounded() -> None:
    site = FlagshipSite(many(30), end="stall")
    out = await sync(site)
    assert out.result.outcome is Outcome.ROUTE_CHANGED and not out.result.complete
    assert out.urns == []  # the unit never filled, so nothing was handed over
    (tab,) = site.pages
    # Two scrolls brought pages 10 and 20, then MAX_IDLE_SCROLLS brought nothing.
    scrolls_with_answers = 2
    wheels_per_scroll = (3, 9)  # DEFAULT_SCROLL_PROFILE.steps_range
    most = (scrolls_with_answers + page_connections.MAX_IDLE_SCROLLS) * wheels_per_scroll[1]
    assert 0 < len(tab.mouse.wheels) <= most


async def test_a_list_with_no_stated_total_is_never_complete() -> None:
    out = await sync(FlagshipSite(many(15), total=None))
    assert out.result.reason is StopReason.END_OF_LIST and not out.result.complete


async def test_a_full_last_page_never_ends_a_list_with_no_stated_total() -> None:
    """#208: the slack is measured from a stated total. With none, a full answer that
    asks for no next page proves nothing, however small the list (0 - 5 is no bar)."""
    out = await sync(FlagshipSite(many(20), end="short", total=None))
    assert out.result.reason is StopReason.RESPONSE
    assert out.result.outcome is Outcome.ROUTE_CHANGED and not out.result.complete


async def test_a_total_a_little_larger_than_the_list_still_completes() -> None:
    """#204: a total that counts one hidden member the list never shows is within the
    slack (5, for a total of 16), so the run still completes rather than aging nobody."""
    out = await sync(FlagshipSite(many(15), total=16))
    assert out.result.reason is StopReason.END_OF_LIST
    assert out.result.shortfall == 1
    assert out.result.complete


async def test_a_total_far_larger_than_the_list_is_still_never_complete() -> None:
    """Past #204's slack (5, for a total of 30), a total the list falls well short of
    still ages nobody."""
    out = await sync(FlagshipSite(many(15), total=30))
    assert out.result.reason is StopReason.END_OF_LIST
    assert out.result.shortfall == 15
    assert not out.result.complete


async def test_an_incremental_sync_stops_at_the_first_page_it_knows() -> None:
    people = many(120)
    known = [p.urn for p in people[40:]]
    site = FlagshipSite(people)
    out = await sync(site, SyncMode.INCREMENTAL, known=known)
    assert out.result.reason is StopReason.CAUGHT_UP and out.result.pages == 2
    # The list has 120 people at 10 a fetch, so reaching the real end takes 12; the
    # exact count short of that varies with the scroll plans a given seed draws
    # (#192 review: sharing the source's rng with the pointer-rest walk shifts
    # which ones, since the walk's own draws land before them once) -- what the
    # invariant actually is, and what stays true regardless, is that it stopped
    # well short of scrolling all the way to the list's own end.
    assert len(site.fetches) < 12


# --- one unit is bounded -----------------------------------------------------------------


async def test_each_unit_costs_one_gate_check_and_about_four_page_answers() -> None:
    site = FlagshipSite(many(200))
    gate = Gate(allow=2)
    out = await sync(site, gate=gate)
    assert out.result.reason is StopReason.BUDGET and gate.units == 2
    assert len(out.urns) == 80
    assert len(site.fetches) <= 8  # 70 more cards than the first screen, at ten an answer


async def test_a_zero_budget_loads_no_page() -> None:
    site = FlagshipSite(many(20))
    out = await sync(site, page_budget=0)
    assert out.result.reason is StopReason.PAGE_BUDGET
    assert site.requests == [] and all(page.goto_calls == [] for page in site.pages)


# --- classified before it is read --------------------------------------------------------


async def test_a_checkpoint_on_landing_stops_the_run_and_nothing_scrolls() -> None:
    site = FlagshipSite(many(20), landing=CHECKPOINT_URL)
    out = await sync(site)
    assert out.result.outcome is Outcome.CHECKPOINT and out.urns == []
    assert all(page.mouse.wheels == [] for page in site.pages)
    # #192: a landing that never scrolls must never rest the pointer either -- there
    # is no scroll plan for it to prepare.
    assert all(page.mouse.moves == [] for page in site.pages)


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (Answer(status=429, body=b"Too many requests"), Outcome.THROTTLED),
        (Answer(status=999, body=b""), Outcome.THROTTLED),
        (Answer(status=302, headers={"location": LOGIN_URL}), Outcome.LOGGED_OUT),
        (Answer(status=302, headers={"location": CHECKPOINT_URL}), Outcome.CHECKPOINT),
        (
            Answer(status=302, headers={"location": "https://www.linkedin.com/feed/"}),
            Outcome.ROUTE_CHANGED,
        ),
        (Answer(status=500, body=b"oops"), Outcome.ROUTE_CHANGED),
        # M1: an error page's own sign-out and sign-in links are not a wall.
        (Answer(status=500, body=SHELL), Outcome.ROUTE_CHANGED),
        (Answer(status=403, body=SHELL), Outcome.ROUTE_CHANGED),
        (Answer(status=401, body=SHELL), Outcome.LOGGED_OUT),
        (Answer(status=200, body=b"<html>not flight</html>"), Outcome.ROUTE_CHANGED),
        (Answer(status=200, body=b"0:{}\n", tab_url=CHECKPOINT_URL), Outcome.CHECKPOINT),
    ],
)
async def test_a_page_answer_that_is_not_ok_stops_the_run_as_what_it_is(
    answer: Answer, outcome: Outcome
) -> None:
    people = many(60)
    out = await sync(FlagshipSite(people, answers={20: answer}))
    assert out.result.reason is StopReason.RESPONSE and out.result.outcome is outcome
    assert out.urns == []  # the first unit of forty never filled
    assert not out.result.complete


async def test_a_stop_is_sticky() -> None:
    site = FlagshipSite(many(60), answers={20: Answer(status=429)})
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        source = PageConnections(run, sleep=no_sleep, response_wait_s=0.01, landing_wait_s=0.05)
        first = await source.fetch_page(start=0, count=40)
        again = await source.fetch_page(start=0, count=40)
    assert first.outcome is Outcome.THROTTLED and again is first


# --- a changed payload writes nothing ------------------------------------------------------


async def test_a_spoiled_card_on_a_later_page_stops_the_run_before_that_unit_is_written() -> None:
    """Card 45 names two profiles. The first unit (0-39) is written; the second unit
    is refused whole, so none of 40-79 is written -- not even the cards before 45."""
    people = many(100)
    site = FlagshipSite(people, card_options={45: CardOptions(other_profile_id="ACoAAFAKE9999999")})
    out = await sync(site)
    assert out.result.outcome is Outcome.ROUTE_CHANGED
    assert out.urns == [p.urn for p in people[:40]]


async def test_a_page_that_went_unseen_stops_the_run() -> None:
    out = await sync(FlagshipSite(many(60), skip=frozenset({20})))
    assert out.result.outcome is Outcome.ROUTE_CHANGED and not out.result.complete


async def test_a_page_asked_for_twice_is_read_once() -> None:
    people = many(45)
    out = await sync(FlagshipSite(people, repeat=frozenset({20})))
    assert out.urns == [p.urn for p in people] and out.result.complete


async def test_another_pagers_answers_are_not_the_list() -> None:
    people = many(25)
    out = await sync(FlagshipSite(people, other_pager=True))
    assert out.urns == [p.urn for p in people] and out.result.complete


async def test_an_incremental_sync_refuses_a_list_not_sorted_newest_first() -> None:
    site = FlagshipSite(many(60), sort="sortByFirstName")
    out = await sync(site, SyncMode.INCREMENTAL)
    assert out.result.outcome is Outcome.ROUTE_CHANGED
    full = await sync(FlagshipSite(many(25), sort="sortByFirstName"))
    assert full.result.complete  # a full sync reads the whole list in any order


async def test_an_incremental_sync_refuses_a_list_with_no_sort_state() -> None:
    """#188 review, S2: a request that carries no sort is not proof of newest first."""
    out = await sync(FlagshipSite(many(60), sort=None), SyncMode.INCREMENTAL)
    assert out.result.outcome is Outcome.ROUTE_CHANGED


async def test_a_page_of_the_list_before_its_first_screen_is_route_changed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#188 review, S2: an answer for start 10 arriving before the first screen is
    refused as out of order, before anything tries to read it as the first screen
    (which its cards' keys would refuse too, but only as a second line)."""
    out = await sync(FlagshipSite(many(30), early_pagination=True))
    assert out.result.outcome is Outcome.ROUTE_CHANGED and out.urns == []
    assert "a page of the list arrived before its first screen" in caplog.text


async def test_a_mechanism_failure_hands_over_the_units_read_whole_then_ends_the_run() -> None:
    """#188 review, follow-up 2: an answer too large to keep is the observation
    failing, not LinkedIn answering. The unit already read whole (0-39) is handed
    over, as before a RouteChanged; the next unit raises, so the run ends by exception:
    recorded failed, never complete, ageing nobody."""
    people = many(90)
    huge = Answer(status=200, body=b"0:" + b"0" * 500_000)
    site = FlagshipSite(people, answers={40: huge})
    sink: list[ConnectionsPage] = []
    with pytest.raises(ObservationFailed):
        await sync(site, limits=ObservationLimits(max_body_bytes=300_000), pages_sink=sink)
    assert [c.urn for page in sink for c in page.connections] == [p.urn for p in people[:40]]


async def test_a_first_screen_that_never_comes_is_route_changed() -> None:
    site = FlagshipSite(many(20), landing="screen")
    site.people = site.people  # the screen request still arrives...
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        source = PageConnections(run, sleep=no_sleep, response_wait_s=0.01, landing_wait_s=0.05)
        site.landing = "https://www.linkedin.com/mynetwork/invite-connect/connections/"
        answer = await source.fetch_page(start=0, count=40)  # ...not this time
    assert answer.outcome is Outcome.ROUTE_CHANGED


async def test_a_shell_that_links_to_sign_out_is_not_a_login_wall() -> None:
    """#188 review, M1: every logged-in page links to /uas/logout, /login, and more.
    A document with no first screen is judged by where the tab is, never by those
    links in its HTML: the screen request that follows is read, and the run completes
    without raising the session flag."""
    people = many(25)
    out = await sync(FlagshipSite(people, landing="screen"))
    assert out.result.outcome is None and out.result.complete
    assert out.urns == [p.urn for p in people]


async def test_a_wall_served_in_place_of_the_document_stops_the_run_unflagged() -> None:
    """A document with no first screen at the connections url, whatever its HTML
    links to, and no screen after it: it cannot be told apart from a page LinkedIn
    changed, so the run stops as RouteChanged -- never LoggedOut on the strength of a
    link, which would flag the session and, for a checkpoint, raise heat."""

    class ShellOnly(FlagshipSite):
        def navigated(self, tab, url):  # type: ignore[no-untyped-def]
            self._send(tab, "GET", url, 200, SHELL, "document")

    out = await sync(ShellOnly(many(20)))
    assert out.result.outcome is Outcome.ROUTE_CHANGED and out.urns == []


async def test_a_wall_the_tab_lands_on_is_still_read_as_one() -> None:
    """The real-wall case the fix keeps: the tab itself is at the login url."""
    out = await sync(FlagshipSite(many(20), landing=LOGIN_URL))
    assert out.result.outcome is Outcome.LOGGED_OUT


async def test_a_first_screen_without_cards_is_the_end_only_when_it_says_the_list_is_empty() -> (
    None
):
    empty = await sync(FlagshipSite([], total=0))
    assert empty.result.reason is StopReason.END_OF_LIST and not empty.result.complete
    cardless = FlagshipSite(many(20), first=0)
    out = await sync(cardless)
    assert out.result.outcome is Outcome.ROUTE_CHANGED and out.urns == []


async def test_a_page_that_never_sends_its_first_screen_is_given_up_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Document after document without the first screen: bounded, then RouteChanged."""

    class ShellsOnly(FlagshipSite):
        def navigated(self, tab, url):  # type: ignore[no-untyped-def]
            for _ in range(page_connections.MAX_LANDING_ANSWERS + 5):
                self._send(tab, "GET", url, 200, b"<html><body>loading</body></html>", "document")

    out = await sync(ShellsOnly(many(20)))
    assert out.result.outcome is Outcome.ROUTE_CHANGED and out.urns == []
    assert "8 answers arrived, none the first screen" in caplog.text


# --- an answer whose body cannot be read (#197) ---------------------------------------------


async def test_a_lost_answer_the_page_asks_for_again_is_read_and_the_run_completes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Run 5 on #31: the answer for start 20 arrives, its body cannot be read. The page
    asks for 20 again on the next scroll, that one reads, and the run completes."""
    caplog.set_level(logging.INFO, logger="netkeeper")
    people = many(55)
    site = FlagshipSite(people, lost={20: Lost("reask")})
    out = await sync(site)
    assert out.result.reason is StopReason.END_OF_LIST and out.result.complete
    assert out.urns == [p.urn for p in people] and out.result.lost is None
    assert "answer for start 20 could not be read (Exception (no resource))" in caplog.text
    assert "the page asked again for start 20, and it read" in caplog.text
    lost = [r for r in caplog.records if "could not be read" in r.getMessage()]
    assert [r.levelno for r in lost] == [logging.INFO]


async def test_a_lost_answer_read_on_the_retry_is_forgotten() -> None:
    """Once the page's second ask for 20 reads, the loss is over: a gap later in the
    same run is RouteChanged, as any gap is, not blamed on the answer already read."""
    out = await sync(FlagshipSite(many(80), lost={20: Lost("reask")}, skip=frozenset({40})))
    assert out.result.reason is StopReason.RESPONSE
    assert out.result.outcome is Outcome.ROUTE_CHANGED and out.result.lost is None


async def test_a_lost_answer_lost_again_on_the_retry_is_still_waited_out() -> None:
    people = many(45)
    out = await sync(FlagshipSite(people, lost={30: Lost("reask", times=2)}))
    assert out.result.complete and out.urns == [p.urn for p in people]


async def test_a_page_that_moves_past_a_lost_answer_reads_on_and_the_run_is_incomplete() -> None:
    """#200: the page read its own copy of 40-49 and asks for 50; netkeeper never saw
    40-49. The loss is recorded and the run reads on to the end of the list, never
    RouteChanged, never complete."""
    people = many(90)
    out = await sync(FlagshipSite(people, lost={40: Lost("move_on")}))
    result = out.result
    assert result.reason is StopReason.END_OF_LIST and result.outcome is None
    assert not result.complete and result.lost is None
    assert [(lost.start, lost.cause, lost.ending) for lost in result.losses] == [
        (40, "Exception (no resource)", "the page moved past it")
    ]
    assert out.urns == [p.urn for p in people[:40] + people[50:]]


async def test_a_lost_answer_the_page_never_asks_for_again_stops_after_the_idle_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page answers every other scroll plan, then loses 30 and asks for nothing
    more. Three idle plans came before the loss; the page still gets the full
    MAX_IDLE_SCROLLS after it to ask again, then the run stops ANSWER_LOST."""
    site = FlagshipSite(many(60), answer_plans=lambda n: n % 2 == 0, lost={30: Lost("silent")})
    _count_plans(monkeypatch, site)
    out = await sync(site)
    assert out.result.reason is StopReason.ANSWER_LOST and out.urns == []
    assert out.result.lost is not None and out.result.lost.start == 30
    assert out.result.lost.ending == (
        f"it was not read again within {page_connections.MAX_IDLE_SCROLLS} scrolls"
    )
    # Plans 2, 4, and 6 brought 10, 20, and the lost 30; six idle plans after it.
    assert site.plans == 6 + page_connections.MAX_IDLE_SCROLLS


async def test_a_retried_answer_without_a_body_for_a_page_already_read_is_skipped() -> None:
    """The duplicate is compared with the expected start before its missing body is
    looked at: already read, so skipped, never fatal."""
    people = many(45)
    out = await sync(FlagshipSite(people, lost={20: Lost("duplicate")}))
    assert out.result.complete and out.urns == [p.urn for p in people]


async def test_a_lost_answer_is_sticky_and_hands_over_only_whole_units() -> None:
    """With no loss allowed, moving past one stops the source: the unit read whole
    before it is still handed over, and every later call raises the same loss."""
    site = FlagshipSite(many(90), lost={40: Lost("move_on")})
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        source = PageConnections(
            run, sleep=no_sleep, response_wait_s=0.01, landing_wait_s=0.05, max_lost=0
        )
        first = await source.fetch_page(start=0, count=40)
        with pytest.raises(AnswerLost) as raised:
            await source.fetch_page(start=40, count=40)
        again = await source.fetch_page(start=0, count=40)
        with pytest.raises(AnswerLost) as twice:
            await source.fetch_page(start=40, count=40)
    assert first.page is not None and len(first.page.connections) == 40
    assert again.page is not None and again.page.connections == first.page.connections
    assert twice.value.lost is raised.value.lost and raised.value.lost.start == 40
    assert raised.value.earlier == () and twice.value.earlier == ()


async def test_a_lost_answer_never_puts_the_exception_message_in_a_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Playwright's messages quote urls; only the class and a fixed category are kept."""
    caplog.set_level(logging.DEBUG, logger="netkeeper")
    out = await sync(FlagshipSite(many(60), lost={20: Lost("move_on")}))
    (lost,) = out.result.losses
    assert "fake-lost-slug" in LOST_BODY_MESSAGE
    assert "fake-lost-slug" not in caplog.text and "identifier" not in caplog.text
    assert "fake-lost-slug" not in lost.describe()


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (RuntimeError("net::ERR_ABORTED at https://x.test/in/a"), "RuntimeError (aborted)"),
        (Exception("Request content was evicted from inspector cache"), "Exception (evicted)"),
        (ValueError("something else entirely"), "ValueError (unclassified)"),
    ],
)
async def test_a_lost_answer_names_its_cause_in_fixed_words(error: Exception, cause: str) -> None:
    out = await sync(FlagshipSite(many(60), lost={20: Lost("move_on", error=error)}))
    assert [lost.cause for lost in out.result.losses] == [cause]


async def test_a_gap_without_a_lost_answer_is_still_route_changed() -> None:
    """The stall and gap rules are unchanged: only a lost answer turns either into
    ANSWER_LOST."""
    gap = await sync(FlagshipSite(many(60), skip=frozenset({20})))
    stall = await sync(FlagshipSite(many(40), end="stall"))
    for out in (gap, stall):
        assert out.result.reason is StopReason.RESPONSE
        assert out.result.outcome is Outcome.ROUTE_CHANGED and out.result.lost is None


async def test_a_lost_first_screen_is_still_the_observation_failing() -> None:
    """Only a pagination answer can be waited out: without the first screen there is no
    list to scroll, so a landing whose body cannot be read fails as before."""

    class LostScreen(FlagshipSite):
        def _send(self, tab, method, url, status, body, resource_type, post_data=None, **kw):  # type: ignore[no-untyped-def]
            if method == "GET":
                kw["body_error"] = Exception(LOST_BODY_MESSAGE)
            super()._send(tab, method, url, status, body, resource_type, post_data, **kw)

    with pytest.raises(ObservationFailed):
        await sync(LostScreen(many(20)))


@pytest.mark.parametrize(
    ("duplicate", "outcome"),
    [
        (Answer(status=429, body=b"Too many requests"), Outcome.THROTTLED),
        (Answer(status=999), Outcome.THROTTLED),
        (Answer(status=302, headers={"location": CHECKPOINT_URL}), Outcome.CHECKPOINT),
        (Answer(status=302, headers={"location": LOGIN_URL}), Outcome.LOGGED_OUT),
    ],
    ids=["429", "999", "checkpoint", "login"],
)
async def test_a_wall_or_throttle_on_a_stale_start_still_stops_the_run(
    duplicate: Answer, outcome: Outcome
) -> None:
    """#198 review, M2: the start comparison skips a stale answer's missing body, never
    its status. A retried request for a page already read that comes back throttled
    or walled is still that throttle or that wall."""
    out = await sync(FlagshipSite(many(60), duplicate_answers={20: duplicate}))
    assert out.result.reason is StopReason.RESPONSE and out.result.outcome is outcome
    assert out.result.lost is None


async def test_a_stale_answer_without_a_body_leaves_nothing_lost() -> None:
    """#198 review, L1: a retried copy of page 20, already read, arrives without a body;
    then the page stops. That is a stall, route_changed as any stall is, never a lost
    answer: the stale copy was skipped before its body was looked at."""
    out = await sync(FlagshipSite(many(30), end="stall", lost={20: Lost("duplicate")}))
    assert out.result.reason is StopReason.RESPONSE
    assert out.result.outcome is Outcome.ROUTE_CHANGED and out.result.lost is None


async def test_an_answer_lost_on_every_ask_stops_within_the_idle_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#198 review, L4: the page asks for 20 on every scroll and every copy is lost. The
    idle count restarts at the first loss only, so exactly MAX_IDLE_SCROLLS more scrolls
    are spent. A count that restarted on every loss would scroll for ever; the hard
    bound below turns that into a failed assertion instead of a hang."""
    site = FlagshipSite(
        many(60), answer_plans=lambda n: True, lost={20: Lost("reask", times=10_000)}
    )
    real = BrowserRun.scroll

    async def bounded(self: BrowserRun, plan: ScrollPlan, **kwargs: Any) -> ScrollOutcome:
        tab = site.pages[-1]
        assert isinstance(tab, ListeningTab)
        site.plan_started(tab)
        assert site.plans <= 50, "the idle bound never stopped the scroll"
        return await real(self, plan, **kwargs)

    monkeypatch.setattr(BrowserRun, "scroll", bounded)
    out = await sync(site)
    assert out.result.reason is StopReason.ANSWER_LOST and out.result.lost is not None
    assert out.result.lost.start == 20
    # Plan 1 brought page 10, plan 2 the first lost 20; then the bound, from the loss.
    assert site.plans == 2 + page_connections.MAX_IDLE_SCROLLS


# --- #200: reading on past a lost answer --------------------------------------------------


async def test_several_lost_answers_are_each_read_past_and_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Run 7 on #31, several times over: every answer the page moved past is recorded,
    in order, and the run reads on to the end of the list, incomplete."""
    caplog.set_level(logging.INFO, logger="netkeeper")
    people = many(120)
    out = await sync(FlagshipSite(people, lost={20: Lost("move_on"), 70: Lost("move_on")}))
    result = out.result
    assert result.reason is StopReason.END_OF_LIST and not result.complete
    assert [lost.start for lost in result.losses] == [20, 70]
    assert out.urns == [p.urn for p in people[:20] + people[30:70] + people[80:]]
    assert "reading on, 1 lost in this run" in caplog.text
    assert "reading on, 2 lost in this run" in caplog.text
    assert "lost in this run (starts 20, 70); the run is incomplete" in caplog.text


async def test_an_incremental_sync_reads_past_the_slice_after_a_loss() -> None:
    """#201 review, L1: 0-44 are new, 45-119 known. Answer 40 is lost and read past;
    the slice after it (50-89) is all known, but it follows the loss, so it cannot
    prove the run caught up. The run reads on, and records the loss."""
    people = many(120)
    known = [p.urn for p in people[45:]]
    out = await sync(
        FlagshipSite(people, lost={40: Lost("move_on")}), SyncMode.INCREMENTAL, known=known
    )
    assert out.result.pages >= 3  # a stop at the slice after the loss would be 2
    assert [x.start for x in out.result.losses] == [40]


async def test_a_pending_loss_is_reported_when_a_wall_stops_the_run() -> None:
    """#201 review, L2: 40 is lost and the page's next answer, for 50, is a checkpoint.
    The loss was never settled by the page, but it is still reported with the stop."""
    people = many(90)
    wall = Answer(status=302, headers={"location": CHECKPOINT_URL})
    out = await sync(FlagshipSite(people, lost={40: Lost("move_on")}, answers={50: wall}))
    assert out.result.reason is StopReason.RESPONSE
    assert out.result.outcome is Outcome.CHECKPOINT
    assert [(x.start, x.ending) for x in out.result.losses] == [
        (40, "the run stopped before it was read again")
    ]


async def test_a_loss_read_past_is_reported_with_a_stop_in_the_same_unit() -> None:
    """#201 review, L3 (M3): 70 is lost and read past, then 80 is throttled, both while
    the source reads the unit 40-79, which is still short. The stop comes back instead
    of that unit, and carries the loss."""
    out = await sync(
        FlagshipSite(many(120), lost={70: Lost("move_on")}, answers={80: Answer(status=429)})
    )
    assert out.result.reason is StopReason.RESPONSE
    assert out.result.outcome is Outcome.THROTTLED and out.result.pages == 1
    assert [x.start for x in out.result.losses] == [70]


async def test_lost_places_cannot_prove_an_early_end() -> None:
    """#201 review, L3 (M5): 20 is lost and read past; the page's answer for 40 is full
    and asks for nothing, though the first screen says 60. 40 read plus 10 lost is 50,
    short of 60, so that is no end: the page stops, and the run is RouteChanged."""
    people = many(60)
    early = Answer(body=pagination_payload(people[40:50], start=40, next_start=None))
    out = await sync(FlagshipSite(people, lost={20: Lost("move_on")}, answers={40: early}))
    assert out.result.reason is StopReason.RESPONSE
    assert out.result.outcome is Outcome.ROUTE_CHANGED


async def test_a_failure_after_a_loss_names_the_loss_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="netkeeper")
    huge = Answer(status=200, body=b"0" * (8 * 1024 * 1024 + 1))
    with pytest.raises(ObservationFailed):
        await sync(FlagshipSite(many(90), lost={20: Lost("move_on")}, answers={30: huge}))
    assert "the run failed with 1 of the page's answers lost (starts 20)" in caplog.text


async def test_as_many_losses_as_the_cap_still_read_to_the_end() -> None:
    people = many(160)
    lost = {start: Lost("move_on") for start in (20, 40, 60, 80, 100)}
    out = await sync(FlagshipSite(people, lost=lost))
    assert out.result.reason is StopReason.END_OF_LIST and not out.result.complete
    assert [x.start for x in out.result.losses] == [20, 40, 60, 80, 100]


async def test_one_loss_more_than_the_cap_stops_the_run_as_answer_lost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="netkeeper")
    people = many(200)
    lost = {start: Lost("move_on") for start in (20, 40, 60, 80, 100, 120)}
    out = await sync(FlagshipSite(people, lost=lost))
    result = out.result
    assert result.reason is StopReason.ANSWER_LOST and not result.complete
    assert result.lost is not None and result.lost.start == 120
    assert result.lost.ending == (
        "the page moved past it, and that is 6 answers lost in this run,"
        " more than the 5 a run may lose"
    )
    assert [x.start for x in result.losses] == [20, 40, 60, 80, 100, 120]
    # Only whole units read before the stop were handed over: 0-39, 40-79 (less the
    # lost places), and so on; nothing after the stop.
    assert len(out.urns) % 40 == 0 and len(out.urns) <= 200 - 60


async def test_losses_read_past_before_a_stall_are_reported_with_the_stop() -> None:
    """20 is moved past (recorded, read on); 50 is lost and the page asks for nothing
    more. The stall after that loss stops the run, and the earlier loss rides along
    with the stopping one, so neither goes unreported."""
    out = await sync(FlagshipSite(many(90), lost={20: Lost("move_on"), 50: Lost("silent")}))
    result = out.result
    assert result.reason is StopReason.ANSWER_LOST
    assert result.lost is not None and result.lost.start == 50
    assert result.lost.ending.startswith("it was not read again within")
    assert [x.start for x in result.losses] == [20, 50]


async def test_a_page_that_jumps_further_than_one_answer_past_a_loss_stops_the_run() -> None:
    """Lost 40, then the page asks for 60: 50-59 went unseen as well as 40-49. The run
    stops safely as answer_lost rather than read on over a gap it cannot account for."""
    out = await sync(FlagshipSite(many(90), lost={40: Lost("move_on")}, skip=frozenset({50})))
    result = out.result
    assert result.reason is StopReason.ANSWER_LOST and result.outcome is None
    assert result.lost is not None and result.lost.start == 40
    assert result.lost.ending == "the page moved more than one answer past it"


async def test_a_lost_answers_places_count_toward_a_full_last_page_ending_the_list() -> None:
    """A list of 60, whose full last answer asks for nothing: that proves the end only
    once the run has accounted for all 60 places. 50 were read and 10 were lost."""
    out = await sync(FlagshipSite(many(60), end="short", lost={30: Lost("move_on")}))
    assert out.result.reason is StopReason.END_OF_LIST and not out.result.complete
    assert [x.start for x in out.result.losses] == [30]


async def test_a_new_loss_after_reading_on_gets_its_own_idle_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """20 is lost, three scrolls bring nothing, then the page moves past 20 and loses
    30, and asks for nothing more: the page still gets MAX_IDLE_SCROLLS scrolls from
    30's loss, not what was left of 20's."""
    site = FlagshipSite(
        many(60),
        answer_plans=lambda n: n in (1, 2, 6),
        lost={20: Lost("move_on"), 30: Lost("silent")},
    )
    _count_plans(monkeypatch, site)
    out = await sync(site)
    assert out.result.reason is StopReason.ANSWER_LOST and out.result.lost is not None
    assert out.result.lost.start == 30 and [x.start for x in out.result.losses] == [20, 30]
    # Plan 1 brought 10, 2 the lost 20, 3-5 nothing, 6 moved past 20 with the lost 30.
    assert site.plans == 6 + page_connections.MAX_IDLE_SCROLLS


async def test_the_loss_log_carries_the_fixed_diagnostics_and_no_promise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#200 items 2 and 3: the loss line names fixed facts only, and no line promises a
    scroll that a stop then contradicts."""
    caplog.set_level(logging.INFO, logger="netkeeper")
    await sync(FlagshipSite(many(60), lost={20: Lost("move_on")}), max_lost=0)
    (loss,) = [
        r.getMessage()
        for r in caplog.records
        if r.name == page_connections.__name__ and "could not be read (" in r.getMessage()
    ]
    for key in (
        "service_worker=",
        "status=200",
        "content_type=",
        "content_length=",
        "transfer_encoding=",
        "request=",
        "read_after_ms=",
        "failed_after_ms=",
        "reads_in_flight=",
        "previous_answer_bytes=",
    ):
        assert key in loss
    assert "scrolling on" not in caplog.text
    stops = [r for r in caplog.records if "stopping incomplete" in r.getMessage()]
    assert [r.levelno for r in stops] == [logging.WARNING]
    assert "observation: answers_kept=" in caplog.text


async def test_the_same_start_lost_again_on_a_re_ask_is_logged_as_such(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="netkeeper")
    out = await sync(FlagshipSite(many(45), lost={30: Lost("reask", times=2)}))
    assert out.result.complete and out.result.losses == ()
    assert "the page asked again for start 30, and it could not be read again" in caplog.text


# --- #200 Part B: the streamed copy of an answer Chrome kept no body for -------------------


async def test_a_lost_answer_is_read_from_its_streamed_copy_and_nothing_is_lost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cause Part B found: the page read answer 40 and aborted its stream, so
    Chrome kept no body. The body tap's copy agrees with what the page asked next
    (50, ten cards in between), so it stands in: nothing is lost, the run completes."""
    caplog.set_level(logging.INFO, logger="netkeeper")
    people = many(90)
    site = FlagshipSite(people, tap=True, lost={40: Lost("move_on", streamed="whole")})
    out = await sync(site)
    assert out.result.reason is StopReason.END_OF_LIST and out.result.complete
    assert out.result.losses == () and out.urns == [p.urn for p in people]
    assert "answer for start 40 was read from the copy streamed as it arrived" in caplog.text
    assert "streamed_bytes=" in caplog.text
    cdp = site.cdp
    assert cdp is not None and cdp.detached
    assert {method for method, _ in cdp.sent} == {
        "Network.enable",
        "Network.streamResourceContent",
    }
    assert cdp.sent[0] == (
        "Network.enable",
        {"maxTotalBufferSize": 32 * 1024 * 1024, "maxResourceBufferSize": 8 * 1024 * 1024},
    )


@pytest.mark.parametrize("streamed", ["half", "none", "short"])
async def test_a_streamed_copy_cut_short_or_refused_leaves_the_answer_lost(streamed: str) -> None:
    people = many(90)
    site = FlagshipSite(people, tap=True, lost={40: Lost("move_on", streamed=streamed)})
    out = await sync(site)
    assert out.result.reason is StopReason.END_OF_LIST and not out.result.complete
    assert [x.start for x in out.result.losses] == [40]


async def test_a_streamed_copy_that_disagrees_with_the_page_is_not_used(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The copy of 40 asks for 50, but the page asked for 60: it does not stand in, and
    the jump past the lost start stops the run safely."""
    caplog.set_level(logging.INFO, logger="netkeeper")
    site = FlagshipSite(
        many(90),
        tap=True,
        lost={40: Lost("move_on", streamed="whole")},
        skip=frozenset({50}),
    )
    out = await sync(site)
    assert out.result.reason is StopReason.ANSWER_LOST
    assert out.result.lost is not None and out.result.lost.start == 40
    assert "does not agree with what the page asked for next; not used" in caplog.text


async def test_a_streamed_last_answer_that_proves_the_end_stands_in_after_a_stall() -> None:
    """The last answer (20-24, short, asking for nothing) is lost and the page goes
    quiet. Its streamed copy proves the end of the list, so the run ends there."""
    people = many(25)
    site = FlagshipSite(people, end="short", tap=True, lost={20: Lost("silent", streamed="whole")})
    out = await sync(site)
    assert out.result.reason is StopReason.END_OF_LIST and out.result.complete
    assert out.urns == [p.urn for p in people]


async def test_a_lost_full_last_answer_is_rescued_when_the_copy_and_the_total_prove_the_end() -> (
    None
):
    """#202 review, item 3: a list of 30, whose last answer (20-29) is full and asks for
    nothing, is lost, and the page goes quiet. The copy asks for nothing, and with it
    the run has all 30 places the first screen names: that proves the end."""
    people = many(30)
    site = FlagshipSite(people, end="short", tap=True, lost={20: Lost("silent", streamed="whole")})
    out = await sync(site)
    assert out.result.reason is StopReason.END_OF_LIST and out.result.complete
    assert out.urns == [p.urn for p in people]


async def test_the_places_of_an_answer_read_past_count_toward_a_stall_rescue() -> None:
    """10 was lost with no copy and read past; the last answer (30-39) is lost too and
    the page goes quiet. Its copy proves the end once the 10 lost places are counted:
    30 read plus 10 lost is the first screen's 40. The run ends, incomplete."""
    site = FlagshipSite(
        many(40),
        end="short",
        tap=True,
        lost={10: Lost("move_on"), 30: Lost("silent", streamed="whole")},
    )
    out = await sync(site)
    assert out.result.reason is StopReason.END_OF_LIST and not out.result.complete
    assert [x.start for x in out.result.losses] == [10]


async def test_a_copy_that_ends_the_list_short_of_the_total_is_not_used_after_a_stall() -> None:
    """#202 review, item 2: the copy of the last answer (20-24) ends the list, but the
    first screen says 40: the run cannot account for every place, so the answer stays
    lost."""
    site = FlagshipSite(
        many(25), end="short", total=40, tap=True, lost={20: Lost("silent", streamed="whole")}
    )
    out = await sync(site)
    assert out.result.reason is StopReason.ANSWER_LOST
    assert out.result.lost is not None and out.result.lost.start == 20


async def test_a_streamed_copy_that_proves_nothing_after_a_stall_is_not_used() -> None:
    site = FlagshipSite(many(60), tap=True, lost={30: Lost("silent", streamed="whole")})
    out = await sync(site)
    assert out.result.reason is StopReason.ANSWER_LOST
    assert out.result.lost is not None and out.result.lost.start == 30


async def test_a_browser_without_cdp_sessions_reads_as_before(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="netkeeper")
    out = await sync(FlagshipSite(many(45)))
    assert out.result.complete
    assert "no body tap on this tab (RuntimeError)" in caplog.text


async def test_a_tap_whose_session_cannot_enable_the_network_is_closed_and_unused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="netkeeper")

    class Refusing(FakeCdpSession):
        async def send(self, method: str, params: Any = None) -> Any:
            await super().send(method, params)
            raise RuntimeError("'Network.enable' wasn't found")

    class Site(FlagshipSite):
        async def new_cdp_session(self, page: object) -> FakeCdpSession:
            self.cdp = Refusing()
            return self.cdp

    site = Site(many(45), tap=True)
    out = await sync(site)
    assert out.result.complete
    assert "the body tap could not start (RuntimeError)" in caplog.text
    assert site.cdp is not None and site.cdp.detached
    assert [method for method, _ in site.cdp.sent] == ["Network.enable"]


class _TapStartFails(FlagshipSite):
    """A site whose tap session fails while starting: ``on`` raises, or ``Network.enable``
    is cancelled mid-send (#202 review, item 4)."""

    def __init__(self, how: str) -> None:
        super().__init__(many(20), tap=True)
        self.how = how

    async def new_cdp_session(self, page: object) -> FakeCdpSession:
        how = self.how

        class Failing(FakeCdpSession):
            def on(self, event: str, handler: Any) -> None:
                if how == "on":
                    raise RuntimeError("listener refused")
                super().on(event, handler)

            async def send(self, method: str, params: Any = None) -> Any:
                await super().send(method, params)
                if how == "cancel":
                    raise asyncio.CancelledError
                return {}

        self.cdp = Failing()
        return self.cdp


async def test_a_listener_that_raises_detaches_the_tap_session() -> None:
    site = _TapStartFails("on")
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        observation = await run.observe(_MATCH, tap=True)
        assert observation._tap is None
    assert site.cdp is not None and site.cdp.detached and site.cdp.sent == []


async def test_a_cancellation_while_enabling_detaches_the_session_and_propagates() -> None:
    site = _TapStartFails("cancel")
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        with pytest.raises(asyncio.CancelledError):
            await run.observe(_MATCH, tap=True)
    assert site.cdp is not None and site.cdp.detached
    assert [method for method, _ in site.cdp.sent] == ["Network.enable"]


_MATCH = ResponseMatch(
    origin="https://www.linkedin.com",
    rules=(ResponseRule("POST", "/flagship-web/rsc-action/actions/pagination"),),
)


async def test_a_connections_page_navigation_timeout_still_ends_the_run() -> None:
    """#197's enrichment fix forgives a profile navigation that times out; the
    connections page's does not change: the run still ends by that exception."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    class SlowToLoad(FlagshipSite):
        async def new_page(self):  # type: ignore[no-untyped-def]
            tab = await super().new_page()
            assert isinstance(tab, ListeningTab)
            tab.fail_next_goto(PlaywrightTimeoutError("Page.goto: Timeout"), closes=False)
            return tab

    with pytest.raises(PlaywrightTimeoutError):
        await sync(SlowToLoad(many(20)))


# --- the tab and the observation ------------------------------------------------------------


async def test_a_tab_replaced_mid_read_aborts_the_run() -> None:
    site = FlagshipSite(many(60))
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        source = PageConnections(run, sleep=no_sleep, response_wait_s=0.01, landing_wait_s=0.05)
        await source.fetch_page(start=0, count=10)  # landed; the first screen is enough
        site.pages[0].user_closed_it()
        with pytest.raises(BrowserUnavailable, match="replaced"):
            await source.fetch_page(start=10, count=40)


async def test_an_answer_too_large_to_keep_fails_the_observation() -> None:
    with pytest.raises(ObservationFailed):
        await sync(FlagshipSite(many(20)), limits=ObservationLimits(max_body_bytes=64))


def test_only_linkedin_or_loopback_may_be_read() -> None:
    provider, _ = fake_provider(FlagshipSite())
    with pytest.raises(ValueError, match="never"):
        PageConnections(provider, origin="https://www.linkedin.com.evil.example.test")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PageConnections(provider, origin="https://www.linkedin.com/in/x")  # type: ignore[arg-type]
    loopback = PageConnections(provider, origin="http://127.0.0.1:9555")  # type: ignore[arg-type]
    assert loopback.page_url == "http://127.0.0.1:9555/mynetwork/invite-connect/connections/"
    assert PageConnections(provider).page_url == PAGE_URL  # type: ignore[arg-type]


def test_the_source_bounds_are_pinned() -> None:
    assert page_connections.MAX_IDLE_SCROLLS == 6
    assert page_connections.RESPONSE_WAIT_S == 5.0
    assert page_connections.LANDING_WAIT_S == 20.0
    assert page_connections.MAX_LANDING_ANSWERS == 8
    assert page_connections.MAX_LOST_PER_RUN == 5


async def test_no_name_slug_or_urn_reaches_a_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="netkeeper")
    people = many(45)
    await sync(FlagshipSite(people, repeat=frozenset({20}), skip=frozenset({40})))
    text = caplog.text
    assert caplog.records  # the run did log: counts
    for person in people:
        assert person.slug not in text and person.urn not in text
        assert person.last not in text and (person.headline or "\0") not in text
