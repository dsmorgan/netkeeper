"""Which Message control the prefill clicks, and what a failed click records (#444).

CP8 showed the one Message click raising on every prefill after the first. Against
an isolated Chrome, Playwright's click raises the same way when the first visible
control in document order is a fixed copy outside the viewport ("element is outside of
the viewport"), or a control with something over its click point ("intercepts pointer
events"), and keeps waiting on one that moves ("element is not stable"). These tests
pin the choice (:func:`choose_message_target`), the failure categories
(:func:`classify_click_failure`), and the whole click against the fake page, whose
invented ``data-box`` boxes stand in for the page's layout.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

import pytest
from messaging_dom import Bubble, MessagingSite
from messaging_pages import (
    THADDEUS,
    ZEPHYRINE,
    Member,
    compose_href,
    existing_bubble_html,
    message_control_html,
)
from test_prefill_page import assert_no_keys, prefill

from netkeeper.linkedin import browser as browser_module
from netkeeper.linkedin.browser import (
    GEOMETRY_TIMEOUT_S,
    HIT_TOLERANCE_PX,
    MESSAGE_MAX_CANDIDATES,
    MESSAGE_MAX_LINKS,
    MESSAGE_NOT_ON_SCREEN,
    MESSAGE_TOP_CARD,
    ClickFailure,
    ClickGeometry,
    ClickTarget,
    _click_point,
    choose_message_target,
    classify_click_failure,
)
from netkeeper.linkedin.messaging import MessageOutcomeKind

VIEWPORT = (1280.0, 800.0)


def box(x: float, y: float, w: float = 100.0, h: float = 32.0) -> dict[str, float]:
    return {"x": x, "y": y, "width": w, "height": h}


def geometry(*hits: Mapping[str, float] | None) -> ClickGeometry:
    return ClickGeometry(VIEWPORT, hits)


# --- the pins ------------------------------------------------------------------------------


def test_the_choice_constants_are_pinned() -> None:
    assert MESSAGE_TOP_CARD == "xpath=following::a"
    assert MESSAGE_MAX_CANDIDATES == 8
    assert MESSAGE_MAX_LINKS == 32
    assert GEOMETRY_TIMEOUT_S == 3.0
    assert HIT_TOLERANCE_PX == 1.0
    assert MESSAGE_NOT_ON_SCREEN == (
        "no Message control is on screen with nothing over it; close or move what covers it"
    )
    assert [c.value for c in ClickFailure] == [
        "intercepted",
        "outside_viewport",
        "not_visible",
        "not_stable",
        "detached",
        "timeout",
        "other",
    ]
    assert [t.value for t in ClickTarget] == [
        "top_card",
        "on_screen",
        "top_card_off_screen",
        "unchecked",
    ]


# --- classify_click_failure ------------------------------------------------------------------


def _playwright(*log: str, resolved: bool = True) -> TimeoutError:
    """A click timeout shaped like Playwright's: the message, then its call log."""
    lines = [
        "Locator.click: Timeout 10000ms exceeded.",
        "Call log:",
        '  - waiting for get_by_role("link", name="Message", exact=True)',
    ]
    if resolved:
        lines.append('  - locator resolved to <a href="/messaging/compose/?x">…</a>')
    lines += ["  - attempting click action", *(f"    - {line}" for line in log)]
    return TimeoutError("\n".join(lines))


@pytest.mark.parametrize(
    ("log", "category"),
    [
        (["<div>…</div> intercepts pointer events"], ClickFailure.INTERCEPTED),
        (["element is outside of the viewport"], ClickFailure.OUTSIDE_VIEWPORT),
        (["element is not visible"], ClickFailure.NOT_VISIBLE),
        (["element is not stable"], ClickFailure.NOT_STABLE),
        (["element was detached from the DOM, retrying"], ClickFailure.DETACHED),
        (["waiting for element to be visible, enabled and stable"], ClickFailure.TIMEOUT),
        # The last marker wins: the state the click was waiting on when it gave up.
        (
            [
                "<html>…</html> intercepts pointer events",
                "retrying click action",
                "element is outside of the viewport",
            ],
            ClickFailure.OUTSIDE_VIEWPORT,
        ),
        (
            ["element is outside of the viewport", "<div>…</div> intercepts pointer events"],
            ClickFailure.INTERCEPTED,
        ),
    ],
)
def test_a_failed_click_is_classified_by_its_last_actionability_line(
    log: list[str], category: ClickFailure
) -> None:
    assert classify_click_failure(_playwright(*log)) is category


