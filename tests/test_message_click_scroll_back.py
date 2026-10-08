"""The prefill's brief scroll can leave the top card's Message control under a sticky
header; the prefill then scrolls back up once before the click (#470).

CP8 saw a prefill refuse with "no Message control is on screen with nothing over it"
three times on one contact, with no bubble open. Once a LinkedIn profile scrolls a
little, a sticky header slides in at the top with its own **Message** control, whose
shape was never captured (#429). A brief scroll of a few hundred pixels can leave the
top card's control inside the viewport but under that header: covered, so not chosen,
and not off screen, so not scrolled to. These tests reproduce that on an invented
profile whose layout follows its scroll position, and pin the fix: a read-only probe
(:meth:`BrowserRun.message_cover`), one scroll back up in small wheel steps, and the
click's own fresh read, which still refuses when nothing is clear. Every box and
header here is invented; none is a capture of LinkedIn's page.
"""

from __future__ import annotations

import logging
import random
from typing import Any

import pytest
from browser_fakes import FakeMouse
from messaging_dom import MessagingSite, MessagingTab
from messaging_pages import ZEPHYRINE, compose_href, message_control_html
from run_fakes import fake_provider
from test_auto_send import auto_spec, page_run, permit, sends
from test_prefill_page import assert_no_keys, prefill

from netkeeper.linkedin import page_messaging
from netkeeper.linkedin.browser import (
    BUBBLE_BAND_PX,
    MESSAGE_NOT_ON_SCREEN,
    STICKY_HEADER_BAND_PX,
    TOP_CARD_COVERED,
    ClickGeometry,
    NotClear,
    not_clear_reason,
)
from netkeeper.linkedin.messaging import MessageOutcomeKind
from netkeeper.linkedin.pacing import (
    BACK_TO_TOP_DELTA_RANGE_PX,
    scroll_back_to_top,
    scroll_like_a_person,
)
from netkeeper.linkedin.page_messaging import BRIEF_SCROLL_DELTA_PX, SCROLL_BACK_DELTA_PX

VIEWPORT = (1280.0, 800.0)
#: The invented layout, in document pixels: a fixed global navigation bar, and a sticky
#: profile header under it that appears once the page has scrolled past
#: ``STICKY_AFTER_PX``. The top card's Message control sits at ``TOP_CARD_Y``; the
#: Highlights copy far enough down to stay off screen.
NAV_HEIGHT = 52.0
STICKY_HEIGHT = 64.0
STICKY_AFTER_PX = 200
TOP_CARD_Y = 400.0
HIGHLIGHTS_Y = 1700.0
#: A brief scroll that parks the top card's center (416) at 86: inside the viewport,
#: under the sticky header (52 to 116).
COVERING_SCROLL_PX = 330


def _link(key: str, where: str) -> str:
    html = message_control_html(ZEPHYRINE, absolute=False).replace(
        '<a aria-disabled="false"', f'<a data-box="{where}" aria-disabled="false"', 1
    )
    return f'<div componentkey="{key}">{html}</div>'


def scrolled_profile(scroll_y: float, *, sticky_button: bool = True) -> str:
    """The invented profile at ``scroll_y``. The sticky header's own Message control is
    a button here, not a link (its real shape is unknown, #429): decision 4 reads links
    only, so it's never a candidate, and nothing may click it."""
    sticky = ""
    if scroll_y >= STICKY_AFTER_PX:
        button = (
            '<button type="button" data-box="1100,62,90,40"><span>Message</span></button>'
            if sticky_button
            else ""
        )
        sticky = (
            f'<header componentkey="sticky" data-box="0,{NAV_HEIGHT},1280,{STICKY_HEIGHT}">'
            f"<h2>Invented sticky header</h2>{button}</header>"
        )
    return (
        f"<main><h1>{ZEPHYRINE.name}</h1>"
        f"{_link('top-card', f'40,{TOP_CARD_Y - scroll_y},110,32')}"
        f"<section><h2>Highlights</h2>{_link('highlights', f'40,{HIGHLIGHTS_Y - scroll_y},110,32')}"
        f"</section></main>"
        # Fixed, so after <main> in document order: the fake's hit test answers the last
        # element over a point, as a fixed overlay sits over what it covers.
        f'<nav data-box="0,0,1280,{NAV_HEIGHT}"><h2>Invented navigation</h2></nav>{sticky}'
    )


class ScrollingMouse(FakeMouse):
    """Wheel events scroll the invented page, which is re-laid out at its new position.
    ``stuck`` makes it ignore scrolls back up (a page that holds its position)."""

    def __init__(self, site: ScrollingSite) -> None:
        super().__init__()
        self.site = site

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await super().wheel(delta_x, delta_y)
        if self.site.stuck and delta_y < 0:
            return
        self.site.scroll_y = max(0.0, self.site.scroll_y + delta_y)
        self.site.scroll_offset = (0.0, self.site.scroll_y)
        self.site.tab.load(self.site.render(self.site.scroll_y))


