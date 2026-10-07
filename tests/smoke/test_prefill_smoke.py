"""The prefill over a real Chrome, against a loopback replica (P4-03, #382, ADR 0007).

Opt in with ``NETKEEPER_BROWSER_TESTS=1``, same as every other file under
``tests/smoke/``, and point ``NETKEEPER_CDP_URL`` at an isolated Chrome on an explicit,
unused port (never 9222). The only site is the replica this file serves for itself: a
profile page laid out as the 2026-10-05 capture showed it (``docs/linkedin-messaging-shapes.md``),
with :mod:`messaging_pages`' invented people.

What this proves that the offline tests can't: that Playwright's real keyboard,
through the narrow methods, types the body into a real ``contenteditable``; that the
text rule reads a browser's own editing back as the body, newlines included; that no
Enter keydown without Shift ever reaches the composer, so the replica's Enter-to-send
toggle (bare Enter when on, Command+Enter when off) never records a send; and that
the tab stays open after the hand-over.

The replica's page script plays the part of LinkedIn's: a click on a Message link
fetches the compose option and the thread, then opens the bubble, and focuses the
composer when the scenario says so. Where focus lands on the real site is not known
(#429); under ADR 0007's option B the run makes one ``Locator.focus()`` on the verified
composer when the page didn't, so a replica that never focuses it is still typed into.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

import pytest
from messaging_pages import (
    Member,
    compose_href,
    compose_option_answer,
    compose_option_urn,
    conversation_urn,
    existing_bubble_html,
    message_control_html,
    messages_sync_url,
    never_messaged_bubble_html,
)

from netkeeper.linkedin.browser import AttachBrowserProvider, ClickFailure, classify_click_failure
from netkeeper.linkedin.messaging import (
    COMPOSE_OPTIONS_PATH,
    MessageJobSpec,
    MessageOutcomeKind,
    plan_typing,
)
from netkeeper.linkedin.page_messaging import PagePrefill

pytestmark = pytest.mark.skipif(
    os.environ.get("NETKEEPER_BROWSER_TESTS") != "1",
    reason="browser smoke suite: start Chrome, then set NETKEEPER_BROWSER_TESTS=1",
)

CDP_URL = os.environ.get("NETKEEPER_CDP_URL", "http://127.0.0.1:9222")
BODY = "Hi there, good to see you!\nShall we catch up?\n\nBest, Odalys"


@dataclass(frozen=True)
class Scenario:
    """One replica profile: who it is, which bubble the click opens, and how it behaves."""

    member: Member
    existing: bool = True
    focus: bool = True
    #: Focus the composer this many milliseconds after the bubble opens.
    focus_delay_ms: int = 0
    #: Pronouns in an ``aria-hidden`` span inside the profile's ``h1``.
    pronouns: bool = False
    enter_sends: bool = True
    draft: str = ""
    decoy_link: Member | None = None
    header_for: Member | None = None
    second_composer: bool = False
    #: #444's layouts. ``sticky``: a fixed header copy of the contact's Message link,
    #: first in the document and slid above the viewport. ``covered``: the top card's
    #: control sits under a fixed bar; a Highlights copy below it is clear. ``sidebar``:
    #: "More profiles for you" with other people's Message buttons. ``minimized``: three
    #: minimized bubbles from earlier conversations and the Messaging bar.
    layout: str = ""


def _member(n: int) -> Member:
    return Member(n, f"Smoke{n}", f"Replica{n}", "Invented headline")


SCENARIOS = {
    "existing": Scenario(_member(301)),
    "never_messaged": Scenario(_member(302), existing=False, enter_sends=False),
    "decoy_link": Scenario(_member(303), decoy_link=_member(399)),
    "draft": Scenario(_member(304), draft="left over"),
    "other_recipient": Scenario(_member(305), header_for=_member(398)),
    "two_composers": Scenario(_member(306), second_composer=True),
    "never_focused": Scenario(_member(307), focus=False),
    "late_focus": Scenario(_member(308), focus_delay_ms=800),
    "pronouns": Scenario(_member(309), existing=False, pronouns=True),
    "sticky": Scenario(_member(310), layout="sticky"),
    "covered": Scenario(_member(311), layout="covered"),
    "sidebar": Scenario(_member(312), layout="sidebar"),
    "minimized": Scenario(_member(313), layout="minimized"),
    "leftover": Scenario(_member(314), layout="leftover"),
}
BY_SLUG = {s.member.slug: s for s in SCENARIOS.values()}

_SCRIPT = """
const cfg = JSON.parse(document.getElementById('cfg').textContent);
window.__events = [];
document.addEventListener('click', async (event) => {
  const link = event.target.closest('a');
  const button = event.target.closest('button');
  if (button && button.type === 'submit') { window.__events.push('SENT:click'); return; }
  if (button) { window.__events.push('click:button:' + button.textContent.trim()); return; }
  if (!link || !link.getAttribute('href').includes('/messaging/compose/')) return;
  event.preventDefault();
  window.__events.push('click:' + link.getAttribute('href'));
  const card = link.closest('[componentkey]');
  if (card) window.__events.push('card:' + card.getAttribute('componentkey'));
  await fetch(cfg.compose_url);
  if (cfg.thread_url) await fetch(cfg.thread_url);
  const host = document.createElement('div');
  host.innerHTML = cfg.bubble;
  document.body.appendChild(host);
  const boxes = host.querySelectorAll('[contenteditable]');
  const box = boxes[boxes.length - 1];
  box.addEventListener('keydown', (ev) => {
    const mods = (ev.shiftKey ? 'Shift+' : '') + (ev.metaKey ? 'Meta+' : '') +
      (ev.ctrlKey ? 'Control+' : '') + (ev.altKey ? 'Alt+' : '');
    window.__events.push('key:' + mods + ev.key);
    if (ev.key === 'Enter' && !ev.shiftKey && (cfg.enter_sends ? true : ev.metaKey)) {
      window.__events.push('SENT:key');
      ev.preventDefault();
    }
  });
  if (cfg.focus) setTimeout(() => box.focus(), cfg.focus_delay_ms);
});
"""


_PRONOUNS = ' <span aria-hidden="true">(They/Them)</span>'


def _keyed(member: Member, key: str, style: str = "") -> str:
    control = message_control_html(member, absolute=False)
    return f'<div componentkey="{key}" style="{style}">{control}</div>'


def _minimized_bubbles() -> str:
    """Three minimized bubbles for other people, as the CP8 page showed them: each a
    ``Messaging`` dialog whose body (and composer) is hidden, not removed, so its draft
    survives; and the Messaging bar. Fixed along the bottom right."""
    bubbles = "".join(
        f'<div style="position:fixed;bottom:0;right:{300 + 220 * i}px;width:210px;height:48px;'
        f'background:#ddd;z-index:20">'
        + existing_bubble_html(_member(380 + i)).replace(
            'role="dialog"',
            'role="dialog" data-msg-overlay-conversation-bubble-is-minimized="true"'
            ' style="height:48px;overflow:hidden"',
            1,
        )
        + "</div>"
        for i in range(3)
    )
    bar = (
        '<aside style="position:fixed;bottom:0;right:0;width:288px;height:48px;'
        'background:#eee;z-index:20"><h2>Messaging</h2></aside>'
    )
    return bubbles + bar


def _layout_html(scenario: Scenario) -> str:
    """#444's profile layouts, in fixed pixels on a page that doesn't scroll, so the
    prefill's brief scroll leaves every box where it is."""
    member = scenario.member
    top = _keyed(member, "top-card", "position:absolute;left:40px;top:220px")
    highlights = (
        '<section style="position:absolute;left:40px;top:420px"><h2>Highlights</h2>'
        f"{_keyed(member, 'highlights')}</section>"
    )
    sticky = ""
    extra = ""
    if scenario.layout == "sticky":
        sticky = (
            '<header style="position:fixed;top:0;left:0;right:0;height:64px;overflow:hidden;'
            'transform:translateY(-100%);background:#fff;z-index:30">'
            f"{_keyed(member, 'sticky')}</header>"
        )
    elif scenario.layout == "covered":
        extra = (
            '<aside style="position:fixed;left:0;top:180px;width:600px;height:120px;'
            'background:#eee;z-index:20"><h2>Messaging</h2></aside>'
        )
    elif scenario.layout == "sidebar":
        people = "".join(
            f'<li><a href="/in/{_member(390 + i).slug}/">{escape(_member(390 + i).name)}</a>'
            '<button type="button"><span>Message</span></button></li>'
            for i in range(3)
        )
        extra = (
            '<aside style="position:absolute;left:700px;top:220px"><h2>More profiles for you'
            f"</h2><ul>{people}</ul></aside>"
        )
    elif scenario.layout == "minimized":
        extra = _minimized_bubbles()
    elif scenario.layout == "leftover":
        # An open bubble left from an earlier prefill, for someone else.
        extra = (
            '<div style="position:fixed;bottom:0;right:300px;width:330px;z-index:20">'
            f"{existing_bubble_html(_member(385))}</div>"
        )
    return (
        # An icon's size, as LinkedIn's CSS gives it; an unsized svg is 300 by 150.
        "<style>html,body{margin:0;height:100%;overflow:hidden}svg{width:16px;height:16px}"
        "</style>"
        f"{sticky}<main><h1 style='position:absolute;left:40px;top:120px;margin:0'>"
        f"{escape(member.name)}</h1>{top}{highlights}"
        f"<a href='/in/{member.slug}/' style='position:absolute;left:40px;top:560px'>"
        f"{escape(member.name)}</a></main>{extra}"
    )


