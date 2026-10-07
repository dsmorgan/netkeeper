"""The prefill's page work under ADR 0007 (P4-03, #382), against fake pages.

:mod:`messaging_dom` serves :mod:`messaging_pages`' invented profile and bubbles, and
its keyboard fails the test on any key that could send. Every refusal is checked to
come before any key call; every stop after the first key is ``partially_typed`` or
``unknown``; and the one click, the one bring-to-front, and the hand-over are counted.
"""

from __future__ import annotations

import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from messaging_dom import (
    Bubble,
    Element,
    MessagingSite,
    MessagingTab,
    parse_into,
)
from messaging_pages import (
    OWNER,
    THADDEUS,
    ZEPHYRINE,
    Member,
    compose_href,
    compose_option_answer,
    compose_option_url,
    conversation_urn,
    existing_bubble_html,
    message_control_html,
    never_messaged_bubble_html,
    profile_message_controls_html,
)
from run_fakes import fake_provider

from netkeeper.linkedin import browser as browser_module
from netkeeper.linkedin.browser import (
    BrowserRun,
    BrowserUnavailable,
    BubbleLayout,
    BubbleRecipient,
    TypingEnd,
    message_control_refusal,
)
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.messaging import (
    MessageJobSpec,
    MessageOutcomeKind,
    PrefillResult,
    plan_typing,
)
from netkeeper.linkedin.pacing import TypeStep, TypingPlan
from netkeeper.linkedin.page_messaging import PagePrefill

BODY = "Hi Zephyrine, good to see you!"
T0 = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)


def spec_for(member: Member, body: str = BODY, *, public_id: str | None = "") -> MessageJobSpec:
    return MessageJobSpec(
        recipient_urn=member.urn,
        recipient_public_id=member.slug if public_id == "" else public_id,
        body=body,
        mode="prefill",
        typing_seed=3,
    )


@dataclass
class Clock:
    """A clock that steps one second a call and notes how many keys had landed."""

    tab: Callable[[], MessagingTab | None]
    calls: list[tuple[datetime, int]] = field(default_factory=list)

    def __call__(self) -> datetime:
        tab = self.tab()
        at = T0 + timedelta(seconds=len(self.calls))
        self.calls.append((at, 0 if tab is None else len(tab.attempts)))
        return at


@dataclass
class Ran:
    """What one prefill did."""

    result: PrefillResult
    site: MessagingSite
    run: BrowserRun
    sleeps: list[tuple[float, int, int]]
    clock: Clock

    @property
    def tab(self) -> MessagingTab:
        return self.site.tab

    @property
    def kind(self) -> MessageOutcomeKind:
        return self.result.outcome.kind


async def prefill(
    site: MessagingSite,
    body: str = BODY,
    *,
    member: Member = ZEPHYRINE,
    spec: MessageJobSpec | None = None,
    plan: TypingPlan | None = None,
    cancelled: Callable[[], Awaitable[bool]] | None = None,
) -> Ran:
    provider, _ = fake_provider(site)
    sleeps: list[tuple[float, int, int]] = []

    def tab() -> MessagingTab | None:
        return site.pages[0] if site.pages else None  # type: ignore[return-value]

    async def sleep(seconds: float) -> None:
        current = tab()
        sleeps.append(
            (seconds, current.reads if current else 0, len(current.attempts) if current else 0)
        )

    async def never() -> bool:
        return False

    clock = Clock(tab)
    the_spec = spec or spec_for(member, body)
    the_plan = plan if plan is not None else plan_typing(the_spec.body, the_spec.typing_seed)
    async with provider.run() as run:
        source = PagePrefill(
            run,
            sleep=sleep,
            clock=clock,
            rng=random.Random(5),
            compose_wait_s=0.05,
            compose_settle_s=0.01,
            thread_wait_s=0.01,
        )
        result = await source.prefill(the_spec, the_plan, cancelled=cancelled or never)
    return Ran(result, site, run, sleeps, clock)


def assert_no_keys(ran: Ran) -> None:
    assert ran.kind is MessageOutcomeKind.NOT_TYPED, ran.result
    assert ran.tab.attempts == [] and ran.tab.keys == []
    assert ran.result.typing_started_at is None
    assert ran.run.keys_sent == 0


def composer_text(tab: MessagingTab) -> str:
    return tab.draft + tab.typed


# --- the happy paths -------------------------------------------------------------------


async def test_an_existing_conversation_is_typed_once_and_handed_over() -> None:
    ran = await prefill(MessagingSite(ZEPHYRINE))
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert composer_text(ran.tab) == BODY
    assert ran.result.outcome.typed_chars == len(BODY)
    # One click, on a Message link bound to the contact's compose href.
    [clicked] = ran.tab.clicks
    assert clicked.tag == "a" and clicked.attrs["href"] == compose_href(ZEPHYRINE)
    assert ran.tab.fronted == 1 and ran.run.fronted == 1
    # Navigation once, never after the click; the tab is left open, the run detached.
    assert ran.site.navigations == [f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/"]
    assert ran.tab.goto_after_click == []
    assert ran.run.handed_over and not ran.tab.is_closed() and ran.tab.close_calls == 0
    # The conversation in the form the inbox poll matches.
    assert ran.result.outcome.conversation_urn == conversation_urn(7)
    assert ran.site.stray_keys == []


async def test_a_never_messaged_contact_is_typed_in_the_new_message_bubble() -> None:
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, None)))
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert composer_text(ran.tab) == BODY
    assert ran.result.outcome.conversation_urn is None


async def test_a_never_messaged_bubble_whose_root_is_the_messaging_dialog_proceeds() -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, None, dialog_root=True))
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result


@pytest.mark.parametrize("mode", ["br", "p"])
async def test_newlines_are_shift_enter_and_read_back_in_either_form(mode: str) -> None:
    body = "Hi Zephyrine,\n\nthanks again.\nBest"
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, newline_mode=mode))
    ran = await prefill(site, body)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    presses = [value for kind, value in ran.tab.attempts if kind == "press"]
    assert presses == ["Shift+Enter"] * 3
    assert composer_text(ran.tab) == body


async def test_a_space_is_inserted_never_typed_and_accents_are_inserted() -> None:
    body = "Olá Zé, até já 👋🏽"
    ran = await prefill(MessagingSite(ZEPHYRINE), body)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    typed = [value for kind, value in ran.tab.attempts if kind == "type"]
    inserted = [value for kind, value in ran.tab.attempts if kind == "insert_text"]
    assert " " not in typed and all(len(v) == 1 and 0x21 <= ord(v) <= 0x7E for v in typed)
    assert " " in inserted and "á" in inserted and "👋🏽" in inserted


async def test_typing_starts_before_the_first_key_and_each_key_follows_its_check() -> None:
    ran = await prefill(MessagingSite(ZEPHYRINE))
    assert ran.result.typing_started_at is not None
    stamped = [at for at, keys in ran.clock.calls if at == ran.result.typing_started_at]
    [(_, keys_then)] = [(at, k) for at, k in ran.clock.calls if at in stamped]
    assert keys_then == 0  # taken before the first key call
    # Delay, check, key: each key landed after at least one read that came after the
    # step's last sleep, so no delay sits between a check and its key.
    for index, key in enumerate(ran.tab.keys):
        before = [reads for _, reads, keys in ran.sleeps if keys == index]
        assert before, "every key follows its step's delay"
        assert key.reads_before > before[-1], index