class ScrollingSite(MessagingSite):
    """A :class:`MessagingSite` whose profile follows its scroll position (#470)."""

    def __init__(self, *, stuck: bool = False, sticky_button: bool = True) -> None:
        super().__init__(ZEPHYRINE, profile_html=scrolled_profile(0))
        self.viewport = VIEWPORT
        self.scroll_y = 0.0
        self.stuck = stuck
        self.sticky_button = sticky_button
        self.scroll_at_click: list[float] = []

    def render(self, scroll_y: float) -> str:
        return scrolled_profile(scroll_y, sticky_button=self.sticky_button)

    def navigated(self, tab: MessagingTab, url: str) -> None:
        super().navigated(tab, url)
        self.scroll_y = 0.0
        self.scroll_offset = (0.0, 0.0)
        tab.mouse = ScrollingMouse(self)

    def open_bubble(self, tab: MessagingTab, bubble: Any) -> None:
        self.scroll_at_click.append(self.scroll_y)
        super().open_bubble(tab, bubble)


@pytest.fixture
def covering_scroll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the brief scroll to one step of :data:`COVERING_SCROLL_PX`."""
    monkeypatch.setattr(page_messaging, "BRIEF_SCROLL_STEPS", (1, 1))
    monkeypatch.setattr(
        page_messaging, "BRIEF_SCROLL_DELTA_PX", (COVERING_SCROLL_PX, COVERING_SCROLL_PX)
    )
    real = scroll_like_a_person

    def no_back_up(rng: random.Random, **kwargs: Any) -> Any:
        return real(rng, back_up_p=0.0, **kwargs)

    monkeypatch.setattr(page_messaging, "scroll_like_a_person", no_back_up)


def _clicked_key(site: MessagingSite) -> str:
    [clicked] = site.tab.clicks
    assert clicked.tag == "a"
    assert clicked.attrs["href"] == compose_href(ZEPHYRINE, absolute=False)
    card = next(a for a in clicked.ancestors() if "componentkey" in a.attrs)
    return card.attrs["componentkey"]


def _wheels(site: MessagingSite) -> list[float]:
    return [dy for _, dy in site.tab.mouse.wheels]


def _assert_sanitized(text: str) -> None:
    for secret in (ZEPHYRINE.name, ZEPHYRINE.slug, ZEPHYRINE.urn, "linkedin.com", "/in/"):
        assert secret not in text, secret
    assert "Invented" not in text  # no page text


# --- the reproduction and the fix --------------------------------------------------------


def test_the_invented_profile_covers_the_top_card_after_the_brief_scroll() -> None:
    """The fixture itself: at the covering scroll the top card's control is inside the
    viewport, and the sticky header lies over its center."""
    center = TOP_CARD_Y - COVERING_SCROLL_PX + 16
    assert NAV_HEIGHT <= center < NAV_HEIGHT + STICKY_HEIGHT
    assert TOP_CARD_Y - COVERING_SCROLL_PX >= 0
    assert BRIEF_SCROLL_DELTA_PX[0] <= COVERING_SCROLL_PX <= BRIEF_SCROLL_DELTA_PX[1]
    assert 'componentkey="sticky"' in scrolled_profile(COVERING_SCROLL_PX)
    assert 'componentkey="sticky"' not in scrolled_profile(0)


@pytest.mark.usefixtures("covering_scroll")
async def test_a_top_card_left_under_the_sticky_header_is_scrolled_back_to_and_clicked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    site = ScrollingSite()
    with caplog.at_level(logging.INFO):
        ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    # One click, on the top card's href-bound link, with the page back at its top.
    assert _clicked_key(site) == "top-card"
    assert site.scroll_at_click == [0.0]
    assert ran.run.message_click_diagnostics == {
        "message_click_target": "top_card",
        "message_click_failure": None,
    }
    # The brief scroll down, then small steps back up that cover it and stop at the top.
    wheels = _wheels(site)
    assert wheels[0] == COVERING_SCROLL_PX
    back = wheels[1:]
    assert back and all(-SCROLL_BACK_DELTA_PX[1] <= dy <= -SCROLL_BACK_DELTA_PX[0] for dy in back)
    assert -sum(back) > COVERING_SCROLL_PX
    # The probe's read, then the click's own read of the scrolled-back page.
    probe, click = site.geometry_sessions
    assert probe.detached and click.detached
    assert "covered_by_sticky_header" in caplog.text and "scrolling back up" in caplog.text
    _assert_sanitized(caplog.text)


@pytest.mark.usefixtures("covering_scroll")
async def test_the_sticky_headers_own_control_is_never_clicked() -> None:
    """Even when the sticky header's control is the only one clear, the click stays on
    the contact's verified link: the header's control is a button here, not a link."""
    site = ScrollingSite(stuck=True)
    ran = await prefill(site)
    assert_no_keys(ran)
    assert site.tab.clicks == []


@pytest.mark.usefixtures("covering_scroll")
async def test_a_page_that_stays_covered_after_the_scroll_back_refuses_as_before(
    caplog: pytest.LogCaptureFixture,
) -> None:
    site = ScrollingSite(stuck=True)
    with caplog.at_level(logging.INFO):
        ran = await prefill(site)
    assert_no_keys(ran)
    assert ran.result.outcome.reason == MESSAGE_NOT_ON_SCREEN
    assert site.tab.clicks == [] and not ran.run.message_click_attempted
    assert ran.run.message_click_diagnostics == {}
    # It tried once: one scroll back up, then the click's fresh read, then the refusal.
    assert any(dy < 0 for dy in _wheels(site))
    assert len(site.geometry_sessions) == 2
    refusal = [r.getMessage() for r in caplog.records if "nothing was clicked" in r.getMessage()]
    assert refusal == [
        "prefill: no Message control was clear to click (covered_by_sticky_header);"
        " nothing was clicked"
    ]
    _assert_sanitized(caplog.text)


async def test_a_brief_scroll_that_leaves_the_top_card_clear_never_scrolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(page_messaging, "BRIEF_SCROLL_STEPS", (1, 1))
    monkeypatch.setattr(page_messaging, "BRIEF_SCROLL_DELTA_PX", (120, 120))
    site = ScrollingSite()
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert _clicked_key(site) == "top-card"
    assert all(dy > 0 for dy in _wheels(site))


async def test_a_top_card_scrolled_out_of_view_is_left_to_playwrights_scroll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off screen is not covered: the click scrolls it into view itself (#444), so the
    probe asks for no scroll back."""
    monkeypatch.setattr(page_messaging, "BRIEF_SCROLL_STEPS", (1, 1))
    monkeypatch.setattr(page_messaging, "BRIEF_SCROLL_DELTA_PX", (600, 600))
    site = ScrollingSite()
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert _clicked_key(site) == "top-card"
    assert ran.run.message_click_diagnostics["message_click_target"] == "top_card_off_screen"
    assert all(dy > 0 for dy in _wheels(site))


