"""netkeeper.linkedin.dom: the DOM fallback, offline (P2-08, #173 review).

Drives :class:`DomContactInfoSource` (the connections half is gone, #187) against
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
from netkeeper.linkedin.contact_info import ContactInfoResult
from netkeeper.linkedin.dom import (
    CONTACT_INFO_OVERLAY_PATH_TEMPLATE,
    LINKEDIN_ORIGIN,
    DomContactInfoSource,
    DomFetchError,
    _parse_contact_info_dom,
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


# --- origin construction: refuses anything but LinkedIn or loopback -----------------


def test_linkedin_origin_is_pinned() -> None:
    """CLAUDE.md: a safety-relevant constant gets one test pinning its literal
    value, never a self-referential ``== module.CONST`` (#173 review, R15)."""
    assert LINKEDIN_ORIGIN == "https://www.linkedin.com"


async def test_dom_contact_info_source_refuses_an_arbitrary_non_loopback_origin() -> None:
    context = SequencedContext([overlay()])
    async with run_with(context) as run:
        with pytest.raises(ValueError, match="linkedin"):
            DomContactInfoSource(run, origin="https://evil.example.invalid")


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
