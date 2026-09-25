"""``DomContactInfoSource`` against a real Chrome (P2-08; the connections half went with #187).

Opt in with ``NETKEEPER_BROWSER_TESTS=1``, same as every other file under
``tests/smoke/``: skipped by default because it needs a browser the developer
started, and the rest of the suite must stay offline. The only site involved
is a loopback replica this file serves for itself.

What this proves that ``tests/test_linkedin_dom.py`` cannot, because that
file's fakes never run real JavaScript: that the overlay's dialog selection
works in a real DOM tree built by a browser's own parser, and that
``page.evaluate``'s ``mailto:``/``tel:`` reading genuinely resolves relative and
absolute hrefs the way a browser does.

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
from netkeeper.linkedin.dom import (
    CONTACT_INFO_OVERLAY_PATH_TEMPLATE,
    DomContactInfoSource,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("NETKEEPER_BROWSER_TESTS") != "1",
    reason="browser smoke suite: start Chrome, then set NETKEEPER_BROWSER_TESTS=1",
)

CDP_URL = os.environ.get("NETKEEPER_CDP_URL", "http://127.0.0.1:9222")


#: F7 of the #173 review, extended by #174 items 1-3: the actual contact info
#: lives inside role="dialog", and the surrounding profile page carries its
#: own, unrelated links -- a bio's mailto:, a LinkedIn short link, and an
#: x.com status permalink (not a handle) -- that must never be read as if the
#: overlay had shared them. Modeled on the review's own reproduction script.
#:
#: #174 item 1: a messaging overlay (also role="dialog") renders *before* the
#: real contact-info dialog in document order, with neither the "Contact
#: info" heading nor a link back to jamie-fake-rivera-1a2b's profile -- so it
#: must never be the one this module reads from, whatever DOM order it
#: happens to render in.
#:
#: #174 item 3: the lnkd.in short link and the x.com/.../status/... permalink
#: are duplicated *inside* the real dialog (the originals outside it stay, to
#: keep exercising dialog scoping on its own) -- if the shortener filter or
#: the reserved-path check were ever weakened, these inside copies are what
#: would leak into the parsed result, not just the outside ones scoping alone
#: already excludes. #174 item 2: every other reserved X/Twitter path segment
#: is exercised too, each in a different case than its canonical lowercase
#: spelling, proving the denylist check is case-insensitive and covers more
#: than just "i".
_CONTACT_INFO_OVERLAY = b"""<!doctype html>
<html><head><title>netkeeper dom smoke replica: contact info</title></head>
<body>
<main>
  <section>About: I write at
    <a href="https://blog.someone-else.example/post">a post</a>,
    reach my old team at
    <a href="mailto:team@former-employer.example">team@former-employer.example</a>,
    my link-in-bio is
    <a href="https://lnkd.in/abc123">here</a>, and I liked
    <a href="https://x.com/i/status/12345">this post</a>.
  </section>
</main>
<div role="dialog" aria-label="Messaging">
  <h2>Messaging</h2>
  <a href="mailto:should-not-be-read@example.test">Reply</a>
  <a href="https://unrelated.example.test/">Unrelated link</a>