# --- refusals before any key -----------------------------------------------------------


def _decoy_page(extra: str) -> str:
    return profile_message_controls_html(ZEPHYRINE) + extra


async def test_a_message_link_naming_someone_else_refuses_before_any_click() -> None:
    site = MessagingSite(
        ZEPHYRINE, profile_html=profile_message_controls_html(ZEPHYRINE, decoy=THADDEUS)
    )
    ran = await prefill(site)
    assert_no_keys(ran)
    assert ran.tab.clicks == []
    assert not ran.run.handed_over  # nothing was clicked: the run's own tab is closed
    assert ran.tab.is_closed()


@pytest.mark.parametrize(
    "href",
    [
        # profileUrn and recipient disagree
        f"/messaging/compose/?profileUrn={ZEPHYRINE.urn}&recipient={THADDEUS.profile_id}",
        # a repeated query parameter
        f"/messaging/compose/?profileUrn={ZEPHYRINE.urn}&recipient={ZEPHYRINE.profile_id}"
        f"&recipient={THADDEUS.profile_id}",
        # another host
        f"https://evil.example/messaging/compose/?profileUrn={ZEPHYRINE.urn}"
        f"&recipient={ZEPHYRINE.profile_id}",
        # another path
        f"/messaging/thread/?profileUrn={ZEPHYRINE.urn}&recipient={ZEPHYRINE.profile_id}",
    ],
    ids=["mismatch", "repeated", "host", "path"],
)
async def test_a_message_link_with_a_wrong_href_refuses(href: str) -> None:
    bad = f'<a href="{href}">Message</a>'
    ran = await prefill(MessagingSite(ZEPHYRINE, profile_html=_decoy_page(bad)))
    assert_no_keys(ran)
    assert ran.tab.clicks == []


async def test_no_message_link_refuses() -> None:
    ran = await prefill(MessagingSite(ZEPHYRINE, profile_html="<main><h1>Zephyrine</h1></main>"))
    assert_no_keys(ran)
    assert ran.tab.clicks == []


async def test_no_visible_message_link_refuses() -> None:
    html = (
        f'<main><h1>{ZEPHYRINE.name}</h1><div style="display: none">'
        f"{message_control_html(ZEPHYRINE, absolute=False)}</div></main>"
    )
    ran = await prefill(MessagingSite(ZEPHYRINE, profile_html=html))
    assert_no_keys(ran)
    assert ran.tab.clicks == []


async def test_the_first_visible_of_three_links_is_clicked_once() -> None:
    relative = message_control_html(ZEPHYRINE, absolute=False)
    absolute = message_control_html(ZEPHYRINE, absolute=True)
    hidden_first = (
        f"<main><h1>{ZEPHYRINE.name}</h1><div hidden>{relative}</div>"
        f"<div>{relative}</div><div>{absolute}</div></main>"
    )
    ran = await prefill(MessagingSite(ZEPHYRINE, profile_html=hidden_first))
    assert ran.kind is MessageOutcomeKind.PREFILLED
    [clicked] = ran.tab.clicks
    links = [
        e
        for e in ran.tab.document.elements()
        if e.tag == "a" and "compose" in e.attrs.get("href", "")
    ]
    assert clicked is links[1]


async def test_a_button_named_message_is_ignored_and_never_clicked() -> None:
    button = '<button type="button"><span>Message</span></button>'
    ran = await prefill(
        MessagingSite(ZEPHYRINE, profile_html=button + profile_message_controls_html(ZEPHYRINE))
    )
    assert ran.kind is MessageOutcomeKind.PREFILLED
    assert [e.tag for e in ran.tab.clicks] == ["a"]


async def test_a_wall_stops_before_the_click_and_says_so() -> None:
    site = MessagingSite(ZEPHYRINE, land_on="https://www.linkedin.com/checkpoint/challenge/x")
    ran = await prefill(site)
    assert_no_keys(ran)
    assert ran.result.wall is Outcome.CHECKPOINT and ran.tab.clicks == []


async def test_a_profile_that_redirects_elsewhere_refuses() -> None:
    site = MessagingSite(ZEPHYRINE, land_on=f"https://www.linkedin.com/in/{THADDEUS.slug}/")
    ran = await prefill(site)
    assert_no_keys(ran)
    assert ran.result.outcome.reason == "the profile opened somewhere else"
    assert ran.tab.clicks == []


async def test_a_contact_without_a_public_id_is_never_opened() -> None:
    site = MessagingSite(ZEPHYRINE)
    ran = await prefill(site, spec=spec_for(ZEPHYRINE, public_id=None))
    assert ran.kind is MessageOutcomeKind.NOT_TYPED
    assert site.navigations == [] and site.pages == []


def _answer(member: Member, **changes: Any) -> str:
    answer = json.loads(compose_option_answer(member, existing_conversation=7))
    context = answer["data"]["composeNavigationContext"]
    for key, value in changes.items():
        if key == "composeOptionType":
            answer["data"][key] = value
        elif value is None:
            context.pop(key, None)
        else:
            context[key] = value
    return json.dumps(answer)


@pytest.mark.parametrize(
    "bubble",
    [
        # the path's id is someone else's, while the answer names the contact
        Bubble(
            ZEPHYRINE,
            compose_member=THADDEUS,
            compose=compose_option_answer(ZEPHYRINE, existing_conversation=7),
        ),
        Bubble(ZEPHYRINE, compose=_answer(ZEPHYRINE, recipientUrns=[THADDEUS.urn])),
        Bubble(ZEPHYRINE, compose=_answer(ZEPHYRINE, recipientUrns=[ZEPHYRINE.urn, THADDEUS.urn])),
        Bubble(ZEPHYRINE, compose=_answer(ZEPHYRINE, composeOptionType="INMAIL")),
        Bubble(ZEPHYRINE, compose=_answer(ZEPHYRINE, existingConversationUrn=None)),
        Bubble(
            ZEPHYRINE,
            compose=_answer(ZEPHYRINE, composeOptionType="CONNECTION_MESSAGE"),
        ),
        Bubble(ZEPHYRINE, compose=None),
        Bubble(ZEPHYRINE, extra_compose=True),
        Bubble(ZEPHYRINE, compose_status=500),
        Bubble(ZEPHYRINE, compose="not json"),
    ],
    ids=[
        "path_id",
        "recipient",
        "two_recipients",
        "type",
        "reply_without_conversation",
        "new_with_conversation",
        "missing",
        "two_options",
        "status",
        "not_json",
    ],
)
async def test_a_compose_option_that_does_not_name_the_contact_refuses(
    bubble: Bubble, request: pytest.FixtureRequest
) -> None:
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=bubble))
    assert_no_keys(ran)
    assert COMPOSE_REASONS[request.node.callspec.id] in ran.result.outcome.reason
    assert len(ran.tab.clicks) == 1
    # A refusal after the click hands the tab over and leaves the bubble open.
    assert ran.run.handed_over and not ran.tab.is_closed()


