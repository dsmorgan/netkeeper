"""netkeeper.linkedin.dom: the DOM fallback, offline (P2-08, #173 review).

Drives :class:`DomConnectionsSource` and :class:`DomContactInfoSource` against
``tests/browser_fakes.py``'s fake tab through a real ``AttachBrowserProvider``
and ``BrowserRun``, the same way ``tests/test_linkedin_fetch.py`` drives
``PageVoyagerFetch``. The fakes cannot run real JavaScript, so
``SequencedPage`` below stands in for ``page.evaluate`` with a scripted queue
of Python values instead -- what a real page.evaluate would have computed,
handed over directly. Structural checks on the generated scripts' text (that
the right selectors are embedded, that a query is actually scoped to the
container/dialog element, that a malformed percent-encoding is caught) are
not this file's job; what a real browser actually renders and how
``page.evaluate`` behaves against it is ``tests/smoke/test_dom_smoke.py``'s.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from browser_fakes import FakeBrowser, FakeConnector, FakeContext, FakeMouse, FakePage

from netkeeper.linkedin.browser import AttachBrowserProvider, BrowserRun, PageLike
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.connections import (
    ConnectionsPage,
    SyncJobSpec,
    SyncMode,
    run_connections_sync,
)
from netkeeper.linkedin.contact_info import ContactInfoResult
from netkeeper.linkedin.dom import (
    CONNECTIONS_LIST_PATH,
    CONTACT_INFO_OVERLAY_PATH_TEMPLATE,
    CONTAINER_RETRY_PAUSE_S,
    LINKEDIN_ORIGIN,
    MAX_SETTLE_ATTEMPTS,
    DomConnectionsSource,
    DomContactInfoSource,
    DomFetchError,
    _parse_cards,
    _parse_contact_info_dom,
    _split_name,
)
from netkeeper.linkedin.voyager import RouteChanged

CDP_URL = "http://127.0.0.1:9222"
ORIGIN = "http://127.0.0.1:52341"
OTHER_ORIGIN = "http://127.0.0.1:9"


async def _fast_sleep(seconds: float) -> None:
    """Stands in for real waits: every test here runs instantly."""
    return None


class SequencedPage(FakePage):
    """A tab whose ``evaluate`` pops the next scripted result, and whose ``goto``
    (or a wheel count threshold) can land somewhere other than where it was sent,
    so a test can simulate a checkpoint, a login wall, or an origin change --
    ``page.url`` genuinely changing, not merely `told to`.
    """

    def __init__(self, context: SequencedContext) -> None:
        super().__init__(context)

    @property
    def url(self) -> str:
        context = self.context
        assert isinstance(context, SequencedContext)
        threshold: tuple[int, str] | None = context.url_after_wheels
        if threshold is not None and len(self.mouse.wheels) >= threshold[0]:
            return threshold[1]
        return self._url

    async def goto(self, url: str) -> object:
        result = await super().goto(url)
        redirect = self.context.redirect_to  # type: ignore[attr-defined]
        if redirect is not None:
            self._url = redirect
        return result

    async def evaluate(self, expression: str) -> Any:
        assert not self.is_closed(), "evaluated on a closed tab"
        self.evaluate_calls.append(expression)
        if self._evaluate_error is not None:
            error, self._evaluate_error = self._evaluate_error, None
            raise error
        context = self.context
        assert isinstance(context, SequencedContext)
        return context.next_result()


class SequencedContext(FakeContext):
    """Hands out :class:`SequencedPage` tabs and a scripted ``evaluate`` queue.

    ``results``: each ``page.evaluate`` call across the whole test pops the next
    entry; the last is repeated once exhausted, so a test does not have to
    script every settle-loop attempt exactly. ``url_after_wheels``, when given,
    is ``(threshold, url)``: once the page's total ``mouse.wheel`` calls reach
    ``threshold``, :attr:`SequencedPage.url` reports ``url`` instead of
    wherever ``goto`` last put it -- simulating a redirect or a session kicked
    to a login wall *during* a scroll, which no ``goto``-based mechanism alone
    can (#173 review, R9/R10).
    """

    def __init__(
        self,
        results: Sequence[Any],
        *,
        redirect_to: str | None = None,
        url_after_wheels: tuple[int, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._results: list[Any] = list(results)
        self.redirect_to = redirect_to
        self.url_after_wheels = url_after_wheels

    def next_result(self) -> Any:
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0] if self._results else None

    async def new_page(self) -> PageLike:
        self.new_page_calls += 1
        if self.new_page_error is not None:
            raise self.new_page_error
        page = SequencedPage(self)
        if self.page_goto_error is not None:
            page.fail_next_goto(self.page_goto_error)
        self.pages.append(page)
        return page


def run_with(context: FakeContext) -> AbstractAsyncContextManager[BrowserRun]:
    connector = FakeConnector([FakeBrowser([context])])
    provider = AttachBrowserProvider(CDP_URL, connector=connector)
    return provider.run()


def card(public_id: str, name: str, headline: str | None = None) -> dict[str, object]:
    return {"publicId": public_id, "name": name, "headline": headline}


def read(
    cards: list[dict[str, object]], *, container: bool = True, end_of_list: bool = False
) -> dict[str, object]:
    """One scripted ``evaluate`` result for the connections list: the
    ``{containerPresent, cards, endOfList}`` shape ``_cards_expression`` actually
    returns (``endOfList`` added by #174 item 5)."""
    return {"containerPresent": container, "cards": cards, "endOfList": end_of_list}


def overlay(
    *,
    dialog: bool = True,
    email: str | None = None,
    phones: list[str] | None = None,
    websites: list[str] | None = None,
    twitter: list[str] | None = None,
) -> dict[str, object]:
    """One scripted ``evaluate`` result for the contact-info overlay: the
    ``{dialogPresent, email, phones, websites, twitterHandles}`` shape
    ``_contact_info_expression`` actually returns."""
    if not dialog:
        return {"dialogPresent": False}
    return {
        "dialogPresent": True,
        "email": email,
        "phones": phones or [],
        "websites": websites or [],
        "twitterHandles": twitter or [],
    }


# --- DomConnectionsSource: parsing helpers -------------------------------------------


def test_split_name_on_the_first_run_of_whitespace() -> None:
    assert _split_name("Jamie Rivera") == ("Jamie", "Rivera")
    assert _split_name("Jamie   van der Rivera") == ("Jamie", "van der Rivera")
    assert _split_name("Prince") == ("Prince", "")


def test_split_name_collapses_embedded_newlines_and_runs_of_space() -> None:
    """S6 of the #173 review: a card whose name selector missed and fell back to a
    link's full text can carry a headline glued on after blank lines. The split
    must never hand back a last name carrying an embedded line break."""
    first, last = _split_name("Priya Okafor\n\n  Data engineer at Fictional")
    assert "\n" not in last
    assert (first, last) == ("Priya", "Okafor Data engineer at Fictional")


def test_parse_cards_skips_one_bad_card_but_keeps_the_rest() -> None:
    raw = [
        card("jamie-fake", "Jamie Rivera", "Designer"),
        {"nope": True},
        card("alex-fake", "Alex Chen"),
    ]
    cards = _parse_cards("dom/connections-list", raw)
    assert [c.public_id for c in cards] == ["jamie-fake", "alex-fake"]
    assert cards[0].first_name == "Jamie" and cards[0].last_name == "Rivera"
    assert cards[0].headline == "Designer"
    assert cards[1].headline is None
    assert all(c.urn is None and c.connected_at is None for c in cards)


def test_parse_cards_raises_route_changed_when_every_card_is_unreadable() -> None:
    with pytest.raises(RouteChanged):
        _parse_cards("dom/connections-list", [{"nope": 1}, {"also-nope": 2}])


def test_parse_cards_raises_route_changed_for_a_non_list_result() -> None:
    with pytest.raises(RouteChanged):
        _parse_cards("dom/connections-list", {"not": "a list"})


def test_parse_cards_on_an_empty_list_is_an_empty_tuple_not_route_changed() -> None:
    assert _parse_cards("dom/connections-list", []) == ()


def test_parse_cards_normalizes_an_empty_headline_to_none() -> None:
    """#173 review, R17: an empty-string headline must not survive as ``""``."""
    cards = _parse_cards("dom/connections-list", [card("a", "A One", "")])
    assert cards[0].headline is None


# --- DomConnectionsSource: fetch_page --------------------------------------------


async def test_the_first_call_navigates_to_the_connections_list() -> None:
    context = SequencedContext([read([card("jamie-fake", "Jamie Rivera", "Designer")])])
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        await source.fetch_page(start=0, count=1)
    assert context.pages[0].goto_calls[0] == f"{ORIGIN}{CONNECTIONS_LIST_PATH}"


async def test_a_page_slices_the_scrolled_so_far_list_by_start_and_count() -> None:
    all_cards = [card(f"person-{i}", f"Given{i} Family{i}") for i in range(5)]
    context = SequencedContext([read(all_cards)])
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        first = await source.fetch_page(start=0, count=2)
        second = await source.fetch_page(start=2, count=2)
    assert first.page is not None
    assert [c.public_id for c in first.page.connections] == ["person-0", "person-1"]
    assert second.page is not None
    assert [c.public_id for c in second.page.connections] == ["person-2", "person-3"]
    # Every DOM page is honest about not knowing a total (see the module docstring).
    assert first.page.total == 0 and second.page.total == 0
    assert all(c.urn is None for c in first.page.connections)


async def test_only_the_first_call_navigates_later_calls_reuse_the_tab() -> None:
    all_cards = [card(f"person-{i}", f"Given{i} Family{i}") for i in range(4)]
    context = SequencedContext([read(all_cards)])
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        await source.fetch_page(start=0, count=2)
        await source.fetch_page(start=2, count=2)
    assert len(context.pages[0].goto_calls) == 1


async def test_scrolling_settles_across_a_few_attempts_until_enough_cards_load() -> None:
    """The list grows lazily: attempt 1 has 1 card, attempt 2 has 3 -- enough for a
    page of 2 -- and the settle loop should stop scrolling once it does."""
    context = SequencedContext(
        [
            read([card("a", "A One")]),
            read([card("a", "A One"), card("b", "B Two"), card("c", "C Three")]),
        ]
    )
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=2)
    assert result.page is not None
    assert [c.public_id for c in result.page.connections] == ["a", "b"]
    assert len(context.pages[0].evaluate_calls) == 2  # exactly the two attempts needed


async def test_scrolling_gives_up_after_max_settle_attempts_and_refuses() -> None:
    """#173 review, F5(c); the stall half of #174 item 5: the list never grows
    past one card, count=5 is never reached, and LinkedIn's own end-of-list
    marker never appears either -- this is a stall, not a confirmed end.
    Exhausting every settle attempt without a confirmed answer is a refusal
    (ROUTE_CHANGED), never an ``Ok`` short page -- this module cannot tell
    "the list truly has only one connection" apart from "a slow render never
    caught up", and guessing the friendlier reading is exactly the mistake a
    fallback exists not to make. See the sibling test below for the case this
    one is deliberately not: the same growth, but the marker present."""
    context = SequencedContext([read([card("a", "A One")])])
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=5)
    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.page is None
    # Bounded: MAX_SETTLE_ATTEMPTS reads, not one per caller-requested count.
    assert len(context.pages[0].evaluate_calls) == MAX_SETTLE_ATTEMPTS