def test_a_timeout_whose_locator_never_resolved_is_detached() -> None:
    assert classify_click_failure(_playwright(resolved=False)) is ClickFailure.DETACHED


def test_an_error_that_isnt_a_timeout_is_other_or_its_marker() -> None:
    assert classify_click_failure(RuntimeError("the click failed")) is ClickFailure.OTHER
    error = RuntimeError("Element is not attached to the DOM")
    assert classify_click_failure(error) is ClickFailure.DETACHED


# --- choose_message_target -------------------------------------------------------------------


def test_the_top_card_on_screen_and_clear_is_chosen_over_an_earlier_control() -> None:
    boxes = [box(10, 300), box(10, 400)]
    choice = choose_message_target(boxes, 1, geometry(box(10, 300), box(10, 400)))
    assert choice == (1, ClickTarget.TOP_CARD)


def test_a_sticky_copy_off_screen_is_passed_over_for_the_top_card() -> None:
    # The fixed copy first in document order, slid above the viewport.
    boxes = [box(0, -60, 1280, 60), box(10, 400)]
    choice = choose_message_target(boxes, 1, geometry(None, box(10, 400)))
    assert choice == (1, ClickTarget.TOP_CARD)
    # Without a top card, the first control on screen and clear.
    assert choose_message_target(boxes, None, geometry(None, box(10, 400))) == (
        1,
        ClickTarget.ON_SCREEN,
    )


def test_a_covered_top_card_gives_way_to_another_control_on_screen() -> None:
    boxes = [box(10, 400), box(10, 600)]
    # The hit at the top card's center is in no Message link: something covers it.
    choice = choose_message_target(boxes, 0, geometry(None, box(10, 600)))
    assert choice == (1, ClickTarget.ON_SCREEN)


def test_a_covered_top_card_with_nothing_else_refuses() -> None:
    boxes = [box(10, 400)]
    assert choose_message_target(boxes, 0, geometry(None)) is None
    # Another copy of the link over this one covers it too: Playwright would refuse.
    assert choose_message_target(boxes, 0, geometry(box(0, 380, 400, 420))) is None


def test_a_top_card_scrolled_out_of_view_is_clicked_when_nothing_is_on_screen() -> None:
    # Playwright scrolls it into view as part of the click.
    boxes = [box(0, -60, 1280, 60), box(10, -200)]
    assert choose_message_target(boxes, 1, geometry(None, None)) == (
        1,
        ClickTarget.TOP_CARD_OFF_SCREEN,
    )
    # Partly on screen counts as off screen: Playwright would scroll it.
    boxes = [box(10, 790)]
    assert choose_message_target(boxes, 0, geometry(None)) == (
        0,
        ClickTarget.TOP_CARD_OFF_SCREEN,
    )
    # Without a top card, nothing off screen is clicked.
    assert choose_message_target([box(10, -200)], None, geometry(None)) is None


@pytest.mark.parametrize(
    "where",
    [box(-1, 400), box(10, -1), box(1181, 400), box(10, 769), box(10, 400, 0, 32)],
)
def test_a_box_not_wholly_on_screen_or_without_area_is_not_chosen_as_on_screen(
    where: dict[str, float],
) -> None:
    # The hit is the control itself: only where it sits keeps it from being chosen.
    assert choose_message_target([where], None, geometry(where)) is None


def test_a_box_at_the_viewports_edge_is_on_screen() -> None:
    edge = box(1180, 768)  # right and bottom edges exactly on the viewport's
    assert choose_message_target([edge], None, geometry(edge)) == (0, ClickTarget.ON_SCREEN)


