"""``PageVoyagerFetch`` against a real Chrome. Opt in with ``NETKEEPER_BROWSER_TESTS=1``.

Skipped by default, because it needs a browser the developer started and the rest of
the suite must stay offline. The only site involved is a loopback fixture server this
file starts and serves for itself: it answers a Voyager-shaped path with a sanitized
fixture body and records the headers each request actually carried, and it sets a
``JSESSIONID`` cookie the ordinary way a site does -- a ``Set-Cookie`` response header
on the first page load -- rather than anything netkeeper writes itself (ADR 0002 forbids
``context.add_cookies``, and this smoke suite proves the fetch works without it).

What it proves that ``tests/test_linkedin_fetch.py`` cannot, because that one drives a
fake tab: that ``page.evaluate`` really runs a ``fetch()`` inside a real Chrome tab,
that the request the fixture server actually received carried the headers
``build_headers`` produces, and that the response really round-trips into a parser
end to end.

Start Chrome first with the command ``netkeeper browser launch`` prints.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from netkeeper.linkedin.browser import AttachBrowserProvider, BrowserUnavailable
from netkeeper.linkedin.fetch import PageVoyagerFetch, parse_ok
from netkeeper.linkedin.voyager import CONNECTIONS_PATH, VoyagerRequest, parse_connections_page

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


class _FixtureServer(BaseHTTPRequestHandler):
    """A two-route loopback site: a page that sets the csrf cookie, and Voyager's path."""

    protocol_version = "HTTP/1.1"
    seen_headers: dict[str, str] = {}

    def do_GET(self) -> None:
        if self.path.startswith(CONNECTIONS_PATH):
            type(self).seen_headers = {name.lower(): value for name, value in self.headers.items()}
            body = json.dumps(CONNECTIONS_FIXTURE).encode()
            self._send(body, "application/json", set_cookie=False)
            return
        body = b"<!doctype html><title>netkeeper fetch smoke fixture</title><body></body>"
        self._send(body, "text/html; charset=utf-8", set_cookie=True)

    def _send(self, body: bytes, content_type: str, *, set_cookie: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header("Set-Cookie", f"JSESSIONID={FIXTURE_JSESSIONID}; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the fixture server's own access log out of the test output."""


@pytest.fixture
def site() -> Iterator[str]:
    """A fresh loopback fixture server, its own port, for the length of the test."""
    _FixtureServer.seen_headers = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureServer)
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