async def test_scrolling_gives_up_but_the_end_of_list_marker_makes_it_a_clean_page() -> None:
    """#174 item 5: the same exhaustion as the test above -- growth stalls at
    one card and count=5 is never reached -- but this time LinkedIn's own
    end-of-list marker is present on every read. That distinguishes a clean
    end of list from a stall: exhaustion now hands back whatever was actually
    accumulated as a confirmed, short ``Ok`` page instead of refusing."""
    context = SequencedContext([read([card("a", "A One")], end_of_list=True)])
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=5)
    assert result.outcome is Outcome.OK
    assert result.page is not None
    assert [c.public_id for c in result.page.connections] == ["a"]
    assert result.page.total == 0  # still never a claimed total -- module docstring
    assert len(context.pages[0].evaluate_calls) == MAX_SETTLE_ATTEMPTS


async def test_the_end_of_list_marker_does_not_short_circuit_normal_growth() -> None:
    """The marker only matters once the settle loop actually exhausts; while the
    list is still growing toward what was asked for, its presence changes
    nothing -- a scroll that keeps reaching the requested count behaves the
    same whether or not the page also renders the marker early."""
    context = SequencedContext(
        [
            read([card("a", "A One")], end_of_list=True),
            read([card("a", "A One"), card("b", "B Two")], end_of_list=True),
        ]
    )
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=2)
    assert result.outcome is Outcome.OK
    assert result.page is not None
    assert [c.public_id for c in result.page.connections] == ["a", "b"]