</div>
<div role="dialog">
  <h2>Contact info</h2>
  <a href="mailto:jamie.fake@example.test">jamie.fake@example.test</a>
  <a href="tel:+15550100000">+1 555 010 0000</a>
  <a href="https://jamie-fake.example.test/">Website</a>
  <a href="https://x.com/jamiefake">X profile</a>
  <a href="https://lnkd.in/xyz789">Shortened link (inside the dialog too)</a>
  <a href="https://x.com/I/status/67890">Another liked post (inside the dialog too)</a>
  <a href="https://x.com/Intent/tweet?text=hi">Intent link</a>
  <a href="https://x.com/SHARE?url=x">Share link</a>
  <a href="https://x.com/Home">Home link</a>
  <a href="https://x.com/search?q=x">Search link</a>
  <a href="https://x.com/HashTag/example">Hashtag link</a>
  <a href="https://x.com/Explore">Explore link</a>
  <a href="https://x.com/Settings">Settings link</a>
  <a href="https://x.com/Messages">Messages link</a>
  <a href="https://x.com/Notifications">Notifications link</a>
  <a href="https://x.com/Login">Login link</a>
  <a href="https://x.com/SignUp">Signup link</a>
  <a href="https://x.com/Compose/tweet">Compose link</a>
  <a href="https://x.com/TOS">Tos link</a>
  <a href="https://x.com/Privacy">Privacy link</a>
  <a href="https://x.com/Account">Account link</a>
  <a href="https://x.com/About">About link</a>
  <a href="https://x.com/Bookmarks">Bookmarks link</a>
  <a href="https://x.com/Lists">Lists link</a>
  <a href="https://x.com/Logout">Logout link</a>
  <a href="https://x.com/Jobs">Jobs link</a>
  <a href="https://x.com/Communities">Communities link</a>
  <a href="https://x.com/Download">Download link</a>
  <a href="https://x.com/OAuth">OAuth link</a>
  <a href="https://x.com/En">Locale-prefix link</a>
  <a href="https://x.com/Premium">Premium link</a>
  <a href="https://x.com/Help">Help link</a>
  <a href="https://x.com/Terms">Terms link</a>
  <!-- #176 review L7: not on any denylist, but too long (16 chars) to be a
       real X handle (X's own limit is 15) -- the shape check must reject
       this even though the word check alone would not. -->
  <a href="https://x.com/waytoolongtoeverbeahandle">Shape-invalid link</a>
  <a href="https://www.linkedin.com/in/jamie-fake-rivera-1a2b/">Back to profile</a>
</div>
</body></html>
"""

#: F5(d): an overlay whose dialog never rendered -- an error page in its place.
_OVERLAY_ERROR_PAGE = b"""<!doctype html>
<html><head><title>smoke: error</title></head>
<body><main>Something went wrong</main></body></html>
"""

#: #174 item 1's "zero qualify" case, distinct from F5(d) above: a dialog
#: genuinely renders (``role="dialog"`` is present), but it is the messaging
#: one, not contact info -- no "Contact info" heading, no link back to the
#: public id visited. Must refuse the same way an absent dialog does, not
#: fall through to whatever this one dialog happens to contain.
_ONLY_MESSAGING_OVERLAY = b"""<!doctype html>
<html><head><title>netkeeper dom smoke replica: contact info</title></head>
<body>
<div role="dialog" aria-label="Messaging">
  <h2>Messaging</h2>
  <a href="mailto:should-not-be-read@example.test">Reply</a>
</div>
</body></html>
"""

#: #174 item 1's "more than one qualify" case: two dialogs both carry the
#: "Contact info" heading marker. Ambiguous, so this must refuse rather than
#: guess which one a caller meant -- the DOM read has no way to know.
_AMBIGUOUS_CONTACT_INFO_OVERLAY = b"""<!doctype html>
<html><head><title>netkeeper dom smoke replica: contact info</title></head>
<body>
<div role="dialog">
  <h2>Contact info</h2>
  <a href="mailto:one@example.test">one@example.test</a>
</div>
<div role="dialog">
  <h2>Contact info</h2>
  <a href="mailto:two@example.test">two@example.test</a>
</div>
</body></html>
"""

#: #176 review, H1's own reproduction (probe case A): a docked chat with the
#: *same* person is open, and nothing else -- no contact-info overlay at
#: all. The chat's own heading is a link to the person's profile, not the
#: text "Contact info", so before H1's fix the link-back alone was enough to
#: qualify this dialog, and the reader returned the chat's own email and
#: links. Also stands in for M2's "link only" case: a link-back with no
#: matching heading must refuse under the new AND rule.
_CHAT_ONLY_OVERLAY = b"""<!doctype html>
<html><head><title>netkeeper dom smoke replica: contact info</title></head>
<body>
<aside role="dialog" aria-label="Messaging">
  <h2><a href="/in/jamie-fake-rivera-1a2b/">Jamie Rivera</a></h2>
  <p>hey check <a href="https://sketchy.example/deal">this</a>, mail me
  <a href="mailto:someone-else@chat.example">here</a>,
  <a href="https://x.com/randomperson">this</a> is my X.</p>
</aside>
</body></html>
"""

#: #176 review M2's "heading only" case: a dialog with the exact "Contact
#: info" heading but no link back to any profile at all. Must refuse under
#: the new AND rule -- the heading alone used to be sufficient too.
_HEADING_ONLY_OVERLAY = b"""<!doctype html>
<html><head><title>netkeeper dom smoke replica: contact info</title></head>
<body>
<div role="dialog">
  <h2>Contact info</h2>
  <a href="mailto:heading-only@example.test">heading-only@example.test</a>
</div>
</body></html>
"""

#: #176 review L1's own reproduction (probe case F): a heading that starts
#: with "Contact info" but is not exactly that -- a real, differently-scoped
#: LinkedIn dialog ("Contact info shared with advertisers"), not the overlay
#: this module means to read. A prefix match let this through; an exact
#: match (after trim/whitespace-normalize) must not. Carries a *matching*
#: link-back too (unlike the "heading only" fixture above) so this isolates
#: L1's own exact-match requirement: a prefix-matching mutation must be
#: caught here even though H1's AND rule is otherwise satisfied.
_LOOKALIKE_HEADING_OVERLAY = b"""<!doctype html>
<html><head><title>netkeeper dom smoke replica: contact info</title></head>
<body>
<div role="dialog">
  <h3>Contact info shared with advertisers</h3>
  <a href="mailto:ads@example.test">ads@example.test</a>
  <a href="/in/jamie-fake-rivera-1a2b/">Back to profile</a>
</div>
</body></html>
"""

#: #176 review M2's own reproduction (probe case E): the heading matches
#: exactly, but the link-back's own host is unrelated to this page's origin
#: or linkedin.com -- ``https://medium.example/in/<id>`` must not qualify a
#: dialog just because its *path* happens to contain ``/in/<id>``.
_FOREIGN_HOST_LINK_OVERLAY = b"""<!doctype html>
<html><head><title>netkeeper dom smoke replica: contact info</title></head>
<body>
<div role="dialog">
  <h2>Contact info</h2>
  <a href="https://medium.example/in/foreignhost-fake/">Not linkedin</a>
  <a href="mailto:leak@example.test">leak@example.test</a>
</div>
</body></html>
"""

#: #176 review, probe case D: a percent-encoded, upper-cased public id in the
#: link-back's href -- proves the match decodes and case-folds both sides
#: (#174 item 1's own wantId is lower-cased on the Python side; M13 pins
#: that this actually matters).
_ENCODED_UPPER_CASE_OVERLAY = b"""<!doctype html>
<html><head><title>netkeeper dom smoke replica: contact info</title></head>
<body>
<div role="dialog">
  <h2>Contact info</h2>
  <a href="https://www.linkedin.com/in/J%C3%96RG-fake-x/">Back to profile</a>
  <a href="mailto:jorg@example.test">jorg@example.test</a>
</div>
</body></html>
"""


class _Replica(BaseHTTPRequestHandler):
    """The connections list, one profile's contact-info overlay, and a login wall."""

    protocol_version = "HTTP/1.1"
    force_overlay_error = False
    force_only_messaging_overlay = False
    force_ambiguous_overlay = False
    force_chat_only_overlay = False
    force_heading_only_overlay = False
    force_lookalike_heading_overlay = False
    force_foreign_host_link_overlay = False
    force_encoded_upper_case_overlay = False

    def do_GET(self) -> None:
        cls = type(self)
        if cls.force_overlay_error and "/overlay/contact-info/" in self.path:
            self._send(_OVERLAY_ERROR_PAGE, "text/html; charset=utf-8")
        elif cls.force_only_messaging_overlay and "/overlay/contact-info/" in self.path:
            self._send(_ONLY_MESSAGING_OVERLAY, "text/html; charset=utf-8")
        elif cls.force_ambiguous_overlay and "/overlay/contact-info/" in self.path:
            self._send(_AMBIGUOUS_CONTACT_INFO_OVERLAY, "text/html; charset=utf-8")
        elif cls.force_chat_only_overlay and "/overlay/contact-info/" in self.path:
            self._send(_CHAT_ONLY_OVERLAY, "text/html; charset=utf-8")
        elif cls.force_heading_only_overlay and "/overlay/contact-info/" in self.path:
            self._send(_HEADING_ONLY_OVERLAY, "text/html; charset=utf-8")
        elif cls.force_lookalike_heading_overlay and "/overlay/contact-info/" in self.path:
            self._send(_LOOKALIKE_HEADING_OVERLAY, "text/html; charset=utf-8")
        elif cls.force_foreign_host_link_overlay and "/overlay/contact-info/" in self.path:
            self._send(_FOREIGN_HOST_LINK_OVERLAY, "text/html; charset=utf-8")
        elif cls.force_encoded_upper_case_overlay and "/overlay/contact-info/" in self.path:
            self._send(_ENCODED_UPPER_CASE_OVERLAY, "text/html; charset=utf-8")
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


def _reset_replica_flags() -> None:
    _Replica.force_overlay_error = False
    _Replica.force_only_messaging_overlay = False
    _Replica.force_ambiguous_overlay = False
    _Replica.force_chat_only_overlay = False
    _Replica.force_heading_only_overlay = False
    _Replica.force_lookalike_heading_overlay = False
    _Replica.force_foreign_host_link_overlay = False
    _Replica.force_encoded_upper_case_overlay = False


@pytest.fixture
def site() -> Iterator[str]:
    _reset_replica_flags()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Replica)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        _reset_replica_flags()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def provider() -> AttachBrowserProvider:
    return AttachBrowserProvider(CDP_URL)


