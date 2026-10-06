"""A fake messaging page that loads its own answers, for the inbox source's offline tests.

:class:`InboxSite` is a :class:`browser_fakes.FakeContext` whose tabs behave like
LinkedIn's messaging page as the #374 capture showed it, without a browser or a socket:
navigating to ``/messaging/`` makes the tab "receive" the document and the list on load,
scrolling it makes the page "send" the next older page, and navigating to a thread's page
makes it "receive" that thread. Answers reach the tab's ``response`` listeners the way
Playwright delivers one. Every conversation is invented (:mod:`messaging_pages`); a
linkedin.com url here is a string a fake tab reports, never fetched.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import messaging_pages as mp
from browser_fakes import FakeContext, FakeMouse, FakePage
from flagship_site import FakeRequest, FakeResponse, ListeningTab

from netkeeper.linkedin.browser import PageLike

ORIGIN = mp.HOST
PAGE_URL = f"{ORIGIN}/messaging/"
CHECKPOINT_URL = "https://www.linkedin.com/checkpoint/challenge/AgFAKE?ctx=invented"
LOGIN_URL = "https://www.linkedin.com/login?session_redirect=invented"


@dataclass(slots=True)
class Reply:
    """What to answer one request with instead of the real answer."""

    status: int = 200
    body: str | bytes = b""
    tab_url: str | None = None


@dataclass(slots=True)
class Behavior:
    """The knobs a test turns. Everything defaults to a healthy page."""

    #: How many conversations the list on load carries; later ones come as older pages.
    first: int = 3
    #: How many each older page carries.
    page_size: int = 3
    #: ``"cursor"``: the last page has no next cursor. ``"empty"``: the end is an empty
    #: page. ``"stall"``: the page stops asking, proving nothing.
    end: str = "cursor"
    #: Thread number whose answer the page loads on its own at landing.
    auto_thread: int | None = None
    #: Where the tab lands for ``/messaging/`` (a wall), or ``None``.
    landing: str | None = None
    #: Replaces the on-load list answer, or the Nth (0-based) older page's.
    list_reply: Reply | None = None
    older_replies: dict[int, Reply] = field(default_factory=dict)
    #: Replaces the thread answer for a thread number.
    thread_replies: dict[int, Reply] = field(default_factory=dict)
    #: Thread numbers that load no messages when opened.
    silent_threads: frozenset[int] = frozenset()
    #: Applied to each older page's request url (a skipped page, a bad cursor).
    older_url: Callable[[int, str], str] | None = None
    #: The page loads no conversation list at all.
    no_list: bool = False
    #: Where the tab lands when a thread's page is opened (a wall), or ``None``.
    thread_landing: str | None = None
    #: A page of the list asked for twice.
    repeat_older: frozenset[int] = frozenset()


class InboxTab(ListeningTab):
    """A tab that tells its site where it went and what was scrolled."""

    def __init__(self, context: FakeContext) -> None:
        super().__init__(context)
        self.mouse = _Mouse(self)

    async def goto(self, url: str) -> object:
        result = await FakePage.goto(self, url)
        site = self.context
        if isinstance(site, InboxSite):
            site.navigated(self, url)
        return result


class _Mouse(FakeMouse):
    def __init__(self, tab: InboxTab) -> None:
        super().__init__()
        self._tab = tab

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await super().wheel(delta_x, delta_y)
        site = self._tab.context
        if delta_y > 0 and isinstance(site, InboxSite):
            site.scrolled(self._tab)


class InboxSite(FakeContext):
    """The messaging page over ``convs`` (newest first) and their ``threads``."""

    def __init__(
        self,
        convs: Sequence[mp.Conv],
        threads: Mapping[int, Sequence[mp.Msg]] | None = None,
        behavior: Behavior | None = None,
    ) -> None:
        super().__init__()
        self.convs = list(convs)
        self.threads = {n: list(m) for n, m in (threads or {}).items()}
        self.b = behavior or Behavior()
        #: Every request the page itself made: (method, url).
        self.requests: list[tuple[str, str]] = []
        #: The urls the tab was navigated to.
        self.navigations: list[str] = []
        self._shown = 0
        self._older_index = 0
        self._done = False

    async def new_page(self) -> PageLike:
        self.new_page_calls += 1
        if self.new_page_error is not None:
            raise self.new_page_error
        tab = InboxTab(self)
        self.pages.append(tab)
        return tab

    @property
    def thread_navigations(self) -> list[str]:
        return [u for u in self.navigations if "/messaging/thread/" in u]

    # --- the page's behavior --------------------------------------------------------------

    def navigated(self, tab: InboxTab, url: str) -> None:
        self.navigations.append(url)
        path = urlsplit(url).path
        if path == "/messaging/":
            if self.b.landing is not None:
                tab._url = self.b.landing
                return
            self._send(tab, url, 200, b"<html></html>")
            if self.b.no_list:
                return
            self._shown = min(self.b.first, len(self.convs))
            reply = self.b.list_reply
            if reply is not None:
                if reply.tab_url:
                    tab._url = reply.tab_url
                self._send(tab, mp.conversations_sync_url(), reply.status, reply.body)
            else:
                body = mp.conversations_by_sync_token(self.convs[: self._shown])
                self._send(tab, mp.conversations_sync_url(), 200, body)
            self._older_index = 0
            self._done = False
            if self.b.auto_thread is not None:
                self._send_thread(tab, self.b.auto_thread)
            return
        if path.startswith("/messaging/thread/"):
            if self.b.thread_landing is not None:
                tab._url = self.b.thread_landing
                return
            self._send(tab, url, 200, b"<html></html>")
            for n in self._thread_numbers():
                if mp.thread_url(n).endswith(path):
                    self._send_thread(tab, n)

    def _thread_numbers(self) -> list[int]:
        return [c.n for c in self.convs]

    def _send_thread(self, tab: InboxTab, n: int) -> None:
        if n in self.b.silent_threads:
            return
        reply = self.b.thread_replies.get(n)
        url = mp.messages_sync_url(n)
        if reply is not None:
            self._send(tab, url, reply.status, reply.body)
            return
        self._send(tab, url, 200, mp.messages_by_sync_token(self.threads.get(n, ())))

    def scrolled(self, tab: InboxTab) -> None:
        if self._done:
            return
        index = self._older_index
        page = self.convs[self._shown : self._shown + self.b.page_size]
        if not page and self.b.end == "stall":
            return
        if self._shown:
            last = self.convs[self._shown - 1].last
            before = last.at_ms if last else mp.T0
        else:
            before = mp.T0
        if index == 0:
            url = mp.conversations_category_url(last_updated_before=before)
        else:
            url = mp.conversations_category_url(next_cursor=f"invented-cursor-{index}")
        if self.b.older_url is not None:
            url = self.b.older_url(index, url)
        self._shown += len(page)
        self._older_index += 1
        exhausted = self._shown >= len(self.convs)
        cursor = (
            None
            if (exhausted and self.b.end == "cursor") or not page
            else (f"invented-cursor-{index + 1}")
        )
        if cursor is None:
            self._done = True
        reply = self.b.older_replies.get(index)
        body = mp.conversations_by_category(page, next_cursor=cursor)
        for _ in range(2 if index in self.b.repeat_older else 1):
            if reply is not None:
                if reply.tab_url:
                    tab._url = reply.tab_url
                self._send(tab, url, reply.status, reply.body)
            else:
                self._send(tab, url, 200, body)

    def _send(self, tab: InboxTab, url: str, status: int, body: str | bytes) -> None:
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.requests.append(("GET", url))
        request = FakeRequest("GET", "fetch", None)
        tab.emit(FakeResponse(url, status, raw, request))


# --- building conversations ---------------------------------------------------------------


def conv(
    n: int,
    who: mp.Member,
    minutes: int,
    *,
    outbound: bool = False,
    text: str = "Invented line.",
    **kwargs: object,
) -> mp.Conv:
    """A one-to-one conversation whose last message came ``minutes`` after ``T0``."""
    sender = mp.OWNER if outbound else who
    last = mp.Msg(n, 1, sender, text, mp.T0 + minutes * mp.MINUTE_MS)
    return mp.Conv(n, (who,), last, **kwargs)  # type: ignore[arg-type]


def people(count: int) -> list[mp.Member]:
    return [mp.Member(200 + i, f"Given{i}", f"Family{i}", f"Role {i}") for i in range(count)]