COMPOSE_REASONS = {
    "path_id": "names another profile",
    "recipient": "another recipient",
    "two_recipients": "another recipient",
    "type": "type is not one",
    "reply_without_conversation": "names no conversation",
    "new_with_conversation": "names a conversation",
    "missing": "no compose option",
    "two_options": "more than one compose option",
    "status": "not answered",
    "not_json": "not JSON",
}


def _reply_with(header: str) -> str:
    return existing_bubble_html(ZEPHYRINE).replace(
        f'<h2 tabindex="-1"><a href="/in/{ZEPHYRINE.profile_id}/">{ZEPHYRINE.name}</a></h2>',
        f'<h2 tabindex="-1">{header}</h2>',
    )


@pytest.mark.parametrize(
    "html",
    [
        _reply_with(f'<a href="/in/{THADDEUS.profile_id}/">x</a>'),
        _reply_with(
            f'<a href="/in/{ZEPHYRINE.profile_id}/">x</a>'
            f'<a href="/in/{ZEPHYRINE.profile_id}/">y</a>'
        ),
        _reply_with(f'<a href="/in/{ZEPHYRINE.slug}/">by slug</a>'),
        existing_bubble_html(ZEPHYRINE) + "<h2>New message</h2>",
        never_messaged_bubble_html([ZEPHYRINE]),  # the other layout
        existing_bubble_html(ZEPHYRINE, draft="half a draft"),
        existing_bubble_html(ZEPHYRINE, draft=" "),
    ],
    ids=[
        "other_id",
        "two_links",
        "slug_not_id",
        "new_message_heading",
        "other_layout",
        "draft",
        "space_draft",
    ],
)
async def test_a_reply_bubble_that_is_not_the_contacts_refuses(
    html: str, request: pytest.FixtureRequest
) -> None:
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, html=html)))
    assert_no_keys(ran)
    assert REPLY_REASONS[request.node.callspec.id] in ran.result.outcome.reason


REPLY_REASONS = {
    "other_id": "for someone else",
    "two_links": "does not name one person",
    "slug_not_id": "for someone else",
    "new_message_heading": "shows a new-message bubble",
    "other_layout": "conversation's bubble is not open",
    "draft": "not empty",
    "space_draft": "not empty",
}


def _new(chips: list[Member], *, cards: str | None = None, heading: str = "") -> str:
    html = never_messaged_bubble_html(chips)
    if cards is not None:
        html = html.replace("".join(f'<a href="/in/{m.slug}/">{m.name}</a>' for m in chips), cards)
    return html + heading


@pytest.mark.parametrize(
    "html",
    [
        _new([]),
        _new([ZEPHYRINE, THADDEUS]),
        _new([ZEPHYRINE], heading="<h2>New message</h2>"),
        _new(
            [ZEPHYRINE],
            cards=f'<a href="/in/{ZEPHYRINE.slug}/">a</a><a href="/in/{ZEPHYRINE.slug}/">b</a>',
        ),
        _new([ZEPHYRINE], cards=f'<a href="/in/{THADDEUS.slug}/">x</a>'),
        _new([ZEPHYRINE], cards=""),
        _new([ZEPHYRINE]).replace('role="combobox"', 'role="searchbox"'),
        _new([ZEPHYRINE]) + '<div role="dialog" aria-label="Messaging"><p>old</p></div>',
        existing_bubble_html(ZEPHYRINE),  # the other layout
        never_messaged_bubble_html([ZEPHYRINE], draft="x"),
    ],
    ids=[
        "no_chip",
        "two_chips",
        "two_headings",
        "two_cards",
        "card_for_another",
        "no_card",
        "no_field",
        "a_dialog_without_the_heading",
        "other_layout",
        "draft",
    ],
)
async def test_a_new_message_bubble_that_is_not_the_contacts_refuses(
    html: str, request: pytest.FixtureRequest
) -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, None, html=html))
    ran = await prefill(site)
    assert_no_keys(ran)
    assert NEW_REASONS[request.node.callspec.id] in ran.result.outcome.reason


NEW_REASONS = {
    "no_chip": "exactly one recipient",
    "two_chips": "exactly one recipient",
    "two_headings": "or more than one is",
    "two_cards": "for someone else",
    "card_for_another": "for someone else",
    "no_card": "for someone else",
    "no_field": "recipient field is missing",
    "a_dialog_without_the_heading": "shows the conversation's bubble",
    "other_layout": "or more than one is",
    "draft": "not empty",
}


async def test_the_innermost_scope_is_read_not_a_wrapper_around_the_page() -> None:
    """The bubble sits in the page's app wrapper next to a profile link for the contact's
    slug, and has no card of its own: only the innermost scope sees that."""
    html = (
        f'<div class="app"><a href="/in/{ZEPHYRINE.slug}/">profile</a>'
        f"{_new([ZEPHYRINE], cards='')}</div>"
    )
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, None, html=html))
    ran = await prefill(site)
    assert_no_keys(ran)
    assert "for someone else" in ran.result.outcome.reason


async def test_a_reply_composer_outside_its_dialog_refuses() -> None:
    html = existing_bubble_html(ZEPHYRINE)
    dialog_only = html.replace("<form", "<!--").replace("</form>", "-->")
    form = html[html.index("<form") : html.index("</form>") + len("</form>")]
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, html=dialog_only + form)))
    assert_no_keys(ran)
    assert "not in the conversation's bubble" in ran.result.outcome.reason


async def test_the_profiles_own_slug_never_stands_in_for_the_bubbles_card() -> None:
    """The profile around the bubble links the contact's slug; the card names another."""
    profile = profile_message_controls_html(ZEPHYRINE) + f'<a href="/in/{ZEPHYRINE.slug}/">me</a>'
    html = _new([ZEPHYRINE], cards=f'<a href="/in/{THADDEUS.slug}/">x</a>')
    site = MessagingSite(ZEPHYRINE, profile_html=profile, bubble=Bubble(ZEPHYRINE, None, html=html))
    ran = await prefill(site)
    assert_no_keys(ran)


async def test_a_chip_that_does_not_read_the_profiles_name_refuses() -> None:
    html = never_messaged_bubble_html([ZEPHYRINE]).replace(
        f"Remove {ZEPHYRINE.name}", "Remove Somebody Else"
    )
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, None, html=html)))
    assert_no_keys(ran)
    assert ran.result.outcome.reason == "recipient_name_mismatch"


async def test_the_name_check_collapses_whitespace_and_normalizes_to_nfc() -> None:
    """The profile's h1 spells the name decomposed and spaced out; the chip, NFC."""
    zoe = Member(140, "Zoe\u0301", "Nfcson", "Invented")  # Zoé, decomposed
    profile = profile_message_controls_html(zoe).replace(
        f"<h1>{zoe.name}</h1>", "<h1>  Zoe\u0301 \n  Nfcson </h1>"
    )
    html = never_messaged_bubble_html([zoe]).replace(
        "Remove Zoe\u0301 Nfcson", "Remove Zo\u00e9 Nfcson"
    )
    site = MessagingSite(zoe, profile_html=profile, bubble=Bubble(zoe, None, html=html))
    ran = await prefill(site, member=zoe)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result