def _profile_html(scenario: Scenario) -> str:
    member = scenario.member
    links = "".join(
        f'<div componentkey="smoke-{i}"{" hidden" if i == 0 else ""}>'
        f"{message_control_html(member, absolute=False)}</div>"
        for i in range(3)
    )
    decoy = (
        f"<aside><h2>People also viewed</h2>"
        f"{message_control_html(scenario.decoy_link, absolute=False)}</aside>"
        if scenario.decoy_link
        else ""
    )
    if scenario.existing:
        bubble = existing_bubble_html(scenario.header_for or member, draft=scenario.draft)
    else:
        bubble = never_messaged_bubble_html([member], draft=scenario.draft)
    if scenario.second_composer:
        bubble = existing_bubble_html(_member(397)) + bubble
    origin_compose = f"{COMPOSE_OPTIONS_PATH}{quote(compose_option_urn(member), safe='')}"
    thread = urlsplit(messages_sync_url(9)) if scenario.existing else None
    cfg = {
        "compose_url": origin_compose,
        "thread_url": f"{thread.path}?{thread.query}" if thread else None,
        "bubble": bubble,
        "focus": scenario.focus,
        "focus_delay_ms": scenario.focus_delay_ms,
        "enter_sends": scenario.enter_sends,
    }
    page = (
        _layout_html(scenario)
        if scenario.layout
        else (
            f"<main><h1>{escape(member.name)}{_PRONOUNS if scenario.pronouns else ''}</h1>"
            '<button type="button"><span>Message</span></button>'  # a button decoy
            f"{links}<a href='/in/{member.slug}/'>{escape(member.name)}</a></main>{decoy}"
        )
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>replica</title></head><body>"
        f"{page}"
        f"<script type='application/json' id='cfg'>{json.dumps(cfg).replace('</', '<\\/')}</script>"
        f"<script>{_SCRIPT}</script></body></html>"
    )


