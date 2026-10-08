"""``netkeeper linkedin message-check``: a prefill's steps up to the Message click (#473).

CP8's prefills still refused with "no Message control is on screen with nothing over it"
after #471, and each try spent a prefill and said one category word. The check runs the
prefill's steps up to the click, never the click, and prints what each read found. These
tests drive it on invented profiles (``messaging_dom``'s fake DOM, and #470's scrolling
one), one per hypothesis the report must tell apart:

1. the top card's Message control is a ``<button>``, not a link, so never a candidate;
2. the top card's selector (the one ``h1``, then ``following::a``) no longer matches;
3. the compose ``href`` changed shape, so decision 4 refuses or the binding fails;
4. the hit test lands on an element over the link, not inside it;
5. the viewport, scroll or zoom differ from what the boxes assume.

Every page, box and name here is invented; nothing is a capture of LinkedIn's page.
"""

from __future__ import annotations

import functools
import random
from collections.abc import Iterator
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

import pytest
from factories import make_contact
from messaging_dom import MessagingSite
from messaging_pages import ZEPHYRINE, compose_href
from run_fakes import fake_provider, no_sleep
from sqlalchemy.orm import Session, sessionmaker
from test_message_click_scroll_back import (
    COVERING_SCROLL_PX,
    NAV_HEIGHT,
    ScrollingSite,
)
from typer.testing import CliRunner

from netkeeper import cli as cli_module
from netkeeper import migrations
from netkeeper.cli import _message_check_lines
from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin import page_messaging
from netkeeper.linkedin.activity_lock import account_key
from netkeeper.linkedin.browser import (
    MESSAGE_NOT_ON_SCREEN,
    BrowserRun,
    HitRelation,
    MessageCheckSnapshot,
    NotClear,
    href_shape,
)
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.pacing import scroll_like_a_person
from netkeeper.linkedin.page_messaging import CHECK_PHASES, MessageCheckResult, PageMessageCheck
from netkeeper.models import Message, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.scoping import install_scope_guard, scoped
from netkeeper.services import budgets, message_check, runs, scheduler
from netkeeper.services.budgets import ActionClass
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import flag_session, session_flag
from netkeeper.services.scheduled_runs import auto_send_spacing_left
from netkeeper.services.users import ensure_local_user
from netkeeper.worker import run_message_check

PATH = f"/in/{ZEPHYRINE.slug}/"
HREF = compose_href(ZEPHYRINE, absolute=False)


# --- driving the check on a page -----------------------------------------------------------


async def _never() -> bool:
    return False


async def check(site: MessagingSite, *, seed: int = 5) -> tuple[MessageCheckResult, BrowserRun]:
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        result = await PageMessageCheck(run, sleep=no_sleep, rng=random.Random(seed)).run(
            ZEPHYRINE.slug, ZEPHYRINE.profile_id, cancelled=_never
        )
    return result, run


def assert_untouched(site: MessagingSite, run: BrowserRun) -> None:
    """Nothing clicked, typed, or focused; the tab fronted once and closed at the end."""
    tab = site.tab
    assert tab.clicks == [] and tab.attempts == [] and tab.keys == []
    assert tab.focus_calls == [] and tab.focused is None
    assert tab.fronted == 1
    assert not run.message_click_attempted and run.keys_sent == 0
    assert tab.is_closed()
    assert all(session.detached for session in site.geometry_sessions)


def report(result: MessageCheckResult) -> str:
    return "\n".join(_message_check_lines(7, result))


def assert_sanitized(text: str) -> None:
    for secret in (
        ZEPHYRINE.name,
        ZEPHYRINE.first,
        ZEPHYRINE.slug,
        ZEPHYRINE.profile_id,
        "linkedin.com",
        "/in/",
        "/messaging/",
        "profileUrn=",
        "Invented",
    ):
        assert secret not in text, secret


@pytest.fixture
def covering_scroll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the brief scroll to one step that parks the top card under the sticky header."""
    monkeypatch.setattr(page_messaging, "BRIEF_SCROLL_STEPS", (1, 1))
    monkeypatch.setattr(
        page_messaging, "BRIEF_SCROLL_DELTA_PX", (COVERING_SCROLL_PX, COVERING_SCROLL_PX)
    )
    real = scroll_like_a_person

    def no_back_up(rng: random.Random, **kwargs: Any) -> Any:
        return real(rng, back_up_p=0.0, **kwargs)

    monkeypatch.setattr(page_messaging, "scroll_like_a_person", no_back_up)


@pytest.fixture
def no_scroll(monkeypatch: pytest.MonkeyPatch) -> None:
    """A brief scroll of one 120 px step on a page that doesn't move (a static fixture)."""
    monkeypatch.setattr(page_messaging, "BRIEF_SCROLL_STEPS", (1, 1))
    monkeypatch.setattr(page_messaging, "BRIEF_SCROLL_DELTA_PX", (120, 120))


