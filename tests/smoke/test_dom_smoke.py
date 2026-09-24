"""``DomConnectionsSource``/``DomContactInfoSource`` against a real Chrome (P2-08).

Opt in with ``NETKEEPER_BROWSER_TESTS=1``, same as every other file under
``tests/smoke/``: skipped by default because it needs a browser the developer
started, and the rest of the suite must stay offline. The only site involved
is a loopback replica this file serves for itself.

What this proves that ``tests/test_linkedin_dom.py`` cannot, because that
file's fakes never run real JavaScript: that scrolling a real page with
``BrowserRun.scroll``'s ``mouse.wheel`` replay actually triggers a real
``scroll`` event and a real lazy-loading script responds to it the way
LinkedIn's own infinite-scroll connections list would (this file's replica
appends more cards only once the page has genuinely scrolled, driven by
nothing but the wheel events a real run sends); that ``CARD_SELECTOR`` and
friends (``netkeeper.linkedin.dom``) actually select something in a real
DOM tree built by a browser's own parser, not a Python dict standing in for
one; and that ``page.evaluate``'s ``mailto:``/``tel:`` reading genuinely
resolves relative and absolute hrefs the way a browser does.

The replica sets no cookies, so unlike ``tests/smoke/test_fetch_smoke.py``
there is nothing for a test here to leave behind in the developer's real
Chrome profile and no teardown is needed for that reason.

Start Chrome first with the command ``netkeeper browser launch`` prints.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from netkeeper.linkedin.browser import AttachBrowserProvider, BrowserUnavailable
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.connections import (
    ConnectionsPage,
    SyncJobSpec,
    SyncMode,
    run_connections_sync,
)
from netkeeper.linkedin.dom import (
    CONNECTIONS_LIST_PATH,
    CONTACT_INFO_OVERLAY_PATH_TEMPLATE,
    DomConnectionsSource,
    DomContactInfoSource,
)
from netkeeper.linkedin.pacing import ScrollProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("NETKEEPER_BROWSER_TESTS") != "1",
    reason="browser smoke suite: start Chrome, then set NETKEEPER_BROWSER_TESTS=1",
)

CDP_URL = os.environ.get("NETKEEPER_CDP_URL", "http://127.0.0.1:9222")

#: A handful of invented cards do not need spec 9.5's real dwell (~3s median) per
#: settle attempt to prove the scroll mechanism works -- large, fast wheel deltas
#: still fire real ``scroll`` events (which is the whole point: the replica's own
#: script gates loading more cards on ``window.scrollY``), just without spending
#: real minutes on a smoke run. Production uses ``DEFAULT_SCROLL_PROFILE`` (spec
#: 9.5's actual pacing); this is this file's own choice, not a change to that.
_FAST_SCROLL = ScrollProfile(
    steps_range=(2, 3),
    delta_range_px=(2500, 3500),
    pause_range_s=(0.02, 0.05),
    back_up_p=0.0,
    dwell_median_s=0.1,
    dwell_sigma=0.05,
)


# Invented and sanitized throughout: fake names, fake companies, example.test
# addresses -- the same rule voyager.py's fixtures follow (CLAUDE.md).
def _person(public_id: str, name: str, headline: str | None) -> dict[str, str | None]:
    return {"publicId": public_id, "name": name, "headline": headline}


_PEOPLE = [
    _person("jamie-fake-rivera-1a2b", "Jamie Rivera", "Product designer at Fictional Robotics Co"),
    _person("alex-fake-chen-3c4d", "Alex Chen", "Staff engineer at Acme Testing Group"),
    _person("blair-fake-doe-5e6f", "Blair Doe", None),
    _person("casey-fake-poe-7g8h", "Casey Poe", "Recruiter at Placeholder Partners"),
    _person("devon-fake-moe-9i0j", "Devon Moe", "Founder, Imaginary Analytics"),
    _person("emery-fake-loe-1k2l", "Emery Loe", "SRE at Nonexistent Networks"),
    _person("frankie-fake-koe-3m4n", "Frankie Koe", "PM at Example Widgets Ltd"),
]

_CONNECTIONS_PAGE = (
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>netkeeper dom smoke replica: connections</title></head>
<body>
<main>
  <ul id="connections-list"></ul>
  <div style="height:6000px">tall enough to scroll like a person would</div>
</main>
<script>
  const PEOPLE = """
    + repr(_PEOPLE).replace("'", '"').replace("None", "null")
    + """;
  const BATCH = 2;
  let loaded = 0;
  const list = document.getElementById('connections-list');
  function loadMore() {
    if (loaded >= PEOPLE.length) { return; }
    for (const p of PEOPLE.slice(loaded, loaded + BATCH)) {
      const li = document.createElement('li');
      li.setAttribute('data-view-name', 'connections-list-item');
      const a = document.createElement('a');
      a.href = '/in/' + p.publicId + '/';
      a.setAttribute('aria-label', p.name);
      const nameEl = document.createElement('span');
      nameEl.setAttribute('data-view-name', 'connections-list-item-name');
      nameEl.textContent = p.name;
      a.appendChild(nameEl);
      li.appendChild(a);
      if (p.headline) {
        const h = document.createElement('p');
        h.setAttribute('data-view-name', 'connections-list-item-headline');
        h.textContent = p.headline;
        li.appendChild(h);
      }
      list.appendChild(li);
    }
    loaded += BATCH;
  }
  loadMore();  // the first batch renders on load, the way a real page would
  window.addEventListener('scroll', () => {
    const scrolledFraction = (window.scrollY + window.innerHeight) / document.body.scrollHeight;
    if (scrolledFraction > 0.3) { loadMore(); }
  });
</script>
</body></html>
"""
)