@pytest.mark.usefixtures("covering_scroll")
async def test_an_auto_send_scrolls_back_the_same_way_and_sends_once() -> None:
    """ADR 0008's auto-send runs the same prefill: the scroll back comes before the one
    Message click, and the one Send click follows the typing as before."""
    site = ScrollingSite()
    result, _, _ = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED, result
    assert site.scroll_at_click == [0.0]
    assert len(sends(site)) == 1 and site.sent == [auto_spec().body]
    links = [c for c in site.tab.clicks if c.tag == "a"]
    assert len(links) == 1


# --- the probe and the categories ----------------------------------------------------------


def _box(y: float) -> dict[str, float]:
    return {"x": 40.0, "y": y, "width": 110.0, "height": 32.0}


def _geometry(*points: tuple[float, float] | None) -> ClickGeometry:
    return ClickGeometry(VIEWPORT, tuple(None for _ in points), points)


def test_the_category_constants_are_pinned() -> None:
    assert [c.value for c in NotClear] == [
        "no_candidates",
        "no_top_card",
        "top_card_off_screen",
        "covered_by_sticky_header",
        "covered_by_bubble",
        "covered_by_other",
    ]
    assert {
        NotClear.COVERED_BY_STICKY_HEADER,
        NotClear.COVERED_BY_BUBBLE,
        NotClear.COVERED_BY_OTHER,
    } == TOP_CARD_COVERED
    assert STICKY_HEADER_BAND_PX == 160.0
    assert BUBBLE_BAND_PX == 64.0
    assert SCROLL_BACK_DELTA_PX == BRIEF_SCROLL_DELTA_PX == (120, 360)