def static_site(html: str, *, viewport: tuple[float, float] = (1280.0, 800.0)) -> MessagingSite:
    """A profile whose layout never moves: the boxes are the ones in ``html``."""
    site = MessagingSite(ZEPHYRINE, profile_html=html)
    site.viewport = viewport
    return site


def link(where: str, href: str = HREF, *, extra: str = "") -> str:
    return (
        f'<a data-box="{where}" href="{escape(href)}"{extra}>'
        '<span><svg aria-hidden="true"></svg><span>Message</span></span></a>'
    )


def last(result: MessageCheckResult) -> MessageCheckSnapshot:
    return result.snapshots[-1]


# --- what it runs, and what it never does ------------------------------------------------


@pytest.mark.usefixtures("covering_scroll")
async def test_a_covered_top_card_is_reported_under_the_sticky_header_and_nothing_is_touched() -> (
    None
):
    """#470's page that stays covered: the report places the cover, the scroll back ran,
    and the verdict is the click's own refusal, with its category."""
    site = ScrollingSite(stuck=True)
    result, run = await check(site)
    assert_untouched(site, run)
    assert [s.phase for s in result.snapshots] == list(CHECK_PHASES)
    assert result.cover is NotClear.COVERED_BY_STICKY_HEADER and result.scrolled_back
    assert result.brief_scroll_px == COVERING_SCROLL_PX
    first, brief, at_click = result.snapshots
    assert first.verdict == "clicks candidate 1 (top_card)"
    assert brief.verdict == f"refuses: {MESSAGE_NOT_ON_SCREEN}"
    assert brief.category is NotClear.COVERED_BY_STICKY_HEADER
    assert at_click.category is NotClear.COVERED_BY_STICKY_HEADER
    assert brief.scroll == (0.0, float(COVERING_SCROLL_PX))
    [top, _] = brief.candidates
    assert top.on_screen is True and top.unobstructed is False and top.control == 1
    assert top.hit is not None and top.hit.relation is HitRelation.COVERED
    assert (top.hit.tag, top.hit.role, top.hit.landmark) == ("header", "banner", "header (banner)")
    assert top.hit.band is not None and "sticky header band" in top.hit.band
    # The sticky header's own control is a button named Message: listed, never a candidate.
    assert [(c.role, c.tag) for c in brief.controls] == [
        ("link", "a"),
        ("link", "a"),
        ("button", "button"),
    ]
    text = report(result)
    assert "covered by header (banner), top 160 px" in text
    assert "scroll back (#471): ran; the cover probe answered covered_by_sticky_header" in text
    assert_sanitized(text)


@pytest.mark.usefixtures("covering_scroll")
async def test_a_page_the_scroll_back_clears_reports_the_click_on_the_top_card() -> None:
    site = ScrollingSite()
    result, run = await check(site)
    assert_untouched(site, run)
    assert result.scrolled_back
    at_click = last(result)
    assert at_click.verdict == "clicks candidate 1 (top_card)" and at_click.category is None
    [top, _] = at_click.candidates
    assert top.hit is not None and top.hit.relation is HitRelation.CONTROL
    assert (top.hit.tag, top.hit.role) == ("a", "link")
    assert at_click.scroll == (0.0, 0.0)
    assert_sanitized(report(result))


async def test_a_clear_brief_scroll_reports_no_scroll_back(no_scroll: None) -> None:
    site = ScrollingSite()
    result, run = await check(site)
    assert_untouched(site, run)
    assert result.cover is None and not result.scrolled_back
    assert "scroll back (#471): did not run; the cover probe answered none" in report(result)
    assert last(result).verdict == "clicks candidate 1 (top_card)"


async def test_the_check_takes_the_prefills_steps_in_its_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same BrowserRun calls as a prefill up to its click, the check's reads between."""
    calls: list[str] = []
    for name in ("bring_tab_forward", "goto", "scroll", "message_cover", "message_check_snapshot"):
        real = getattr(BrowserRun, name)

        async def counted(
            self: BrowserRun, *args: Any, _real: Any = real, _name: str = name, **kw: Any
        ) -> Any:
            calls.append(_name)
            return await _real(self, *args, **kw)

        monkeypatch.setattr(BrowserRun, name, counted)
    monkeypatch.setattr(page_messaging, "BRIEF_SCROLL_STEPS", (1, 1))
    monkeypatch.setattr(page_messaging, "BRIEF_SCROLL_DELTA_PX", (COVERING_SCROLL_PX,) * 2)
    await check(ScrollingSite())
    assert calls == [
        "bring_tab_forward",
        "goto",
        "message_check_snapshot",
        "scroll",
        "message_cover",
        "message_check_snapshot",
        "scroll",
        "message_check_snapshot",
    ]


async def test_the_click_pause_comes_before_the_last_read() -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    site = ScrollingSite()
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        await PageMessageCheck(run, sleep=sleep, rng=random.Random(5)).run(
            ZEPHYRINE.slug, ZEPHYRINE.profile_id, cancelled=_never
        )
    low, high = page_messaging.CLICK_PAUSE_RANGE_S
    assert low <= waits[-1] <= high


