"""``PageProfiles`` over a real Chrome: one Contact info click, and the page's own answers (#190).

Opt in with ``NETKEEPER_BROWSER_TESTS=1``, same as every other file under
``tests/smoke/``. The only site involved is a loopback replica this file serves for
itself; the people are :mod:`voyager_pages`' invented cast and the payloads
:mod:`flagship_pages`' hand-built ones.

What this proves that the offline tests cannot, because their tab is a fake: that
``get_by_role("link", name="Contact info", exact=True)`` finds a real link, that the
one real click reaches the page as one trusted click and nothing else (no key, no
second click), that the page's *own* script then sends the overlay request and
Playwright's ``response`` event hands its answer to the observation, that a wheel
replay makes the page ask for its lazy card, and that nothing about any request was
altered or added: the replica records every request it received, the page records
every request it sent, and the two lists are the same, marker header included. And
that a page with two such links, or none, gets no click at all.

Start Chrome first with the command ``netkeeper browser launch`` prints, and point
``NETKEEPER_CDP_URL`` at it.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import socket
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import urlsplit

import pytest
from flagship_pages import (
    Role,
    Website,
    contact_info_payload,
    document_html,
    experience_payload,
    profile_payload,
)
from voyager_pages import PEOPLE, Person

from netkeeper.linkedin.browser import AttachBrowserProvider, BrowserRun, BrowserUnavailable
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.enrich import (
    EnrichJobSpec,
    EnrichResult,
    EnrichTarget,
    PacingProfile,
    ProfileHarvest,
    StopReason,
    run_enrichment,
)
from netkeeper.linkedin.flagship import CONTACT_DETAILS_SCREEN_ID, NAVIGATION_PATH
from netkeeper.linkedin.flagship_profile import COMPONENT_PATH
from netkeeper.linkedin.pacing import DelayProfile, ScrollProfile
from netkeeper.linkedin.page_profiles import PageProfiles

pytestmark = pytest.mark.skipif(
    os.environ.get("NETKEEPER_BROWSER_TESTS") != "1",
    reason="browser smoke suite: start Chrome, then set NETKEEPER_BROWSER_TESTS=1",
)

CDP_URL = os.environ.get("NETKEEPER_CDP_URL", "http://127.0.0.1:9222")
PRIYA, MATEO = PEOPLE[0], PEOPLE[1]
LOCATION = "Faketown, State of Example"
ROLES = (Role("Staff Engineer", "Fictional Robotics Co", "Full-time", "Aug 2021 - Present"),)

#: Large, fast wheel deltas: real scroll events without a real dwell per scroll.
_FAST = PacingProfile(
    delay=DelayProfile(median=0.2, sigma=0.1, tail_p=0.0, tail_range=(0.0, 0.0)),
    scroll=ScrollProfile(
        steps_range=(2, 3),
        delta_range_px=(900, 1200),
        pause_range_s=(0.02, 0.05),
        back_up_p=0.0,
        dwell_median_s=0.2,
        dwell_sigma=0.05,
    ),
)

#: The header the replica page sets on every request it sends, and checks came back.
MARKER = "x-replica-marker"

#: The page's own script. It counts every click and key event the page sees (and
#: whether the browser marked it trusted), asks for its lazy card once the page is
#: scrolled, and asks for the overlay when a Contact info link is clicked -- the way
#: the real page does -- recording each request it sends in ``window.__sent``.
_SCRIPT = """
<script>
  const CONFIG = __CONFIG__;
  window.__sent = [];
  window.__clicks = [];
  window.__keys = 0;
  document.addEventListener('click', (e) => {
    window.__clicks.push({trusted: e.isTrusted, text: (e.target.textContent || '').trim()});
  }, true);
  document.addEventListener('keydown', () => { window.__keys += 1; }, true);
  function send(path, body) {
    const marker = 'req-' + (window.__sent.length + 1);
    window.__sent.push({path: path, body: body, marker: marker});
    return fetch(path, {
      method: 'POST',
      headers: {'content-type': 'application/json', 'x-replica-marker': marker},
      body: body,
    }).then((answer) => answer.arrayBuffer());
  }
  if (CONFIG.screen) { send(CONFIG.screen, '{}').catch(() => {}); }
  let asked = false;
  const content = document.getElementById('content');
  content.addEventListener('scroll', () => {
    if (asked || content.scrollTop < 200) { return; }
    asked = true;
    send(CONFIG.component, JSON.stringify({vanityName: CONFIG.slug}));
  });
  for (const link of document.querySelectorAll('a.contact-info')) {
    link.addEventListener('click', (event) => {
      event.preventDefault();
      send(CONFIG.navigation, JSON.stringify({
        clientArguments: {
          requestedStateKeys: [],
          payload: {vanityName: CONFIG.slug, givenName: CONFIG.first,
                    familyName: CONFIG.last, isVanityNameResolved: true},
          states: [], screenId: CONFIG.screenId, knownTemplateIds: []
        },
        isModal: true
      }));
    });
  }