_LOGIN_WALL_PAGE = b"""<!doctype html>
<html><head><title>smoke: sign in</title></head><body><h1>Sign in</h1></body></html>
"""

_CONTACT_INFO_OVERLAY = b"""<!doctype html>
<html><head><title>netkeeper dom smoke replica: contact info</title></head>
<body>
<main>
  <a href="mailto:jamie.fake@example.test">Email</a>
  <a href="tel:+15550100000">Phone</a>
  <a href="https://jamie-fake.example.test/">Website</a>
  <a href="https://x.com/jamiefake">X profile</a>
  <a href="https://www.linkedin.com/in/jamie-fake-rivera-1a2b/">Back to profile</a>
</main>
</body></html>
"""


class _Replica(BaseHTTPRequestHandler):
    """The connections list, one profile's contact-info overlay, and a login wall."""

    protocol_version = "HTTP/1.1"
    force_login_wall = False

    def do_GET(self) -> None:
        if type(self).force_login_wall and self.path.startswith(CONNECTIONS_LIST_PATH):
            self._redirect("/uas/login?session_redirect=x")
        elif self.path.startswith("/uas/login"):
            self._send(_LOGIN_WALL_PAGE, "text/html; charset=utf-8")
        elif self.path.startswith(CONNECTIONS_LIST_PATH):
            self._send(_CONNECTIONS_PAGE.encode(), "text/html; charset=utf-8")
        elif self.path.startswith("/in/") and "/overlay/contact-info/" in self.path:
            self._send(_CONTACT_INFO_OVERLAY, "text/html; charset=utf-8")
        else:
            self._send(b"<!doctype html><title>netkeeper dom smoke replica</title>", "text/html")

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the replica's own access log out of the test output."""


@pytest.fixture
def site() -> Iterator[str]:
    _Replica.force_login_wall = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Replica)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        _Replica.force_login_wall = False
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def provider() -> AttachBrowserProvider:
    return AttachBrowserProvider(CDP_URL)


async def test_scrolling_a_real_page_loads_more_cards_and_the_run_completes_them_all(
    provider: AttachBrowserProvider, site: str
) -> None:
    """Cards appear only as the page genuinely scrolls (the replica's own script
    gates them on window.scrollY), so a full sync reaching every one of them proves
    BrowserRun.scroll's real mouse.wheel replay is what drove it."""
    pages: list[ConnectionsPage] = []

    class Gate:
        async def before_page(self, number: int) -> bool:
            return True

        async def between_pages(self) -> None:
            return None

    async def on_page(page: ConnectionsPage) -> None:
        pages.append(page)

    try:
        async with provider.run() as run:
            source = DomConnectionsSource(run, origin=site, scroll_profile=_FAST_SCROLL)
            result = await run_connections_sync(
                # page_size matching len(_PEOPLE) exactly: the whole list loads in
                # one settled page, so only the trailing confirming-empty page (spec
                # 9.3's short/empty-page logic) needs a second round of settling.
                SyncJobSpec(mode=SyncMode.FULL, page_budget=20, page_size=len(_PEOPLE)),
                source,
                Gate(),
                on_page=on_page,
            )
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    seen = {c.public_id for page in pages for c in page.connections}
    assert seen == {p["publicId"] for p in _PEOPLE}
    assert all(c.urn is None for page in pages for c in page.connections)
    assert result.max_total == 0
    assert not result.complete  # spec 9.3/P2-08: DOM alone never proves a total

    all_connections = [c for page in pages for c in page.connections]
    jamie = next(c for c in all_connections if c.public_id == "jamie-fake-rivera-1a2b")
    assert (jamie.first_name, jamie.last_name) == ("Jamie", "Rivera")
    assert jamie.headline == "Product designer at Fictional Robotics Co"


async def test_a_real_login_wall_redirect_is_classified_not_read_as_an_empty_list(
    provider: AttachBrowserProvider, site: str
) -> None:
    _Replica.force_login_wall = True
    try:
        async with provider.run() as run:
            source = DomConnectionsSource(run, origin=site)
            answer = await source.fetch_page(start=0, count=3)
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    assert answer.outcome is Outcome.LOGGED_OUT
    assert answer.page is None


async def test_the_contact_info_overlay_is_read_from_mailto_and_tel_links(
    provider: AttachBrowserProvider, site: str
) -> None:
    try:
        async with provider.run() as run:
            source = DomContactInfoSource(run, origin=site)
            result = await source.fetch_contact_info("jamie-fake-rivera-1a2b")
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    assert result.outcome is Outcome.OK
    info = result.info
    assert info is not None
    assert info.email == "jamie.fake@example.test"
    assert info.phones == ("+15550100000",)
    assert info.websites == ("https://jamie-fake.example.test/",)
    assert info.twitter_handles == ("jamiefake",)


async def test_the_overlay_url_visited_matches_the_public_id(
    provider: AttachBrowserProvider, site: str
) -> None:
    async with provider.run() as run:
        source = DomContactInfoSource(run, origin=site)
        await source.fetch_contact_info("jamie-fake-rivera-1a2b")
        page = await run.ensure_page()
    expected_path = CONTACT_INFO_OVERLAY_PATH_TEMPLATE.format(public_id="jamie-fake-rivera-1a2b")
    assert page.url == f"{site}{expected_path}"
