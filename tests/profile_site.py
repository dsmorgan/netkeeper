"""Fake profile pages that load their own answers, for the offline enrichment tests (#190).

:class:`ProfileSite` is a :class:`browser_fakes.FakeContext` whose tabs behave like a
LinkedIn profile as the #149 capture showed it, without a browser or a socket:
navigating a tab to ``/in/<slug>/`` makes it "receive" the document (the profile screen
in ``rehydrate-data``), scrolling it makes the page "send" its lazy-card requests, and
clicking the one **Contact info** control makes it "send" the overlay's
``actions/navigation`` request and receive its answer. Every answer reaches the tab's
``request`` and ``response`` listeners the way Playwright delivers them.

The control is found the way :meth:`netkeeper.linkedin.browser.BrowserRun.click_contact_info`
finds it -- ``get_by_role("link", name="Contact info", exact=True)`` -- and the fake
records every lookup and every click, so a test can assert there was exactly one click
and nothing else. The tab has no ``evaluate`` result, no keyboard, and no other
control; reaching for one fails the test.

The people are :mod:`voyager_pages`' invented cast; the payloads are
:mod:`flagship_pages`' hand-built ones. A linkedin.com url here is a string a fake tab
reports, never fetched.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from browser_fakes import FakeContext, FakeMouse, FakePage
from flagship_pages import (
    Role,
    Website,
    contact_info_payload,
    document_html,
    profile_payload,
)
from flagship_site import SHELL, FakeCdpSession, FakeResponse
from voyager_pages import Person

from netkeeper.linkedin.browser import PageLike
from netkeeper.linkedin.flagship import CONTACT_DETAILS_SCREEN_ID, NAVIGATION_PATH
from netkeeper.linkedin.flagship_profile import COMPONENT_PATH

ORIGIN = "https://www.linkedin.com"
CHECKPOINT_URL = "https://www.linkedin.com/checkpoint/challenge/AgFAKE?ctx=invented"
LOGIN_URL = "https://www.linkedin.com/login?session_redirect=invented"
LOCATION = "Faketown, State of Example"
ROLES = (Role("Staff Engineer", "Fictional Robotics Co", "Full-time", "Aug 2021 - Present"),)


class SiteRequest:
    """The request side of an answer: read-only, with the url the rehearsal log records."""

    __slots__ = ("_method", "_post_data", "_resource_type", "_url")

    def __init__(self, method: str, url: str, resource_type: str, post_data: str | None) -> None:
        self._method = method
        self._url = url
        self._resource_type = resource_type
        self._post_data = post_data

    @property
    def method(self) -> str:
        return self._method

    @property
    def url(self) -> str:
        return self._url

    @property
    def resource_type(self) -> str:
        return self._resource_type

    @property
    def post_data(self) -> str | None:
        return self._post_data


def navigation_timeout(url: str = "http://127.0.0.1/in/fake-lost-slug/") -> Exception:
    """Playwright's own ``TimeoutError``, as ``page.goto`` raises it (message quotes the url)."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    return PlaywrightTimeoutError(f"Page.goto: Timeout 30000ms exceeded. navigating to {url}")


