"""A fake connections page that loads its own answers, for the offline observation tests.

:class:`FlagshipSite` is a :class:`browser_fakes.FakeContext` whose tabs behave like
LinkedIn's connections page as the #149 capture showed it, without a browser or a
socket: navigating a tab to the connections page makes it "receive" the HTML document
(the first screen in ``rehydrate-data``), and scrolling it makes the page "send" the
next pagination request and receive its answer. Every answer reaches the tab's
``response`` listeners the way Playwright delivers one, so the code under test reads
exactly what it would read from a real tab: :class:`FakeResponse` objects whose
request side is read-only.

The people are :mod:`voyager_pages`' invented cast; the payloads are
:mod:`flagship_pages`' hand-built ones. A linkedin.com url here is a string a fake
tab reports, never fetched.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from browser_fakes import FakeContext, FakeMouse, FakePage
from flagship_pages import (
    CardOptions,
    document_html,
    pagination_payload,
    pagination_request,
    screen_payload,
)
from voyager_pages import PEOPLE, Person

from netkeeper.linkedin.browser import PageLike
from netkeeper.linkedin.flagship import (
    CONNECTIONS_PAGE_PATH,
    CONNECTIONS_SCREEN_PATH,
    PAGINATION_PATH,
    SORT_NEWEST_FIRST,
)

ORIGIN = "https://www.linkedin.com"
PAGE_URL = f"{ORIGIN}{CONNECTIONS_PAGE_PATH}"
CHECKPOINT_URL = "https://www.linkedin.com/checkpoint/challenge/AgFAKE?ctx=invented"
LOGIN_URL = "https://www.linkedin.com/login?session_redirect=invented"


class FakeRequest:
    """The request side of a response: method, resource type, and the body the page sent.

    Read-only by construction -- it has no method at all -- and it counts every read,
    so a test can assert what an observation looked at.
    """

    __slots__ = ("_method", "_post_data", "_resource_type", "reads")

    def __init__(self, method: str, resource_type: str, post_data: str | None) -> None:
        self._method = method
        self._resource_type = resource_type
        self._post_data = post_data
        self.reads: list[str] = []

    @property
    def method(self) -> str:
        self.reads.append("method")
        return self._method

    @property
    def resource_type(self) -> str:
        self.reads.append("resource_type")
        return self._resource_type

    @property
    def post_data(self) -> str | None:
        self.reads.append("post_data")
        return self._post_data


class FakeResponse:
    """One answer the page received. ``body()`` can be made slow or failing."""

    def __init__(
        self,
        url: str,
        status: int,
        body: bytes,
        request: FakeRequest,
        *,
        headers: Mapping[str, str] | None = None,
        body_error: Exception | None = None,
        body_delay: Callable[[], Any] | None = None,
    ) -> None:
        self._url = url
        self._status = status
        self._body = body
        self._request = request
        self._headers = dict(headers or {})
        self._body_error = body_error
        self._body_delay = body_delay

    @property
    def url(self) -> str:
        return self._url

    @property
    def status(self) -> int:
        return self._status

    @property
    def headers(self) -> Mapping[str, str]:
        return self._headers

    @property
    def request(self) -> FakeRequest:
        return self._request

    async def body(self) -> bytes:
        if self._body_delay is not None:
            await self._body_delay()
        if self._body_error is not None:
            raise self._body_error
        return self._body


class ListeningTab(FakePage):
    """A tab with ``on``/``remove_listener``, and nothing that could touch a request."""

    def __init__(self, context: FakeContext) -> None:
        super().__init__(context)
        self.listeners: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self.mouse = _SiteMouse(self)

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self.listeners[event].append(handler)

    def remove_listener(self, event: str, handler: Callable[[Any], None]) -> None:
        self.listeners[event].remove(handler)

    def emit(self, response: FakeResponse) -> None:
        for handler in list(self.listeners["response"]):
            handler(response)

    async def goto(self, url: str) -> object:
        result = await super().goto(url)
        site = self.context
        if isinstance(site, FlagshipSite):
            site.navigated(self, url)
        return result


class _SiteMouse(FakeMouse):
    def __init__(self, tab: ListeningTab) -> None:
        super().__init__()
        self._tab = tab

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await super().wheel(delta_x, delta_y)
        site = self._tab.context
        if delta_y > 0 and isinstance(site, FlagshipSite):
            site.scrolled(self._tab)


@dataclass(slots=True)
class Answer:
    """What to answer one pagination request with, instead of the next page."""

    status: int = 200
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    #: Where the tab itself goes as the answer arrives (a wall served mid-scroll).
    tab_url: str | None = None


class FlagshipSite(FakeContext):
    """The connections page, answering its own scrolls. One list of people per site.

    ``first`` cards come with the page; each later page is ``size`` cards. ``end`` is how
    the list ends: ``"empty"`` (the last page asks for one more, which is empty),
    ``"short"`` (the last page is short and asks for nothing), or ``"stall"`` (the page
    stops asking, having proven nothing). ``total`` is what the first screen states
    (``None`` for no total). ``landing`` is ``"document"`` (the first screen in the HTML),
    ``"screen"`` (an HTML shell, then the screen request), or a url the navigation lands
    on instead (a wall). ``answers`` replaces the answer for a pagination ``startIndex``;
    ``repeat`` sends a page's request twice; ``skip`` makes the page ask for the page
    after instead; ``other_pager`` also sends another pager's request on landing.
    """

    def __init__(
        self,
        people: Sequence[Person] = PEOPLE,
        *,
        first: int = 10,
        size: int = 10,
        end: str = "empty",
        total: int | str | None = "count",
        landing: str = "document",
        sort: str = SORT_NEWEST_FIRST,
        wheels_per_page: int = 1,
        answers: Mapping[int, Answer] | None = None,
        repeat: frozenset[int] = frozenset(),
        skip: frozenset[int] = frozenset(),
        other_pager: bool = False,
        card_options: Mapping[int, CardOptions] | None = None,
        origin: str = ORIGIN,
    ) -> None:
        super().__init__()
        self.people = list(people)
        self.first = first
        self.size = size
        self.end = end
        self.total = len(self.people) if total == "count" else total
        self.landing = landing
        self.sort = sort
        self.wheels_per_page = wheels_per_page
        self.answers = dict(answers or {})
        self.repeat = repeat
        self.skip = skip
        self.other_pager = other_pager
        self.card_options = dict(card_options or {})
        self.origin = origin
        #: Every request the page itself made: (method, path, body).
        self.requests: list[tuple[str, str, str | None]] = []
        self._next: int | None = None
        self._wheels = 0
        self._ended = False

    @property
    def fetches(self) -> list[str | None]:
        """The pagination request bodies the page sent, in order."""
        return [body for method, path, body in self.requests if path == PAGINATION_PATH]

    async def new_page(self) -> PageLike:
        self.new_page_calls += 1
        if self.new_page_error is not None:
            raise self.new_page_error
        tab = ListeningTab(self)
        self.pages.append(tab)
        return tab

    # --- the page's behavior ---------------------------------------------------------

    def navigated(self, tab: ListeningTab, url: str) -> None:
        if urlsplit(url).path != CONNECTIONS_PAGE_PATH:
            return
        if self.landing not in ("document", "screen"):
            tab._url = self.landing
            return
        first = self.people[: self.first]
        next_start: int | None = len(first)
        if not first or (self.end == "short" and len(first) >= len(self.people)):
            next_start = None
        screen = screen_payload(
            first,
            total=self.total if isinstance(self.total, int) else None,
            next_start=next_start,
            card_options={k: v for k, v in self.card_options.items() if k < self.first},
        )
        self._next = next_start
        self._ended = next_start is None
        if self.landing == "document":
            self._send(tab, "GET", url, 200, document_html(screen).encode("utf-8"), "document")
        else:
            shell = b"<!doctype html><html><body><div id=root></div></body></html>"
            self._send(tab, "GET", url, 200, shell, "document")
            self._send(
                tab, "POST", f"{self.origin}{CONNECTIONS_SCREEN_PATH}", 200, screen, "fetch", "{}"
            )
        if self.other_pager:
            other = pagination_request(0, pager="com.linkedin.sdui.pagers.mynetwork.other")
            self._send(
                tab, "POST", f"{self.origin}{PAGINATION_PATH}", 200, b"0:[]\n", "fetch", other
            )

    def scrolled(self, tab: ListeningTab) -> None:
        self._wheels += 1
        if self._wheels < self.wheels_per_page:
            return
        self._wheels = 0
        if self._ended or self._next is None:
            return
        start = self._next
        if self.end == "stall" and start >= len(self.people):
            return
        asked = start + self.size if start in self.skip else start
        request = pagination_request(asked, sort=self.sort)
        times = 2 if start in self.repeat else 1
        answer = self.answers.get(start)
        for _ in range(times):
            if answer is not None:
                if answer.tab_url is not None:
                    tab._url = answer.tab_url
                self._send(
                    tab,
                    "POST",
                    f"{self.origin}{PAGINATION_PATH}",
                    answer.status,
                    answer.body,
                    "fetch",
                    request,
                    headers=answer.headers,
                )
                continue
            self._send(
                tab,
                "POST",
                f"{self.origin}{PAGINATION_PATH}",
                200,
                self._page(asked),
                "fetch",
                request,
            )
        if answer is not None:
            self._ended = True  # the page does not ask again after a failed answer

    def _page(self, start: int) -> bytes:
        people = self.people[start : start + self.size]
        if not people:
            self._next = None
            self._ended = True
            return pagination_payload([], start=start, next_start=None)
        after = start + len(people)
        next_start: int | None = after
        if after >= len(self.people) and self.end == "short":
            next_start = None  # the last page asks for nothing, short or full
        self._next = next_start
        self._ended = next_start is None
        options = {
            index - start: option
            for index, option in self.card_options.items()
            if start <= index < after
        }
        return pagination_payload(people, start=start, next_start=next_start, card_options=options)

    def _send(
        self,
        tab: ListeningTab,
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
        request = FakeRequest(method, resource_type, post_data)
        tab.emit(FakeResponse(url, status, body, request, headers=headers))