@pytest.mark.parametrize("h1s", [0, 2])
async def test_zero_or_two_h1s_skip_the_name_check_and_the_card_decides(h1s: int) -> None:
    profile = profile_message_controls_html(ZEPHYRINE).replace(
        f"<h1>{ZEPHYRINE.name}</h1>", "<h1>One</h1><h1>Two</h1>" if h1s == 2 else ""
    )
    html = never_messaged_bubble_html([ZEPHYRINE]).replace(
        f"Remove {ZEPHYRINE.name}", "Remove Somebody Else"
    )
    site = MessagingSite(ZEPHYRINE, profile_html=profile, bubble=Bubble(ZEPHYRINE, None, html=html))
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    # ... and the card still binds the recipient.
    wrong = html.replace(f"/in/{ZEPHYRINE.slug}/", f"/in/{THADDEUS.slug}/")
    site = MessagingSite(
        ZEPHYRINE, profile_html=profile, bubble=Bubble(ZEPHYRINE, None, html=wrong)
    )
    assert_no_keys(await prefill(site))


@pytest.mark.parametrize("hidden", [False, True])
async def test_a_bubble_already_on_the_page_refuses_before_the_click(hidden: bool) -> None:
    """#444: decision 3 is read before the click too, hidden bubbles counted, so a
    leftover bubble costs no click and opens no second bubble."""
    other = existing_bubble_html(THADDEUS)
    if hidden:
        other = f'<div style="display:none">{other}</div>'
    ran = await prefill(MessagingSite(ZEPHYRINE, before=other))
    assert_no_keys(ran)
    assert ran.result.outcome.reason == browser_module.BUBBLE_ALREADY_OPEN
    assert ran.tab.clicks == [] and not ran.run.message_click_attempted
    assert len(ran.tab.composers()) == 1  # no second bubble


async def test_a_minimized_dialog_alone_refuses_before_the_click() -> None:
    dialog = '<div role="dialog" aria-label="Messaging" hidden><p>minimized</p></div>'
    ran = await prefill(MessagingSite(ZEPHYRINE, before=dialog))
    assert_no_keys(ran)
    assert ran.result.outcome.reason == browser_module.BUBBLE_ALREADY_OPEN
    assert ran.tab.clicks == [] and not ran.run.message_click_attempted


@pytest.mark.parametrize("hidden", [False, True])
async def test_a_composer_alone_refuses_before_the_click(hidden: bool) -> None:
    """The composer half of the check before the click: a ``Write a message…`` textbox,
    hidden or not, with no ``Messaging`` dialog around it."""
    style = ' style="display:none"' if hidden else ""
    composer = (
        f'<div{style}><div contenteditable="true" role="textbox" aria-multiline="true"'
        ' aria-label="Write a message…"><p><br></p></div></div>'
    )
    ran = await prefill(MessagingSite(ZEPHYRINE, before=composer))
    assert_no_keys(ran)
    assert ran.result.outcome.reason == browser_module.BUBBLE_ALREADY_OPEN
    assert ran.tab.clicks == [] and not ran.run.message_click_attempted


async def test_a_bubble_check_that_cannot_read_refuses_with_no_click(
    caplog: pytest.LogCaptureFixture,
) -> None:
    site = MessagingSite(ZEPHYRINE)

    async def unreadable(self: BrowserRun, tab: object) -> bool:
        raise RuntimeError("Target crashed <secret markup>")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(BrowserRun, "_bubble_already_open", unreadable)
        with caplog.at_level("WARNING"):
            ran = await prefill(site)
    assert_no_keys(ran)
    assert ran.result.outcome.reason == browser_module.BUBBLE_UNREADABLE
    assert ran.tab.clicks == [] and not ran.run.message_click_attempted
    assert "could not be read (RuntimeError)" in caplog.text
    assert "secret markup" not in caplog.text


async def test_an_unrelated_textbox_and_dialog_do_not_stop_the_click() -> None:
    """The check is narrow: a comment box and the Contact info dialog are not a message
    bubble, so the prefill clicks and types."""
    unrelated = (
        '<div contenteditable="true" role="textbox" aria-label="Add a comment…"><p><br></p>'
        '</div><div role="dialog" aria-label="Contact info" hidden><p>overlay</p></div>'
    )
    ran = await prefill(MessagingSite(ZEPHYRINE, before=unrelated))
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert len(ran.tab.clicks) == 1


def test_the_bubble_check_words_are_pinned() -> None:
    assert browser_module.BUBBLE_UNREADABLE == (
        "whether a message bubble is open could not be read"
    )


def test_the_bubble_already_open_words_are_pinned() -> None:
    assert browser_module.BUBBLE_ALREADY_OPEN == (
        "a message bubble is already open in Chrome, minimized ones included;"
        " close it, then try again"
    )


@pytest.mark.parametrize("hidden", [False, True])
async def test_another_composer_arriving_with_the_click_refuses(hidden: bool) -> None:
    other = existing_bubble_html(THADDEUS)
    if hidden:
        other = f'<div style="display:none">{other}</div>'
    bubble = Bubble(ZEPHYRINE, html=other + existing_bubble_html(ZEPHYRINE))
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=bubble))
    assert_no_keys(ran)
    assert len(ran.tab.clicks) == 1
    assert "another message composer is on the page" in ran.result.outcome.reason


async def test_two_messaging_dialogs_after_the_click_refuse_without_a_second_composer() -> None:
    dialog = '<div role="dialog" aria-label="Messaging" hidden><p>minimized</p></div>'
    rooted = (
        f'<div role="dialog" aria-label="Messaging">{never_messaged_bubble_html([ZEPHYRINE])}</div>'
    )
    for bubble in (
        Bubble(ZEPHYRINE, html=dialog + existing_bubble_html(ZEPHYRINE)),
        Bubble(ZEPHYRINE, None, html=dialog + rooted),
    ):
        ran = await prefill(MessagingSite(ZEPHYRINE, bubble=bubble))
        assert_no_keys(ran)
        assert len(ran.tab.clicks) == 1
        assert "another message bubble is on the page" in ran.result.outcome.reason


async def test_a_url_change_before_the_first_key_types_nothing() -> None:
    site = MessagingSite(ZEPHYRINE)

    def move(tab: MessagingTab) -> None:
        if tab.clicks:
            tab._url = "https://www.linkedin.com/feed/"

    async def go() -> Ran:
        ran_holder: list[Ran] = []
        return await prefill(site) if ran_holder else await _with_hook(site, move)

    ran = await go()
    assert_no_keys(ran)


async def _with_hook(site: MessagingSite, hook: Callable[[MessagingTab], None]) -> Ran:
    original = site.open_bubble

    def opened(tab: MessagingTab, bubble: Bubble) -> None:
        original(tab, bubble)
        tab.on_read(hook)

    site.open_bubble = opened  # type: ignore[method-assign]
    return await prefill(site)