async def test_a_wall_stops_the_check_before_any_read() -> None:
    site = MessagingSite(ZEPHYRINE, land_on="https://www.linkedin.com/checkpoint/challenge/x")
    site.viewport = (1280.0, 800.0)
    result, _ = await check(site)
    assert result.wall is Outcome.CHECKPOINT and result.snapshots == ()
    assert result.stopped == "the page answered checkpoint"
    assert site.geometry_sessions == []


async def test_a_cancel_stops_before_the_reads() -> None:
    async def yes() -> bool:
        return True

    site = ScrollingSite()
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        result = await PageMessageCheck(run, sleep=no_sleep).run(
            ZEPHYRINE.slug, ZEPHYRINE.profile_id, cancelled=yes
        )
    assert result.stopped == "cancelled" and result.snapshots == ()


async def test_a_check_runs_once() -> None:
    site = ScrollingSite()
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        page = PageMessageCheck(run, sleep=no_sleep)
        await page.run(ZEPHYRINE.slug, ZEPHYRINE.profile_id, cancelled=_never)
        with pytest.raises(RuntimeError, match="runs once"):
            await page.run(ZEPHYRINE.slug, ZEPHYRINE.profile_id, cancelled=_never)


# --- the hypotheses, each with its own report ------------------------------------------------


@pytest.mark.usefixtures("no_scroll")
async def test_hypothesis_1_a_top_card_button_is_listed_but_never_a_candidate() -> None:
    """The top card's Message is a ``<button>`` with no ``href``; the only link is a copy
    before the ``h1`` (a sticky bar, say), under the navigation. The click refuses with
    no top card; the report shows the button after the ``h1``, on screen, and clear."""
    html = (
        f'<nav data-box="0,0,1280,{NAV_HEIGHT}">{link("1100,10,90,32")}</nav>'
        f"<main><h1>{ZEPHYRINE.name}</h1>"
        '<button type="button" data-box="40,400,110,32"><span>Message</span></button></main>'
        f'<div data-box="0,0,1280,{NAV_HEIGHT}" componentkey="global-nav-overlay"></div>'
    )
    site = static_site(html)
    result, run = await check(site)
    assert_untouched(site, run)
    snap = last(result)
    assert snap.verdict == f"refuses: {MESSAGE_NOT_ON_SCREEN}"
    assert snap.category is NotClear.NO_TOP_CARD
    assert not snap.top_card_matched
    [nav_link, button] = snap.controls
    assert (nav_link.role, nav_link.after_heading, nav_link.href) == (
        "link",
        False,
        "verified compose",
    )
    assert (button.role, button.tag, button.href) == ("button", "button", "no href")
    assert button.after_heading is True and button.top_card_selector is False
    assert button.on_screen is True
    assert button.hit is not None and button.hit.relation is HitRelation.CONTROL
    [candidate] = snap.candidates
    assert candidate.control == 1 and candidate.unobstructed is False
    text = report(result)
    assert "no_top_card" in text and "no href" in text
    assert_sanitized(text)


@pytest.mark.usefixtures("no_scroll")
@pytest.mark.parametrize("headings", [0, 2])
async def test_hypothesis_2_the_top_card_selector_no_longer_matches(headings: int) -> None:
    """No single ``h1``: the selector can't match, so the link is chosen as any other."""
    h1s = "".join(f"<h1>{ZEPHYRINE.name}</h1>" for _ in range(headings))
    html = f"<main>{h1s}<section>{link('40,400,110,32')}</section></main>"
    site = static_site(html)
    result, run = await check(site)
    assert_untouched(site, run)
    snap = last(result)
    assert snap.heading_count == headings and not snap.top_card_matched
    assert snap.verdict == "clicks candidate 1 (on_screen)"
    assert snap.controls[0].after_heading is None and snap.controls[0].top_card_selector is None
    text = report(result)
    assert (
        f"h1 count: {headings}; top-card selector (the h1, then following::a) matched: no" in text
    )


@pytest.mark.usefixtures("no_scroll")
@pytest.mark.parametrize(
    ("href", "shape"),
    [
        (
            f"/messaging/compose/?profileUrn={ZEPHYRINE.urn}&recipient=900000101",
            "compose, profileUrn matches, recipient differs",
        ),
        (
            f"/messaging/compose/?recipient={ZEPHYRINE.profile_id}",
            "compose, profileUrn missing, recipient matches",
        ),
        (f"/messaging/thread/new/?recipient={ZEPHYRINE.profile_id}", "not a compose link"),
        (
            f"https://example.test/messaging/compose/?profileUrn={ZEPHYRINE.urn}"
            f"&recipient={ZEPHYRINE.profile_id}",
            "compose on another host",
        ),
    ],
)
async def test_hypothesis_3_a_changed_compose_href_is_named_by_its_shape(
    href: str, shape: str
) -> None:
    html = f"<main><h1>{ZEPHYRINE.name}</h1>{link('40,400,110,32', href)}</main>"
    site = static_site(html)
    result, run = await check(site)
    assert_untouched(site, run)
    snap = last(result)
    assert snap.decision_4 == "a Message control opens something other than this contact's compose"
    assert snap.verdict.startswith("refuses: a Message control opens something other")
    assert snap.candidates == () and not snap.geometry_read
    assert [c.href for c in snap.controls] == [shape]
    text = report(result)
    assert shape in text
    assert_sanitized(text.replace(shape, ""))