async def test_the_contact_info_overlay_is_read_from_mailto_and_tel_links(
    provider: AttachBrowserProvider, site: str
) -> None:
    """Also exercises #174 items 1-3 against a real browser: the fixture
    renders an unrelated messaging dialog *before* the real one (item 1's
    dialog-qualification check must still pick the right dialog), duplicates
    the lnkd.in and x.com/.../status/... links *inside* the dialog alongside
    the ones already outside it (item 3's scoping-vs-filtering split), and
    exercises every other reserved X/Twitter path in a non-canonical case
    (item 2's denylist, case-insensitively) -- none of that should change
    what a correct read returns."""
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
    # Item 3: shorteners are excluded even from inside the dialog, not merely
    # scoped out by being outside it.
    assert info.websites == ("https://jamie-fake.example.test/",)
    # Item 1: the messaging dialog's own links never leak in.
    assert info.email != "should-not-be-read@example.test"
    assert not any("unrelated.example.test" in w for w in info.websites)
    # Items 2/3, extended by #176 review L7: every reserved X path (status,
    # intent, share, home, search, hashtag, explore, settings, messages,
    # notifications, login, signup, compose, tos, privacy, account, about,
    # bookmarks, lists, logout, jobs, communities, download, oauth, en,
    # premium, help, terms), in a non-canonical case, is excluded, and so is
    # a segment that is not on any list but does not look like a real handle
    # by shape (too long) -- only the one real handle survives.
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