async def test_a_lost_tab_before_the_first_key_types_nothing() -> None:
    def close(tab: MessagingTab) -> None:
        tab._closed = True

    ran = await _with_hook(MessagingSite(ZEPHYRINE), close)
    assert_no_keys(ran)


async def test_a_cancel_before_the_first_key_types_nothing() -> None:
    async def yes() -> bool:
        return True

    ran = await prefill(MessagingSite(ZEPHYRINE), cancelled=yes)
    assert_no_keys(ran)
    assert ran.tab.clicks == []


def _raw_step(chunk: str) -> TypeStep:
    """A step the constructor would refuse: built around it, for the method's own check."""
    step = object.__new__(TypeStep)
    for name, value in (
        ("chunk", chunk),
        ("delay_before_s", 0.1),
        ("newline", False),
        ("line", ""),
        ("offset", 0),
    ):
        object.__setattr__(step, name, value)
    return step


@pytest.mark.parametrize("chunk", ["\n", "\r", "a\x1b", "\x85"])
async def test_a_plan_with_a_control_character_types_nothing(chunk: str) -> None:
    plan = (*plan_typing("Hi", 1), _raw_step(chunk))
    ran = await prefill(MessagingSite(ZEPHYRINE), "Hi", plan=plan)
    assert_no_keys(ran)


# --- stops after the first key ---------------------------------------------------------


def _after(k: int, change: Callable[[MessagingTab], None]) -> MessagingSite:
    site = MessagingSite(ZEPHYRINE)
    site.after_key[k] = change
    return site


def _move_focus(tab: MessagingTab) -> None:
    tab.focused = next(e for e in tab.document.elements() if e.tag == "button")


def _second_composer(tab: MessagingTab) -> None:
    parse_into(tab.document, existing_bubble_html(THADDEUS))


def _second_hidden_dialog(tab: MessagingTab) -> None:
    parse_into(tab.document, '<div role="dialog" aria-label="Messaging" hidden></div>')


def _header_changes(tab: MessagingTab) -> None:
    for element in tab.document.elements():
        if element.tag == "a" and element.attrs.get("href") == f"/in/{ZEPHYRINE.profile_id}/":
            element.attrs["href"] = f"/in/{THADDEUS.profile_id}/"


def _text_diverges(tab: MessagingTab) -> None:
    tab.typed += "Z"
    tab._render_composer()


def _url_changes(tab: MessagingTab) -> None:
    tab._url = "https://www.linkedin.com/feed/"


@pytest.mark.parametrize(
    "change",
    [
        _move_focus,
        _second_composer,
        _second_hidden_dialog,
        _header_changes,
        _text_diverges,
        _url_changes,
    ],
)
async def test_a_failed_check_after_key_k_stops_with_no_further_key(
    change: Callable[[MessagingTab], None],
) -> None:
    ran = await prefill(_after(3, change))
    assert ran.kind is MessageOutcomeKind.PARTIALLY_TYPED, ran.result
    assert len(ran.tab.attempts) == 3
    assert ran.result.outcome.typed_chars == 3
    assert ran.result.typing_started_at is not None
    assert ran.run.handed_over and not ran.tab.is_closed()


async def test_a_new_message_chip_that_changes_mid_type_stops() -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, None))

    def chip(tab: MessagingTab) -> None:
        parse_into(
            next(e for e in tab.document.elements() if e.tag == "section"),
            f'<button aria-label="Remove {THADDEUS.name}" type="button"></button>',
        )

    site.after_key[2] = chip
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PARTIALLY_TYPED
    assert len(ran.tab.attempts) == 2


async def test_a_tab_lost_mid_type_is_unknown() -> None:
    def close(tab: MessagingTab) -> None:
        tab._closed = True

    ran = await prefill(_after(4, close))
    assert ran.kind is MessageOutcomeKind.UNKNOWN
    assert len(ran.tab.attempts) == 4


async def test_a_cancel_mid_type_is_partially_typed_with_no_further_key() -> None:
    site = MessagingSite(ZEPHYRINE)

    async def cancel() -> bool:
        return len(site.tab.attempts) >= 5 if site.pages else False

    ran = await prefill(site, cancelled=cancel)
    assert ran.kind is MessageOutcomeKind.PARTIALLY_TYPED
    assert len(ran.tab.attempts) == 5


async def test_a_read_that_raises_after_the_first_key_is_never_not_typed() -> None:
    def boom(tab: MessagingTab) -> None:
        if len(tab.attempts) >= 2:
            raise RuntimeError("the page went strange")

    ran = await _with_hook(MessagingSite(ZEPHYRINE), boom)
    assert ran.kind in (MessageOutcomeKind.PARTIALLY_TYPED, MessageOutcomeKind.UNKNOWN)
    assert len(ran.tab.attempts) == 2


async def test_a_key_call_that_raises_is_unknown_and_counts_as_attempted() -> None:
    site = MessagingSite(ZEPHYRINE)
    original = MessagingTab.landed

    async def landed(self: MessagingTab, kind: str, value: str) -> None:
        if len(self.keys) == 1:
            raise RuntimeError("the key call failed")
        await original(self, kind, value)

    MessagingTab.landed = landed  # type: ignore[method-assign]
    try:
        ran = await prefill(site)
    finally:
        MessagingTab.landed = original  # type: ignore[method-assign]
    assert ran.kind is MessageOutcomeKind.UNKNOWN
    assert ran.run.keys_sent == 2 and len(ran.tab.attempts) == 2


async def test_a_final_text_that_is_not_the_body_is_partially_typed() -> None:
    site = MessagingSite(ZEPHYRINE)

    def lose_last(tab: MessagingTab) -> None:
        tab.typed = tab.typed[:-1]
        tab._render_composer()

    site.after_key[len(plan_typing(BODY, 3))] = lose_last
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PARTIALLY_TYPED
    assert "after typing" in ran.result.outcome.reason


# --- the browser methods on their own ---------------------------------------------------


async def test_after_the_click_the_run_never_opens_navigates_scrolls_or_observes() -> None:
    from netkeeper.linkedin.observe import ResponseMatch, ResponseRule
    from netkeeper.linkedin.pacing import ScrollPlan

    site = MessagingSite(ZEPHYRINE)
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
        click = await run.click_message(f"/in/{ZEPHYRINE.slug}/", ZEPHYRINE.profile_id, pause_s=0)
        assert click.clicked
        with pytest.raises(BrowserUnavailable):
            await run.goto("https://www.linkedin.com/feed/")
        with pytest.raises(BrowserUnavailable):
            await run.ensure_page()
        with pytest.raises(BrowserUnavailable):
            await run.scroll(ScrollPlan((), 0.0))
        with pytest.raises(BrowserUnavailable):
            await run.observe(
                ResponseMatch("https://www.linkedin.com", (ResponseRule("GET", "/x"),))
            )
        again = await run.click_message(f"/in/{ZEPHYRINE.slug}/", ZEPHYRINE.profile_id, pause_s=0)
        assert not again.clicked and not again.attempted
        await run.hand_over()
    assert len(site.tab.clicks) == 1 and site.new_page_calls == 1
    assert site.tab.goto_after_click == []
    assert not site.tab.is_closed()  # the provider's exit closed nothing