@dataclass(slots=True)
class ProfilePage:
    """How one profile's page behaves. Default: a faithful profile with one control.

    ``landing`` is ``"document"`` (the screen in the HTML), ``"screen"`` (an HTML shell,
    then the screen request), ``"shell"`` (a shell and no screen: a wall served in
    place), ``"404"``, ``"status:<n>"`` (the document answers ``n``), or a url the tab
    lands on instead (a wall, a feed). ``redirect_to`` is a slug the document redirects
    to. ``components`` answer the first scroll, each with its request body.
    ``controls`` is how many Contact info controls the page has; ``href`` their href.
    ``overlay_answers`` is how many answers a click brings (0: the overlay never
    answers). ``overlay_vanity`` is the slug the page's own overlay request names.
    """

    person: Person
    screen: bytes | None = None
    landing: str = "document"
    redirect_to: str | None = None
    components: tuple[tuple[bytes, str | None], ...] = ()
    component_status: int = 200
    controls: int = 1
    href: str | None = None
    overlay: bytes | None = None
    overlay_status: int = 200
    overlay_vanity: str | None = None
    overlay_screen: str = CONTACT_DETAILS_SCREEN_ID
    overlay_answers: int = 1
    overlay_before: Sequence[tuple[bytes, str]] = ()  # other navigation answers first
    click_error: Exception | None = None
    tab_after_scroll: str | None = None
    tab_after_click: str | None = None
    silently_to: str | None = None  # a slug the tab ends on with no redirect answered
    overlay_request: str | None = None  # the page's own overlay request body, verbatim
    screen_status: int = 200
    #: #197: the screen's answer (the document, or the screen request) arrives with a
    #: body the browser cannot hand over, raising this. ``screen_lost_times`` is how
    #: many visits to this profile lose it.
    screen_error: Exception | None = None
    screen_lost_times: int = 1
    #: #197: the same for the overlay's answer, and for every lazy card.
    overlay_error: Exception | None = None
    component_error: Exception | None = None
    #: #203: what the body tap's session receives as a lost overlay or lazy card
    #: streams in, on a site built with ``tap``: ``"whole"`` (the page's own copy),
    #: ``"half"`` (cut mid-row), ``"rows"`` (root first, cut at a row boundary, so it
    #: still parses but names a row it never got), ``"orphan"`` (whole, plus a row
    #: nothing reaches), ``"custom"`` (the site's ``custom_copies`` for that body), or
    #: ``"none"`` (Chrome refuses to stream it).
    overlay_streamed: str = "none"
    component_streamed: str = "none"
    #: #197: the navigation to this profile raises this after the page's answers
    #: arrived (Playwright's ``TimeoutError``: the document broke off and ``load`` never
    #: fired). ``tab_after_goto``, when set, is where the tab is by then; the first
    #: ``goto_closes_tab`` such navigations also close the tab.
    goto_error: Exception | None = None
    tab_after_goto: str | None = None
    goto_closes_tab: int = 0
    #: A url the document answers a redirect to that the tab never follows (#198
    #: review, H1: a 3xx to a wall, then the navigation hangs).
    redirect_location: str | None = None
    #: #405: the document's HTML verbatim, in place of one built from the screen.
    document: str | None = None
    #: #405: reading the Contact info control raises this (not a lost tab).
    control_error: Exception | None = None

    def screen_body(self) -> bytes:
        if self.screen is not None:
            return self.screen
        return profile_payload(self.person, location=LOCATION, roles=ROLES)

    def overlay_body(self) -> bytes:
        if self.overlay is not None:
            return self.overlay
        return contact_info_payload(
            self.person,
            emails=[f"{self.person.slug}@example.test"],
            websites=[Website(f"https://{self.person.slug}.example.test")],
        )


class ProfileControls:
    """What ``get_by_role`` hands back: the page's Contact info controls, as one locator."""

    def __init__(self, tab: ProfileTab, role: str, name: str, exact: bool) -> None:
        self._tab = tab
        self.role, self.name, self.exact = role, name, exact

    def _matches(self) -> int:
        page = self._tab.profile
        if page is None or (self.role, self.name, self.exact) != ("link", "Contact info", True):
            return 0
        return page.controls

    async def count(self) -> int:
        self._tab.site.lookups.append("count")
        page = self._tab.profile
        if page is not None and page.control_error is not None:
            raise page.control_error
        return self._matches()

    async def get_attribute(
        self,
        name: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- Playwright's own signature
    ) -> str | None:
        self._tab.site.lookups.append(f"get_attribute:{name}")
        page = self._tab.profile
        if name != "href" or page is None or self._matches() != 1:
            return None
        return (
            page.href if page.href is not None else f"/in/{page.person.slug}/overlay/contact-info/"
        )

    async def click(
        self,
        *,
        delay: float | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 -- Playwright's own signature
    ) -> None:
        site = self._tab.site
        site.lookups.append("click")
        if self._matches() != 1:
            raise AssertionError("strict mode violation: not exactly one Contact info control")
        page = self._tab.profile
        assert page is not None
        site.clicks.append((page.person.slug, delay, timeout))
        if page.click_error is not None:
            raise page.click_error
        site.clicked(self._tab, page)