</script>
"""


class _Replica(BaseHTTPRequestHandler):
    people: ClassVar[dict[str, Person]] = {}
    controls: ClassVar[int] = 1
    received: ClassVar[list[dict[str, Any]]] = []
    #: #197: answers that break off mid-body, once each: ``"screen:<slug>"`` for the
    #: profile's screen request, ``"overlay:<slug>"`` for its Contact info answer.
    drop: ClassVar[set[str]] = set()
    #: Serve an HTML shell and let the page fetch the profile screen itself, the way an
    #: in-app navigation does, instead of carrying the screen in the document.
    screen_by_request: ClassVar[bool] = False

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        slug = path.removeprefix("/in/").strip("/") if path.startswith("/in/") else ""
        person = self.people.get(slug)
        if person is None:
            self._send(404, b"<html>gone</html>", "text/html")
            return
        screen = profile_payload(person, location=LOCATION, experience_inline=False)
        config = json.dumps(
            {
                "slug": person.slug,
                "first": person.first,
                "last": person.last,
                "screenId": CONTACT_DETAILS_SCREEN_ID,
                "navigation": f"{NAVIGATION_PATH}?screenId={CONTACT_DETAILS_SCREEN_ID}",
                "component": f"{COMPONENT_PATH}?componentId=fake.profileCardsExperienceOnly",
                "screen": f"/flagship-web/in/{person.slug}/" if self.screen_by_request else None,
            }
        )
        links = "".join(
            f'<p><a class="contact-info" href="/in/{person.slug}/overlay/contact-info/">'
            "Contact info</a></p>"
            for _ in range(self.controls)
        )
        # flagship-web's layout (#192): a fixed header at the top left, and the content
        # scrolling inside its own container, not the window.
        document = (
            "<!doctype html><html><body></body></html>"
            if self.screen_by_request
            else document_html(screen)
        )
        page = document.replace(
            "</body>",
            "<style>body{margin:0}"
            "#hdr{position:fixed;top:0;left:0;right:0;height:52px;background:#eee;z-index:2}"
            "#content{position:fixed;top:52px;bottom:0;left:0;right:0;overflow:auto}</style>"
            '<header id="hdr">replica header</header><main id="content">'
            f"<h1>{person.first} {person.last}</h1>{links}"
            '<div style="height:4000px">a page tall enough to scroll</div></main>'
            + _SCRIPT.replace("__CONFIG__", config)
            + "</body>",
        )
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _dropping(self, key: str) -> bool:
        if key not in self.drop:
            return False
        self.drop.discard(key)
        return True

    def _break_off(self, body: bytes, content_type: str) -> None:
        """Every header, most of the body, then the connection dropped (#197)."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body[: len(body) * 9 // 10])
        self.wfile.flush()
        self.close_connection = True
        self.connection.shutdown(socket.SHUT_RDWR)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        type(self).received.append({"path": path, "body": body, "marker": self.headers.get(MARKER)})
        if path.startswith("/flagship-web/in/"):
            slug = path.removeprefix("/flagship-web/in/").strip("/")
            screen = profile_payload(self.people[slug], location=LOCATION, experience_inline=False)
            if self._dropping(f"screen:{slug}"):
                self._break_off(screen, "application/octet-stream")
                return
            self._send(200, screen, "application/octet-stream")
        elif path == COMPONENT_PATH:
            self._send(200, experience_payload(ROLES), "application/octet-stream")
        elif path == NAVIGATION_PATH:
            slug = json.loads(body)["clientArguments"]["payload"]["vanityName"]
            person = self.people[slug]
            answer = contact_info_payload(
                person,
                emails=[f"{person.slug}@example.test"],
                websites=[Website(f"https://{person.slug}.example.test")],
            )
            if self._dropping(f"overlay:{slug}"):
                self._break_off(answer, "application/octet-stream")
                return
            self._send(200, answer, "application/octet-stream")
        else:
            self._send(404, b"", "text/plain")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the replica's own access log out of the test output."""


@pytest.fixture
def site() -> Iterator[str]:
    _Replica.people = {p.slug: p for p in (PRIYA, MATEO)}
    _Replica.controls = 1
    _Replica.received = []
    _Replica.drop = set()
    _Replica.screen_by_request = False
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
    async def before_visit(self, number: int) -> StopReason | None:
        return None

    async def pause(self, seconds: float) -> bool:
        return True


async def _fast(seconds: float) -> None:
    await asyncio.sleep(seconds / 5)


async def _visit(
    provider: AttachBrowserProvider,
    site: str,
    targets: list[EnrichTarget],
    *,
    landing_wait_s: float = 20.0,
) -> tuple[EnrichResult, list[ProfileHarvest], list[dict[str, Any]], dict[str, Any]]:
    harvests: list[ProfileHarvest] = []

    async def keep(harvest: ProfileHarvest) -> None:
        harvests.append(harvest)

    try:
        async with provider.run() as run:
            source = PageProfiles(
                run, origin=site, sleep=_fast, overlay_wait_s=5.0, landing_wait_s=landing_wait_s
            )
            result = await run_enrichment(
                EnrichJobSpec(targets=tuple(targets), visit_budget=len(targets), pacing=_FAST),
                source,
                _Gate(),
                on_harvest=keep,
                rng=random.Random(11),
                clock=lambda: datetime.now(UTC),
            )
            page_log = await _page_log(run)
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")
    return result, harvests, page_log["sent"], page_log


async def _page_log(run: BrowserRun) -> dict[str, Any]:
    # The test reads the replica page's own record of what it sent and saw. This is
    # test code on a loopback page, never something netkeeper does.
    page = await run.ensure_page()
    log: dict[str, Any] = await page.evaluate(
        "({sent: window.__sent, clicks: window.__clicks, keys: window.__keys})"
    )
    return log


def _target(person: Person, urn: str | None = None) -> EnrichTarget:
    return EnrichTarget(person.n, person.slug, urn or person.urn)


async def test_one_click_brings_the_overlay_and_the_visit_reads_whole(
    provider: AttachBrowserProvider, site: str
) -> None:
    result, harvests, sent, log = await _visit(provider, site, [_target(PRIYA)])

    assert result.reason is StopReason.END_OF_PLAN and result.clicks == 1
    (harvest,) = harvests
    assert harvest.outcome is Outcome.OK and harvest.details is not None
    assert harvest.details.urn == PRIYA.urn and harvest.details.location == LOCATION
    # The lazy card comes only if the wheel really scrolled the content container, not
    # the fixed header over the pointer's default spot (#192): it must have.
    assert [p.title for p in harvest.details.positions] == ["Staff Engineer"]
    assert harvest.contact_info is not None
    assert harvest.contact_info.emails == (f"{PRIYA.slug}@example.test",)

    # Exactly one click reached the page, on Contact info, trusted; no key at all.
    assert log["clicks"] == [{"trusted": True, "text": "Contact info"}]
    assert log["keys"] == 0
    # Nothing altered or added: what the replica received is what the page sent, the
    # lazy card on the scroll and the overlay on the click, and nothing of netkeeper's.
    received = [(r["path"], r["body"], r["marker"]) for r in _Replica.received]
    assert received == [(urlsplit(s["path"]).path, s["body"], s["marker"]) for s in sent]
    assert [path for path, _, _ in received] == [COMPONENT_PATH, NAVIGATION_PATH]


async def test_two_visits_click_once_each(provider: AttachBrowserProvider, site: str) -> None:
    result, harvests, _, _ = await _visit(provider, site, [_target(PRIYA), _target(MATEO)])
    assert [h.outcome for h in harvests] == [Outcome.OK, Outcome.OK]
    assert result.clicks == 2
    navigations = [r for r in _Replica.received if r["path"] == NAVIGATION_PATH]
    slugs = [json.loads(r["body"])["clientArguments"]["payload"]["vanityName"] for r in navigations]
    assert slugs == [PRIYA.slug, MATEO.slug]


@pytest.mark.parametrize("controls", [0, 2])
async def test_a_page_without_exactly_one_control_gets_no_click(
    provider: AttachBrowserProvider, site: str, controls: int
) -> None:
    _Replica.controls = controls
    _, harvests, _, log = await _visit(provider, site, [_target(PRIYA)])
    assert [h.outcome for h in harvests] == [Outcome.ROUTE_CHANGED]
    assert log["clicks"] == [] and log["keys"] == 0
    assert all(r["path"] != NAVIGATION_PATH for r in _Replica.received)


async def test_a_profile_under_another_urn_gets_no_click(
    provider: AttachBrowserProvider, site: str
) -> None:
    _, harvests, _, log = await _visit(
        provider, site, [_target(PRIYA, urn="urn:li:fsd_profile:ACoAAFAKE9999999")]
    )
    (harvest,) = harvests
    assert harvest.outcome is Outcome.OK and harvest.contact_info is None
    assert log["clicks"] == []
    assert all(r["path"] != NAVIGATION_PATH for r in _Replica.received)


# --- #197: an answer that breaks off mid-body ------------------------------------------------


async def test_a_profile_whose_answer_breaks_off_is_one_unreadable_visit(
    provider: AttachBrowserProvider, site: str
) -> None:
    """Priya's profile screen, which the page fetches for itself, breaks off mid-body.
    Her visit is unreadable, with a fixed cause; nothing is clicked for her; Mateo's
    visit reads whole."""
    _Replica.screen_by_request = True
    _Replica.drop = {f"screen:{PRIYA.slug}"}
    result, harvests, _, log = await _visit(
        provider, site, [_target(PRIYA), _target(MATEO)], landing_wait_s=3.0
    )
    assert [h.outcome for h in harvests] == [Outcome.ROUTE_CHANGED, Outcome.OK]
    assert result.reason is StopReason.END_OF_PLAN and result.unreadable == 1
    (lost,) = result.lost
    assert lost.startswith("visit 1: the profile screen could not be read (")
    assert "127.0.0.1" not in lost
    assert log["clicks"] == [{"trusted": True, "text": "Contact info"}]  # Mateo's only
    assert harvests[1].contact_info is not None


async def test_a_contact_info_answer_that_breaks_off_is_not_clicked_for_again(
    provider: AttachBrowserProvider, site: str
) -> None:
    """The overlay's answer breaks off after the one click: no contact info for
    Priya this visit, no second click, and the run goes on to Mateo."""
    _Replica.drop = {f"overlay:{PRIYA.slug}"}
    result, harvests, _, _ = await _visit(provider, site, [_target(PRIYA), _target(MATEO)])
    assert [h.outcome for h in harvests] == [Outcome.ROUTE_CHANGED, Outcome.OK]
    assert harvests[0].contact_info is None and harvests[1].contact_info is not None
    assert result.reason is StopReason.END_OF_PLAN and result.clicks == 2
    (lost,) = result.lost
    assert lost.startswith("visit 1: the Contact info answer could not be read (")
    navigations = [r for r in _Replica.received if r["path"] == NAVIGATION_PATH]
    slugs = [json.loads(r["body"])["clientArguments"]["payload"]["vanityName"] for r in navigations]
    assert slugs == [PRIYA.slug, MATEO.slug]  # one overlay request each: never asked again