@pytest.mark.parametrize("landed", [True, False])
async def test_any_navigation_after_the_click_or_the_hand_over_raises_and_leaves_the_tab(
    landed: bool,
) -> None:
    """#456: once the click was attempted, landed or not, and again after the hand-over,
    every way to navigate or reopen raises, and the tab stays on the profile, open."""
    site = MessagingSite(ZEPHYRINE)
    if not landed:
        site.click_error = RuntimeError("the click failed")
    provider, _ = fake_provider(site)
    profile = f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/"
    async with provider.run() as run:
        await run.goto(profile)
        click = await run.click_message(f"/in/{ZEPHYRINE.slug}/", ZEPHYRINE.profile_id, pause_s=0)
        assert click.attempted and click.clicked is landed
        for attempt in range(2):
            with pytest.raises(BrowserUnavailable):
                await run.goto("https://www.linkedin.com/feed/")
            with pytest.raises(BrowserUnavailable):
                await run.ensure_page()
            if attempt == 0:
                await run.hand_over()
    assert site.navigations == [profile] and site.tab.goto_after_click == []
    assert site.tab.url == profile and not site.tab.is_closed()
    assert site.new_page_calls == 1


async def test_the_tab_comes_to_the_front_once() -> None:
    site = MessagingSite(ZEPHYRINE)
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        await run.bring_tab_forward()
        with pytest.raises(RuntimeError):
            await run.bring_tab_forward()
    assert site.tab.fronted == 1


async def test_a_run_that_did_not_hand_over_closes_its_tab() -> None:
    site = MessagingSite(ZEPHYRINE)
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
    assert site.tab.is_closed()


async def test_typing_without_a_clicked_message_types_nothing() -> None:
    site = MessagingSite(ZEPHYRINE)
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
        result = await run.type_into_composer(
            plan_typing("Hi", 1),
            BubbleRecipient(BubbleLayout.EXISTING, ZEPHYRINE.profile_id, ZEPHYRINE.slug),
            clock=lambda: T0,
        )
    assert result.end is TypingEnd.NOT_TYPED and site.tab.attempts == []


@pytest.mark.parametrize(
    ("hrefs", "ok"),
    [
        ([compose_href(ZEPHYRINE)] * 3, True),
        ([compose_href(ZEPHYRINE), compose_href(ZEPHYRINE, absolute=True)], True),
        ([], False),
        ([None], False),
        ([compose_href(ZEPHYRINE), compose_href(THADDEUS)], False),
        ([compose_href(ZEPHYRINE) + "#x"], False),
        ([compose_href(ZEPHYRINE).replace("https", "http")], True),  # relative: no scheme
        ([compose_href(ZEPHYRINE, absolute=True).replace("https:", "http:")], False),
        ([compose_href(ZEPHYRINE) + "&interop=msgOverlay"], False),  # repeated
    ],
)
def test_the_message_click_rule(hrefs: list[str | None], ok: bool) -> None:
    assert (message_control_refusal(hrefs, ZEPHYRINE.profile_id) is None) is ok


def test_the_prefill_literals_are_pinned() -> None:
    """ADR 0007's role, name, and selector constants, against literals written out here."""
    b = browser_module
    assert (b.MESSAGE_CONTROL_ROLE, b.MESSAGE_CONTROL_NAME) == ("link", "Message")
    assert (b.COMPOSER_ROLE, b.COMPOSER_NAME) == ("textbox", "Write a message…")
    assert (b.BUBBLE_ROLE, b.BUBBLE_NAME) == ("dialog", "Messaging")
    assert (b.NEW_MESSAGE_ROLE, b.NEW_MESSAGE_NAME) == ("heading", "New message")
    assert (b.RECIPIENTS_FIELD_ROLE, b.RECIPIENTS_FIELD_NAME) == (
        "combobox",
        "Enter message recipients",
    )
    assert (b.CHIP_ROLE, b.CHIP_NAME_PREFIX, b.CHIP_NAME.pattern) == (
        "button",
        "Remove ",
        "^Remove ",
    )
    assert b.MESSAGE_COMPOSE_PATH == "/messaging/compose/"
    assert b.MESSAGE_COMPOSE_HOST == "www.linkedin.com"
    assert b.BUBBLE_HEADER_LINK == "header h2 a"
    assert b.FOCUSED == ":focus"
    assert b.MESSAGE_PRESS_MS == 90.0


def test_only_one_printable_ascii_character_other_than_a_space_is_keyed() -> None:
    keyed = browser_module._keyed
    assert keyed("a") and keyed("~") and keyed("!")
    for chunk in (" ", "\n", "\r", "\t", "é", "ab", "", "\x7f", "👋"):
        assert not keyed(chunk), chunk


def test_unused_owner_import_keeps_the_fixture_cast_honest() -> None:
    # The owner is never a prefill's recipient; the fixtures name it only as the mailbox.
    assert OWNER.urn != ZEPHYRINE.urn
    assert isinstance(Element("p", {}), Element)


@pytest.mark.parametrize(
    "inside",
    ["<p><span></span><br></p>", "<div><br></div>", "<p><br></p><ul><li></li></ul>"],
    ids=["span_in_p", "div", "list"],
)
async def test_a_composer_the_text_rule_cannot_read_types_nothing(inside: str) -> None:
    html = existing_bubble_html(ZEPHYRINE).replace("<p><br></p>", inside, 1)
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, html=html)))
    assert_no_keys(ran)


@pytest.mark.parametrize(
    ("paragraphs", "text"),
    [
        (["\n"], ""),
        ([""], ""),
        (["a\u00a0"], "a "),
        (["a\nb\n"], "a\nb"),
        (["a", "\n", "b"], "a\n\nb"),
        (["a\n\n"], "a\n"),
    ],
)
def test_the_composer_text_rule(paragraphs: list[str], text: str) -> None:
    from netkeeper.linkedin.messaging import composer_text

    assert composer_text(paragraphs) == text


# --- the #434 safety review -------------------------------------------------------------


@pytest.mark.parametrize(
    "inside",
    ["x", "x<p><br></p>", "<p><br></p>x", "x<br>"],
    ids=["bare_text", "text_before_p", "text_after_p", "chrome_select_all"],
)
async def test_a_draft_outside_any_paragraph_is_unreadable_not_empty(inside: str) -> None:
    """Chrome leaves bare text after select-all, Backspace, and typing: never "empty"."""
    html = existing_bubble_html(ZEPHYRINE).replace("<p><br></p>", inside, 1)
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, html=html)))
    assert_no_keys(ran)
    assert ran.result.outcome.reason == "the composer is not empty"


async def test_bare_text_appearing_mid_type_stops_typing() -> None:
    def bare(tab: MessagingTab) -> None:
        assert tab.composer is not None
        tab.composer.children.insert(0, "x")

    ran = await prefill(_after(3, bare))
    assert ran.kind is MessageOutcomeKind.PARTIALLY_TYPED
    assert len(ran.tab.attempts) == 3


