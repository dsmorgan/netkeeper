"""#195: a run opens its tab in the background and never takes focus on its own.

``context.new_page()`` opens a foreground tab, and Chrome on macOS then activates its
window, taking keyboard focus from whatever app you're using. ``BrowserRun._open_tab``
asks Chrome for a background tab instead (``Target.createTarget`` with
``background: true``) and finds it by its target id. Only a prefill or an auto-send
brings its tab forward, once, at its start (ADR 0007, ADR 0008).

These run against fakes of the CDP calls; ``tests/test_browser_safety.py`` pins which
methods each site may send.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import pytest
from browser_fakes import FakeBrowser, FakeConnector, FakeContext, FakePage

from netkeeper.linkedin import browser
from netkeeper.linkedin.browser import ActivityLocks, AttachBrowserProvider, BrowserUnavailable

CDP_URL = "http://127.0.0.1:9222"
LOCAL_PAGE = "http://127.0.0.1:8123/replica/list.html"
FOCUS_METHODS = {"Page.bringToFront", "Target.activateTarget"}


class CdpPage(FakePage):
    """A tab with a CDP target id, and a count of the times it was brought forward."""

    def __init__(self, context: FakeContext, target_id: str) -> None:
        super().__init__(context)
        self.target_id = target_id
        self.fronted = 0

    async def bring_to_front(self) -> None:
        self.fronted += 1


class PageSession:
    """A page-level CDP session: answers ``Target.getTargetInfo`` for its own tab."""

    def __init__(self, page: CdpPage, log: list[tuple[str, str]]) -> None:
        self._page = page
        self._log = log
        self.detached = False

    async def send(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self._log.append(("page", method))
        assert method == "Target.getTargetInfo" and not params
        return {"targetInfo": {"targetId": self._page.target_id, "type": "page"}}

    def on(self, event: str, handler: Any) -> None:
        raise AssertionError("the target-id read listens to nothing")

    async def detach(self) -> None:
        self.detached = True


class CdpContext(FakeContext):
    """The user's context, with tabs Chrome creates by target id."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sends: list[tuple[str, str]] = []
        self.page_sessions: list[PageSession] = []
        self.ids = 0

    def add_tab(self, target_id: str | None = None) -> CdpPage:
        self.ids += 1
        tab = CdpPage(self, target_id or f"TARGET-{self.ids}")
        self.pages.append(tab)
        return tab

    async def new_cdp_session(self, page: Any) -> PageSession:
        assert isinstance(page, CdpPage)
        session = PageSession(page, self.sends)
        self.page_sessions.append(session)
        return session


