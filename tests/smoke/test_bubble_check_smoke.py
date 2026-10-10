"""The bubble check over a real Chrome, against a loopback replica (#497, #495).

Opt in with ``NETKEEPER_BROWSER_TESTS=1``, same as every other file under
``tests/smoke/``, and point ``NETKEEPER_CDP_URL`` at an isolated Chrome on an explicit,
unused port (never 9222). The only site is the replica this file serves for itself: a
page holding an existing-conversation bubble shaped like the 2026-10-05 capture
(``docs/linkedin-messaging-shapes.md``): a ``Messaging`` dialog whose header ``h2`` links
to ``/in/<profile id>/``, its three header buttons, and the composer, for invented people.

What this proves that the offline tests can't: that ``BrowserRun.bubble_check``,
``read_close_shape``, and ``close_sent_bubble``'s close-control lookup run end to end
through real Playwright, on a close button that carries an ``aria-hidden`` icon as
LinkedIn's do. Before #497 that icon stopped the check: Playwright's ``inner_text``
raises on an ``svg``. It also pins how a real browser's role-and-name matcher answers
the exact close lookup for a few button shapes, and the words
:func:`~netkeeper.linkedin.browser.classify_read_failure` gives Playwright's own errors.
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest
from messaging_pages import Member, existing_bubble_html

from netkeeper.linkedin.browser import (
    BUBBLE_NAME,
    BUBBLE_ROLE,
    COMPOSER_NAME,
    COMPOSER_ROLE,
    AttachBrowserProvider,
    BubbleLayout,
    BubbleRecipient,
    NameRelation,
    ReadFailure,
    classify_read_failure,
    read_close_shape,
)
from netkeeper.linkedin.page_messaging import PageBubbleCheck

pytestmark = pytest.mark.skipif(
    os.environ.get("NETKEEPER_BROWSER_TESTS") != "1",
    reason="browser smoke suite: start Chrome, then set NETKEEPER_BROWSER_TESTS=1",
)

CDP_URL = os.environ.get("NETKEEPER_CDP_URL", "http://127.0.0.1:9222")
CLOSE = "Close your conversation with "
#: An icon as LinkedIn draws one inside a header button: an ``aria-hidden`` svg, no text.
ICON = '<svg aria-hidden="true" width="16" height="16"><use href="#close-small"></use></svg>'

#: Every input the page could see, recorded so a test can prove none arrived.
_SCRIPT = """
window.__events = [];
for (const kind of ['click', 'mousedown', 'keydown', 'focusin', 'input', 'beforeinput']) {
  document.addEventListener(kind, (e) => window.__events.push(kind), true);
}
"""


def _member(n: int) -> Member:
    return Member(n, f"Smoke{n}", f"Bubble{n}", "Invented headline")


#: The close button's inside, by variant, for a member; the bubble's own is plain text.
_CLOSE_INSIDES = {
    "icon": lambda name: f"{ICON}<span>{CLOSE}{name}</span>",
    "icon_text_only_hidden": lambda name: (
        f'{ICON}<span style="position:absolute;width:1px;height:1px;overflow:hidden;'
        f'clip:rect(0 0 0 0)">{CLOSE}{name}</span>'
    ),
    "split_spans": lambda name: f"<span>{CLOSE}</span><span>{name}</span>",
    "split_no_space": lambda name: f"<span>{CLOSE.rstrip()}</span><span>{name}</span>",
    "hidden_glyph_first": lambda name: (
        f'<span aria-hidden="true">x</span><span>{CLOSE}{name}</span>'
    ),
    "hidden_glyph_after": lambda name: (
        f'<span>{CLOSE}{name}</span><span aria-hidden="true"> x</span>'
    ),
    "icon_with_title": lambda name: (
        '<svg aria-hidden="true" width="16" height="16"><title>close-small</title></svg>'
        f"<span>{CLOSE}{name}</span>"
    ),
}

#: Whether the exact lookup, ``get_by_role("button", name=..., exact=True,
#: include_hidden=True)``, finds the close button in a real Chrome, today. Hidden text
#: counts in a name under ``include_hidden=True``, so an aria-hidden glyph or an svg's
#: ``<title>`` breaks it; spans with no space between them run the words together. These
#: are evidence for the close lookup's matching, not a decision about it.
EXACT_LOOKUP_FINDS = {
    "icon": True,
    "icon_text_only_hidden": True,
    "split_spans": True,
    "split_no_space": False,
    "hidden_glyph_first": False,
    "hidden_glyph_after": False,
    "icon_with_title": False,
}
MEMBERS = {variant: _member(500 + i) for i, variant in enumerate(_CLOSE_INSIDES)}


def _bubble(variant: str, member: Member) -> str:
    html = existing_bubble_html(member)
    name = escape(member.name)
    plain = f"<button><span>{CLOSE}{name}</span></button>"
    assert plain in html
    return html.replace(plain, f"<button>{_CLOSE_INSIDES[variant](name)}</button>", 1)


def _page(variant: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>replica</title></head><body>"
        f"<main><h1>Invented feed</h1></main>{_bubble(variant, MEMBERS[variant])}"
        f"<script>{_SCRIPT}</script></body></html>"
    )


class _Replica(BaseHTTPRequestHandler):
    requests: ClassVar[list[str]] = []

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self.requests.append(self.path)
        variant = self.path.strip("/").split("?")[0]
        status, body = (200, _page(variant)) if variant in MEMBERS else (404, "no such page")
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


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


@asynccontextmanager
async def _opened_by_hand(url: str) -> AsyncIterator[Any]:
    """A tab a person opened, through a separate test-only connection: the check finds it
    in the context's tabs and never opens one of its own. Closed afterwards."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(CDP_URL)
        try:
            page = await browser.contexts[0].new_page()
            try:
                await page.goto(url)
                yield page
            finally:
                await page.close()
        finally:
            await browser.close()