async def test_focus_is_the_last_read_before_every_key() -> None:
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, None)))
    assert ran.kind is MessageOutcomeKind.PREFILLED
    for key in ran.tab.keys:
        last = ran.tab.read_log[key.log_before - 1]
        assert last.startswith("count") and ".locator(:focus)" in last, last


async def test_a_no_break_space_in_the_body_is_typed_and_read_back() -> None:
    body = "Hi\u00a0Zephyrine, see you"
    ran = await prefill(MessagingSite(ZEPHYRINE), body)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result


async def test_a_click_that_raises_hands_the_tab_over() -> None:
    site = MessagingSite(ZEPHYRINE)
    site.click_error = RuntimeError("the click failed")
    ran = await prefill(site)
    assert_no_keys(ran)
    assert len(ran.tab.clicks) == 1
    assert ran.run.handed_over and not ran.tab.is_closed()


async def test_a_cancel_during_the_click_hands_the_tab_over() -> None:
    import asyncio

    site = MessagingSite(ZEPHYRINE)
    site.click_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await prefill(site)
    assert len(site.tab.clicks) == 1
    assert not site.tab.is_closed() and site.tab.close_calls == 0


async def test_close_never_closes_the_tab_once_the_click_was_attempted() -> None:
    site = MessagingSite(ZEPHYRINE)
    site.click_error = RuntimeError("the click failed")
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
        click = await run.click_message(f"/in/{ZEPHYRINE.slug}/", ZEPHYRINE.profile_id, pause_s=0)
        assert click.attempted and not click.clicked
    assert not site.tab.is_closed() and site.tab.close_calls == 0


async def test_the_conversation_is_the_one_whose_thread_id_the_option_named() -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, other_thread=8))
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED
    assert ran.result.outcome.conversation_urn == conversation_urn(7)


async def test_no_thread_request_reports_no_conversation_never_the_other_form() -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, thread=False, other_thread=8))
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED
    assert ran.result.outcome.conversation_urn is None


async def test_a_reply_page_with_a_recipient_field_refuses() -> None:
    html = existing_bubble_html(ZEPHYRINE) + (
        '<label for="f">Enter message recipients</label><input id="f" role="combobox">'
    )
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, html=html)))
    assert_no_keys(ran)
    assert "shows a new-message bubble" in ran.result.outcome.reason


@pytest.mark.parametrize(
    "href",
    [
        f"https://evil.example/in/{ZEPHYRINE.profile_id}/",
        f"/in/{ZEPHYRINE.profile_id.lower()}/",
    ],
    ids=["other_host", "id_case"],
)
async def test_a_reply_header_on_another_host_or_another_ids_case_refuses(href: str) -> None:
    html = _reply_with(f'<a href="{href}">x</a>')
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, html=html)))
    assert_no_keys(ran)
    assert "for someone else" in ran.result.outcome.reason


async def test_a_slug_is_compared_without_case() -> None:
    html = never_messaged_bubble_html([ZEPHYRINE]).replace(
        f"/in/{ZEPHYRINE.slug}/", f"/in/{ZEPHYRINE.slug.upper()}/"
    )
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, None, html=html)))
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result


@pytest.mark.parametrize("layout", [7, None])
async def test_a_bubble_drawn_after_the_compose_option_is_waited_for(layout: int | None) -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, layout, draw_after_reads=30))
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert ran.tab.attempts and ran.result.typing_started_at is not None


# --- the #434 round-2 review ------------------------------------------------------------


async def test_focus_that_lands_during_the_wait_lets_the_run_type() -> None:
    """The page focuses the composer a moment later, after the seam's one focus() call was
    ignored: the full pass, polled after the seam, sees it."""
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, focus_after_reads=40))
    site.focus_ignored = True
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result


def test_the_composer_wait_is_pinned() -> None:
    assert browser_module.COMPOSER_WAIT_S == 5


async def test_a_late_second_compose_option_refuses_the_authorizing_pass() -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, late_compose_after_reads=1))
    ran = await prefill(site)
    assert_no_keys(ran)
    assert ran.result.outcome.reason == "another_compose"


ANN = Member(150, "Ann", "Example", "Invented")


def _pronoun_site(chip_name: str) -> MessagingSite:
    profile = profile_message_controls_html(ANN).replace(
        f"<h1>{ANN.name}</h1>", '<h1>Ann Example <span aria-hidden="true">(He/Him)</span></h1>'
    )
    html = never_messaged_bubble_html([ANN]).replace(f"Remove {ANN.name}", chip_name)
    return MessagingSite(ANN, profile_html=profile, bubble=Bubble(ANN, None, html=html))


async def test_aria_hidden_pronouns_in_the_h1_are_not_part_of_its_name() -> None:
    ran = await prefill(_pronoun_site("Remove Ann Example"), member=ANN)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert ran.result.outcome.recipient_name_checked is True


async def test_a_chip_naming_someone_else_beside_a_pronoun_h1_refuses() -> None:
    ran = await prefill(_pronoun_site("Remove Bob Other"), member=ANN)
    assert_no_keys(ran)
    assert ran.result.outcome.reason == "recipient_name_mismatch"


async def test_a_chip_whose_name_the_matcher_cannot_confirm_is_unreadable() -> None:
    html = (
        never_messaged_bubble_html([ZEPHYRINE]).replace(
            f'aria-label="Remove {ZEPHYRINE.name}"', 'aria-labelledby="who"'
        )
        + f'<span id="who">Remove {ZEPHYRINE.name}</span>'
    )
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, None, html=html)))
    assert_no_keys(ran)
    assert ran.result.outcome.reason == "recipient_name_unreadable"


@pytest.mark.parametrize(
    "inside",
    ["<p><br></p> <p><br></p>", " <p><br></p>", "<p><br></p>\n"],
    ids=["between", "before", "after"],
)
async def test_whitespace_outside_the_paragraphs_is_unreadable(inside: str) -> None:
    html = existing_bubble_html(ZEPHYRINE).replace("<p><br></p>", inside, 1)
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, html=html)))
    assert_no_keys(ran)
    assert ran.result.outcome.reason == "the composer is not empty"


@pytest.mark.parametrize(("h1", "checked"), [("one", True), ("none", False), ("two", False)])
async def test_the_outcome_says_whether_the_chip_was_checked(h1: str, checked: bool) -> None:
    profile = profile_message_controls_html(ZEPHYRINE)
    if h1 != "one":
        profile = profile.replace(
            f"<h1>{ZEPHYRINE.name}</h1>", "" if h1 == "none" else "<h1>A</h1><h1>B</h1>"
        )
    site = MessagingSite(ZEPHYRINE, profile_html=profile, bubble=Bubble(ZEPHYRINE, None))
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED
    assert ran.result.outcome.recipient_name_checked is checked


async def test_an_existing_conversation_has_no_chip_to_check() -> None:
    ran = await prefill(MessagingSite(ZEPHYRINE))
    assert ran.result.outcome.recipient_name_checked is None


