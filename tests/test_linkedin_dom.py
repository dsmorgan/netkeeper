"""netkeeper.linkedin.dom: the DOM fallback, offline (P2-08).

Drives :class:`DomConnectionsSource` and :class:`DomContactInfoSource` against
``tests/browser_fakes.py``'s fake tab through a real ``AttachBrowserProvider``
and ``BrowserRun``, the same way ``tests/test_linkedin_fetch.py`` drives
``PageVoyagerFetch``. The fakes cannot run real JavaScript, so
``SequencedPage`` below stands in for ``page.evaluate`` with a scripted queue
of Python values instead -- what a real page.evaluate would have computed,
handed over directly. Structural checks on the generated scripts' text (that
the right selectors are embedded) are not this file's job; what a real
browser actually renders and how ``page.evaluate`` behaves against it is
``tests/smoke/test_dom_smoke.py``'s.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from browser_fakes import FakeBrowser, FakeConnector, FakeContext, FakePage

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
    can land somewhere other than where it was sent (an armed redirect), so a
    test can simulate a checkpoint or a login wall the way a real navigation
    would land on one -- ``page.url`` genuinely changing, not merely `told to`.
    """

    def __init__(self, context: SequencedContext) -> None:
        super().__init__(context)

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
    script every settle-loop attempt exactly.
    """

    def __init__(
        self, results: Sequence[Any], *, redirect_to: str | None = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._results: list[Any] = list(results)
        self.redirect_to = redirect_to

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


# --- DomConnectionsSource: parsing helpers -------------------------------------------


def test_split_name_on_the_first_run_of_whitespace() -> None:
    assert _split_name("Jamie Rivera") == ("Jamie", "Rivera")
    assert _split_name("Jamie   van der Rivera") == ("Jamie", "van der Rivera")
    assert _split_name("Prince") == ("Prince", "")


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


# --- DomConnectionsSource: fetch_page --------------------------------------------


async def test_the_first_call_navigates_to_the_connections_list() -> None:
    context = SequencedContext([[card("jamie-fake", "Jamie Rivera", "Designer")]])
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        await source.fetch_page(start=0, count=1)
    assert context.pages[0].goto_calls[0] == f"{ORIGIN}{CONNECTIONS_LIST_PATH}"


async def test_a_page_slices_the_scrolled_so_far_list_by_start_and_count() -> None:
    all_cards = [card(f"person-{i}", f"Given{i} Family{i}") for i in range(5)]
    context = SequencedContext([all_cards])
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
    context = SequencedContext([all_cards])
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
            [card("a", "A One")],
            [card("a", "A One"), card("b", "B Two"), card("c", "C Three")],
        ]
    )
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=2)
    assert result.page is not None
    assert [c.public_id for c in result.page.connections] == ["a", "b"]
    assert len(context.pages[0].evaluate_calls) == 2  # exactly the two attempts needed


async def test_scrolling_gives_up_after_max_settle_attempts_and_reports_a_short_page() -> None:
    """The list never grows past one card: after the bounded retries this is a short
    (here, empty-relative-to-count) page, not an infinite loop."""
    context = SequencedContext([[card("a", "A One")]])
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=5)
    assert result.outcome is Outcome.OK
    assert result.page is not None
    assert [c.public_id for c in result.page.connections] == ["a"]
    # Bounded: MAX_SETTLE_ATTEMPTS reads, not one per caller-requested count.
    assert len(context.pages[0].evaluate_calls) == MAX_SETTLE_ATTEMPTS


async def test_a_login_wall_is_classified_not_parsed_as_an_empty_list() -> None:
    """Attack the fallback: a login wall must not read as an honest empty page."""
    context = SequencedContext([[]], redirect_to=f"{ORIGIN}/uas/login?session_redirect=x")
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=2)
    assert result.outcome is Outcome.LOGGED_OUT
    assert result.page is None
    assert context.pages[0].evaluate_calls == []  # never even tried to read cards


async def test_a_checkpoint_is_classified_too() -> None:
    context = SequencedContext([[]], redirect_to=f"{ORIGIN}/checkpoint/challenge/?ctx=x")
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=2)
    assert result.outcome is Outcome.CHECKPOINT


async def test_a_structural_break_is_route_changed_and_ends_the_page() -> None:
    context = SequencedContext([{"not": "a list"}])
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        result = await source.fetch_page(start=0, count=2)
    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.page is None


async def test_the_origin_check_refuses_a_tab_on_the_wrong_origin() -> None:
    bad_origin_connections = f"{OTHER_ORIGIN}{CONNECTIONS_LIST_PATH}"
    context = SequencedContext([[]], redirect_to=bad_origin_connections)
    async with run_with(context) as run:
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        with pytest.raises(DomFetchError, match="not on"):
            await source.fetch_page(start=0, count=1)


async def test_an_evaluate_failure_is_a_dom_fetch_error_not_a_route_change() -> None:
    context = SequencedContext([[]])
    async with run_with(context) as run:
        page = await run.ensure_page()
        assert isinstance(page, SequencedPage)
        source = DomConnectionsSource(run, origin=ORIGIN, sleep=_fast_sleep)
        page.fail_next_evaluate(RuntimeError("boom"))
        with pytest.raises(DomFetchError):
            await source.fetch_page(start=0, count=1)


# --- automatic end-to-end: a DOM-only run can never complete -------------------------


async def test_a_dom_only_full_sync_reaches_the_end_but_is_never_complete() -> None:
    all_cards = [card(f"p{i}", f"Given{i} Family{i}") for i in range(3)]
    context = SequencedContext([all_cards, all_cards, []])
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


def _overlay(
    *,
    email: str | None = None,
    phones: list[str] | None = None,
    websites: list[str] | None = None,
    twitter: list[str] | None = None,
) -> dict[str, object]:
    return {
        "email": email,
        "phones": phones or [],
        "websites": websites or [],
        "twitterHandles": twitter or [],
    }


def test_parse_contact_info_dom_reads_every_field() -> None:
    raw = _overlay(
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
    info = _parse_contact_info_dom("dom/contact-info-overlay", _overlay())
    assert info.email is None
    assert info.phones == info.websites == info.twitter_handles == ()


def test_parse_contact_info_dom_raises_route_changed_for_a_non_object() -> None:
    with pytest.raises(RouteChanged):
        _parse_contact_info_dom("dom/contact-info-overlay", ["not", "an", "object"])


async def test_dom_contact_info_source_navigates_to_the_overlay_url() -> None:
    context = SequencedContext([_overlay(email="jamie@example.test")])
    async with run_with(context) as run:
        source = DomContactInfoSource(run, origin=ORIGIN)
        result = await source.fetch_contact_info("jamie-fake-rivera")
    expected = f"{ORIGIN}{CONTACT_INFO_OVERLAY_PATH_TEMPLATE.format(public_id='jamie-fake-rivera')}"
    assert context.pages[0].goto_calls == [expected]
    assert result.outcome is Outcome.OK
    assert result.info is not None and result.info.email == "jamie@example.test"


async def test_dom_contact_info_source_classifies_a_login_wall() -> None:
    context = SequencedContext([_overlay()], redirect_to=f"{ORIGIN}/uas/login?session_redirect=x")
    async with run_with(context) as run:
        source = DomContactInfoSource(run, origin=ORIGIN)
        result: ContactInfoResult = await source.fetch_contact_info("jamie-fake-rivera")
    assert result.outcome is Outcome.LOGGED_OUT
    assert result.info is None
    assert context.pages[0].evaluate_calls == []


async def test_dom_contact_info_source_refuses_the_wrong_origin() -> None:
    context = SequencedContext(
        [_overlay()], redirect_to=f"{OTHER_ORIGIN}/in/jamie-fake-rivera/overlay/contact-info/"
    )
    async with run_with(context) as run:
        source = DomContactInfoSource(run, origin=ORIGIN)
        with pytest.raises(DomFetchError, match="not on"):
            await source.fetch_contact_info("jamie-fake-rivera")


async def test_dom_contact_info_source_refuses_an_empty_public_id() -> None:
    context = SequencedContext([_overlay()])
    async with run_with(context) as run:
        source = DomContactInfoSource(run, origin=ORIGIN)
        with pytest.raises(ValueError, match="public_id"):
            await source.fetch_contact_info("   ")