async def test_end_of_list_reflects_only_the_latest_read_not_a_stale_earlier_one() -> None:
    """#176 review M6: end_of_list must be read fresh on every settle attempt,
    not stick once true. A marker seen on an early attempt that is genuinely
    gone by a later one must not let a real, later stall be misread as a
    clean end -- the exhaustion branch has to trust only the *last* read's
    answer."""
    context = SequencedContext(
        [
            read([card("a", "A One")], end_of_list=True),  # attempt 0: marker up, but short
            read([card("a", "A One")], end_of_list=False),  # attempt 1: no growth, marker gone
            read([card("a", "A One")], end_of_list=False),  # attempt 2: still gone, still short
        ]
    )
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=5)
    # The latest read said no marker -- a real stall, refused, whatever an
    # earlier attempt's marker said.
    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.page is None


async def test_reaching_the_target_partway_through_a_later_attempt_still_succeeds() -> None:
    """The exhaustion refusal only fires when the loop never breaks -- growth that
    lands on a *later* attempt (not the very first) is still a confirmed,
    successful page, caught by the top-of-loop check on the attempt after it
    grew. (Renamed from "...on_the_final_attempt..." -- with
    MAX_SETTLE_ATTEMPTS=3 this reaches the target during attempt 1 of 0-2 and
    is caught at the top of attempt 2, which is not actually the *last*
    attempt reaching it; see the test below for that literal edge case,
    #176 review L4.)"""
    context = SequencedContext(
        [read([card("a", "A One")]), read([card("a", "A One"), card("b", "B Two")])]
    )
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=2)
    assert result.outcome is Outcome.OK
    assert result.page is not None
    assert [c.public_id for c in result.page.connections] == ["a", "b"]


