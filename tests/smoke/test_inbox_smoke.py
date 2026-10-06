"""The inbox poll over a real Chrome, against a loopback replica of ``/messaging/`` (#438).

Opt in with ``NETKEEPER_BROWSER_TESTS=1``, same as every other file under
``tests/smoke/``, and point ``NETKEEPER_CDP_URL`` at an isolated Chrome on an explicit,
unused port (never 9222). The only site is the replica this file serves for itself.
Every person, message and id in it comes from :mod:`messaging_pages`' invented
fixtures; nothing is read from, or sent to, linkedin.com.

The real :class:`~netkeeper.linkedin.page_inbox.PageInbox` runs through a real
:class:`~netkeeper.linkedin.browser.BrowserRun`. The replica's page script plays
LinkedIn's: on load it fetches the conversation list, and when its *list pane* scrolls
near the bottom it fetches the next older page. A thread's page fetches its own
messages. Each answer is the fixture's own body, served from the url the fixture builds.

What this proves that the offline fakes (:mod:`inbox_site`) can't:

* Playwright's ``response`` events reach ``BrowserRun.observe``, in order, with bodies
  the source can parse: the delta holds the invented text.
* Which pane ``BrowserRun.scroll``'s pointer rest scrolls. The replica is a split
  layout: a fixed header over a ``<main>`` that holds a conversation list pane and a
  thread pane, each its own scroll container. Each pane's scroll events are reported to
  the replica, so :func:`test_the_wheel_scrolls_the_list_pane` can say which one moved.
* Older pages load, so the read reaches ``since`` and ``complete`` is true.
* Navigating to a thread's address makes the page load that thread, and the source
  reads its answer.

The layout is an assumption, as in ``test_observe_smoke.py``: the #374 capture shows the
answers, not how the page lays them out. Two shapes are tested (:data:`LAYOUTS`).

The failing scenarios (a renamed field, a non-200 thread, a third sender) each stop the
poll with ``InboxReadStopped``: none returns a delta, so none can be ``complete``.
"""

from __future__ import annotations

import json
import os
import random
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import urlsplit

import messaging_pages as mp
import pytest
from inbox_site import conv, people

from netkeeper.linkedin.browser import AttachBrowserProvider
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.inbox import InboxDelta, InboxJobSpec, InboxReadStopped
from netkeeper.linkedin.messaging_shapes import MESSAGING_PAGE_PATH
from netkeeper.linkedin.pacing import ScrollProfile
from netkeeper.linkedin.page_inbox import PageInbox

pytestmark = pytest.mark.skipif(
    os.environ.get("NETKEEPER_BROWSER_TESTS") != "1",
    reason="browser smoke suite: start Chrome, then set NETKEEPER_BROWSER_TESTS=1",
)

CDP_URL = os.environ.get("NETKEEPER_CDP_URL", "http://127.0.0.1:9222")

T0 = datetime.fromtimestamp(mp.T0 / 1000, tz=UTC)

#: Large, fast wheel deltas: real scroll events without a real dwell per scroll.
_FAST_SCROLL = ScrollProfile(
    steps_range=(2, 3),
    delta_range_px=(2500, 3500),
    pause_range_s=(0.02, 0.05),
    back_up_p=0.0,
    dwell_median_s=0.2,
    dwell_sigma=0.05,
)

_CSS_BASE = """
  html, body { margin: 0; height: 100%; overflow: hidden; font-family: sans-serif; }
  #nav { position: fixed; top: 0; left: 0; right: 0; height: 56px; background: #0a66c2;
         z-index: 10; }
  main { position: fixed; top: 56px; bottom: 0; left: 50%; transform: translateX(-50%);
         width: min(1128px, 100vw); display: flex; background: #fff; }
  .pane { overflow-y: auto; box-sizing: border-box; }
  .card { height: 100px; border-bottom: 1px solid #ddd; }
"""

#: Two plausible shapes, both tested. In both the list pane is on the left and each
#: pane scrolls on its own; they differ in how wide the list is. ``narrow-list`` is a
#: list column a third of ``<main>``'s width (the pointer rest, which aims at
#: ``<main>``'s horizontal center, then lands over the thread pane); ``wide-list`` is a
#: list column more than half of it (the center lands over the list).
LAYOUTS: dict[str, str] = {
    "narrow-list": _CSS_BASE
    + "#list-pane { width: 33%; } #thread-pane { flex: 1; background: #f9f9f9; }",
    "wide-list": _CSS_BASE
    + "#list-pane { width: 60%; } #thread-pane { flex: 1; background: #f9f9f9; }",
}

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>replica</title>
<style>__CSS__</style></head><body>
<div id="nav"></div>
<main>
  <div id="list-pane" class="pane"><div id="list"></div><div style="height:1500px"></div></div>
  <div id="thread-pane" class="pane"><div id="thread"></div><div style="height:3000px"></div></div>