class _Replica(BaseHTTPRequestHandler):
    requests: ClassVar[list[str]] = []

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, body: str, kind: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.requests.append(self.path)
        path = urlsplit(self.path).path
        if path.startswith("/in/"):
            scenario = BY_SLUG.get(path[len("/in/") :].strip("/"))
            if scenario is None:
                self._send(404, "no such profile", "text/plain")
                return
            self._send(200, _profile_html(scenario), "text/html; charset=utf-8")
        elif path.startswith(COMPOSE_OPTIONS_PATH):
            for scenario in SCENARIOS.values():
                if quote(compose_option_urn(scenario.member), safe="") in self.path:
                    answer = compose_option_answer(
                        scenario.member, existing_conversation=9 if scenario.existing else None
                    )
                    self._send(200, answer, "application/json")
                    return
            self._send(404, "{}", "application/json")
        else:
            self._send(200, "{}", "application/json")


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


async def _no_cancel() -> bool:
    return False


async def _fast(seconds: float) -> None:
    await asyncio.sleep(min(seconds, 0.02))


async def _prefill(origin: str, scenario: Scenario) -> tuple[Any, dict[str, Any]]:
    spec = MessageJobSpec(
        recipient_urn=scenario.member.urn,
        recipient_public_id=scenario.member.slug,
        body=BODY,
        mode="prefill",
        typing_seed=11,
    )
    provider = AttachBrowserProvider(CDP_URL)
    async with provider.run(f"smoke-prefill-{scenario.member.n}") as run:
        source = PagePrefill(run, origin=origin, sleep=_fast, compose_settle_s=0.5)
        result = await source.prefill(spec, plan_typing(BODY, 11), cancelled=_no_cancel)
        # Read the tab before the run ends: a run that never clicked closes its tab.
        state = await _inspect(origin, scenario)
        state["attempted"] = source.message_click_attempted
    return result, state


