"""The prefill over a real Chrome, against a loopback replica (P4-03, #382, ADR 0007).

Opt in with ``NETKEEPER_BROWSER_TESTS=1``, same as every other file under
``tests/smoke/``, and point ``NETKEEPER_CDP_URL`` at an isolated Chrome on an explicit,
unused port (never 9222), in a window of about 1440 by 1000 (start it with
``--window-size=1440,1000``, or resize it): the click-geometry scenarios assume a
desktop-sized viewport. The only site is the replica this file serves for itself: a
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
from urllib.parse import quote, unquote, urlsplit

import pytest
from browser_guard import isolated_cdp
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

from netkeeper.linkedin.browser import (
    MESSAGE_NOT_ON_SCREEN,
    AttachBrowserProvider,
    ClickFailure,
    classify_click_failure,
)
from netkeeper.linkedin.messaging import (
    COMPOSE_OPTIONS_PATH,
    MessageJobSpec,
    MessageOutcomeKind,
    SendPermit,
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
    #: ``scroll_sticky`` (#470): a page that scrolls, with a fixed navigation bar and an
    #: invented sticky header that appears once it has scrolled 200 pixels.
    #: ``pseudo_icon`` (#475): the top card's link draws its icon with a ``::before``
    #: over its whole box, click point included. ``pseudo_cover``: another element's
    #: ``::after`` lies over both of the contact's controls. ``first_letter_link`` and
    #: ``first_letter_span`` (#490): the top card's ``::first-letter``, styled on the link
    #: or on its label's span, is floated with padding over the click point; Chrome's hit
    #: there is the ``::first-letter``. ``first_letter_cover``: another element's floated
    #: ``::first-letter`` lies over both controls.
    layout: str = ""
    #: #481, the never-messaged card: ``photo_and_name`` links the contact twice, the
    #: photo by member id under a longer path, as a button, and the name by slug;
    #: ``plus_other`` adds a link to someone else.
    card: str = ""


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
    "scroll_sticky": Scenario(_member(315), layout="scroll_sticky"),
    "scroll_sticky_unfixed": Scenario(_member(316), layout="scroll_sticky"),
    "scroll_clear": Scenario(_member(317), layout="scroll_sticky"),
    "pseudo_icon": Scenario(_member(318), layout="pseudo_icon"),
    "pseudo_cover": Scenario(_member(319), layout="pseudo_cover"),
    "first_letter_link": Scenario(_member(327), layout="first_letter_link"),
    "first_letter_span": Scenario(_member(328), layout="first_letter_span"),
    "first_letter_cover": Scenario(_member(329), layout="first_letter_cover"),
    # ADR 0008: auto-send, against this replica only.
    "auto_existing": Scenario(_member(320)),
    "auto_never_messaged": Scenario(_member(321), existing=False, enter_sends=False),
    "auto_refused": Scenario(_member(322)),
    # A bubble left from earlier on the page: auto-send refuses, and sends nothing.
    "auto_leftover": Scenario(_member(323), second_composer=True),
    # #481: the never-messaged card linking the contact twice, or the contact and another.
    "card_two_links": Scenario(_member(324), existing=False, card="photo_and_name"),
    "card_plus_other": Scenario(_member(325), existing=False, card="plus_other"),
    "auto_card_two_links": Scenario(
        _member(326), existing=False, enter_sends=False, card="photo_and_name"
    ),
}
BY_SLUG = {s.member.slug: s for s in SCENARIOS.values()}

_SCRIPT = """
const cfg = JSON.parse(document.getElementById('cfg').textContent);
window.__events = [];
// Each event is also told to the replica's server, so a tab the run closed can be read.
const note = (e) => {
  window.__events.push(e);
  fetch('/__event/' + cfg.slug + '?e=' + encodeURIComponent(e));
};
// LinkedIn's client sends the message itself; the form never submits a page load.
document.addEventListener('submit', (event) => event.preventDefault());
document.addEventListener('click', async (event) => {
  const link = event.target.closest('a');
  const button = event.target.closest('button');
  if (button && button.getAttribute('type') === 'submit') {
    // As LinkedIn does: the message goes, and the composer empties.
    const box = button.closest('form').querySelector('[contenteditable]');
    note('SENT:click');
    // The page's own send, answered by the replica's server as LinkedIn answers it.
    const sentText = box.innerText.replace(/\\u00a0/g, ' ').replace(/\\n+$/, '');
    fetch('/voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage', {
      method: 'POST',
      body: JSON.stringify({message: {body: {text: sentText}, conversationUrn: cfg.conversation}}),
    });
    note('SENT:text:' + box.innerText.replace(/\\u00a0/g, ' ').replace(/\\n+$/, ''));
    box.innerHTML = '<p><br></p>';
    return;
  }
  if (button && button.textContent.trim() === 'Close your conversation with ' + cfg.name) {
    note('closed');
    const bubble = button.closest('[role="dialog"]');
    if (bubble) bubble.remove();
    return;
  }
  if (button) { note('click:button:' + button.textContent.trim()); return; }
  if (!link || !link.getAttribute('href').includes('/messaging/compose/')) return;
  event.preventDefault();
  note('click:' + link.getAttribute('href'));
  const card = link.closest('[componentkey]');
  if (card) note('card:' + card.getAttribute('componentkey'));
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
    note('key:' + mods + ev.key);
    if (ev.key === 'Enter' && !ev.shiftKey && (cfg.enter_sends ? true : ev.metaKey)) {
      note('SENT:key');
      ev.preventDefault();
    }
  });
  box.addEventListener('input', () => {
    // As LinkedIn's never-messaged bubble does: Send enables once the composer holds text.
    const send = box.closest('form').querySelector('button[type="submit"]');
    if (send && box.innerText.trim()) send.disabled = false;
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
    if scenario.layout == "scroll_sticky":
        return _scrolling_layout_html(member)
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
    elif scenario.layout == "pseudo_icon":
        # The link's own pseudo-element, on its inner span: what the hit test answers.
        extra = (
            "<style>[componentkey=top-card] a{position:relative;display:inline-block}"
            "[componentkey=top-card] a>span::before{content:'';position:absolute;inset:0;"
            "background:#0a66c2;opacity:.3}</style>"
        )
    elif scenario.layout == "pseudo_cover":
        # An empty element whose ::after is a fixed overlay over both controls.
        extra = (
            "<style>#veil::after{content:'';position:fixed;left:0;top:180px;width:600px;"
            "height:420px;background:#eee;z-index:20}</style><div id='veil'></div>"
        )
    elif scenario.layout in ("first_letter_link", "first_letter_span"):
        # #490: the link's own ::first-letter at its click point. The icon is hidden, so
        # the link's first quad is its label, and its middle lies in the floated letter.
        # An inline-block label would hold its own first letter, so only the host is one.
        host = "a" if scenario.layout == "first_letter_link" else "a span span"
        blocks = "a" if host == "a" else "a,[componentkey=top-card] a span span"
        extra = (
            "<style>[componentkey=top-card] svg{display:none}"
            f"[componentkey=top-card] {blocks}{{display:inline-block}}"
            f"[componentkey=top-card] {host}::first-letter{{float:left;"
            "padding:0 80px 20px 0;background:rgba(10,102,194,.3)}</style>"
        )
    elif scenario.layout == "first_letter_cover":
        # An element with no height whose floated ::first-letter covers both controls.
        extra = (
            "<style>#veil{position:fixed;left:0;top:180px;width:600px;height:0;z-index:20;"
            "font-size:10px}#veil::first-letter{float:left;padding:0 590px 420px 0;"
            "background:#eee}</style><div id='veil'>M</div>"
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


#: #470's scrolling page: where the top card's control sits, and the brief scroll that
#: parks its center under the sticky header (52 to 116 pixels from the top).
SCROLL_TOP_CARD_PX = 400
COVERING_SCROLL_PX = 330


def _scrolling_layout_html(member: Member) -> str:
    """A profile that scrolls (#470). A fixed navigation bar sits at the top; once the
    page has scrolled 200 pixels, an invented sticky header appears under it, with a
    Message *button* (the real header's shape was never captured, #429). Invented
    boxes, not LinkedIn's. The pixel numbers here and in :data:`COVERING_SCROLL_PX`
    assume the isolated Chrome runs at 100% zoom: wheel deltas are screen points, so
    another zoom scrolls the page a different number of CSS pixels."""
    return (
        "<style>html,body{margin:0}svg{width:16px;height:16px}</style>"
        "<nav style='position:fixed;top:0;left:0;right:0;height:52px;background:#333;"
        "z-index:40'>Invented navigation</nav>"
        "<header id='sticky' style='position:fixed;top:52px;left:0;right:0;height:64px;"
        "background:#fff;z-index:30;display:none'><button type='button' "
        "style='position:absolute;right:40px;top:12px'><span>Message</span></button></header>"
        "<main style='position:relative;height:3000px'>"
        f"<h1 style='position:absolute;left:40px;top:300px;margin:0'>{escape(member.name)}</h1>"
        f"{_keyed(member, 'top-card', f'position:absolute;left:40px;top:{SCROLL_TOP_CARD_PX}px')}"
        "<section style='position:absolute;left:40px;top:1700px'><h2>Highlights</h2>"
        f"{_keyed(member, 'highlights')}</section>"
        f"<a href='/in/{member.slug}/' style='position:absolute;left:40px;top:1900px'>"
        f"{escape(member.name)}</a></main>"
        "<script>addEventListener('scroll', () => {"
        "document.getElementById('sticky').style.display = scrollY >= 200 ? 'block' : 'none';"
        "note('scroll:' + Math.round(scrollY));"
        "});</script>"
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
        if scenario.card:
            name_link = f'<a href="/in/{member.slug}/">{escape(member.name)}</a>'
            photo = (
                f'<a href="/in/{member.profile_id}/overlay/photo/" role="button">'
                '<img alt="" width="48" height="48"></a>'
            )
            other = (
                f'<a href="/in/{_member(396).slug}/">x</a>' if scenario.card == "plus_other" else ""
            )
            bubble = bubble.replace(name_link, photo + name_link + other)
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
        "slug": member.slug,
        "name": (scenario.header_for or member).name,
        "conversation": conversation_urn(9) if scenario.existing else None,
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

    def do_POST(self) -> None:
        self.requests.append(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        sent = json.loads(self.rfile.read(length) or b"{}").get("message", {})
        value = {"body": sent.get("body", {}), "conversationUrn": sent.get("conversationUrn")}
        self._send(200, json.dumps({"value": value}), "application/json")

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


async def _prefill(
    origin: str, scenario: Scenario, *, permit: SendPermit | None = None
) -> tuple[Any, dict[str, Any]]:
    spec = MessageJobSpec(
        recipient_urn=scenario.member.urn,
        recipient_public_id=scenario.member.slug,
        body=BODY,
        mode="prefill" if permit is None else "auto_send",
        typing_seed=11,
    )
    provider = AttachBrowserProvider(CDP_URL)
    async with provider.run(f"smoke-prefill-{scenario.member.n}") as run:
        source = PagePrefill(run, origin=origin, sleep=_fast, compose_settle_s=0.5)
        if permit is None:
            result = await source.prefill(spec, plan_typing(BODY, 11), cancelled=_no_cancel)
        else:
            result = await source.prefill(
                spec, plan_typing(BODY, 11), cancelled=_no_cancel, permit=permit
            )
        # Read the tab before the run ends: a run that never clicked closes its tab.
        state = await _inspect(origin, scenario)
        state["attempted"] = source.message_click_attempted
        state["diagnostics"] = source.message_click_diagnostics
    return result, state


async def _inspect(origin: str, scenario: Scenario) -> dict[str, Any]:
    """Read the handed-over tab back through a separate test-only connection, then close it."""
    from playwright.async_api import async_playwright

    # A raw connect skips the guarded connector: refuse a personal Chrome first.
    cdp_url = isolated_cdp(CDP_URL)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
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
        "pseudo_icon",
        "card_two_links",
        "first_letter_link",
        "first_letter_span",
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
    expected_card = {
        "sticky": "top-card",
        "covered": "highlights",
        "sidebar": "top-card",
        "pseudo_icon": "top-card",
        "first_letter_link": "top-card",
        "first_letter_span": "top-card",
    }
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
    # #475: another element's ::after over every control covers them; no click.
    "pseudo_cover": MESSAGE_NOT_ON_SCREEN,
    # #481: a card linking the contact and someone else refuses before any key.
    "card_plus_other": "the new-message bubble links to more than one person",
    # #490: another element's ::first-letter over every control covers them; no click.
    "first_letter_cover": MESSAGE_NOT_ON_SCREEN,
}


@pytest.mark.parametrize(
    "name",
    [
        "decoy_link",
        "draft",
        "other_recipient",
        "two_composers",
        "minimized",
        "leftover",
        "pseudo_cover",
        "card_plus_other",
        "first_letter_cover",
    ],
)
async def test_a_refused_prefill_types_nothing(origin: str, name: str) -> None:
    result, state = await _prefill(origin, SCENARIOS[name])
    assert result.outcome.kind is MessageOutcomeKind.NOT_TYPED, result
    assert REFUSALS[name] in result.outcome.reason, result
    assert not [e for e in state["events"] if e.startswith("key:")], state["events"]
    _no_send(state)
    if name == "decoy_link":
        assert not [e for e in state["events"] if e.startswith("click:")]
    if name in ("pseudo_cover", "first_letter_cover"):
        assert not state["attempted"], state["events"]
        assert not [e for e in state["events"] if e.startswith("click:")], state["events"]
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

    # A raw connect skips the guarded connector: refuse a personal Chrome first.
    cdp_url = isolated_cdp(CDP_URL)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
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
    # A raw connect skips the guarded connector: refuse a personal Chrome first.
    cdp_url = isolated_cdp(CDP_URL)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
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


# --- ADR 0008: auto-send's one click on Send, on the replica only ---------------------------


async def _holds() -> str | None:
    return None


async def _refuses() -> str | None:
    return "outside LinkedIn's active hours"


def _server_events(slug: str) -> list[str]:
    """What the replica's page told its server, in order: it outlives a closed tab."""
    prefix = f"/__event/{slug}?e="
    return [
        unquote(path[len(prefix) :]) for path in list(_Replica.requests) if path.startswith(prefix)
    ]


async def test_auto_send_clicks_send_once_then_closes_the_bubble_and_its_tab(origin: str) -> None:
    assert origin.startswith("http://127.0.0.1:")  # never a real site
    scenario = SCENARIOS["auto_existing"]
    result, state = await _prefill(origin, scenario, permit=SendPermit(recheck=_holds))
    await asyncio.sleep(0.5)  # the page's last event fetches
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED, result
    assert result.bubble_closed is True, result
    assert not state["open"]  # D3: the run closed its own tab
    events = _server_events(scenario.member.slug)
    assert events.count("SENT:click") == 1, events
    assert f"SENT:text:{BODY}" in events
    assert events.count("closed") == 1 and events.index("closed") > events.index("SENT:click")
    assert "SENT:key" not in events
    assert not [e for e in events if e in ("key:Enter", "key:Meta+Enter", "key:Control+Enter")]
    assert not [e for e in events if e.startswith("click:button")], events  # not Send options
    assert events.index("SENT:click") > max(i for i, e in enumerate(events) if e.startswith("key:"))


async def test_auto_send_in_a_new_conversation_sends_once_and_leaves_the_tab(origin: str) -> None:
    scenario = SCENARIOS["auto_never_messaged"]
    result, state = await _prefill(origin, scenario, permit=SendPermit(recheck=_holds))
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED, (result, state.get("html"))
    assert result.bubble_closed is False  # no conversation dialog: left for the person
    events = state["events"]
    assert events.count("SENT:click") == 1, events
    assert f"SENT:text:{BODY}" in events
    assert "SENT:key" not in events and "closed" not in events
    assert state["open"]


async def test_auto_send_sends_once_to_a_card_linking_the_contact_twice(origin: str) -> None:
    """#481: Send's own recheck reads the same card check, and passes the two links."""
    scenario = SCENARIOS["auto_card_two_links"]
    result, state = await _prefill(origin, scenario, permit=SendPermit(recheck=_holds))
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED, (result, state.get("html"))
    events = state["events"]
    assert events.count("SENT:click") == 1, events
    assert f"SENT:text:{BODY}" in events


async def test_auto_send_refuses_a_page_with_a_leftover_bubble(origin: str) -> None:
    result, state = await _prefill(
        origin, SCENARIOS["auto_leftover"], permit=SendPermit(recheck=_holds)
    )
    assert result.outcome.kind is MessageOutcomeKind.NOT_TYPED, result
    _no_send(state)
    assert not [e for e in state["events"] if e.startswith("key:")]


async def test_an_auto_send_whose_gate_refuses_types_but_never_clicks_send(origin: str) -> None:
    result, state = await _prefill(
        origin, SCENARIOS["auto_refused"], permit=SendPermit(recheck=_refuses)
    )
    assert result.outcome.kind is MessageOutcomeKind.PREFILLED, result
    assert result.send_refusal == "outside LinkedIn's active hours"
    _no_send(state)
    assert state["text"].replace("\u00a0", " ").rstrip("\n") == BODY


@pytest.fixture
def covering_scroll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the prefill's brief scroll to one step that leaves the scrolling page's top
    card under its sticky header (#470)."""
    from netkeeper.linkedin import page_messaging
    from netkeeper.linkedin.pacing import scroll_like_a_person

    monkeypatch.setattr(page_messaging, "BRIEF_SCROLL_STEPS", (1, 1))
    monkeypatch.setattr(
        page_messaging, "BRIEF_SCROLL_DELTA_PX", (COVERING_SCROLL_PX, COVERING_SCROLL_PX)
    )

    def no_back_up(rng: Any, **kwargs: Any) -> Any:
        return scroll_like_a_person(rng, back_up_p=0.0, **kwargs)

    monkeypatch.setattr(page_messaging, "scroll_like_a_person", no_back_up)


@pytest.mark.usefixtures("covering_scroll")
async def test_without_the_scroll_back_the_sticky_header_makes_the_click_refuse(
    origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#470's failure, in a real Chrome: with the probe answering nothing, the brief
    scroll leaves the top card's control under the sticky header and the click refuses."""
    from netkeeper.linkedin.browser import MESSAGE_NOT_ON_SCREEN, BrowserRun

    async def nothing(self: BrowserRun, profile_path: str, profile_id: str) -> None:
        return None

    monkeypatch.setattr(BrowserRun, "message_cover", nothing)
    result, state = await _prefill(origin, SCENARIOS["scroll_sticky_unfixed"])
    assert result.outcome.kind is MessageOutcomeKind.NOT_TYPED, (
        state["diagnostics"],
        state["events"],
    )
    assert result.outcome.reason == MESSAGE_NOT_ON_SCREEN
    assert not state["attempted"], state["events"]
    assert not [e for e in state["events"] if e.startswith("click:")], state["events"]


@pytest.mark.usefixtures("covering_scroll")
async def test_a_top_card_under_the_sticky_header_is_scrolled_back_to_and_clicked(
    origin: str,
) -> None:
    """#470's fix, in a real Chrome: the run scrolls back up with real wheel events, the
    sticky header goes, and the one click lands on the top card's control. The sticky
    header's own button is never clicked."""
    result, state = await _prefill(origin, SCENARIOS["scroll_sticky"])
    assert result.outcome.kind is MessageOutcomeKind.PREFILLED, (result, state.get("html"))
    _no_send(state)
    clicks = [e for e in state["events"] if e.startswith("click:")]
    assert clicks == [f"click:{compose_href(SCENARIOS['scroll_sticky'].member)}"]
    assert [e for e in state["events"] if e.startswith("card:")] == ["card:top-card"]


async def test_a_scrolled_page_is_hit_tested_where_the_control_is(
    origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#470, in a real Chrome: ``DOM.getNodeForLocation`` takes document coordinates, so
    on a page scrolled 120 pixels (the sticky header not yet shown) the top card's
    control reads clear, and the click needs no scroll back."""
    from netkeeper.linkedin import page_messaging
    from netkeeper.linkedin.pacing import scroll_like_a_person

    monkeypatch.setattr(page_messaging, "BRIEF_SCROLL_STEPS", (1, 1))
    monkeypatch.setattr(page_messaging, "BRIEF_SCROLL_DELTA_PX", (120, 120))

    def no_back_up(rng: Any, **kwargs: Any) -> Any:
        return scroll_like_a_person(rng, back_up_p=0.0, **kwargs)

    monkeypatch.setattr(page_messaging, "scroll_like_a_person", no_back_up)
    result, state = await _prefill(origin, SCENARIOS["scroll_clear"])
    assert result.outcome.kind is MessageOutcomeKind.PREFILLED, (result, state.get("html"))
    assert state["diagnostics"]["message_click_target"] == "top_card", state["diagnostics"]
    before_click = state["events"][
        : next(i for i, e in enumerate(state["events"]) if e.startswith("click:"))
    ]
    assert before_click == ["scroll:120"], state["events"]