def test_the_hit_link_must_be_this_control_give_or_take_a_pixel() -> None:
    control = box(10, 400)
    for near in (box(9, 399, 102, 34), box(11, 401, 98, 30)):  # sub-pixel rounding
        assert choose_message_target([control], None, geometry(near)) == (
            0,
            ClickTarget.ON_SCREEN,
        ), near
    for other in (
        box(8, 400),
        box(12, 400),
        box(10, 398),
        box(10, 402),
        box(10, 400, 102),
        box(10, 400, 97),
        box(10, 400, 100, 35),
        box(10, 400, 100, 29),
    ):
        assert choose_message_target([control], None, geometry(other)) is None, other


def test_without_geometry_the_top_card_or_else_the_first_is_chosen_unchecked() -> None:
    boxes = [box(0, -60), box(10, 400)]
    assert choose_message_target(boxes, 1, None) == (1, ClickTarget.UNCHECKED)
    assert choose_message_target(boxes, None, None) == (0, ClickTarget.UNCHECKED)
    assert choose_message_target([], None, None) is None


# --- the click, against the fake page -------------------------------------------------------


def _link(member: Member, key: str, where: str, style: str = "") -> str:
    """A Message control in a box: the ``<a>``'s own ``data-box``, inside a keyed card."""
    html = message_control_html(member, absolute=False).replace(
        '<a aria-disabled="false"', f'<a data-box="{where}" aria-disabled="false"', 1
    )
    return f'<div componentkey="{key}"{style}>{html}</div>'


def _profile(*, sticky: str = "", top: str = "40,300,110,32", extra: str = "") -> str:
    """A profile like the CP8 page: a sticky header copy before ``<main>`` (when given),
    the top card's control after the ``h1``, a Highlights copy, and ``extra``."""
    header = f"<header>{_link(ZEPHYRINE, 'sticky', sticky)}</header>" if sticky else ""
    return (
        f"{header}<main><h1>{ZEPHYRINE.name}</h1>"
        f"{_link(ZEPHYRINE, 'top-card', top)}"
        f"<section><h2>Highlights</h2>{_link(ZEPHYRINE, 'highlights', '40,620,110,32')}"
        f"</section></main>{extra}"
    )


def _clicked_key(site: MessagingSite) -> str:
    [clicked] = site.tab.clicks
    assert clicked.attrs["href"] == compose_href(ZEPHYRINE, absolute=False)
    card = next(a for a in clicked.ancestors() if "componentkey" in a.attrs)
    return card.attrs["componentkey"]


def _site(profile: str, *, before: str = "") -> MessagingSite:
    site = MessagingSite(ZEPHYRINE, profile_html=profile, before=before)
    site.viewport = VIEWPORT
    return site


async def test_a_sticky_copy_first_in_the_page_and_off_screen_is_never_clicked() -> None:
    site = _site(_profile(sticky="0,-64,1280,64"))
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert _clicked_key(site) == "top-card"
    assert ran.run.message_click_diagnostics == {
        "message_click_target": "top_card",
        "message_click_failure": None,
    }
    # The geometry session sent its reads, and detached.
    [session] = site.geometry_sessions
    assert session.detached
    # The top card is one candidate, not two: it's found again among the visible links
    # and passed over, so only the two controls on screen are hit-tested.
    hits = [m for m, _ in session.sent if m == "DOM.getNodeForLocation"]
    assert len(hits) == 2
    assert {method for method, _ in session.sent} == {
        "Page.getLayoutMetrics",
        "DOM.getDocument",
        "DOM.querySelectorAll",
        "DOM.describeNode",
        "DOM.getBoxModel",
        "DOM.getContentQuads",
        "DOM.getNodeForLocation",
    }


async def test_a_sticky_copy_on_screen_is_clicked_when_the_top_card_scrolled_away() -> None:
    site = _site(_profile(sticky="0,0,1280,64", top="40,-120,110,32"))
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert _clicked_key(site) == "sticky"
    assert ran.run.message_click_diagnostics["message_click_target"] == "on_screen"