async def test_reaching_the_target_on_literally_the_final_settle_attempt_still_succeeds() -> None:
    """#176 review L4: growth that reaches the target during the *last*
    settle attempt (index MAX_SETTLE_ATTEMPTS - 1, with no next iteration
    whose top-of-loop check could ever catch it) must still succeed. Before
    this fix, the target-reached check ran only at the top of each
    iteration, so a page whose growth simply took the entire settle budget
    to catch up fell through to the exhaustion branch on its very last read,
    even though it had genuinely reached what was asked for -- exactly the
    kind of "genuinely fine" answer F5(c)'s refusal exists not to give,
    but for the opposite reason (a real success misread as a stall)."""
    assert MAX_SETTLE_ATTEMPTS == 3, "this test scripts exactly 3 reads for the 3 attempts"
    context = SequencedContext(
        [
            read([card("a", "A One")]),  # attempt 0: short
            read([card("a", "A One")]),  # attempt 1: no growth, still short
            read(  # attempt 2 (the last one): reaches the target right here
                [card("a", "A One"), card("b", "B Two"), card("c", "C Three")]
            ),
        ]
    )
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=3)
    assert result.outcome is Outcome.OK
    assert result.page is not None
    assert [c.public_id for c in result.page.connections] == ["a", "b", "c"]
    # All 3 scripted reads were used and none were repeated/refused.
    assert len(context.pages[0].evaluate_calls) == 3