def test_href_shapes_are_fixed_words() -> None:
    pid = ZEPHYRINE.profile_id
    assert href_shape(None, pid) == "no href"
    assert href_shape(HREF, pid) == "verified compose"
    assert href_shape(compose_href(ZEPHYRINE, absolute=True), pid) == "verified compose"
    repeated = f"{HREF}&recipient={pid}"
    assert href_shape(repeated, pid) == "compose, profileUrn matches, recipient repeated"
    assert (
        href_shape(f"{HREF}#x", pid)
        == "compose, profileUrn matches, recipient matches, has a fragment"
    )
    assert href_shape("/messaging/compose/?a", pid) == "compose, unparsable query"
    other = compose_href(ZEPHYRINE, absolute=False).replace(pid, "ACoAAOther")
    assert href_shape(other, pid) == "compose, profileUrn differs, recipient differs"


@pytest.mark.usefixtures("no_scroll")
async def test_hypothesis_4_an_overlay_over_the_link_is_named_and_a_child_is_not_a_cover() -> None:
    """An invented full-size overlay after the link (as a button's hover layer might be)
    covers its click point: the report names the cover's tag, outside any landmark, in
    the middle band. A child that overflows the link is the link's own."""
    overlay = (
        f"<main><h1>{ZEPHYRINE.name}</h1>"
        f'<div class="wrapper">{link("40,400,110,32")}'
        '<span data-box="40,400,110,32" class="overlay"></span></div></main>'
    )
    site = static_site(overlay)
    result, run = await check(site)
    assert_untouched(site, run)
    snap = last(result)
    assert snap.category is NotClear.COVERED_BY_OTHER
    [candidate] = snap.candidates
    assert candidate.hit is not None and candidate.hit.relation is HitRelation.COVERED
    assert (candidate.hit.tag, candidate.hit.role, candidate.hit.landmark) == ("span", "none", None)
    assert candidate.hit.band == "middle"
    assert "covered by span (none), middle" in report(result)

    child = (
        f"<main><h1>{ZEPHYRINE.name}</h1>"
        f'<a data-box="40,400,110,32" href="{escape(HREF)}">'
        '<span data-box="30,390,130,52"><span>Message</span></span></a></main>'
    )
    site = static_site(child)
    result, run = await check(site)
    snap = last(result)
    assert snap.verdict == "clicks candidate 1 (top_card)"
    [candidate] = snap.candidates
    assert candidate.hit is not None and candidate.hit.relation is HitRelation.INSIDE
    assert (candidate.hit.tag, candidate.hit.role) == ("span", "none")
    assert "inside the control (span, none)" in report(result)


@pytest.mark.usefixtures("no_scroll")
async def test_hypothesis_5_the_viewport_scroll_and_zoom_are_reported() -> None:
    """A page zoomed to 125 % on a 1x screen, scrolled 40 px, with a viewport smaller
    than the boxes assume: the top card reads off screen and Playwright would scroll."""
    html = f"<main><h1>{ZEPHYRINE.name}</h1>{link('40,700,110,32')}</main>"
    site = static_site(html, viewport=(1024.0, 640.0))
    site.zoom = 1.25
    site.device_pixel_ratio = 1.25
    site.scroll_offset = (0.0, 40.0)
    result, run = await check(site)
    assert_untouched(site, run)
    snap = last(result)
    assert snap.viewport == (1024.0, 640.0) and snap.scroll == (0.0, 40.0)
    assert snap.zoom == 1.25 and snap.device_pixel_ratio == 1.25
    assert snap.verdict == "clicks candidate 1 (top_card_off_screen)"
    assert snap.controls[0].on_screen is False
    text = report(result)
    assert (
        "viewport 1024x640 CSS px; scroll x 0, y 40; zoom 1.25; device px per CSS px 1.25" in text
    )


@pytest.mark.usefixtures("no_scroll")
async def test_a_bubble_already_open_is_the_verdict() -> None:
    html = (
        f"<main><h1>{ZEPHYRINE.name}</h1>{link('40,400,110,32')}</main>"
        '<div role="dialog" aria-label="Messaging"></div>'
    )
    result, _ = await check(static_site(html))
    snap = last(result)
    assert snap.bubble == "open" and snap.verdict.startswith("refuses: a message bubble")


@pytest.mark.usefixtures("no_scroll")
async def test_a_name_that_only_starts_with_message_is_counted_never_printed() -> None:
    html = (
        f"<main><h1>{ZEPHYRINE.name}</h1>{link('40,400,110,32')}"
        f'<button type="button" aria-label="Message {ZEPHYRINE.first}" data-box="200,400,90,32">'
        "</button></main>"
    )
    result, _ = await check(static_site(html))
    snap = last(result)
    assert (snap.links_named_like, snap.buttons_named_like) == (0, 1)
    text = report(result)
    assert "named Message plus more: 0 link(s), 1 button(s)" in text
    assert_sanitized(text)