async def test_a_top_card_under_the_messaging_bar_gives_way_to_the_highlights_copy() -> None:
    bar = '<aside data-box="0,280,600,80"><h2>Messaging</h2></aside>'
    site = _site(_profile(extra=bar))
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert _clicked_key(site) == "highlights"
    assert ran.run.message_click_diagnostics["message_click_target"] == "on_screen"


async def test_every_control_covered_refuses_with_no_click() -> None:
    cover = '<aside data-box="0,0,1280,800"><h2>Messaging</h2></aside>'
    site = _site(_profile(extra=cover))
    ran = await prefill(site)
    assert_no_keys(ran)
    assert ran.result.outcome.reason == MESSAGE_NOT_ON_SCREEN
    assert site.tab.clicks == [] and not ran.run.message_click_attempted
    assert ran.run.message_click_diagnostics == {}


async def test_an_overlay_that_lets_pointer_events_through_does_not_cover() -> None:
    glass = '<div data-box="0,0,1280,800" data-pointer-events="none"></div>'
    site = _site(_profile(extra=glass))
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert _clicked_key(site) == "top-card"
    assert ran.run.message_click_diagnostics["message_click_target"] == "top_card"


async def test_a_sidebar_of_other_peoples_message_buttons_is_ignored() -> None:
    """The "More profiles for you" sidebar's controls are buttons, not links: decision 4
    reads links only, so they neither refuse the prefill nor get clicked."""
    sidebar = (
        '<aside><h2>More profiles for you</h2><ul><li><a href="/in/someone-else/">Someone</a>'
        '<button type="button" data-box="900,300,90,32"><span>Message</span></button></li>'
        '<li><button type="button" data-box="900,380,90,32"><span>Message</span></button>'
        "</li></ul></aside>"
    )
    site = _site(_profile(extra=sidebar))
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert _clicked_key(site) == "top-card"


async def test_a_sidebar_link_naming_someone_else_still_refuses_under_decision_4() -> None:
    sidebar = f"<aside><h2>More profiles for you</h2>{_link(THADDEUS, 'other', '900,300,90,32')}"
    ran = await prefill(_site(_profile(extra=sidebar + "</aside>")))
    assert_no_keys(ran)
    assert "something other than this contact's compose" in ran.result.outcome.reason
    assert ran.tab.clicks == []


async def test_a_geometry_read_that_fails_clicks_the_top_card_unchecked() -> None:
    site = _site(_profile(sticky="0,-64,1280,64"))
    site.geometry_error = RuntimeError("Protocol error")
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert _clicked_key(site) == "top-card"
    assert ran.run.message_click_diagnostics["message_click_target"] == "unchecked"
    assert all(s.detached for s in site.geometry_sessions)


async def test_a_geometry_read_that_hangs_times_out_and_clicks_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_module, "GEOMETRY_TIMEOUT_S", 0.05)
    site = _site(_profile(sticky="0,-64,1280,64"))
    site.geometry_hangs = True
    async with asyncio.timeout(5):  # without the read's own timeout, this would hang
        ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert _clicked_key(site) == "top-card"
    assert ran.run.message_click_diagnostics["message_click_target"] == "unchecked"
    assert all(s.detached for s in site.geometry_sessions)


async def test_without_one_h1_there_is_no_top_card_and_the_first_clear_control_wins() -> None:
    profile = _profile(sticky="0,0,1280,64").replace(f"<h1>{ZEPHYRINE.name}</h1>", "")
    site = _site(profile)
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert _clicked_key(site) == "sticky"


async def test_two_h1s_mean_no_top_card_so_the_first_clear_control_is_on_screen() -> None:
    profile = _profile().replace("<h2>Highlights</h2>", "<h1>Highlights</h1>")
    site = _site(profile)
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert _clicked_key(site) == "top-card"
    assert ran.run.message_click_diagnostics["message_click_target"] == "on_screen"