async def test_a_missing_list_container_still_missing_after_one_retry_is_route_changed() -> None:
    """#173 review, F5(b), retried once per #174 item 6: a page that never
    renders the container at all -- an error page, a wall in its place -- gets
    one paced re-check (not a fresh scroll: nothing new to scroll to) and is
    refused once that retry still finds it missing, without spending the
    remaining settle attempts scrolling at something that cannot resolve."""
    context = SequencedContext([read([], container=False)])
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=3)
    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.page is None
    # The initial read plus exactly one retry -- not a full extra settle
    # attempt, which would need a second scroll (only one settle attempt's
    # worth of scrolling happened: see the sibling test below for the
    # positive case, where reaching the goal after the retry needs no more
    # scrolling than this failing case did).
    assert len(context.pages[0].evaluate_calls) == 2


async def test_a_missing_list_container_that_appears_on_retry_succeeds() -> None:
    """#174 item 6: the mirror of the test above -- a container missing on the
    first read but present by the paced retry is a success, not a refusal,
    and costs no second scroll to get there: exactly the settle loop's first
    (and only) scroll, same as the failing case above, just with a container
    that shows up on the retry instead of staying missing."""
    context = SequencedContext(
        [read([], container=False), read([card("a", "A One")])],
    )
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=1)
    assert result.outcome is Outcome.OK
    assert result.page is not None
    assert [c.public_id for c in result.page.connections] == ["a"]
    assert len(context.pages[0].evaluate_calls) == 2


def test_container_retry_pause_is_pinned() -> None:
    """#176 review M14: a safety-relevant constant gets one test pinning its
    literal value (CLAUDE.md), not a self-referential ``== module.CONST`` --
    a mutation to e.g. 0.0 would otherwise pass every other test here, since
    none of them check the pause's actual duration, only that a pause of
    *some* kind happened (see the sibling test below)."""
    assert CONTAINER_RETRY_PAUSE_S == 1.0


async def test_the_container_retry_pause_actually_waits() -> None:
    """#176 review M7: proves the pause itself really happens, with the
    documented duration -- a mutation that deletes the ``await
    self._pause(...)`` call entirely still passes the other retry tests
    (same 2 evaluate calls, same outcome) unless something pins the wait."""
    waited: list[float] = []

    async def spy_sleep(seconds: float) -> None:
        waited.append(seconds)

    context = SequencedContext([read([], container=False), read([card("a", "A One")])])
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=spy_sleep)
        await source.fetch_page(start=0, count=1)
    assert CONTAINER_RETRY_PAUSE_S in waited


async def test_a_url_change_during_the_container_retry_pause_is_caught_by_origin_recheck() -> None:
    """#176 review L3/M8: the origin re-check must run again after the
    container retry's pause, not just once before it -- a tab is as free to
    navigate during a plain wait as during a scroll (#173 review R9's same
    reasoning). The custom sleep below only acts when its duration matches
    the retry's own pause, so it never touches BrowserRun.scroll's unrelated
    per-wheel waits."""
    context = SequencedContext([read([], container=False)])

    async def sleep_that_navigates_away(seconds: float) -> None:
        if seconds == CONTAINER_RETRY_PAUSE_S:
            page = context.pages[0]
            assert isinstance(page, SequencedPage)
            page._url = f"{OTHER_ORIGIN}{CONNECTIONS_LIST_PATH}"

    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=sleep_that_navigates_away)
        with pytest.raises(DomFetchError, match="not on"):
            await source.fetch_page(start=0, count=3)


async def test_a_login_wall_reached_during_the_container_retry_pause_is_classified() -> None:
    """#176 review L3/M9: the classify check must run again after the pause
    too -- a session kicked to a login wall *during* the wait must read as
    LOGGED_OUT, not fall through and read as a structural ROUTE_CHANGED the
    way a mutation that skips this re-check would produce."""
    context = SequencedContext([read([], container=False)])

    async def sleep_that_hits_a_login_wall(seconds: float) -> None:
        if seconds == CONTAINER_RETRY_PAUSE_S:
            page = context.pages[0]
            assert isinstance(page, SequencedPage)
            page._url = f"{ORIGIN}/uas/login?session_redirect=x"

    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=sleep_that_hits_a_login_wall)
        result = await source.fetch_page(start=0, count=3)
    assert result.outcome is Outcome.LOGGED_OUT
    assert result.page is None


