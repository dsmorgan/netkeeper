"""``PageConnections`` over a real Chrome: the page loads its own answers, netkeeper reads them.

Opt in with ``NETKEEPER_BROWSER_TESTS=1``, same as every other file under
``tests/smoke/``. The only site involved is a loopback replica this file serves for
itself; the people are :mod:`voyager_pages`' invented cast and the payloads
:mod:`flagship_pages`' hand-built ones.

What this proves that the offline tests cannot, because their tab is a fake: that a
real ``mouse.wheel`` replay scrolls a real page far enough that the page's *own*
script sends the pagination request; that Playwright's ``response`` event hands the
observation a real body, in order; and that the observation changed nothing about
any request -- the replica records every request it received, the page records
every request it sent, and the two lists are the same, headers included.

**The layout is deliberately not a flat scrolling document (#192).** A fixed header
sits across the top of the viewport; below it, a ``<main>`` column scrolls (capped
at 800px, centered) with non-scrolling left and right rails flanking it -- the
``html``/``body`` do not scroll at all, and neither do the rails. That is what the
#149 capture showed the real connections page does (a centered column, not a
full-bleed one), and it is why the bug in #192 went uncaught here before:
Playwright's ``mouse.wheel`` fires at the virtual pointer's position, which starts
at (0, 0) and sits under the fixed header, not over the scrolling column, so a
wheel replay that never moved the pointer first scrolled nothing.
``BrowserRun.scroll`` rests the pointer over the content before its first wheel
event now (``_rest_pointer_over_content``), reading ``<main>``'s own on-screen box
rather than guessing at the viewport (#192 review, F1) -- a guess that missed the
centered column entirely on a wide window, and had nowhere reliable to land on a
narrow one. Without that fix,
``test_a_real_page_loads_its_own_pages_and_the_run_reads_them_all`` below stalls
and ends ``RouteChanged`` instead of ``END_OF_LIST`` -- checked by hand against the
pre-#192 code, and recorded in that PR's description rather than as a test of the
old code, which no longer exists to run. Run at several window sizes
(560, 740, 1280, 2200px wide) to cover a phone-width netkeeper Chrome window, the
maintainer's MacBook Air split-screen width, an ordinary laptop, and an ultrawide.

Start Chrome first with the command ``netkeeper browser launch`` prints, and point
``NETKEEPER_CDP_URL`` at it.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import urlsplit

import pytest
from flagship_pages import document_html, pagination_payload, pagination_request, screen_payload
from voyager_pages import PEOPLE, Person

from netkeeper.linkedin.browser import AttachBrowserProvider, BrowserUnavailable
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.connections import (
    ConnectionsPage,
    StopReason,
    SyncJobSpec,
    SyncMode,
    run_connections_sync,
)
from netkeeper.linkedin.flagship import CONNECTIONS_PAGE_PATH, PAGINATION_PATH
from netkeeper.linkedin.pacing import ScrollProfile
from netkeeper.linkedin.page_connections import PageConnections

pytestmark = pytest.mark.skipif(
    os.environ.get("NETKEEPER_BROWSER_TESTS") != "1",
    reason="browser smoke suite: start Chrome, then set NETKEEPER_BROWSER_TESTS=1",
)

CDP_URL = os.environ.get("NETKEEPER_CDP_URL", "http://127.0.0.1:9222")

#: Large, fast wheel deltas: real scroll events without a real dwell per scroll.
_FAST_SCROLL = ScrollProfile(
    steps_range=(2, 3),
    delta_range_px=(2500, 3500),
    pause_range_s=(0.02, 0.05),
    back_up_p=0.0,
    dwell_median_s=0.2,
    dwell_sigma=0.05,
)

#: The header the replica page sets on every request it sends, and checks came back.
MARKER = "x-replica-marker"


def _people(count: int) -> list[Person]:
    extra = [
        Person(600 + i, f"Given{i}", f"Family{i}", f"Role {i} at Invented Firm {i % 5}")
        for i in range(max(count - len(PEOPLE), 0))
    ]
    return [*PEOPLE, *extra][:count]


#: The real layout (#192, and the #192 review's F1): a fixed header across the top,
#: covering (0, 0) -- where Playwright's virtual pointer starts -- flagship-web's
#: own centered ~1128px column (``<main>``, capped here at 800px so it shows up at
#: ordinary laptop widths too, not just an ultrawide), and non-scrolling left and
#: right rails flanking it wide enough to fill whatever room the viewport has left.
#: ``html``/``body`` do not scroll at all; neither do the rails -- only ``<main>``
#: does, so a target that landed on a rail or the header, not on ``<main>``, has no
#: scrollable ancestor to reach and scrolls nothing. This is what a plain fraction
#: of a *guessed* viewport size misses on a wide monitor (a centered column, not a
#: full-bleed one) and on a narrow one (too little width left for a fixed-pixel
#: guess to land inside); reading ``<main>``'s own real box does not.
_LAYOUT_CSS = """
<style>
  html, body { margin: 0; height: 100%; overflow: hidden; }
  #nav { position: fixed; top: 0; left: 0; right: 0; height: 56px;
         background: #0a66c2; z-index: 10; }
  #rail-left, #rail-right { position: fixed; top: 56px; bottom: 0; width: 50%;
         background: #f3f2ef; overflow: hidden; }
  #rail-left { left: 0; }
  #rail-right { right: 0; }
  main { position: fixed; top: 56px; bottom: 0; left: 50%; transform: translateX(-50%);
         width: min(800px, 100vw); background: #fff; overflow-y: auto; z-index: 5; }
</style>
<div id="nav"></div>
<div id="rail-left"></div>
<div id="rail-right"></div>
<main id="scroll">
  <div id="list"></div>
  <div style="height:900px"></div>
</main>
"""

#: The page's own script: render cards, and when the *container* (not the document)
#: scrolls near the bottom, ask the pagination endpoint for the next page -- the way
#: the real page does, inside its own scroll container -- recording each request it
#: sends in ``window.__sent`` so the test can compare it with what the replica
#: received.
_SCRIPT = """
<script>
  const TEMPLATE = __TEMPLATE__;
  let next = __FIRST_NEXT__;
  let busy = false;
  window.__sent = [];
  const list = document.getElementById('list');
  const container = document.getElementById('scroll');
  function render(n) {
    for (let i = 0; i < n; i++) {
      const card = document.createElement('div');
      card.style.height = '140px';
      card.textContent = 'card';
      list.appendChild(card);
    }
  }
  render(__FIRST_CARDS__);
  async function more() {
    if (busy || next === null) { return; }
    busy = true;
    const body = TEMPLATE.split('"startIndex": 0').join('"startIndex": ' + next);
    const marker = 'page-' + (window.__sent.length + 1);
    window.__sent.push({body: body, marker: marker});
    const answer = await fetch('/flagship-web/rsc-action/actions/pagination?sduiid=replica', {
      method: 'POST',
      headers: {'content-type': 'application/json', 'x-replica-marker': marker},
      body: body,
    });
    const cards = parseInt(answer.headers.get('x-replica-cards'), 10);
    const after = answer.headers.get('x-replica-next');
    await answer.arrayBuffer();
    next = after === 'none' ? null : parseInt(after, 10);
    render(cards);
    busy = false;
  }
  container.addEventListener('scroll', () => {
    if (container.scrollTop + container.clientHeight > container.scrollHeight - 600) { more(); }
  });
</script>
"""


class _Replica(BaseHTTPRequestHandler):
    people: ClassVar[list[Person]] = []
    stall_after: ClassVar[int | None] = None
    land_on_checkpoint: ClassVar[bool] = False
    received: ClassVar[list[dict[str, Any]]] = []

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == CONNECTIONS_PAGE_PATH:
            if self.land_on_checkpoint:
                self._redirect("/checkpoint/challenge/AgFAKE")
                return
            first = self.people[:10]
            screen = screen_payload(first, total=len(self.people))
            first_next = "null" if len(first) >= len(self.people) else str(len(first))
            page = document_html(screen).replace(
                "</body>",
                _LAYOUT_CSS
                + _SCRIPT.replace("__TEMPLATE__", json.dumps(pagination_request(0)))
                .replace("__FIRST_NEXT__", first_next)
                .replace("__FIRST_CARDS__", str(len(first)))
                + "</body>",
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif path.startswith("/checkpoint/"):
            self._send(200, b"<html><body>security check</body></html>", "text/html")
        else:
            self._send(404, b"", "text/plain")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        type(self).received.append({"path": path, "body": body, "marker": self.headers.get(MARKER)})
        if path != PAGINATION_PATH:
            self._send(404, b"", "text/plain")
            return
        start = json.loads(body)["clientArguments"]["payload"]["startIndex"]
        stop = len(self.people) if self.stall_after is None else self.stall_after
        people = self.people[start : start + 10] if start < stop else []
        after = start + len(people)
        next_start = after if people else None
        if self.stall_after is not None and after >= self.stall_after:
            # The page stops asking here, without its answer having said so.
            payload = pagination_payload(people, start=start, next_start=after)
            self._send(200, payload, "application/octet-stream", cards=len(people), next_=None)
            return
        payload = pagination_payload(people, start=start, next_start=next_start)
        self._send(200, payload, "application/octet-stream", cards=len(people), next_=next_start)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        cards: int = 0,
        next_: int | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-replica-cards", str(cards))
        self.send_header("x-replica-next", "none" if next_ is None else str(next_))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the replica's own access log out of the test output."""


@pytest.fixture
def site() -> Iterator[str]:
    _Replica.people = _people(35)
    _Replica.stall_after = None
    _Replica.land_on_checkpoint = False
    _Replica.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Replica)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def provider() -> AttachBrowserProvider:
    return AttachBrowserProvider(CDP_URL)