</main>
<script type="application/json" id="cfg">__CFG__</script>
<script>
const cfg = JSON.parse(document.getElementById('cfg').textContent);
const listPane = document.getElementById('list-pane');
const threadPane = document.getElementById('thread-pane');
let nextOlder = 0, busy = false;
function render(into, count) {
  for (let i = 0; i < count; i++) {
    const card = document.createElement('div');
    card.className = 'card';
    card.textContent = 'invented card';
    into.appendChild(card);
  }
}
function report(pane) { fetch('/__scrolled/' + pane, {method: 'POST'}); }
listPane.addEventListener('scroll', async () => {
  report('list');
  const near = listPane.scrollTop + listPane.clientHeight >= listPane.scrollHeight - 400;
  if (busy || !near || nextOlder >= cfg.older.length) return;
  busy = true;
  await fetch(cfg.older[nextOlder++]);
  render(document.getElementById('list'), 2);
  busy = false;
});
threadPane.addEventListener('scroll', () => report('thread'));
(async () => {
  for (const url of cfg.on_load) await fetch(url);
  render(document.getElementById('list'), cfg.first);
  if (cfg.on_load.length > 1) render(document.getElementById('thread'), 3);
})();
</script></body></html>"""


def _rel(url: str) -> str:
    """A fixture's absolute url as the replica's own path and query."""
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}" if parts.query else parts.path


@dataclass(slots=True)
class Answer:
    status: int
    body: str


@dataclass(slots=True)
class Site:
    """What the replica serves, and what it saw."""

    layout: str = "wide-list"
    pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    answers: dict[str, Answer] = field(default_factory=dict)
    #: Every GET the replica received, in order (path and query as sent).
    requests: list[str] = field(default_factory=list)
    scrolled: dict[str, int] = field(default_factory=lambda: {"list": 0, "thread": 0})


class _Replica(BaseHTTPRequestHandler):
    site: ClassVar[Site]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, body: str, kind: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        pane = self.path.rsplit("/", 1)[-1]
        if self.path.startswith("/__scrolled/") and pane in self.site.scrolled:
            self.site.scrolled[pane] += 1
        self._send(204, "", "text/plain")

    def do_GET(self) -> None:
        site = self.site
        path = urlsplit(self.path).path
        if path == "/favicon.ico":
            self._send(404, "", "text/plain")
            return
        site.requests.append(self.path)
        if path in site.pages:
            cfg = json.dumps(site.pages[path]).replace("</", "<\\/")
            html = _PAGE.replace("__CSS__", LAYOUTS[site.layout]).replace("__CFG__", cfg)
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path in site.answers:
            answer = site.answers[self.path]
            self._send(answer.status, answer.body, "application/json")
        else:
            self._send(404, "{}", "application/json")


@pytest.fixture(scope="module")
def origin() -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), _Replica)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


# --- the invented mailbox ---------------------------------------------------------------

FIRST = 2
PAGE_SIZE = 2
THREAD_CONVERSATION = mp.ONE_TO_ONE_INBOUND  # conversation 11, newest, minute 50


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _conversations() -> list[mp.Conv]:
    """Six one-to-one conversations, newest first: minutes 50, 40, 30, 20, 10, 0."""
    others = people(5)
    return [
        THREAD_CONVERSATION,
        *(conv(12 + i, others[i], 40 - 10 * i) for i in range(5)),
    ]


def _site(layout: str, convs: list[mp.Conv], thread: Answer | None = None) -> Site:
    """The replica for ``convs``: the list on load, the older pages, and one thread."""
    site = Site(layout=layout)
    first_url = _rel(mp.conversations_sync_url())
    site.answers[first_url] = Answer(200, mp.conversations_by_sync_token(convs[:FIRST]))
    older: list[str] = []
    shown = FIRST
    index = 0
    while shown < len(convs):
        page = convs[shown : shown + PAGE_SIZE]
        last = convs[shown - 1].last
        assert last is not None
        if index == 0:
            url = mp.conversations_category_url(last_updated_before=last.at_ms)
        else:
            url = mp.conversations_category_url(next_cursor=f"invented-cursor-{index}")
        shown += len(page)
        cursor = None if shown >= len(convs) else f"invented-cursor-{index + 1}"
        site.answers[_rel(url)] = Answer(
            200, mp.conversations_by_category(page, next_cursor=cursor)
        )
        older.append(_rel(url))
        index += 1
    site.pages[MESSAGING_PAGE_PATH] = {"on_load": [first_url], "older": older, "first": FIRST}
    thread_path = _rel(mp.thread_url(THREAD_CONVERSATION.n))
    thread_answer_url = _rel(mp.messages_sync_url(THREAD_CONVERSATION.n))
    site.answers[thread_answer_url] = thread or Answer(
        200, mp.messages_by_sync_token(mp.THREAD_ONE_TO_ONE)
    )
    site.pages[thread_path] = {"on_load": [thread_answer_url], "older": [], "first": 0}
    return site


def _spec(*, since: datetime | None, open_thread: bool) -> InboxJobSpec:
    return InboxJobSpec(
        since=since,
        watched_urns=frozenset(),
        max_conversations=50,
        open_threads_for=(
            frozenset({mp.conversation_urn(THREAD_CONVERSATION.n)}) if open_thread else frozenset()
        ),
    )


async def _no_sleep(seconds: float) -> None:
    return None


async def _poll(origin: str, spec: InboxJobSpec) -> tuple[InboxDelta, PageInbox]:
    provider = AttachBrowserProvider(CDP_URL)
    async with provider.run("smoke-inbox") as run:
        source = PageInbox(
            run,
            origin=origin,
            rng=random.Random(5),
            scroll_profile=_FAST_SCROLL,
            sleep=_no_sleep,
            thread_pause_s=(0.0, 0.0),
            response_wait_s=1.0,
        )
        return await source.read(spec), source


def _install(site: Site) -> None:
    _Replica.site = site


# --- the healthy poll ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "layout",
    [
        "wide-list",
        pytest.param(
            "narrow-list",
            marks=pytest.mark.xfail(
                strict=True,
                raises=InboxReadStopped,
                reason=(
                    "finding (#438): the pointer rest aims at <main>'s horizontal center, which is"
                    " over the thread pane when the list is under half of <main>'s width, so the"
                    " wheel scrolls the thread pane and the list never loads older pages"
                ),
            ),
        ),
    ],
)
async def test_the_wheel_scrolls_the_list_pane(origin: str, layout: str) -> None:
    """The pointer rest must land where the wheel moves the *list*, never only the thread."""
    site = _site(layout, _conversations())
    _install(site)
    delta, _ = await _poll(origin, _spec(since=None, open_thread=False))
    assert site.scrolled["list"] > 0, (layout, site.scrolled)
    assert site.scrolled["thread"] == 0, (layout, site.scrolled)
    assert delta.complete


async def test_older_pages_load_and_the_read_is_complete_against_since(origin: str) -> None:
    layout = "wide-list"
    site = _site(layout, _conversations())
    _install(site)
    spec = _spec(since=at(25), open_thread=True)
    delta, source = await _poll(origin, spec)

    # since is reached on the first older page (conversation 14, minute 20): complete.
    assert delta.complete
    assert {c.conversation_urn for c in delta.conversations} == {
        mp.conversation_urn(n) for n in (11, 12, 13)
    }
    assert delta.owner_urn == mp.OWNER.urn

    # Bodies were kept: the list's last message and the thread's three, whole.
    by_urn = {c.conversation_urn: c for c in delta.conversations}
    opened = by_urn[mp.conversation_urn(11)]
    texts = {m.text_snippet for m in opened.messages}
    assert "Invented opener about a pretend conference." in texts
    assert any(t.startswith("Invented answer, line one") for t in texts)
    assert "Invented reply about the fictional robotics meetup." in texts
    assert len(opened.messages) == 3
    assert source.threads_opened == 1

    # What the page asked for, and nothing netkeeper added: the document, the list, older
    # pages in order from the first (the page itself may ask for one more than the read
    # needs), then the thread's document and its answer.
    older = site.pages[MESSAGING_PAGE_PATH]["older"]
    graphql = [r for r in site.requests if "/graphql" in r]
    assert graphql[0] == _rel(mp.conversations_sync_url())
    assert graphql[-1] == _rel(mp.messages_sync_url(11))
    assert graphql[1:-1] == older[: len(graphql) - 2]
    assert len(graphql) >= 3
    documents = [r for r in site.requests if "/graphql" not in r]
    assert documents == [MESSAGING_PAGE_PATH, _rel(mp.thread_url(11))]


async def test_a_list_read_to_its_end_without_since_is_complete(origin: str) -> None:
    site = _site("wide-list", _conversations())
    _install(site)
    delta, source = await _poll(origin, _spec(since=None, open_thread=False))
    assert delta.complete
    assert len(delta.conversations) == 6
    assert source.threads_opened == 0
    assert len([r for r in site.requests if "category" in r.lower() or "cursor" in r]) == 2


# --- the failing polls: each stops, none completes -------------------------------------------


def _renamed(body: str) -> str:
    assert '"deliveredAt"' in body
    return body.replace('"deliveredAt"', '"deliveredOn"')


def _third_sender_thread() -> str:
    stranger = mp.THADDEUS
    msgs = (
        *mp.THREAD_ONE_TO_ONE[:2],
        mp.Msg(11, 4, stranger, "Invented line from a third person.", mp.T0 + 45 * mp.MINUTE_MS),
        mp.THREAD_ONE_TO_ONE[2],
    )
    return mp.messages_by_sync_token(msgs)


FAILURES = {
    "renamed_field": Answer(200, _renamed(mp.messages_by_sync_token(mp.THREAD_ONE_TO_ONE))),
    "non_200_thread": Answer(500, "{}"),
    "third_sender": Answer(200, _third_sender_thread()),
}


@pytest.mark.parametrize("name", list(FAILURES))
async def test_a_bad_thread_stops_the_poll_and_never_completes(origin: str, name: str) -> None:
    site = _site("wide-list", _conversations(), thread=FAILURES[name])
    _install(site)
    with pytest.raises(InboxReadStopped) as stopped:
        await _poll(origin, _spec(since=at(25), open_thread=True))
    assert stopped.value.outcome is Outcome.ROUTE_CHANGED
    # The thread was reached: the stop came from its answer, not from an earlier problem.
    assert _rel(mp.messages_sync_url(11)) in site.requests