async def test_the_container_retry_is_capped_at_one_per_call() -> None:
    """#176 review L3: a single ``fetch_page`` call may spend at most one
    container retry total, not one per settle attempt -- the reviewer's own
    probe found the uncapped version could pause up to MAX_SETTLE_ATTEMPTS
    times in a single call. Missing again on a *later* attempt, after the
    one retry this call gets was already spent, refuses at once instead of
    pausing again."""
    context = SequencedContext(
        [
            read([], container=False),  # attempt 0: missing
            read([card("a", "A One")]),  # attempt 0's one retry: present, 1 card
            read([], container=False),  # attempt 1: missing again -- no more retries left
        ]
    )
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=5)
    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.page is None
    # attempt 0's initial read + its one retry, then attempt 1's single read
    # (refused at once, no second retry) -- 3 total, not 4.
    assert len(context.pages[0].evaluate_calls) == 3


async def test_a_login_wall_is_classified_not_parsed_as_an_empty_list() -> None:
    """Attack the fallback: a login wall must not read as an honest empty page."""
    context = SequencedContext([read([])], redirect_to=f"{ORIGIN}/uas/login?session_redirect=x")
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=2)
    assert result.outcome is Outcome.LOGGED_OUT
    assert result.page is None
    assert context.pages[0].evaluate_calls == []  # never even tried to read cards


async def test_a_checkpoint_is_classified_too() -> None:
    context = SequencedContext([read([])], redirect_to=f"{ORIGIN}/checkpoint/challenge/?ctx=x")
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=2)
    assert result.outcome is Outcome.CHECKPOINT


async def test_a_login_wall_reached_mid_scroll_is_classified() -> None:
    """#173 review, R10: the classify check must run again after every scroll, not
    only before the first one -- a session can be kicked to a login wall *during*
    a run, not only before it starts."""
    context = SequencedContext(
        [read([card("a", "A One")]), read([card("a", "A One"), card("b", "B Two")])],
        url_after_wheels=(1, f"{ORIGIN}/uas/login?session_redirect=x"),
    )
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=5)
    assert result.outcome is Outcome.LOGGED_OUT
    assert result.page is None


async def test_the_origin_is_rechecked_after_every_scroll() -> None:
    """#173 review, R9: the wrong-origin refusal must fire again after a scroll,
    not only on the tab this instance first navigated -- the tab is free to end
    up somewhere else mid-run."""
    context = SequencedContext(
        [read([card("a", "A One")]), read([card("a", "A One"), card("b", "B Two")])],
        url_after_wheels=(1, f"{OTHER_ORIGIN}{CONNECTIONS_LIST_PATH}"),
    )
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        with pytest.raises(DomFetchError, match="not on"):
            await source.fetch_page(start=0, count=5)


async def test_a_structural_break_is_route_changed_and_ends_the_page() -> None:
    context = SequencedContext([{"not": "the expected shape"}])
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        with pytest.raises(DomFetchError):
            await source.fetch_page(start=0, count=2)


async def test_the_origin_check_refuses_a_tab_on_the_wrong_origin() -> None:
    bad_origin_connections = f"{OTHER_ORIGIN}{CONNECTIONS_LIST_PATH}"
    context = SequencedContext([read([])], redirect_to=bad_origin_connections)
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        with pytest.raises(DomFetchError, match="not on"):
            await source.fetch_page(start=0, count=1)


async def test_an_evaluate_failure_is_a_dom_fetch_error_not_a_route_change() -> None:
    context = SequencedContext([read([])])
    async with run_with(context) as run:
        page = await run.ensure_page()
        assert isinstance(page, SequencedPage)
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        page.fail_next_evaluate(RuntimeError("boom"))
        with pytest.raises(DomFetchError):
            await source.fetch_page(start=0, count=1)