async def test_a_page_without_cdp_reports_the_reads_it_could_make(no_scroll: None) -> None:
    site = MessagingSite(ZEPHYRINE)  # no viewport: no CDP session opens
    result, run = await check(site)
    assert_untouched(site, run)
    snap = last(result)
    assert snap.verdict == "clicks candidate 1 (unchecked)"
    assert snap.viewport is None and not snap.geometry_read
    assert any(e.startswith("detail read: ") for e in snap.errors)
    assert "reads that failed: detail read: RuntimeError" in report(result)


async def test_the_detail_read_never_hangs_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from netkeeper.linkedin import browser

    monkeypatch.setattr(browser, "CHECK_DETAIL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(browser, "GEOMETRY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(page_messaging, "BRIEF_SCROLL_STEPS", (1, 1))
    site = ScrollingSite()
    site.geometry_hangs = True
    result, run = await check(site)
    assert_untouched(site, run)
    assert all("detail read: TimeoutError" in s.errors for s in result.snapshots)


# --- the command, its run, and what it writes ------------------------------------------------


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session, settings=Settings())
    yield factory
    engine.dispose()


def _user(session: Session) -> User:
    return ensure_local_user(session, settings=Settings())


def _contact(factory: sessionmaker[Session], **overrides: Any) -> int:
    fields: dict[str, Any] = {
        "li_urn": ZEPHYRINE.urn,
        "li_public_id": ZEPHYRINE.slug,
        "first_name": ZEPHYRINE.first,
        "last_name": ZEPHYRINE.last,
    }
    fields.update(overrides)
    with session_scope(factory, write=True) as session:
        return make_contact(session, _user(session), **fields).id


@pytest.fixture
def chrome(monkeypatch: pytest.MonkeyPatch) -> ScrollingSite:
    site = ScrollingSite(stuck=True)
    provider, _ = fake_provider(site)
    monkeypatch.setattr(cli_module, "_provider", lambda settings: provider)
    monkeypatch.setattr(
        cli_module, "run_message_check", functools.partial(run_message_check, sleep=no_sleep)
    )
    return site


def _visits(factory: sessionmaker[Session], action: ActionClass) -> int:
    with session_scope(factory) as session:
        user = _user(session)
        account = ensure_account(session, user).id
        snapshot = budgets.status(
            session,
            user,
            account,
            action,
            now=datetime.now(UTC),
            settings=Settings().linkedin.budget,
        )
        return snapshot.day.count


