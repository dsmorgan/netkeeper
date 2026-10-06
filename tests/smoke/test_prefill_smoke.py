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
composer when the scenario says so (where focus lands on the real site is not known,
#429; ADR 0007's option A adds no focusing input, so an unfocused composer refuses).
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

from netkeeper.linkedin.browser import AttachBrowserProvider
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


def _member(n: int) -> Member:
    return Member(n, f"Smoke{n}", f"Replica{n}", "Invented headline")


SCENARIOS = {
    "existing": Scenario(_member(301)),
    "never_messaged": Scenario(_member(302), existing=False, enter_sends=False),
    "decoy_link": Scenario(_member(303), decoy_link=_member(399)),
    "draft": Scenario(_member(304), draft="left over"),
    "other_recipient": Scenario(_member(305), header_for=_member(398)),
    "two_composers": Scenario(_member(306), second_composer=True),
    "unfocused": Scenario(_member(307), focus=False),
    "late_focus": Scenario(_member(308), focus_delay_ms=800),
    "pronouns": Scenario(_member(309), existing=False, pronouns=True),
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
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>replica</title></head><body>"
        f"<main><h1>{escape(member.name)}{_PRONOUNS if scenario.pronouns else ''}</h1>"
        '<button type="button"><span>Message</span></button>'  # a button decoy: never clicked
        f"{links}<a href='/in/{member.slug}/'>{escape(member.name)}</a></main>{decoy}"
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
    return result, await _inspect(origin, scenario)


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
                          html: box ? box.innerHTML : null};
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


@pytest.mark.parametrize("name", ["existing", "never_messaged", "late_focus", "pronouns"])
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


REFUSALS = {
    "decoy_link": "something other than this contact's compose",
    "draft": "not empty",
    "other_recipient": "for someone else",
    "two_composers": "more than one message composer",
    "unfocused": "does not hold focus",
}


@pytest.mark.parametrize(
    "name", ["decoy_link", "draft", "other_recipient", "two_composers", "unfocused"]
)
async def test_a_refused_prefill_types_nothing(origin: str, name: str) -> None:
    result, state = await _prefill(origin, SCENARIOS[name])
    assert result.outcome.kind is MessageOutcomeKind.NOT_TYPED, result
    assert REFUSALS[name] in result.outcome.reason, result
    assert not [e for e in state["events"] if e.startswith("key:")], state["events"]
    _no_send(state)
    if name == "decoy_link":
        assert not [e for e in state["events"] if e.startswith("click:")]
