"""``PageVoyagerFetch`` against a real Chrome. Opt in with ``NETKEEPER_BROWSER_TESTS=1``.

Skipped by default, because it needs a browser the developer started and the rest of
the suite must stay offline. The only site involved is a loopback fixture server this
file starts and serves for itself: it answers a Voyager-shaped path with a sanitized
fixture body and records the headers each request actually carried, and it sets a
``JSESSIONID`` cookie the ordinary way a site does -- a ``Set-Cookie`` response header
on the first page load -- rather than anything netkeeper writes itself (ADR 0002 forbids
``context.add_cookies``, and this smoke suite proves the fetch works without it).

This runs against the developer's own, real Chrome profile (spec 9.1), so the fixture
cookie it sets is bounded (``Max-Age=60``) and every test tears itself down by hitting
a route that expires it immediately -- the smoke suite must not leave anything behind
in a browser the developer keeps using for their own LinkedIn session (#168 review, F7).

What it proves that ``tests/test_linkedin_fetch.py`` cannot, because that one drives a
fake tab that never actually runs the generated script: that ``page.evaluate`` really
runs a ``fetch()`` inside a real Chrome tab, that ``document.cookie`` really is where
the csrf-token value comes from now (F2 of the #168 review -- it no longer reaches
Python at all), that the request the fixture server actually received carried the
headers ``build_headers`` produces, that a real redirect is followed and its *actual*
final url is what gets classified (not the url the request started at -- #168 review,
F6/N2), and that the response really round-trips into a parser end to end.

Start Chrome first with the command ``netkeeper browser launch`` prints.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from netkeeper.linkedin.browser import AttachBrowserProvider, BrowserUnavailable
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.fetch import PageVoyagerFetch, VoyagerNotOk, parse_ok
from netkeeper.linkedin.voyager import CONNECTIONS_PATH, VoyagerRequest, parse_connections_page

log = logging.getLogger(__name__)

pytestmark = pytest.mark.skipif(
    os.environ.get("NETKEEPER_BROWSER_TESTS") != "1",
    reason="browser smoke suite: start Chrome, then set NETKEEPER_BROWSER_TESTS=1",
)

CDP_URL = os.environ.get("NETKEEPER_CDP_URL", "http://127.0.0.1:9222")

# Invented and sanitized: a made-up URN, a made-up public id, no real name.
CONNECTIONS_FIXTURE = {
    "data": {
        "elements": [{"*connectedMemberResolutionResult": "urn:li:fsd_profile:SMOKEURN0001"}],
        "paging": {"start": 0, "count": 1, "total": 1},
    },
    "included": [
        {
            "entityUrn": "urn:li:fsd_profile:SMOKEURN0001",
            "publicIdentifier": "smoke-fixture-person",
            "firstName": "Smoke",
            "lastName": "Fixture",
            "headline": "A fixture, not a person",
        }
    ],
}

# Invented; this is what the fixture server's own Set-Cookie puts in the tab's jar,
# never a real LinkedIn session value.
FIXTURE_JSESSIONID = '"ajax:smoke-fixture-csrf-token"'

# A Voyager-shaped path (so PageVoyagerFetch's own prefix check accepts it) that the
# fixture server answers with a redirect instead of JSON, landing on a path shaped
# like LinkedIn's own checkpoint interstitial (spec 9.7).
CHECKPOINT_TRIGGER_PATH = "/voyager/api/smoke/checkpoint-trigger"
CHECKPOINT_PATH = "/checkpoint/challenge"


class _FixtureServer(BaseHTTPRequestHandler):
    """A loopback site: a page that sets the csrf cookie, Voyager's path, and a redirect."""

    protocol_version = "HTTP/1.1"
    seen_headers: dict[str, str] = {}

    def do_GET(self) -> None:
        if self.path.startswith(CONNECTIONS_PATH):
            type(self).seen_headers = {name.lower(): value for name, value in self.headers.items()}
            body = json.dumps(CONNECTIONS_FIXTURE).encode()
            self._send(body, "application/json", set_cookie=False)
        elif self.path.startswith(CHECKPOINT_TRIGGER_PATH):
            self._redirect(CHECKPOINT_PATH)
        elif self.path.startswith(CHECKPOINT_PATH):
            body = b"<!doctype html><title>smoke checkpoint interstitial</title><body></body>"
            self._send(body, "text/html; charset=utf-8", set_cookie=False)
        elif self.path.startswith("/expire-cookie"):
            self._send(b"", "text/plain", set_cookie=False, expire_cookie=True)
        elif self.path == "/" or self.path.startswith("/?"):
            # #173 review, F6: the cookie is set on exactly this route. A real
            # Chrome tab requests /favicon.ico on its own for any top-level
            # navigation with no <link rel="icon">, and that request used to hit
            # the catch-all below, which re-set the cookie moments after the
            # teardown navigation to /expire-cookie had just cleared it --
            # #170 item 4 was never actually fixed while every path re-armed it.
            body = b"<!doctype html><title>netkeeper fetch smoke fixture</title><body></body>"
            self._send(body, "text/html; charset=utf-8", set_cookie=True)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(
        self,
        body: bytes,
        content_type: str,
        *,
        set_cookie: bool,
        expire_cookie: bool = False,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            # Bounded lifetime (F7): this runs against the developer's real Chrome
            # profile, and a session cookie with no Max-Age would otherwise outlive
            # this one test run in a Chrome the developer keeps open.
            self.send_header("Set-Cookie", f"JSESSIONID={FIXTURE_JSESSIONID}; Path=/; Max-Age=60")
        if expire_cookie:
            self.send_header("Set-Cookie", "JSESSIONID=; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the fixture server's own access log out of the test output."""


@pytest.fixture
def provider() -> AttachBrowserProvider:
    return AttachBrowserProvider(CDP_URL)


@pytest.fixture
async def site(provider: AttachBrowserProvider) -> AsyncIterator[str]:
    """A fresh loopback fixture server, its own port, for the length of the test.

    Teardown **navigates the tab** to ``/expire-cookie`` rather than hitting it with
    ``httpx`` (#170 item 4): an HTTP client making its own request never touches
    Chrome's cookie jar at all -- the ``Set-Cookie: JSESSIONID=; Max-Age=0`` that
    route answers with only expires the cookie in whatever store actually received
    the response, and an ``httpx.get`` call's store is httpx's own, not the
    developer's real Chrome profile this suite runs against (spec 9.1). Only a real
    navigation, through the same ``BrowserRun`` the tests themselves use, puts that
    response in front of the browser that is actually holding the cookie. The
    `Max-Age=60` this fixture set in the first place is the backstop if this
    teardown itself cannot run (Chrome closed between the test and its teardown).

    After navigating, reads the jar back and asserts ``JSESSIONID`` is actually
    gone (#173 review's jar check) -- names only, the same discipline
    ``preflight.py`` holds to: this reaches the developer's real Chrome profile,
    so the assertion below never prints a cookie's value, only whether a name
    it must not still hold is present.
    """
    _FixtureServer.seen_headers = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base
    finally:
        try:
            async with provider.run() as run:
                await run.goto(f"{base}/expire-cookie")
                jar = await run.context.cookies()
                survived = [
                    str(cookie.get("name"))
                    for cookie in jar
                    if str(cookie.get("domain", "")).lstrip(".") == "127.0.0.1"
                    and cookie.get("name") == "JSESSIONID"
                ]
                assert not survived, (
                    f"{len(survived)} JSESSIONID cookie(s) survived teardown on 127.0.0.1"
                )
        except BrowserUnavailable as exc:
            log.warning("could not navigate to expire the fetch smoke suite's cookie: %s", exc)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_the_fetch_carries_the_built_headers_and_the_body_round_trips(
    provider: AttachBrowserProvider, site: str
) -> None:
    """Load the fixture site once (so the tab holds its csrf cookie), then fetch."""
    try:
        async with provider.run() as run:
            await run.goto(f"{site}/")
            fetch = PageVoyagerFetch(run, origin=site)
            response = await fetch(VoyagerRequest(path=CONNECTIONS_PATH))
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    assert response.status == 200

    page = parse_ok(response, parse_connections_page)
    assert page.connections[0].public_id == "smoke-fixture-person"
    assert page.connections[0].first_name == "Smoke"

    sent: Mapping[str, str] = _FixtureServer.seen_headers
    assert sent.get("csrf-token") == "ajax:smoke-fixture-csrf-token", sent
    assert sent.get("accept") == "application/vnd.linkedin.normalized+json+2.1", sent
    assert sent.get("x-restli-protocol-version") == "2.0.0", sent
    assert sent.get("x-li-lang") == "en_US", sent
    # The whole point of #150's cookie-value discipline: a fetch failure must never
    # put the value the fixture server issued into anything the suite prints, and
    # this assertion is the only place in this file that is allowed to name it.
    assert FIXTURE_JSESSIONID not in repr(response)


async def test_a_redirect_to_a_checkpoint_is_classified_from_the_real_final_url(
    provider: AttachBrowserProvider, site: str
) -> None:
    """N2: the classified url must be where the browser actually landed (``r.url``),
    not the url the request started at -- a script that returned the request url
    instead would classify this as an unrecognized 200, not a checkpoint, and spec
    9.7's "never retry a checkpoint" rule would never fire.
    """
    try:
        async with provider.run() as run:
            await run.goto(f"{site}/")
            fetch = PageVoyagerFetch(run, origin=site)
            response = await fetch(VoyagerRequest(path=CHECKPOINT_TRIGGER_PATH))
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    assert response.final_url.endswith(CHECKPOINT_PATH), response.final_url
    assert response.final_url != f"{site}{CHECKPOINT_TRIGGER_PATH}"

    with pytest.raises(VoyagerNotOk) as excinfo:
        parse_ok(response, parse_connections_page)
    assert excinfo.value.outcome is Outcome.CHECKPOINT
