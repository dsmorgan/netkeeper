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
from flagship_site import SHELL, FakeResponse
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


class ProfileSite(FakeContext):
    """Profile pages by slug. ``requests`` is every request the pages made, in order."""

    def __init__(
        self,
        pages: Sequence[ProfilePage] = (),
        *,
        origin: str = ORIGIN,
        stale: Sequence[Stale] = (),
        extra: Mapping[str, ProfilePage] | None = None,
    ) -> None:
        super().__init__()
        self.origin = origin
        self.profiles: dict[str, ProfilePage] = {p.person.slug.casefold(): p for p in pages}
        self.profiles.update({k.casefold(): v for k, v in (extra or {}).items()})
        self.tabs: list[ProfileTab] = []
        self.stale = list(stale)
        self.requests: list[tuple[str, str, str | None]] = []
        self.lookups: list[str] = []
        self.clicks: list[tuple[str, float | None, float | None]] = []

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
                200,
                stale.body,
                "fetch",
                stale.post_data,
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
        if page.redirect_to is not None:
            target = f"{self.origin}/in/{page.redirect_to}/"
            self._send(tab, "GET", url, 301, b"", "document", headers={"location": target})
            tab._url = target
            page = self.profiles[page.redirect_to.casefold()]
            tab.profile = page
            url = target
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
            html = document_html(page.screen_body()).encode("utf-8")
            self._send(tab, "GET", url, 200, html, "document")
        else:
            self._send(tab, "GET", url, 200, SHELL, "document")
            if landing == "screen":
                screen_url = f"{self.origin}/flagship-web{urlsplit(url).path}"
                self._send(tab, "POST", screen_url, 200, page.screen_body(), "fetch", "{}")

    def scrolled(self, tab: ProfileTab, delta_y: float) -> None:
        page = tab.profile
        if page is None or tab.scrolled or delta_y <= 0:
            return
        tab.scrolled = True
        for body, request in page.components:
            url = f"{self.origin}{COMPONENT_PATH}?componentId=fake"
            self._send(tab, "POST", url, page.component_status, body, "fetch", request)
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
        for _ in range(page.overlay_answers):
            self._send(tab, "POST", url, page.overlay_status, page.overlay_body(), "fetch", request)
        if page.tab_after_click is not None:
            tab._url = page.tab_after_click

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
    ) -> None:
        self.requests.append((method, urlsplit(url).path, post_data))
        request = SiteRequest(method, url, resource_type, post_data)
        tab.emit(FakeResponse(url, status, body, request, headers=headers), request)  # type: ignore[arg-type]