async def test_a_shrunk_read_never_overwrites_the_larger_accumulated_list() -> None:
    """#173 review, R14: a transient render glitch that shows *fewer* cards than an
    earlier read must not lose what was already confirmed."""
    context = SequencedContext(
        [
            read([card(f"p{i}", f"Given{i} Family{i}") for i in range(5)]),
            read([card("p0", "Given0 Family0")]),  # a later, shrunk read
        ]
    )
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        first = await source.fetch_page(start=0, count=5)
        # Forces a second scroll+read that comes back shrunk to 1 card.
        second = await source.fetch_page(start=5, count=1)
        # Re-asking for the original 5 proves they are still there: if the shrunk
        # read above had overwritten self._cards down to 1, this would now need to
        # scroll for more, find the same shrunk read forever, and refuse instead.
        third = await source.fetch_page(start=0, count=5)
    assert first.page is not None
    assert len(first.page.connections) == 5
    # Nothing new was ever confirmed past the original 5.
    assert second.outcome is Outcome.ROUTE_CHANGED
    assert third.outcome is Outcome.OK
    assert third.page is not None
    assert [c.public_id for c in third.page.connections] == [f"p{i}" for i in range(5)]


async def test_a_cancelled_scroll_stops_the_settle_loop_and_reports_what_it_has() -> None:
    """#173 review, L5: ScrollOutcome.cancelled stops the loop at once, the same as
    running out of the page's own count -- not treated as a failure."""
    context = SequencedContext([read([card("a", "A One")])])

    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep, cancelled=lambda: True)
        result = await source.fetch_page(start=0, count=5)
    # Cancelled before the first wheel event is even sent -- scroll() polls
    # `cancelled` before sending anything (BrowserRun.scroll's own contract) --
    # so no card is ever read and the page is a normal, if empty/short, Ok page.
    assert result.outcome is Outcome.OK
    assert context.pages[0].evaluate_calls == []


# --- origin construction: refuses anything but LinkedIn or loopback -----------------


def test_linkedin_origin_is_pinned() -> None:
    """CLAUDE.md: a safety-relevant constant gets one test pinning its literal
    value, never a self-referential ``== module.CONST`` (#173 review, R15)."""
    assert LINKEDIN_ORIGIN == "https://www.linkedin.com"


async def test_dom_connections_source_refuses_an_arbitrary_non_loopback_origin() -> None:
    """#173 review, R15: only LINKEDIN_ORIGIN or this machine's own loopback --
    never an arbitrary http(s) host."""
    context = SequencedContext([read([])])
    async with run_with(context) as run:
        with pytest.raises(ValueError, match="linkedin"):
            DomConnectionsSource(run, origin="https://evil.example.invalid")


async def test_dom_contact_info_source_refuses_an_arbitrary_non_loopback_origin() -> None:
    context = SequencedContext([overlay()])
    async with run_with(context) as run:
        with pytest.raises(ValueError, match="linkedin"):
            DomContactInfoSource(run, origin="https://evil.example.invalid")


# --- automatic end-to-end: a DOM-only run can never complete -------------------------


async def test_a_dom_only_full_sync_reaches_the_end_but_is_never_complete() -> None:
    all_cards = [card(f"p{i}", f"Given{i} Family{i}") for i in range(3)]
    context = SequencedContext([read(all_cards), read(all_cards), read([])])
    pages: list[ConnectionsPage] = []

    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)

        class Gate:
            async def before_page(self, number: int) -> bool:
                return True

            async def between_pages(self) -> None:
                return None

        async def on_page(page: ConnectionsPage) -> None:
            pages.append(page)

        result = await run_connections_sync(
            SyncJobSpec(mode=SyncMode.FULL, page_budget=10, page_size=3),
            source,
            Gate(),
            on_page=on_page,
        )

    assert [p.public_id for p in pages[0].connections] == ["p0", "p1", "p2"]
    assert result.max_total == 0
    assert not result.complete
    assert result.seen_urns == frozenset()
    assert result.seen_public_ids == {"p0", "p1", "p2"}


# --- DomContactInfoSource --------------------------------------------------------