class ProfileTab(FakePage):
    """A tab with listeners, a mouse that makes the page load, and ``get_by_role``."""

    def __init__(self, site: ProfileSite) -> None:
        super().__init__(site)
        self.site = site
        self.listeners: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self.mouse = _ProfileMouse(self)
        self.profile: ProfilePage | None = None
        self.scrolled = False

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self.listeners[event].append(handler)

    def remove_listener(self, event: str, handler: Callable[[Any], None]) -> None:
        self.listeners[event].remove(handler)

    def get_by_role(self, role: str, *, name: str, exact: bool) -> ProfileControls:
        self.site.lookups.append(f"get_by_role:{role}:{name}:{exact}")
        return ProfileControls(self, role, name, exact)

    def emit(self, response: FakeResponse, request: SiteRequest) -> None:
        for handler in list(self.listeners["request"]):
            handler(request)
        for handler in list(self.listeners["response"]):
            handler(response)

    async def goto(self, url: str) -> object:
        result = await super().goto(url)
        self.site.navigated(self, url)
        path = urlsplit(url).path
        slug = unquote(path[len("/in/") :].strip("/")) if path.startswith("/in/") else ""
        page = self.site.profiles.get(slug.casefold())
        if page is not None and page.goto_error is not None:
            if page.tab_after_goto is not None:
                self._url = page.tab_after_goto
            if page.goto_closes_tab > 0:
                page.goto_closes_tab -= 1
                self._closed = True
            raise page.goto_error
        return result


class _ProfileMouse(FakeMouse):
    def __init__(self, tab: ProfileTab) -> None:
        super().__init__()
        self._tab = tab

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await super().wheel(delta_x, delta_y)
        self._tab.site.scrolled(self._tab, delta_y)


@dataclass(slots=True)
class Stale:
    """An answer a previous page was still loading, delivered at the next navigation."""

    method: str
    path: str
    body: bytes
    post_data: str | None = None
    #: A stale redirect (#196 item 1): its status, and its ``location`` header.
    status: int = 200
    headers: Mapping[str, str] | None = None