class BrowserSession:
    """A browser-level CDP session. ``Target.createTarget`` adds the tab to the context
    (after ``user_tab_first``, a tab the person opened at the same moment), unless
    ``never_reports`` is set: then Chrome answers but the tab never shows up as a page.
    """

    def __init__(self, owner: CdpBrowser) -> None:
        self._owner = owner
        self.detached = False

    async def send(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        owner = self._owner
        context = owner.cdp_context
        context.sends.append(("browser", method))
        owner.calls.append((method, dict(params or {})))
        if method == "Target.createTarget":
            if owner.create_error is not None:
                owner.connected = not owner.create_disconnects
                raise owner.create_error
            if owner.user_tab_first:
                context.add_tab("USER-TAB")
            context.ids += 1
            target_id = f"CREATED-{context.ids}"
            if not owner.never_reports:
                context.pages.append(CdpPage(context, target_id))
            return {"targetId": target_id}
        if method == "Target.closeTarget":
            return {"success": True}
        raise AssertionError(f"unexpected browser-level method {method}")

    def on(self, event: str, handler: Any) -> None:
        raise AssertionError("the tab opener listens to nothing")

    async def detach(self) -> None:
        self.detached = True


class CdpBrowser(FakeBrowser):
    """The attached Chrome, with a browser-level CDP session."""

    def __init__(
        self,
        context: CdpContext | None = None,
        *,
        user_tab_first: bool = False,
        never_reports: bool = False,
        create_error: Exception | None = None,
        create_disconnects: bool = True,
    ) -> None:
        self.cdp_context = context if context is not None else CdpContext()
        super().__init__([self.cdp_context])
        self.user_tab_first = user_tab_first
        self.never_reports = never_reports
        self.create_error = create_error
        self.create_disconnects = create_disconnects
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sessions: list[BrowserSession] = []

    async def new_browser_cdp_session(self) -> BrowserSession:
        session = BrowserSession(self)
        self.sessions.append(session)
        return session


def provider_for(*browsers: FakeBrowser) -> AttachBrowserProvider:
    return AttachBrowserProvider(
        CDP_URL, connector=FakeConnector(list(browsers)), locks=ActivityLocks()
    )


def methods(chrome: CdpBrowser) -> list[str]:
    return [method for _, method in chrome.cdp_context.sends]


async def test_a_run_opens_its_tab_in_the_background() -> None:
    """The read-only runs (connections sync, enrichment, inbox poll) all open their tab
    through ``_ensure_page``: in the background, never with ``new_page()``, and nothing
    brings it forward."""
    chrome = CdpBrowser()
    context = chrome.cdp_context
    async with provider_for(chrome).run() as run:
        page = await run.goto(LOCAL_PAGE)
        assert isinstance(page, CdpPage)
        assert page.goto_calls == [LOCAL_PAGE]
        assert page.fronted == 0
    assert chrome.calls == [("Target.createTarget", {"url": "about:blank", "background": True})]
    assert context.new_page_calls == 0
    assert not FOCUS_METHODS & set(methods(chrome))
    assert all(session.detached for session in chrome.sessions)
    assert all(session.detached for session in context.page_sessions)
    assert page.close_calls == 1, "the run still closes the tab it opened"


async def test_a_tab_the_person_opens_at_the_same_moment_is_left_alone() -> None:
    """The run's tab is the one whose target id Chrome answered, not the newest one."""
    chrome = CdpBrowser(user_tab_first=True)
    context = chrome.cdp_context
    async with provider_for(chrome).run() as run:
        page = await run.goto(LOCAL_PAGE)
        assert isinstance(page, CdpPage)
        assert page.target_id.startswith("CREATED-")
    [user_tab] = [
        tab for tab in context.pages if isinstance(tab, CdpPage) and tab.target_id == "USER-TAB"
    ]
    assert user_tab.goto_calls == [] and user_tab.close_calls == 0
    assert not user_tab.is_closed()


async def test_tabs_open_before_the_run_are_never_read() -> None:
    """Only tabs that appeared after the create are asked for their target id."""
    context = CdpContext()
    existing = context.add_tab("ALREADY-OPEN")
    chrome = CdpBrowser(context)
    async with provider_for(chrome).run() as run:
        await run.goto(LOCAL_PAGE)
    assert [s for s in context.page_sessions if s._page is existing] == []
    assert existing.goto_calls == [] and existing.close_calls == 0


async def test_a_reopened_tab_opens_in_the_background_too() -> None:
    chrome = CdpBrowser()
    context = chrome.cdp_context
    async with provider_for(chrome).run() as run:
        first = await run.goto(LOCAL_PAGE)
        assert isinstance(first, FakePage)
        first.user_closed_it()
        second = await run.ensure_page()
        assert second is not first
        assert isinstance(second, CdpPage) and second.goto_calls == [LOCAL_PAGE]
    assert [m for m, _ in chrome.calls] == ["Target.createTarget", "Target.createTarget"]
    assert all(params["background"] is True for _, params in chrome.calls)
    assert context.new_page_calls == 0


async def test_only_the_prefill_step_brings_the_tab_forward() -> None:
    """ADR 0007 decision 1 and ADR 0008: the tab opens in the background, and
    ``bring_tab_forward`` (called only at a prefill's or an auto-send's start) brings
    it forward once. Navigation and the rest of the run don't."""
    chrome = CdpBrowser()
    async with provider_for(chrome).run() as run:
        page = await run.ensure_page()
        assert isinstance(page, CdpPage)
        assert page.fronted == 0
        await run.bring_tab_forward()
        assert page.fronted == 1
        await run.goto(LOCAL_PAGE)
        assert page.fronted == 1
    assert not FOCUS_METHODS & set(methods(chrome))


async def test_a_tab_chrome_never_reports_is_closed_and_the_run_opens_one_in_front(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Never worse than before #195: the created tab is closed by its own target id,
    and the run opens its tab with ``new_page()``, as it used to, and says so."""
    monkeypatch.setattr(browser, "BACKGROUND_TAB_WAIT_S", 0.05)
    monkeypatch.setattr(browser, "BACKGROUND_TAB_POLL_S", 0.01)
    chrome = CdpBrowser(never_reports=True)
    context = chrome.cdp_context
    with caplog.at_level(logging.WARNING, logger=browser.log.name):
        async with provider_for(chrome).run() as run:
            page = await run.goto(LOCAL_PAGE)
            assert page.goto_calls == [LOCAL_PAGE]  # type: ignore[attr-defined]
    assert [m for m, _ in chrome.calls] == ["Target.createTarget", "Target.closeTarget"]
    assert chrome.calls[1][1] == {"targetId": "CREATED-1"}
    assert context.new_page_calls == 1
    assert "opening it in front" in caplog.text
    assert all(session.detached for session in chrome.sessions)


async def test_a_browser_without_the_session_opens_the_tab_as_before() -> None:
    """A browser that can't open a browser-level session gets ``new_page()``."""
    context = FakeContext()
    async with provider_for(FakeBrowser([context])).run() as run:
        await run.goto(LOCAL_PAGE)
    assert context.new_page_calls == 1


async def test_a_create_that_fails_while_chrome_is_up_falls_back_without_a_reattach() -> None:
    chrome = CdpBrowser(create_error=RuntimeError("no such method"), create_disconnects=False)
    connector = FakeConnector([chrome])
    provider = AttachBrowserProvider(CDP_URL, connector=connector, locks=ActivityLocks())
    async with provider.run() as run:
        await run.goto(LOCAL_PAGE)
    assert chrome.cdp_context.new_page_calls == 1
    assert connector.attaches == 1
    assert [m for m, _ in chrome.calls] == ["Target.createTarget"], "no target id, no close"


async def test_losing_the_browser_while_opening_the_tab_spends_the_reattach() -> None:
    """A browser that went away is a loss, not a reason to open the tab in front."""
    gone = CdpBrowser(create_error=RuntimeError("Target closed"))
    back = CdpBrowser()
    connector = FakeConnector([gone, back])
    provider = AttachBrowserProvider(CDP_URL, connector=connector, locks=ActivityLocks())
    async with provider.run() as run:
        page = await run.goto(LOCAL_PAGE)
        assert isinstance(page, CdpPage) and page.context is back.cdp_context
    assert connector.attaches == 2
    assert gone.cdp_context.new_page_calls == 0
    assert back.calls == [("Target.createTarget", {"url": "about:blank", "background": True})]


async def test_the_second_lost_browser_while_opening_ends_the_run() -> None:
    gone = CdpBrowser(create_error=RuntimeError("Target closed"))
    again = CdpBrowser(create_error=RuntimeError("Target closed"))
    provider = provider_for(gone, again)
    with pytest.raises(BrowserUnavailable, match="still cannot open a tab"):
        async with provider.run() as run:
            await run.goto(LOCAL_PAGE)


def test_the_background_tab_wait_is_pinned() -> None:
    assert browser.BACKGROUND_TAB_WAIT_S == 5.0
    assert browser.BACKGROUND_TAB_POLL_S == 0.05