class _Gate:
    async def before_page(self, number: int) -> bool:
        return True

    async def between_pages(self) -> None:
        return None


async def test_a_real_page_loads_its_own_pages_and_the_run_reads_them_all(
    provider: AttachBrowserProvider, site: str
) -> None:
    pages: list[ConnectionsPage] = []

    async def on_page(page: ConnectionsPage) -> None:
        pages.append(page)

    try:
        async with provider.run() as run:
            source = PageConnections(run, origin=site, scroll_profile=_FAST_SCROLL)
            result = await run_connections_sync(
                SyncJobSpec(mode=SyncMode.FULL, page_budget=20),
                source,
                _Gate(),
                on_page=on_page,
            )
            sent = await (await run.ensure_page()).evaluate("window.__sent")
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    people = _Replica.people
    assert [c.urn for page in pages for c in page.connections] == [p.urn for p in people]
    assert result.reason is StopReason.END_OF_LIST and result.complete
    first = pages[0].connections[0]
    assert (first.public_id, first.first_name, first.headline) == (
        people[0].slug,
        "Priya",
        "Data engineer at Fictional Robotics Co",
    )

    # The observation altered nothing: every request the replica received is one the
    # page sent, byte for byte, with the page's own marker header, and there are no
    # others -- netkeeper sent none of its own.
    received = [(r["body"], r["marker"]) for r in _Replica.received]
    assert received == [(s["body"], s["marker"]) for s in sent]
    assert [
        json.loads(body)["clientArguments"]["payload"]["startIndex"] for body, _ in received
    ] == [
        10,
        20,
        30,
        35,
    ]


async def test_a_page_that_stops_asking_is_route_changed_not_the_end(
    provider: AttachBrowserProvider, site: str
) -> None:
    _Replica.stall_after = 20
    try:
        async with provider.run() as run:
            source = PageConnections(
                run, origin=site, scroll_profile=_FAST_SCROLL, response_wait_s=0.5
            )
            answer = await source.fetch_page(start=0, count=40)
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")
    assert answer.outcome is Outcome.ROUTE_CHANGED and answer.page is None


async def test_a_landing_on_a_checkpoint_is_read_as_one_and_nothing_scrolls(
    provider: AttachBrowserProvider, site: str
) -> None:
    _Replica.land_on_checkpoint = True
    try:
        async with provider.run() as run:
            source = PageConnections(run, origin=site, scroll_profile=_FAST_SCROLL)
            answer = await source.fetch_page(start=0, count=40)
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")
    assert answer.outcome is Outcome.CHECKPOINT
    assert _Replica.received == []