async def test_a_click_that_raises_records_its_category_and_logs_no_page_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    site = _site(_profile())
    site.click_error = _playwright('<div class="secret-overlay">…</div> intercepts pointer events')
    with caplog.at_level(logging.INFO):
        ran = await prefill(site)
    assert_no_keys(ran)
    assert ran.result.outcome.reason == "the Message control could not be clicked"
    assert ran.run.message_click_attempted and not ran.run.message_clicked
    assert ran.run.message_click_diagnostics == {
        "message_click_target": "top_card",
        "message_click_failure": "intercepted",
    }
    line = next(r.getMessage() for r in caplog.records if "could not be clicked" in r.getMessage())
    assert "intercepted" in line and "top_card" in line
    assert "secret-overlay" not in caplog.text and "Call log" not in caplog.text


async def test_minimized_bubbles_from_earlier_prefills_refuse_before_the_click() -> None:
    """Three minimized bubbles, as on the CP8 page: each a ``Messaging`` dialog whose
    composer is hidden but still on the page. Decision 3, read before the click,
    refuses with no click, in words that say a minimized bubble counts."""
    minimized = "".join(
        existing_bubble_html(THADDEUS).replace(
            'role="dialog"',
            'role="dialog" data-msg-overlay-conversation-bubble-is-minimized'
            '="true" style="display:none"',
            1,
        )
        for _ in range(3)
    )
    site = _site(_profile(), before=minimized)
    site.bubble = Bubble(ZEPHYRINE)
    ran = await prefill(site)
    assert_no_keys(ran)
    assert site.tab.clicks == [] and not ran.run.message_click_attempted
    assert ran.result.outcome.reason == (
        "a message bubble is already open in Chrome, minimized ones included;"
        " close it, then try again"
    )
    assert site.geometry_sessions == []  # nothing read past the refusal


def test_the_click_point_is_the_middle_of_the_first_quad_with_area_on_screen() -> None:
    """As Playwright's own click finds it: each quad clipped to the viewport, the first
    with an area over 0.99 square pixels, its middle. A link holding a tall icon has a
    first quad whose middle is far from its box's center."""
    tall_icon = [40.0, 220.0, 340.0, 220.0, 340.0, 370.0, 40.0, 370.0]
    label = [340.0, 356.0, 396.0, 356.0, 396.0, 374.0, 340.0, 374.0]
    assert _click_point([tall_icon, label], VIEWPORT) == (190.0, 295.0)
    flat = [0.0, 10.0, 500.0, 10.0, 500.0, 10.5, 0.0, 10.5]  # 250 square pixels: kept
    sliver = [0.0, 10.0, 1.0, 10.0, 1.0, 10.9, 0.0, 10.9]  # 0.9: skipped
    assert _click_point([sliver, label], VIEWPORT) == (368.0, 365.0)
    assert _click_point([flat], VIEWPORT) == (250.0, 10.25)
    above = [0.0, -100.0, 100.0, -100.0, 100.0, -10.0, 0.0, -10.0]  # clipped to no area
    assert _click_point([above, label], VIEWPORT) == (368.0, 365.0)
    half = [0.0, -50.0, 100.0, -50.0, 100.0, 50.0, 0.0, 50.0]  # clipped to its lower half
    assert _click_point([half], VIEWPORT) == (50.0, 25.0)
    assert _click_point([above], VIEWPORT) is None


async def test_a_hit_on_the_links_own_icon_counts_even_when_it_overflows_the_link() -> None:
    """The hit is matched by node: an icon inside the link, bigger than the link's box,
    is still the link's own, not something over it."""
    profile = _profile()
    link = profile.index('<a data-box="40,300,110,32"')
    span = profile.index("<span>", link)
    profile = f'{profile[:span]}<span data-box="30,250,300,150">{profile[span + 6 :]}'
    assert 'data-box="30,250,300,150"' in profile
    site = _site(profile)
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert _clicked_key(site) == "top-card"
    assert ran.run.message_click_diagnostics["message_click_target"] == "top_card"