@pytest.mark.parametrize(
    ("y", "category"),
    [
        (70.0, NotClear.COVERED_BY_STICKY_HEADER),
        (STICKY_HEADER_BAND_PX - 17, NotClear.COVERED_BY_STICKY_HEADER),
        (STICKY_HEADER_BAND_PX - 16, NotClear.COVERED_BY_OTHER),
        (400.0, NotClear.COVERED_BY_OTHER),
        (VIEWPORT[1] - BUBBLE_BAND_PX - 16, NotClear.COVERED_BY_BUBBLE),
        (VIEWPORT[1] - BUBBLE_BAND_PX - 17, NotClear.COVERED_BY_OTHER),
    ],
)
def test_a_covered_top_card_is_placed_by_its_click_point(y: float, category: NotClear) -> None:
    point = (95.0, y + 16)
    assert not_clear_reason([_box(y)], 0, _geometry(point)) is category
    # Without a read click point, its box's center places it the same way.
    assert not_clear_reason([_box(y)], 0, _geometry(None)) is category


def test_the_other_categories() -> None:
    assert not_clear_reason([], None, _geometry()) is NotClear.NO_CANDIDATES
    assert not_clear_reason([_box(400)], None, _geometry(None)) is NotClear.NO_TOP_CARD
    assert not_clear_reason([None], 0, _geometry(None)) is NotClear.TOP_CARD_OFF_SCREEN
    assert not_clear_reason([_box(-200)], 0, _geometry(None)) is NotClear.TOP_CARD_OFF_SCREEN


@pytest.mark.usefixtures("covering_scroll")
async def test_the_probe_reads_only_and_never_after_the_click() -> None:
    site = ScrollingSite(stuck=True)
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
        path = f"/in/{ZEPHYRINE.slug}/"
        assert await run.message_cover(path, ZEPHYRINE.profile_id) is None  # at the top
        await site.tab.mouse.wheel(0, COVERING_SCROLL_PX)
        assert await run.message_cover(path, ZEPHYRINE.profile_id) is (
            NotClear.COVERED_BY_STICKY_HEADER
        )
        assert await run.message_cover("/in/someone-else/", ZEPHYRINE.profile_id) is None
        assert site.tab.clicks == [] and site.tab.attempts == []
        assert not run.message_click_attempted
        assert all(s.detached for s in site.geometry_sessions)


def test_the_scroll_back_takes_small_steps_and_overshoots_the_depth() -> None:
    plan = scroll_back_to_top(random.Random(1), 330, delta_range_px=SCROLL_BACK_DELTA_PX)
    deltas = [s.delta_px for s in plan.steps]
    assert all(-360 <= d <= -120 for d in deltas) and -sum(deltas) > 330
    # The default stays enrichment's.
    plan = scroll_back_to_top(random.Random(1), 330)
    assert all(
        -BACK_TO_TOP_DELTA_RANGE_PX[1] <= s.delta_px <= -BACK_TO_TOP_DELTA_RANGE_PX[0]
        for s in plan.steps
    )
    with pytest.raises(ValueError, match="delta_range_px"):
        scroll_back_to_top(random.Random(1), 330, delta_range_px=(0, 10))


@pytest.mark.usefixtures("covering_scroll")
async def test_the_geometry_read_keeps_each_on_screen_controls_click_point() -> None:
    """The category is placed by the point Playwright's click would press, as read."""
    site = ScrollingSite()
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
        await site.tab.mouse.wheel(0, COVERING_SCROLL_PX)
        top = _box(TOP_CARD_Y - COVERING_SCROLL_PX)
        off = _box(HIGHLIGHTS_Y - COVERING_SCROLL_PX)
        verified = [compose_href(ZEPHYRINE, absolute=False)]
        geometry = await run._read_click_geometry(site.tab, [top, off, None], verified)
    assert geometry is not None
    assert geometry.points == ((95.0, TOP_CARD_Y - COVERING_SCROLL_PX + 16), None, None)
    assert geometry.hits == (None, None, None)


async def test_a_scrolled_page_is_hit_tested_at_the_document_point() -> None:
    """``DOM.getContentQuads`` answers in viewport coordinates and
    ``DOM.getNodeForLocation`` takes document ones, as Chrome's do (checked on an
    isolated Chrome for #470). So the hit test adds the scroll offset: without it, a
    page scrolled 120 pixels is hit-tested 120 pixels above the control."""
    site = ScrollingSite()
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
        await site.tab.mouse.wheel(0, 120)
        site.scroll_offset = (30.0, 120.0)  # and 30 pixels across, the boxes unchanged
        top = _box(TOP_CARD_Y - 120)
        verified = [compose_href(ZEPHYRINE, absolute=False)]
        geometry = await run._read_click_geometry(site.tab, [top], verified)
    assert geometry is not None
    assert geometry.hits == (top,)  # clear: nothing over it, once the point is right
    [session] = site.geometry_sessions
    [hit] = [params for method, params in session.sent if method == "DOM.getNodeForLocation"]
    assert hit == {
        "x": 95 + 30,
        "y": round(TOP_CARD_Y - 120 + 16 + 120),
        "ignorePointerEventsNone": True,
    }