class ProfileSite(FakeContext):
    """Profile pages by slug. ``requests`` is every request the pages made, in order."""

    def __init__(
        self,
        pages: Sequence[ProfilePage] = (),
        *,
        origin: str = ORIGIN,
        stale: Sequence[Stale] = (),
        extra: Mapping[str, ProfilePage] | None = None,
        tap: bool = False,
    ) -> None:
        super().__init__()
        self.origin = origin
        #: Whether a body tap's CDP session can be opened here (#203), and the one that was.
        self.tap = tap
        self.cdp: FakeCdpSession | None = None
        self._request_ids = 0
        #: What a ``"custom"`` stream receives, by the answer's own body.
        self.custom_copies: dict[bytes, bytes] = {}
        self.profiles: dict[str, ProfilePage] = {p.person.slug.casefold(): p for p in pages}
        self.profiles.update({k.casefold(): v for k, v in (extra or {}).items()})
        self.tabs: list[ProfileTab] = []
        self.stale = list(stale)
        self.requests: list[tuple[str, str, str | None]] = []
        self.lookups: list[str] = []
        self.clicks: list[tuple[str, float | None, float | None]] = []

    async def new_cdp_session(self, page: object) -> FakeCdpSession:
        """Only when the site was built with ``tap``: otherwise, like a browser without it."""
        if not self.tap:
            raise RuntimeError("this fake browser has no CDP sessions")
        self.cdp = FakeCdpSession()
        return self.cdp

    @property
    def streamed_ids(self) -> list[str]:
        """The request ids the body tap asked Chrome to stream, in order."""
        assert self.cdp is not None
        return [
            str(params["requestId"])
            for method, params in self.cdp.sent
            if method == "Network.streamResourceContent"
        ]

    async def new_page(self) -> PageLike:
        self.new_page_calls += 1
        if self.new_page_error is not None:
            raise self.new_page_error
        tab = ProfileTab(self)
        self.tabs.append(tab)
        self.pages.append(tab)
        return tab

    @property
    def navigations(self) -> list[str]:
        return [path for method, path, _ in self.requests if method == "GET"]

    # --- the pages' behavior ---------------------------------------------------------

    def navigated(self, tab: ProfileTab, url: str) -> None:
        for stale in self.stale:
            self._send(
                tab,
                stale.method,
                f"{self.origin}{stale.path}",
                stale.status,
                stale.body,
                "fetch",
                stale.post_data,
                headers=stale.headers,
            )
        self.stale = []
        tab.scrolled = False
        path = urlsplit(url).path
        if not path.startswith("/in/"):
            tab.profile = None
            return
        slug = unquote(path[len("/in/") :].strip("/"))
        page = self.profiles.get(slug.casefold())
        tab.profile = page
        if page is None:
            self._send(tab, "GET", url, 404, b"<html>gone</html>", "document")
            return
        if page.silently_to is not None:
            target = f"{self.origin}/in/{page.silently_to}/"
            tab._url = target
            page = self.profiles[page.silently_to.casefold()]
            tab.profile = page
            url = target
        else:
            # A chain of renames is followed hop by hop, each its own redirect.
            while page.redirect_to is not None:
                target = f"{self.origin}/in/{page.redirect_to}/"
                self._send(tab, "GET", url, 301, b"", "document", headers={"location": target})
                tab._url = target
                page = self.profiles[page.redirect_to.casefold()]
                tab.profile = page
                url = target
        if page.redirect_location is not None:
            location = {"location": page.redirect_location}
            self._send(tab, "GET", url, 302, b"", "document", headers=location)
            return
        landing = page.landing
        if landing not in ("document", "screen", "shell", "404") and not landing.startswith(
            "status:"
        ):
            tab._url = landing
            tab.profile = None
            return
        if landing == "404":
            self._send(tab, "GET", url, 404, b"<html>This page doesn't exist</html>", "document")
        elif landing.startswith("status:"):
            self._send(tab, "GET", url, int(landing.split(":")[1]), SHELL, "document")
        elif landing == "document":
            html = (
                page.document if page.document is not None else document_html(page.screen_body())
            ).encode("utf-8")
            self._send(tab, "GET", url, 200, html, "document", body_error=self._lose_screen(page))
        else:
            self._send(tab, "GET", url, 200, SHELL, "document")
            if landing == "screen":
                screen_url = f"{self.origin}/flagship-web{urlsplit(url).path}"
                self._send(
                    tab,
                    "POST",
                    screen_url,
                    page.screen_status,
                    page.screen_body(),
                    "fetch",
                    "{}",
                    body_error=self._lose_screen(page),
                )

    def _lose_screen(self, page: ProfilePage) -> Exception | None:
        if page.screen_error is None or page.screen_lost_times <= 0:
            return None
        page.screen_lost_times -= 1
        return page.screen_error

    def scrolled(self, tab: ProfileTab, delta_y: float) -> None:
        page = tab.profile
        if page is None or tab.scrolled or delta_y <= 0:
            return
        tab.scrolled = True
        for body, request in page.components:
            url = f"{self.origin}{COMPONENT_PATH}?componentId=fake"
            self._send(
                tab,
                "POST",
                url,
                page.component_status,
                body,
                "fetch",
                request,
                body_error=page.component_error,
                streamed=self._copy(body, page.component_streamed),
            )
        if page.tab_after_scroll is not None:
            tab._url = page.tab_after_scroll

    def clicked(self, tab: ProfileTab, page: ProfilePage) -> None:
        url = f"{self.origin}{NAVIGATION_PATH}?screenId={page.overlay_screen}&sduiid=fake"
        for body, screen in page.overlay_before:
            other = json.dumps({"clientArguments": {"screenId": screen, "payload": {}}})
            self._send(tab, "POST", url, 200, body, "fetch", other)
        vanity = page.person.slug if page.overlay_vanity is None else page.overlay_vanity
        request = json.dumps(
            {
                "clientArguments": {
                    "$type": "proto.sdui.actions.requests.RequestedArguments",
                    "requestedStateKeys": [],
                    "payload": {
                        "vanityName": vanity,
                        "givenName": page.person.first,
                        "familyName": page.person.last,
                        "isVanityNameResolved": True,
                    },
                    "states": [],
                    "screenId": page.overlay_screen,
                    "knownTemplateIds": [],
                },
                "isModal": True,
            }
        )
        if page.overlay_request is not None:
            request = page.overlay_request
        for _ in range(page.overlay_answers):
            self._send(
                tab,
                "POST",
                url,
                page.overlay_status,
                page.overlay_body(),
                "fetch",
                request,
                body_error=page.overlay_error,
                streamed=self._copy(page.overlay_body(), page.overlay_streamed),
            )
        if page.tab_after_click is not None:
            tab._url = page.tab_after_click

    def _copy(self, body: bytes, how: str) -> bytes | None:
        return self.custom_copies[body] if how == "custom" else streamed_copy(body, how)

    def _send(
        self,
        tab: ProfileTab,
        method: str,
        url: str,
        status: int,
        body: bytes,
        resource_type: str,
        post_data: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        body_error: Exception | None = None,
        streamed: bytes | None = None,
    ) -> None:
        self.requests.append((method, urlsplit(url).path, post_data))
        request = SiteRequest(method, url, resource_type, post_data)
        cdp = self.cdp
        request_id = ""
        if cdp is not None:
            # What the tap's session hears, as Chrome sends it for every answer.
            self._request_ids += 1
            request_id = f"fake.{self._request_ids}"
            asked: dict[str, Any] = {"url": url, "method": method}
            if post_data is not None:
                asked["postData"] = post_data
            cdp.emit("Network.requestWillBeSent", {"requestId": request_id, "request": asked})
            cdp.emit("Network.responseReceived", {"requestId": request_id})
            if streamed is not None:
                cdp.bodies[request_id] = streamed
        response = FakeResponse(
            url,
            status,
            body,
            request,  # type: ignore[arg-type]
            headers=headers,
            body_error=body_error,
        )
        tab.emit(response, request)
        if cdp is not None:
            if body_error is None:
                cdp.emit("Network.loadingFinished", {"requestId": request_id})
            else:
                # The page's own client cancelled it after reading it (#200).
                cdp.emit(
                    "Network.loadingFailed",
                    {"requestId": request_id, "canceled": True, "errorText": "net::ERR_ABORTED"},
                )


def streamed_copy(body: bytes, how: str) -> bytes | None:
    """What a body tap would have received of ``body`` (see ``ProfilePage``)."""
    if how == "whole":
        return body
    if how == "half":
        return body[: len(body) // 2]
    if how in ("rows", "reordered"):
        # The fixtures write row 0, the root, last. Put it first ("reordered"), then,
        # for "rows", lose the last row: a cut that still parses.
        lines = [line for line in body.split(b"\n") if line]
        ordered = [line for line in lines if line.startswith(b"0:")] + [
            line for line in lines if not line.startswith(b"0:")
        ]
        kept = ordered[:-1] if how == "rows" else ordered
        return b"\n".join(kept) + b"\n"
    if how == "orphan":
        return body + b'fff:{"stray":true}\n'
    assert how == "none", how
    return None