async def test_the_bubble_check_reads_a_bubble_whose_close_button_has_an_icon(
    origin: str,
) -> None:
    """#497: the live failure's shape. The check reads it to the end, finds the contact's
    one bubble, and nothing reaches the page: no click, key, focus, or input."""
    member = MEMBERS["icon"]
    async with _opened_by_hand(f"{origin}/icon/") as tab:
        async with AttachBrowserProvider(CDP_URL).run("smoke-bubble-check") as run:
            check = await PageBubbleCheck(run, origin=origin).run(member.profile_id)
        events = await tab.evaluate("() => window.__events")
    assert check.on_origin == 1 and check.with_bubble == 1 and check.for_contact == 1
    assert check.dialogs == 1 and check.dialogs_for_contact == 1
    assert check.composers == 1 and check.composer_in_dialog is True
    shape = check.shape
    assert shape is not None
    assert (shape.by_prefix, shape.by_prefix_visible, shape.exact) == (1, 1, 1)
    assert shape.header_links == 1
    assert [b.relation for b in shape.buttons] == [NameRelation.EXACT]
    assert events == []


async def test_the_bubble_check_finds_no_bubble_for_someone_else(origin: str) -> None:
    async with (
        _opened_by_hand(f"{origin}/icon/"),
        AttachBrowserProvider(CDP_URL).run("smoke-bubble-check") as run,
    ):
        check = await PageBubbleCheck(run, origin=origin).run(_member(599).profile_id)
    assert (check.on_origin, check.with_bubble, check.for_contact) == (1, 1, 0)
    assert check.shape is None


@pytest.mark.parametrize("variant", sorted(_CLOSE_INSIDES))
async def test_the_close_shape_and_the_close_lookup_in_a_real_browser(
    origin: str, variant: str
) -> None:
    """``read_close_shape`` reads each shape without raising, and ``close_sent_bubble``'s
    lookup (``_close_control``, reads only) finds the button exactly when
    :data:`EXACT_LOOKUP_FINDS` says a real Chrome does."""
    member = MEMBERS[variant]
    async with _opened_by_hand(f"{origin}/{variant}/") as tab:
        async with AttachBrowserProvider(CDP_URL).run("smoke-bubble-check") as run:
            dialog = tab.get_by_role(BUBBLE_ROLE, name=BUBBLE_NAME, exact=True, include_hidden=True)
            shape = await read_close_shape(tab, dialog)
            composer = tab.get_by_role(
                COMPOSER_ROLE, name=COMPOSER_NAME, exact=True, include_hidden=True
            )
            recipient = BubbleRecipient(BubbleLayout.EXISTING, member.profile_id, member.slug)
            control = await run._close_control(tab, composer, recipient)
        events = await tab.evaluate("() => window.__events")
    finds = EXACT_LOOKUP_FINDS[variant]
    assert shape.exact == (1 if finds else 0), shape.describe()
    assert not isinstance(control, str) if finds else isinstance(control, str), control
    assert events == []


async def test_read_failures_name_their_kind_in_a_real_browser(origin: str) -> None:
    """#497: :func:`classify_read_failure` against the errors Playwright really raises."""
    async with _opened_by_hand(f"{origin}/icon/") as tab:
        kinds: dict[str, ReadFailure] = {}
        reads = {
            "not_html": tab.locator("header button svg").inner_text(timeout=1_000),
            "strict": tab.locator("header button").inner_text(timeout=1_000),
            "css": tab.locator("header[").count(),
            "regex": tab.locator("internal:role=button[name=/(/]").count(),
            "engine": tab.locator("nope=x").count(),
            "timeout": tab.locator("table").inner_text(timeout=200),
        }
        for label, read in reads.items():
            try:
                await read
            except Exception as exc:
                kinds[label] = classify_read_failure(exc)
        events = await tab.evaluate("() => window.__events")
        stale = tab.locator("main")
    try:
        await stale.count()
    except Exception as exc:
        kinds["closed"] = classify_read_failure(exc)
    assert kinds == {
        "not_html": ReadFailure.NOT_HTML,
        "strict": ReadFailure.STRICT_MODE,
        "css": ReadFailure.INVALID_SELECTOR,
        "regex": ReadFailure.INVALID_SELECTOR,
        "engine": ReadFailure.INVALID_SELECTOR,
        "timeout": ReadFailure.TIMEOUT,
        "closed": ReadFailure.TARGET_CLOSED,
    }
    assert events == []
