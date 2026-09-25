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

import base64
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

#: A logged-in page's HTML shell with no first screen in it. Like every logged-in
#: LinkedIn page it links to sign-out and sign-in paths (#188 review, M1): a
#: classifier that searched the body for wall paths would read it as a login wall.
SHELL = (
    b"<!doctype html><html><body><nav>"
    b'<a href="/uas/logout?session_full_logout=&csrfToken=fake">Sign out</a>'
    b'<a href="/login?fromSignIn=true">Switch account</a>'
    b'<a href="/checkpoint/lg/login-submit">Security</a>'
    b"</nav><div id=root></div></body></html>"
)


class FakeRequest:
    """The request side of a response: method, resource type, and the body the page sent.

    Read-only by construction -- it has no method at all -- and it counts every read,
    so a test can assert what an observation looked at.
    """

    __slots__ = ("_failure", "_method", "_post_data", "_resource_type", "_url", "reads")

    def __init__(
        self,
        method: str,
        resource_type: str,
        post_data: str | None,
        *,
        url: str = "",
        failure: str | None = None,
    ) -> None:
        self._method = method
        self._resource_type = resource_type
        self._post_data = post_data
        self._url = url
        self._failure = failure
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

    @property
    def url(self) -> str:
        """What a ``requestfinished``/``requestfailed`` event's request says (#200)."""
        self.reads.append("url")
        return self._url

    @property
    def failure(self) -> str | None:
        self.reads.append("failure")
        return self._failure


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
        from_service_worker: bool = False,
    ) -> None:
        self._url = url
        self._from_service_worker = from_service_worker
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

    @property
    def from_service_worker(self) -> bool:
        return self._from_service_worker

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

    def emit_request_end(self, event: str, request: FakeRequest) -> None:
        """``requestfinished`` or ``requestfailed`` for ``request`` (#200)."""
        for handler in list(self.listeners[event]):
            handler(request)

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


#: What Chrome says when the body of a response it received is gone (#197), with an
#: invented profile url in it the way Playwright's messages quote urls: a test can
#: assert the message never reaches a log or a run.
LOST_BODY_MESSAGE = (
    "Protocol error (Network.getResponseBody): No resource with given identifier found"
    " for https://www.linkedin.com/in/fake-lost-slug-0000/"
)


@dataclass(slots=True)
class Lost:
    """A pagination answer the browser receives but cannot hand the body of over (#197).

    ``then`` is what the page does next: ``"reask"`` (its fetch failed, so the next
    scroll asks for the same start again), ``"move_on"`` (the page itself read the
    answer -- only the browser's copy is gone -- so the next scroll asks for the page
    after), ``"silent"`` (the page asks for nothing more), or ``"duplicate"`` (the
    answer reads, then the page's retried copy of the same request arrives without a
    body). ``times`` is how many asks in a row are lost, for ``"reask"``.
    """

    then: str = "reask"
    error: Exception = field(default_factory=lambda: Exception(LOST_BODY_MESSAGE))
    times: int = 1
    #: What the body tap's session receives as the answer streams in (#200), when the
    #: site has one: ``"whole"`` (the page's own copy), ``"half"`` (cut short),
    #: ``"short"`` (a whole payload with half the cards, still asking for the next
    #: page), or ``"none"`` (Chrome refuses to stream it).
    streamed: str = "none"