def test_parse_contact_info_dom_reads_every_field() -> None:
    raw = overlay(
        email="jamie@example.test",
        phones=["+1-555-0100", " "],
        websites=["https://jamie.example.test"],
        twitter=["jamiefake"],
    )
    info = _parse_contact_info_dom("dom/contact-info-overlay", raw)
    assert info.email == "jamie@example.test"
    assert info.phones == ("+1-555-0100",)  # the blank entry is dropped, not kept as ""
    assert info.websites == ("https://jamie.example.test",)
    assert info.twitter_handles == ("jamiefake",)


def test_parse_contact_info_dom_with_nothing_shared_is_all_empty() -> None:
    info = _parse_contact_info_dom("dom/contact-info-overlay", overlay())
    assert info.email is None
    assert info.phones == info.websites == info.twitter_handles == ()


def test_parse_contact_info_dom_normalizes_a_blank_email_to_none() -> None:
    """#173 review, R18: an empty-string email (e.g. a bare ``mailto:`` link) must
    not survive as ``""``."""
    info = _parse_contact_info_dom("dom/contact-info-overlay", overlay(email=""))
    assert info.email is None


def test_parse_contact_info_dom_raises_route_changed_for_a_non_object() -> None:
    with pytest.raises(RouteChanged):
        _parse_contact_info_dom("dom/contact-info-overlay", ["not", "an", "object"])


async def test_dom_contact_info_source_navigates_to_the_overlay_url() -> None:
    context = SequencedContext([overlay(email="jamie@example.test")])
    async with run_with(context) as run:
        source = DomContactInfoSource(run, origin=ORIGIN)
        result = await source.fetch_contact_info("jamie-fake-rivera")
    expected = f"{ORIGIN}{CONTACT_INFO_OVERLAY_PATH_TEMPLATE.format(public_id='jamie-fake-rivera')}"
    assert context.pages[0].goto_calls == [expected]
    assert result.outcome is Outcome.OK
    assert result.info is not None and result.info.email == "jamie@example.test"


async def test_a_missing_overlay_dialog_is_unreadable_not_an_empty_ok() -> None:
    """#173 review, F5(d): an overlay that never renders its dialog (an error page,
    a wall) must count as unreadable (ROUTE_CHANGED), not "Ok, shared nothing" --
    apply_harvest already reads ROUTE_CHANGED as HarvestResult.UNREADABLE, feeding
    enrichment's own two-unreadable-in-a-row stop."""
    context = SequencedContext([overlay(dialog=False)])
    async with run_with(context) as run:
        source = DomContactInfoSource(run, origin=ORIGIN)
        result = await source.fetch_contact_info("jamie-fake-rivera")
    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.info is None


async def test_dom_contact_info_source_classifies_a_login_wall() -> None:
    context = SequencedContext([overlay()], redirect_to=f"{ORIGIN}/uas/login?session_redirect=x")
    async with run_with(context) as run:
        source = DomContactInfoSource(run, origin=ORIGIN)
        result: ContactInfoResult = await source.fetch_contact_info("jamie-fake-rivera")
    assert result.outcome is Outcome.LOGGED_OUT
    assert result.info is None
    assert context.pages[0].evaluate_calls == []


async def test_dom_contact_info_source_refuses_the_wrong_origin() -> None:
    context = SequencedContext(
        [overlay()], redirect_to=f"{OTHER_ORIGIN}/in/jamie-fake-rivera/overlay/contact-info/"
    )
    async with run_with(context) as run:
        source = DomContactInfoSource(run, origin=ORIGIN)
        with pytest.raises(DomFetchError, match="not on"):
            await source.fetch_contact_info("jamie-fake-rivera")


async def test_dom_contact_info_source_refuses_an_empty_public_id() -> None:
    context = SequencedContext([overlay()])
    async with run_with(context) as run:
        source = DomContactInfoSource(run, origin=ORIGIN)
        with pytest.raises(ValueError, match="public_id"):
            await source.fetch_contact_info("   ")


# --- the scanners themselves: FakeMouse's wheel count backs url_after_wheels --------


def test_fake_mouse_tracks_wheel_calls_for_the_test_harness_itself() -> None:
    """Not a dom.py test: pins the fake's own contract, since url_after_wheels
    above depends on it counting every wheel() call."""
    mouse = FakeMouse()
    assert mouse.wheels == []