async def test_a_real_overlay_error_page_is_unreadable_not_an_empty_ok(
    provider: AttachBrowserProvider, site: str
) -> None:
    """#173 review, F5(d): an overlay whose dialog never rendered must not read as
    "Ok, nobody shared anything"."""
    _Replica.force_overlay_error = True
    try:
        async with provider.run() as run:
            source = DomContactInfoSource(run, origin=site)
            result = await source.fetch_contact_info("jamie-fake-rivera-1a2b")
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.info is None


async def test_a_real_page_with_only_a_messaging_dialog_refuses_as_unreadable(
    provider: AttachBrowserProvider, site: str
) -> None:
    """#174 item 1's "zero qualify" case: a dialog genuinely renders, but it is
    the messaging one, not contact info -- distinct from F5(d) above, where no
    dialog renders at all. Must still refuse rather than read the one dialog
    that happens to be there."""
    _Replica.force_only_messaging_overlay = True
    try:
        async with provider.run() as run:
            source = DomContactInfoSource(run, origin=site)
            result = await source.fetch_contact_info("jamie-fake-rivera-1a2b")
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.info is None


async def test_a_real_page_with_two_qualifying_dialogs_refuses_as_ambiguous(
    provider: AttachBrowserProvider, site: str
) -> None:
    """#174 item 1's "more than one qualify" case: two dialogs both carry the
    "Contact info" heading. Ambiguous, so this refuses rather than guessing
    which one a caller meant."""
    _Replica.force_ambiguous_overlay = True
    try:
        async with provider.run() as run:
            source = DomContactInfoSource(run, origin=site)
            result = await source.fetch_contact_info("jamie-fake-rivera-1a2b")
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.info is None