async def _inspect(origin: str, scenario: Scenario) -> dict[str, Any]:
    """Read the handed-over tab back through a separate test-only connection, then close it."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(CDP_URL)
        try:
            pages = [
                page
                for page in browser.contexts[0].pages
                if page.url.startswith(f"{origin}/in/{scenario.member.slug}")
            ]
            if not pages:
                return {"open": False, "events": [], "text": None}
            page = pages[-1]
            state = await page.evaluate(
                """() => {
                  const boxes = document.querySelectorAll('[contenteditable]');
                  const box = boxes[boxes.length - 1];
                  return {events: window.__events, text: box ? box.innerText : null,
                          html: box ? box.innerHTML : null, composers: boxes.length};
                }"""
            )
            state["open"] = True
            await page.close()
            return dict(state)
        finally:
            await browser.close()


def _no_send(state: dict[str, Any]) -> None:
    events = state["events"]
    assert not [e for e in events if e.startswith("SENT")], events
    assert not [e for e in events if e in ("key:Enter", "key:Meta+Enter", "key:Control+Enter")]
    assert not [e for e in events if e.startswith("click:button")], events


@pytest.mark.parametrize(
    "name",
    [
        "existing",
        "never_messaged",
        "late_focus",
        "pronouns",
        "never_focused",
        "sticky",
        "covered",
        "sidebar",
    ],
)
async def test_the_body_is_typed_into_a_real_composer_and_never_sent(
    origin: str, name: str
) -> None:
    result, state = await _prefill(origin, SCENARIOS[name])
    assert result.outcome.kind is MessageOutcomeKind.PREFILLED, (result, state.get("html"))
    assert state["open"]  # the tab stays open after the hand-over
    _no_send(state)
    # The browser's own rendering of what was typed, newlines included.
    assert state["text"].replace("\u00a0", " ").rstrip("\n") == BODY, state["html"]
    assert state["events"].count("key:Shift+Enter") == BODY.count("\n")
    # The chip was checked against the profile's h1, pronouns or not.
    expected_checked = None if SCENARIOS[name].existing else True
    assert result.outcome.recipient_name_checked is expected_checked
    expected_urn = conversation_urn(9) if SCENARIOS[name].existing else None
    assert result.outcome.conversation_urn == expected_urn
    clicks = [e for e in state["events"] if e.startswith("click:")]
    assert clicks == [f"click:{compose_href(SCENARIOS[name].member)}"]
    # #444: which control took the click, on the layouts that have a choice.
    expected_card = {"sticky": "top-card", "covered": "highlights", "sidebar": "top-card"}
    if name in expected_card:
        cards = [e for e in state["events"] if e.startswith("card:")]
        assert cards == [f"card:{expected_card[name]}"], state["events"]


REFUSALS = {
    "decoy_link": "something other than this contact's compose",
    "draft": "not empty",
    "other_recipient": "for someone else",
    "two_composers": "another message composer is on the page",
    # #444: minimized bubbles keep their composers, hidden; decision 3, read before the
    # click, refuses them and a leftover open bubble with no click.
    "minimized": "a message bubble is already open in Chrome, minimized ones included",
    "leftover": "a message bubble is already open in Chrome, minimized ones included",
}


@pytest.mark.parametrize(
    "name", ["decoy_link", "draft", "other_recipient", "two_composers", "minimized", "leftover"]
)
async def test_a_refused_prefill_types_nothing(origin: str, name: str) -> None:
    result, state = await _prefill(origin, SCENARIOS[name])
    assert result.outcome.kind is MessageOutcomeKind.NOT_TYPED, result
    assert REFUSALS[name] in result.outcome.reason, result
    assert not [e for e in state["events"] if e.startswith("key:")], state["events"]
    _no_send(state)
    if name == "decoy_link":
        assert not [e for e in state["events"] if e.startswith("click:")]
    if name in ("minimized", "leftover"):
        # No click, so no second bubble: only the leftover composers are on the page.
        assert state["open"] and not state["attempted"]
        assert not [e for e in state["events"] if e.startswith("click:")], state["events"]
        assert state["composers"] == (3 if name == "minimized" else 1)
        assert not result.outcome.reason.startswith("the Message control")


#: Test-only pages for Playwright's own click errors. The click here is the test's, on
#: its own page in the isolated Chrome, never the package's.
_ERROR_PAGES = {
    ClickFailure.OUTSIDE_VIEWPORT: (
        '<header style="position:fixed;top:0;left:0;right:0;height:60px;'
        'transform:translateY(-100%)"><a href="/m">Message</a></header>'
    ),
    ClickFailure.INTERCEPTED: (
        '<a href="/m" style="position:fixed;left:20px;top:20px">Message</a>'
        '<div style="position:fixed;left:0;top:0;width:300px;height:100px;background:#eee">'
        "</div>"
    ),
    ClickFailure.NOT_STABLE: (
        "<style>@keyframes s{from{transform:translateX(0)}to{transform:translateX(400px)}}"
        '</style><a href="/m" style="position:fixed;top:20px;animation:s 1s linear infinite">'
        "Message</a>"
    ),
}


@pytest.mark.parametrize("category", list(_ERROR_PAGES))
async def test_playwrights_own_click_errors_classify_to_their_category(
    category: ClickFailure,
) -> None:
    """#444's root cause, reproduced: a fixed copy slid off screen, a control under
    another element, and one that keeps moving each make Playwright's click time out,
    and :func:`classify_click_failure` reads each one's category from the real error."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(CDP_URL)
        page = await browser.contexts[0].new_page()
        try:
            await page.set_content(f"<body style='margin:0'>{_ERROR_PAGES[category]}</body>")
            link = page.get_by_role("link", name="Message", exact=True)
            # Playwright calls each of these visible: it is the click that fails.
            assert await link.filter(visible=True).count() == 1
            with pytest.raises(Exception) as raised:
                await link.click(timeout=1_500)
            assert classify_click_failure(raised.value) is category
        finally:
            await page.close()
            await browser.close()


async def test_the_old_first_visible_choice_fails_on_the_sticky_page(origin: str) -> None:
    """#444's root cause on the replica's own sticky page: the first visible Message link
    in document order (the rule before #444) is the sticky copy, wholly above the
    viewport, and Playwright's click on it fails as outside the viewport. The prefill
    test above clicks the top card on the same page."""
    from playwright.async_api import async_playwright

    member = SCENARIOS["sticky"].member
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(CDP_URL)
        page = await browser.contexts[0].new_page()
        try:
            await page.goto(f"{origin}/in/{member.slug}/")
            links = page.get_by_role("link", name="Message", exact=True, include_hidden=True)
            first = links.filter(visible=True).first
            card = first.locator("xpath=ancestor::*[@componentkey][1]")
            assert await card.get_attribute("componentkey") == "sticky"
            box = await first.bounding_box()
            assert box is not None and box["y"] + box["height"] <= 0, box
            with pytest.raises(Exception) as raised:
                await first.click(timeout=1_500)
            assert classify_click_failure(raised.value) is ClickFailure.OUTSIDE_VIEWPORT
        finally:
            await page.close()
            await browser.close()