async def test_a_chip_name_the_matcher_does_not_confirm_is_a_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``aria-labelledby``, an unconfirmed chip name is ``recipient_name_mismatch``
    (ADR 0007, fd571b1). The fake page always confirms its own names, so the matcher's
    answer for the chip is stood in for here."""
    original = browser_module._accessible_name

    async def unconfirmed_chip(page: Any, element: Any, role: str, **kwargs: Any) -> str | None:
        if role == browser_module.CHIP_ROLE:
            return None
        return await original(page, element, role, **kwargs)

    monkeypatch.setattr(browser_module, "_accessible_name", unconfirmed_chip)
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, None)))
    assert_no_keys(ran)
    assert ran.result.outcome.reason == "recipient_name_mismatch"


# --- decision 5, option B: one Locator.focus() on the verified composer ------------------


async def test_an_unfocused_composer_is_focused_once_then_typed() -> None:
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, focus_composer=False)))
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert ran.tab.focus_calls == [ran.tab.composer]  # one focus(), on the composer
    assert [e.tag for e in ran.tab.clicks] == ["a"]  # the Message click, never a click into it
    assert browser_module.FOCUS_INPUT_AUTHORIZED is True


async def test_a_composer_that_already_holds_focus_is_not_focused_again() -> None:
    ran = await prefill(MessagingSite(ZEPHYRINE))
    assert ran.kind is MessageOutcomeKind.PREFILLED
    assert ran.tab.focus_calls == []


@pytest.mark.parametrize("how", ["ignored", "raises"])
async def test_a_focus_that_does_not_take_types_nothing_after_the_whole_wait(how: str) -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, focus_composer=False))
    if how == "ignored":
        site.focus_ignored = True
    else:
        site.focus_error = RuntimeError("focus failed")
    ran = await prefill(site)
    assert_no_keys(ran)
    assert ran.result.outcome.reason == "the composer does not hold focus"
    assert len(ran.tab.focus_calls) == 1  # never retried
    waits = [s for s, _, _ in ran.sleeps if s == browser_module.COMPOSER_WAIT_S / 50]
    assert len(waits) == browser_module.COMPOSER_WAIT_POLLS


@pytest.mark.parametrize(
    "html",
    [
        existing_bubble_html(ZEPHYRINE, draft="x"),
        _reply_with(f'<a href="/in/{THADDEUS.profile_id}/">x</a>'),
        existing_bubble_html(ZEPHYRINE) + existing_bubble_html(THADDEUS),
    ],
    ids=["draft", "wrong_recipient", "two_composers"],
)
async def test_a_bubble_that_fails_a_check_never_reaches_the_focus_call(html: str) -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, html=html, focus_composer=False))
    ran = await prefill(site)
    assert_no_keys(ran)
    assert ran.tab.focus_calls == []


async def test_the_seam_runs_once_only_after_a_pass_without_focus_then_full_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0007: the seam follows a pass in which every check but focus held; every pass
    after it includes focus."""
    passes: list[tuple[bool, str | None]] = []
    original = BrowserRun._composer_refusal

    async def recorded(self: BrowserRun, *args: Any, **kwargs: Any) -> str | None:
        refusal = await original(self, *args, **kwargs)
        passes.append((kwargs.get("focus", True), refusal))
        return refusal

    seams: list[int] = []
    seam = BrowserRun._focus_seam

    async def counted(self: BrowserRun, tab: Any, composer: Any) -> None:
        assert passes and passes[-1] == (False, None), passes[-3:]
        seams.append(len(passes))
        await seam(self, tab, composer)

    monkeypatch.setattr(BrowserRun, "_composer_refusal", recorded)
    monkeypatch.setattr(BrowserRun, "_focus_seam", counted)
    bubble = Bubble(ZEPHYRINE, focus_composer=False, draw_after_reads=20)
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=bubble))
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert len(seams) == 1 and len(ran.tab.focus_calls) == 1
    before, after = passes[: seams[0]], passes[seams[0] :]
    assert any(refusal is not None for _, refusal in before)  # it waited for the bubble
    assert all(focus is False for focus, _ in before)
    assert all(focus is True for focus, _ in after)


async def test_the_seam_raises_on_a_second_call() -> None:
    site = MessagingSite(ZEPHYRINE)
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
        tab = site.tab
        composer = tab.get_by_role("link", name="Message", exact=True)
        await run._focus_seam(tab, composer.first)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="at most once"):
            await run._focus_seam(tab, composer.first)  # type: ignore[arg-type]


async def test_option_a_stays_one_switch_away(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_module, "FOCUS_INPUT_AUTHORIZED", False)
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, focus_composer=False)))
    assert_no_keys(ran)
    assert ran.tab.focus_calls == []


# --- #434 check at 9a0e169 ---------------------------------------------------------------


async def test_a_second_compose_option_before_the_seam_means_no_focus_call() -> None:
    """B13: the pass before the seam also refuses a later compose option, so the one
    focus() is never spent on a bubble the run won't type into."""
    bubble = Bubble(ZEPHYRINE, focus_composer=False, late_compose_after_reads=1)
    ran = await prefill(MessagingSite(ZEPHYRINE, bubble=bubble))
    assert_no_keys(ran)
    assert ran.result.outcome.reason == "another_compose"
    assert ran.tab.focus_calls == []


async def test_a_compose_option_arriving_mid_type_stops_with_no_further_key() -> None:
    def another(tab: MessagingTab) -> None:
        tab.emit(
            compose_option_url(ZEPHYRINE), compose_option_answer(ZEPHYRINE, existing_conversation=7)
        )

    ran = await prefill(_after(3, another))
    assert ran.kind is MessageOutcomeKind.PARTIALLY_TYPED
    assert ran.result.outcome.reason == "another_compose"
    assert len(ran.tab.attempts) == 3


async def test_focus_elsewhere_on_the_page_is_not_the_composer_holding_it() -> None:
    """B11: the seam asks whether the composer holds focus, not whether anything does."""
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, focus_composer=False))
    original = site.open_bubble

    def opened(tab: MessagingTab, bubble: Bubble) -> None:
        original(tab, bubble)
        tab.focused = next(e for e in tab.document.elements() if e.tag == "button")

    site.open_bubble = opened  # type: ignore[method-assign]
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert ran.tab.focus_calls == [ran.tab.composer]


async def test_a_failed_focus_still_uses_the_one_call() -> None:
    """B12: the seam is spent before the attempt, and stays spent after a failure."""
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, focus_composer=False))
    site.focus_error = RuntimeError("focus failed")
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
        click = await run.click_message(f"/in/{ZEPHYRINE.slug}/", ZEPHYRINE.profile_id, pause_s=0)
        assert click.clicked
        tab = site.tab
        composer = tab.get_by_role(
            "textbox", name=browser_module.COMPOSER_NAME, exact=True, include_hidden=True
        )
        await run._focus_seam(tab, composer)  # type: ignore[arg-type]
        assert len(tab.focus_calls) == 1 and tab.focused is not tab.composer
        with pytest.raises(RuntimeError, match="at most once"):
            await run._focus_seam(tab, composer)  # type: ignore[arg-type]
        assert len(tab.focus_calls) == 1
        await run.hand_over()