async def test_a_chat_with_the_same_person_never_qualifies_as_the_contact_info_dialog(
    provider: AttachBrowserProvider, site: str
) -> None:
    """#176 review H1 (HIGH), the reviewer's own probe case A: a docked chat
    with the same person is open, and no contact-info overlay at all. The
    chat's own header links back to that person's profile -- before H1's
    fix, the link-back check alone was enough to qualify this dialog, and
    the read returned the chat's own email and links instead of refusing.
    Also stands in for M2's "link only" smoke case."""
    _Replica.force_chat_only_overlay = True
    try:
        async with provider.run() as run:
            source = DomContactInfoSource(run, origin=site)
            result = await source.fetch_contact_info("jamie-fake-rivera-1a2b")
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.info is None


async def test_a_heading_with_no_link_back_never_qualifies(
    provider: AttachBrowserProvider, site: str
) -> None:
    """#176 review M2's "heading only" smoke case: the exact "Contact info"
    heading is present, but there is no link back to any profile at all --
    must refuse under the new AND rule, the same as "link only" above."""
    _Replica.force_heading_only_overlay = True
    try:
        async with provider.run() as run:
            source = DomContactInfoSource(run, origin=site)
            result = await source.fetch_contact_info("jamie-fake-rivera-1a2b")
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.info is None


async def test_a_lookalike_heading_never_qualifies(
    provider: AttachBrowserProvider, site: str
) -> None:
    """#176 review L1, probe case F: "Contact info shared with advertisers"
    is a real, differently-scoped LinkedIn dialog, not the contact-info
    overlay this module means to read. A prefix match let it through; an
    exact match (trimmed, whitespace-normalized, case-insensitive) must
    refuse it."""
    _Replica.force_lookalike_heading_overlay = True
    try:
        async with provider.run() as run:
            source = DomContactInfoSource(run, origin=site)
            result = await source.fetch_contact_info("jamie-fake-rivera-1a2b")
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.info is None


async def test_a_link_back_to_a_foreign_host_never_qualifies(
    provider: AttachBrowserProvider, site: str
) -> None:
    """#176 review M2, probe case E: the heading matches exactly, but the
    link-back's own host (medium.example) is neither this page's origin nor
    linkedin.com -- its path merely *contains* ``/in/<id>``. Must refuse:
    a link-back is only trusted when its own origin is trusted too."""
    _Replica.force_foreign_host_link_overlay = True
    try:
        async with provider.run() as run:
            source = DomContactInfoSource(run, origin=site)
            result = await source.fetch_contact_info("foreignhost-fake")
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.info is None


async def test_an_upper_case_public_id_still_matches_the_lower_case_link(
    provider: AttachBrowserProvider, site: str
) -> None:
    """#176 review M13: wantId must be lower-cased on the Python side before
    it is embedded in the script -- without that, a caller holding the
    public_id in a different case than the rendered link (both sides are
    supposed to compare case-insensitively) would wrongly refuse a real
    overlay. Reuses the default overlay fixture, whose own link-back is
    lower-case, and asks for it upper-case instead."""
    try:
        async with provider.run() as run:
            source = DomContactInfoSource(run, origin=site)
            result = await source.fetch_contact_info("JAMIE-FAKE-RIVERA-1A2B")
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    assert result.outcome is Outcome.OK
    assert result.info is not None
    assert result.info.email == "jamie.fake@example.test"


async def test_a_percent_encoded_upper_case_public_id_in_the_link_still_matches(
    provider: AttachBrowserProvider, site: str
) -> None:
    """#176 review, probe case D: the link-back's href carries a
    percent-encoded, upper-cased public id (a real diacritic, the way a
    browser would actually render one) -- the match must decode and
    case-fold both sides, not just one."""
    _Replica.force_encoded_upper_case_overlay = True
    try:
        async with provider.run() as run:
            source = DomContactInfoSource(run, origin=site)
            result = await source.fetch_contact_info("jörg-fake-x")
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    assert result.outcome is Outcome.OK
    assert result.info is not None
    assert result.info.email == "jorg@example.test"