@pytest.mark.usefixtures("inside_active_hours", "covering_scroll")
def test_the_command_prints_the_report_and_records_one_run_and_one_visit(
    cli_db: sessionmaker[Session], chrome: ScrollingSite
) -> None:
    contact_id = _contact(cli_db)
    result = CliRunner().invoke(cli, ["linkedin", "message-check", str(contact_id)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "--- message check report" in out and "--- end of report ---" in out
    assert "[after load]" in out and "[after the brief scroll]" in out and "[at the click]" in out
    assert "covered_by_sticky_header" in out
    assert (
        "run 1: completed (checked the Message control up to the click; nothing was clicked)" in out
    )
    assert_sanitized(out.replace("Who viewed your profile", ""))
    assert chrome.tab.clicks == [] and chrome.tab.keys == [] and chrome.tab.focus_calls == []
    with session_scope(cli_db) as session:
        user = _user(session)
        run = runs.get_run(session, user, 1)
        assert (run.kind, run.trigger, run.status, run.stop_reason) == (
            SyncRunKind.MESSAGE_SEND,
            SyncRunTrigger.MANUAL,
            SyncRunStatus.COMPLETED,
            message_check.MESSAGE_CHECK_STOP,
        )
        assert run.notes == message_check.MESSAGE_CHECK_NOTE
        assert run.error is None and not run.counts_json and not run.progress_json
        assert session.scalars(scoped(user, Message)).all() == []
        # Spec 9.5's gap after a prefill, and auto-send's spacing, both count it.
        assert scheduler._last_message_send(cli_db, user, datetime.now(UTC)) is not None
        assert auto_send_spacing_left(session, user, datetime.now(UTC)).total_seconds() > 0
    assert _visits(cli_db, ActionClass.PROFILE_VISITS) == 1
    assert _visits(cli_db, ActionClass.LI_PREFILLS) == 0


@pytest.mark.usefixtures("inside_active_hours")
def test_a_flagged_session_refuses_before_any_run_is_recorded(
    cli_db: sessionmaker[Session], chrome: ScrollingSite
) -> None:
    contact_id = _contact(cli_db)
    with session_scope(cli_db, write=True) as session:
        flag_session(session, _user(session), Outcome.CHECKPOINT, url="/checkpoint/x")
    result = CliRunner().invoke(cli, ["linkedin", "message-check", str(contact_id)])
    assert result.exit_code == 1 and "flagged" in result.output
    assert chrome.pages == []
    with session_scope(cli_db) as session:
        assert runs.list_runs(session, _user(session))[1] == 0


def test_outside_active_hours_refuses_before_any_run(
    cli_db: sessionmaker[Session], chrome: ScrollingSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    contact_id = _contact(cli_db)

    def closed(*args: object, **kwargs: object) -> None:
        raise runs.OutsideActiveHours("outside active hours (invented)")

    monkeypatch.setattr(runs, "refuse_if_outside_active_hours", closed)
    result = CliRunner().invoke(cli, ["linkedin", "message-check", str(contact_id)])
    assert result.exit_code == 1 and "outside active hours" in result.output
    assert chrome.pages == []


@pytest.mark.usefixtures("inside_active_hours")
@pytest.mark.parametrize(
    ("overrides", "words"),
    [
        ({"li_public_id": None}, "has no LinkedIn public profile id"),
        ({"li_urn": "urn:li:member:1"}, "has no usable LinkedIn profile URN"),
        ({"li_urn": "urn:li:fsd_profile:a/b"}, "has no usable LinkedIn profile URN"),
    ],
)
def test_a_contact_it_cannot_open_is_refused(
    cli_db: sessionmaker[Session], chrome: ScrollingSite, overrides: dict[str, Any], words: str
) -> None:
    contact_id = _contact(cli_db, **overrides)
    result = CliRunner().invoke(cli, ["linkedin", "message-check", str(contact_id)])
    assert result.exit_code == 1 and words in result.output
    assert chrome.pages == []


@pytest.mark.usefixtures("inside_active_hours")
def test_an_unknown_contact_is_refused(
    cli_db: sessionmaker[Session], chrome: ScrollingSite
) -> None:
    result = CliRunner().invoke(cli, ["linkedin", "message-check", "999"])
    assert result.exit_code == 1 and "no contact 999" in result.output


@pytest.mark.usefixtures("inside_active_hours")
def test_a_wall_flags_the_session_and_fails_the_run(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    site = MessagingSite(ZEPHYRINE, land_on="https://www.linkedin.com/checkpoint/challenge/x")
    provider, _ = fake_provider(site)
    monkeypatch.setattr(cli_module, "_provider", lambda settings: provider)
    contact_id = _contact(cli_db)
    result = CliRunner().invoke(cli, ["linkedin", "message-check", str(contact_id)])
    assert result.exit_code == 1
    assert "stopped before its reads finished: the page answered checkpoint" in result.output
    with session_scope(cli_db) as session:
        user = _user(session)
        assert session_flag(session, user) is not None
        run = runs.get_run(session, user, 1)
        assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "checkpoint")


async def test_a_busy_browser_fails_the_run_and_spends_nothing(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "refuse_if_outside_active_hours", lambda *_, **__: None)
    contact_id = _contact(cli_db)
    with session_scope(cli_db, write=True) as session:
        target = message_check.start(
            session, _user(session), contact_id, now=datetime.now(UTC), settings=Settings()
        )
    site = ScrollingSite()
    provider, _ = fake_provider(site)
    async with provider.run(account_key(target.account_id)):  # another run holds it
        result = await run_message_check(
            provider, cli_db, 1, target, settings=Settings(), sleep=no_sleep
        )
    assert result.stopped == "the browser was busy with another run"
    with session_scope(cli_db) as session:
        run = runs.get_run(session, _user(session), target.run_id)
        assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "browser_busy")
    assert _visits(cli_db, ActionClass.PROFILE_VISITS) == 0


async def test_a_check_cancelled_before_the_visit_spends_nothing(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "refuse_if_outside_active_hours", lambda *_, **__: None)
    contact_id = _contact(cli_db)
    with session_scope(cli_db, write=True) as session:
        user = _user(session)
        target = message_check.start(
            session, user, contact_id, now=datetime.now(UTC), settings=Settings()
        )
        runs.request_cancel(
            session, user, target.run_id, now=datetime.now(UTC), browser_held=lambda _: True
        )
    site = ScrollingSite()
    provider, _ = fake_provider(site)
    await run_message_check(provider, cli_db, 1, target, settings=Settings(), sleep=no_sleep)
    assert site.pages == [] or site.navigations == []
    assert _visits(cli_db, ActionClass.PROFILE_VISITS) == 0


async def test_a_failure_records_only_its_type(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Playwright error's text can quote a selector holding the compose href: the run
    row gets the exception's type, never its text."""
    monkeypatch.setattr(runs, "refuse_if_outside_active_hours", lambda *_, **__: None)
    contact_id = _contact(cli_db)
    with session_scope(cli_db, write=True) as session:
        target = message_check.start(
            session, _user(session), contact_id, now=datetime.now(UTC), settings=Settings()
        )

    async def boom(self: BrowserRun, *args: object) -> None:
        raise ValueError(f'locator("[href={HREF}]") timed out')

    monkeypatch.setattr(BrowserRun, "message_check_snapshot", boom)
    provider, _ = fake_provider(ScrollingSite())
    result = await run_message_check(
        provider, cli_db, 1, target, settings=Settings(), sleep=no_sleep
    )
    assert result.stopped == "the check failed (ValueError)"
    with session_scope(cli_db) as session:
        run = runs.get_run(session, _user(session), target.run_id)
        assert run.status is SyncRunStatus.FAILED and run.error == "the check failed (ValueError)"


def test_a_message_check_run_needs_its_gate(cli_db: sessionmaker[Session]) -> None:
    with session_scope(cli_db, write=True) as session:
        with pytest.raises(runs.RunError, match="prefill claim"):
            runs.create_run(
                session,
                _user(session),
                SyncRunKind.MESSAGE_SEND,
                trigger=SyncRunTrigger.MANUAL,
                now=datetime.now(UTC),
            )
        with pytest.raises(runs.RunError, match="never scheduled"):
            runs.create_run(
                session,
                _user(session),
                SyncRunKind.MESSAGE_SEND,
                trigger=SyncRunTrigger.SCHEDULED,
                now=datetime.now(UTC),
                gate=runs.MESSAGE_CHECK_GATE,
            )


def test_the_stop_reason_has_plain_words() -> None:
    assert runs.STOP_REASON_TEXT[message_check.MESSAGE_CHECK_STOP] == (
        "checked the Message control up to the click; nothing was clicked"
    )


# --- pseudo-elements, shadow roots and frames (review of #474) ------------------------------


@pytest.mark.usefixtures("no_scroll")
@pytest.mark.parametrize(
    ("after_link", "link_attrs", "relation", "words"),
    [
        (
            "",
            ' data-after-box="40,400,110,32"',
            HitRelation.PSEUDO_INSIDE,
            "a pseudo-element inside the control (its host: a, link)",
        ),
        (
            '<div data-box="0,300,1280,300" data-after-box="40,400,110,32"></div>',
            "",
            HitRelation.PSEUDO_COVERED,
            "covered by a pseudo-element of div (none), middle",
        ),
        (
            '<my-overlay data-box="0,300,1280,300"><shadow-root>'
            '<span data-box="40,400,110,32"></span></shadow-root></my-overlay>',
            "",
            HitRelation.COVERED,
            "covered by span (none) in a shadow root, middle",
        ),
        (
            '<my-overlay data-box="0,300,1280,300">'
            '<span data-untreed data-box="40,400,110,32"></span></my-overlay>',
            "",
            HitRelation.IN_SHADOW_ROOT,
            "an element inside a shadow root the tree didn't include",
        ),
        (
            '<iframe data-frame-owner="child-1" data-box="0,300,1280,300">'
            '<div data-untreed data-frame="child-1" data-box="40,400,110,32"></div></iframe>',
            "",
            HitRelation.IN_FRAME,
            "an element inside a frame",
        ),
        (
            # A frame whose owner the tree doesn't show: its frame id isn't the page's.
            '<div data-untreed data-frame="child-2" data-box="40,400,110,32"></div>',
            "",
            HitRelation.IN_FRAME,
            "an element inside a frame",
        ),
    ],
)
async def test_a_pseudo_element_a_shadow_root_and_a_frame_are_told_apart(
    after_link: str, link_attrs: str, relation: HitRelation, words: str
) -> None:
    html = (
        f"<main><h1>{ZEPHYRINE.name}</h1>"
        f'<a data-box="40,400,110,32" href="{escape(HREF)}"{link_attrs}><span>Message</span></a>'
        f"{after_link}</main>"
    )
    site = static_site(html)
    result, run = await check(site)
    assert_untouched(site, run)
    [candidate] = last(result).candidates
    assert candidate.hit is not None and candidate.hit.relation is relation
    text = report(result)
    assert words in text
    assert_sanitized(text)


@pytest.mark.usefixtures("no_scroll")
async def test_a_covers_label_and_role_text_never_reach_the_report() -> None:
    """A covering landmark with an invented label, and a role attribute holding invented
    words: the report names the tag and a role from the ARIA list, nothing else."""
    html = (
        f"<main><h1>{ZEPHYRINE.name}</h1>{link('40,400,110,32')}</main>"
        '<header aria-label="Invented Secret Label" role="Zephyrine Mockwell"'
        ' data-box="0,380,1280,80">'
        '<div role="Invented-Text" aria-label="Invented Other" data-box="0,380,1280,80"></div>'
        "</header>"
    )
    result, _ = await check(static_site(html))
    [candidate] = last(result).candidates
    hit = candidate.hit
    assert hit is not None and hit.relation is HitRelation.COVERED
    assert (hit.tag, hit.role, hit.landmark) == ("div", "other", "header (other)")
    text = report(result)
    assert "covered by div (other) in header (other)" in text
    assert "Secret" not in text and "Label" not in text and "Mockwell" not in text
    assert_sanitized(text)


def test_a_role_is_a_listed_aria_role_or_other() -> None:
    from netkeeper.linkedin.browser import _check_role

    assert _check_role("div", {"role": "Navigation"}) == "navigation"
    assert _check_role("div", {"role": "region extra words"}) == "region"
    assert _check_role("div", {"role": "invented"}) == "other"
    assert _check_role("div", {"role": "Zephyrine Mockwell"}) == "other"
    assert _check_role("header", {}) == "banner"
    assert _check_role("a", {"href": "x"}) == "link" and _check_role("a", {}) == "none"


async def test_each_snapshot_says_when_it_was_taken(no_scroll: None) -> None:
    result, _ = await check(ScrollingSite())
    assert len(result.since_load_s) == 3
    assert all(s >= 0 for s in result.since_load_s)
    assert list(result.since_load_s) == sorted(result.since_load_s)
    assert f"[after load] {result.since_load_s[0]:.1f} s after the load" in report(result)


@pytest.mark.usefixtures("no_scroll")
async def test_a_hidden_controls_top_card_selector_is_marked_hidden() -> None:
    html = (
        f"<main><h1>{ZEPHYRINE.name}</h1>{link('40,400,110,32')}"
        f'<div style="display: none">{link("40,900,110,32")}</div></main>'
    )
    result, _ = await check(static_site(html))
    [shown, hidden] = last(result).controls
    assert shown.visible and not hidden.visible and hidden.top_card_selector is True
    assert "yes (hidden)" in report(result)


# --- the gates under the lock, and a wall's heat (review of #474) ---------------------------


def _start(factory: sessionmaker[Session]) -> message_check.CheckTarget:
    contact_id = _contact(factory)
    with session_scope(factory, write=True) as session:
        return message_check.start(
            session, _user(session), contact_id, now=datetime.now(UTC), settings=Settings()
        )


@pytest.mark.usefixtures("inside_active_hours")
async def test_a_session_flagged_after_the_start_refuses_under_the_lock(
    cli_db: sessionmaker[Session],
) -> None:
    target = _start(cli_db)
    with session_scope(cli_db, write=True) as session:
        flag_session(session, _user(session), Outcome.CHECKPOINT, url="/checkpoint/x")
    site = ScrollingSite()
    provider, _ = fake_provider(site)
    result = await run_message_check(
        provider, cli_db, 1, target, settings=Settings(), sleep=no_sleep
    )
    assert result.snapshots == () and site.navigations == []
    with session_scope(cli_db) as session:
        run = runs.get_run(session, _user(session), target.run_id)
        assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "session_flagged")
    assert _visits(cli_db, ActionClass.PROFILE_VISITS) == 0


@pytest.mark.usefixtures("inside_active_hours")
async def test_heat_over_its_threshold_after_the_start_refuses_under_the_lock(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from netkeeper.services import heat as heat_service

    target = _start(cli_db)
    monkeypatch.setattr(heat_service, "should_skip", lambda *_, **__: True)
    site = ScrollingSite()
    provider, _ = fake_provider(site)
    result = await run_message_check(
        provider, cli_db, 1, target, settings=Settings(), sleep=no_sleep
    )
    assert result.snapshots == () and site.navigations == []
    with session_scope(cli_db) as session:
        run = runs.get_run(session, _user(session), target.run_id)
        assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "heat_skip")
    assert _visits(cli_db, ActionClass.PROFILE_VISITS) == 0


@pytest.mark.usefixtures("inside_active_hours")
@pytest.mark.parametrize(
    ("wall", "heated", "flagged"),
    [
        (Outcome.THROTTLED, True, False),
        (Outcome.CHECKPOINT, True, True),
        (Outcome.LOGGED_OUT, False, True),
    ],
)
def test_a_wall_raises_heat_and_flags_as_a_prefills_does(
    cli_db: sessionmaker[Session], wall: Outcome, heated: bool, flagged: bool
) -> None:
    from netkeeper.services import heat as heat_service

    target = _start(cli_db)
    message_check.finish(
        cli_db,
        1,
        target,
        status=SyncRunStatus.FAILED,
        stop_reason=wall.value,
        now=datetime.now(UTC),
        settings=Settings(),
        wall=wall,
        wall_url="https://www.linkedin.com/checkpoint/challenge/x",
    )
    with session_scope(cli_db) as session:
        user = _user(session)
        state = heat_service.state(session, user, target.account_id)
        assert (state is not None and state.score > 0) is heated
        assert (session_flag(session, user) is not None) is flagged


@pytest.mark.usefixtures("inside_active_hours")
def test_the_check_records_its_run_through_its_own_gate(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    gates: list[object] = []
    real = runs.create_run

    def recording(*args: Any, **kwargs: Any) -> Any:
        gates.append(kwargs.get("gate"))
        return real(*args, **kwargs)

    monkeypatch.setattr(runs, "create_run", recording)
    _start(cli_db)
    assert gates == [runs.MESSAGE_CHECK_GATE]
    assert runs.MESSAGE_CHECK_GATE is not runs.MESSAGE_SEND_GATE