class FakeCdpSession:
    """The body tap's CDP session (#200): records every ``send``; the site emits events.

    ``streamResourceContent`` answers with what the site says arrived for that request,
    or refuses (an answer that already finished, as Chrome does). Nothing here can hold
    or change a request: there is nothing to hold.
    """

    def __init__(self) -> None:
        self.handlers: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.bodies: dict[str, bytes] = {}
        self.detached = False

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self.handlers[event].append(handler)

    async def send(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self.sent.append((method, dict(params or {})))
        if method == "Network.streamResourceContent":
            request_id = str((params or {})["requestId"])
            if request_id not in self.bodies:
                raise RuntimeError("Request with the provided ID has already finished loading")
            return {"bufferedData": base64.b64encode(self.bodies[request_id]).decode()}
        return {}

    async def detach(self) -> None:
        self.detached = True

    def emit(self, event: str, params: Mapping[str, Any]) -> None:
        for handler in list(self.handlers[event]):
            handler(params)


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
    ``lost`` makes the answer for a ``startIndex`` arrive without a readable body;
    ``duplicate_answers`` follows a page's answer with the page's retried copy of the
    same request, answered as given (a stale start answered with a wall or a throttle).
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
        sort: str | None = SORT_NEWEST_FIRST,
        wheels_per_page: int = 1,
        answers: Mapping[int, Answer] | None = None,
        repeat: frozenset[int] = frozenset(),
        skip: frozenset[int] = frozenset(),
        other_pager: bool = False,
        early_pagination: bool = False,
        answer_plans: Callable[[int], bool] | None = None,
        card_options: Mapping[int, CardOptions] | None = None,
        origin: str = ORIGIN,
        lost: Mapping[int, Lost] | None = None,
        duplicate_answers: Mapping[int, Answer] | None = None,
        tap: bool = False,
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
        self.early_pagination = early_pagination
        #: When set, answers come per scroll plan (see :meth:`plan_started`), not per
        #: wheel: plan ``n`` (from 1) brings the next page when ``answer_plans(n)``.
        self.answer_plans = answer_plans
        self.plans = 0
        self.card_options = dict(card_options or {})
        self.origin = origin
        self.lost = dict(lost or {})
        self.duplicate_answers = dict(duplicate_answers or {})
        #: Whether a body tap's CDP session can be opened here (#200), and the one that was.
        self.tap = tap
        self.cdp: FakeCdpSession | None = None
        self._request_ids = 0
        #: Every request the page itself made: (method, path, body).
        self.requests: list[tuple[str, str, str | None]] = []
        self._next: int | None = None
        self._wheels = 0
        self._ended = False

    @property
    def fetches(self) -> list[str | None]:
        """The pagination request bodies the page sent, in order."""
        return [body for method, path, body in self.requests if path == PAGINATION_PATH]

    async def new_cdp_session(self, page: object) -> FakeCdpSession:
        """Only when the site was built with ``tap``: otherwise, like a browser without it."""
        if not self.tap:
            raise RuntimeError("this fake browser has no CDP sessions")
        self.cdp = FakeCdpSession()
        return self.cdp

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
        if self.early_pagination:
            self._send(
                tab,
                "POST",
                f"{self.origin}{PAGINATION_PATH}",
                200,
                pagination_payload(self.people[10:20], start=10, next_start=20),
                "fetch",
                pagination_request(10, sort=self.sort),
            )
        if self.landing == "document":
            self._send(tab, "GET", url, 200, document_html(screen).encode("utf-8"), "document")
        else:
            shell = SHELL
            self._send(tab, "GET", url, 200, shell, "document")
            self._send(
                tab, "POST", f"{self.origin}{CONNECTIONS_SCREEN_PATH}", 200, screen, "fetch", "{}"
            )
        if self.other_pager:
            other = pagination_request(0, pager="com.linkedin.sdui.pagers.mynetwork.other")
            self._send(
                tab, "POST", f"{self.origin}{PAGINATION_PATH}", 200, b"0:[]\n", "fetch", other
            )

    def plan_started(self, tab: ListeningTab) -> None:
        """One scroll plan began (a test wraps ``BrowserRun.scroll`` to call this)."""
        self.plans += 1
        if self.answer_plans is not None and self.answer_plans(self.plans):
            self._answer(tab)

    def scrolled(self, tab: ListeningTab) -> None:
        if self.answer_plans is not None:
            return
        self._wheels += 1
        if self._wheels < self.wheels_per_page:
            return
        self._wheels = 0
        self._answer(tab)

    def _answer(self, tab: ListeningTab) -> None:
        if self._ended or self._next is None:
            return
        start = self._next
        if self.end == "stall" and start >= len(self.people):
            return
        asked = start + self.size if start in self.skip else start
        request = pagination_request(asked, sort=self.sort)
        if self._lose(tab, start, request):
            return
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
        duplicate = self.duplicate_answers.get(start)
        if answer is None and duplicate is not None:
            self._send(
                tab,
                "POST",
                f"{self.origin}{PAGINATION_PATH}",
                duplicate.status,
                duplicate.body,
                "fetch",
                request,
                headers=duplicate.headers,
            )
        if answer is not None:
            self._ended = True  # the page does not ask again after a failed answer

    def _lose(self, tab: ListeningTab, start: int, request: str) -> bool:
        """Send ``start``'s answer without a readable body, if ``lost`` says to."""
        lost = self.lost.get(start)
        if lost is None or lost.times <= 0:
            return False
        url = f"{self.origin}{PAGINATION_PATH}"
        if lost.then == "duplicate":
            lost.times = 0
            self._send(tab, "POST", url, 200, self._page(start), "fetch", request)
            self._send(tab, "POST", url, 200, b"", "fetch", request, body_error=lost.error)
            return True
        lost.times -= 1
        # The page's own copy: what it goes on to ask for depends on whether it read it.
        copy = self._page(start) if lost.then == "move_on" or lost.streamed != "none" else b""
        body = copy if lost.then == "move_on" else b""
        short = pagination_payload(
            self.people[start : start + self.size // 2], start=start, next_start=start + self.size
        )
        streamed = {"whole": copy, "half": copy[: len(copy) // 2], "short": short}.get(
            lost.streamed
        )
        self._send(
            tab,
            "POST",
            url,
            200,
            body,
            "fetch",
            request,
            body_error=lost.error,
            streamed=streamed,
        )
        if lost.then == "silent":
            self._ended = True
        return True

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
        body_error: Exception | None = None,
        streamed: bytes | None = None,
    ) -> None:
        self.requests.append((method, urlsplit(url).path, post_data))
        request = FakeRequest(method, resource_type, post_data)
        cdp = self.cdp
        request_id = ""
        if cdp is not None:
            self._request_ids += 1
            request_id = f"fake.{self._request_ids}"
            asked: dict[str, Any] = {"url": url, "method": method}
            if post_data is not None:
                asked["postData"] = post_data
            cdp.emit("Network.requestWillBeSent", {"requestId": request_id, "request": asked})
            cdp.emit("Network.responseReceived", {"requestId": request_id})
            if streamed is not None:
                cdp.bodies[request_id] = streamed
        tab.emit(FakeResponse(url, status, body, request, headers=headers, body_error=body_error))
        if cdp is not None:
            if body_error is None:
                cdp.emit("Network.loadingFinished", {"requestId": request_id})
            else:
                # The page's own client cancelled it after reading it (#200).
                cdp.emit(
                    "Network.loadingFailed",
                    {"requestId": request_id, "canceled": True, "errorText": "net::ERR_ABORTED"},
                )
