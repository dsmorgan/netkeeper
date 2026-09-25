"""PageProfiles: profiles and contact info from the page's own answers, with one click (#190).

Driven through the real :class:`~netkeeper.linkedin.browser.BrowserRun` (and its one
click, :meth:`~netkeeper.linkedin.browser.BrowserRun.click_contact_info`) and the real
:func:`~netkeeper.linkedin.enrich.run_enrichment` over :mod:`profile_site`'s fake pages,
which "receive" a profile on navigation, "send" lazy cards on a scroll, and "send" the
overlay's request when the one Contact info control is clicked.

The attacks this file makes, each a test below: can the click hit the wrong control, a
control that is not alone, or fire twice? Can a profile answer for a different person --
a redirect to another slug, a stale answer from the last page, a rail of other people --
be applied to this contact? Can a changed shape write partial or wrong data? Can the
unreadable cap be walked past? Can a body or a slug reach a log?
"""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from flagship_pages import (
    Role,
    contact_info_payload,
    document_html,
    experience_payload,
    profile_payload,
)
from profile_site import (
    CHECKPOINT_URL,
    LOCATION,
    LOGIN_URL,
    ORIGIN,
    ProfilePage,
    ProfileSite,
    ProfileTab,
    Stale,
)
from run_fakes import fake_provider
from voyager_pages import PEOPLE, Person

from netkeeper.linkedin import browser as browser_module
from netkeeper.linkedin.browser import BrowserUnavailable
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.enrich import (
    MAX_UNREADABLE_IN_A_ROW,
    EnrichJobSpec,
    EnrichResult,
    EnrichTarget,
    ProfileHarvest,
    StopReason,
    run_enrichment,
)
from netkeeper.linkedin.flagship import CONTACT_DETAILS_SCREEN_ID
from netkeeper.linkedin.observe import ObservationFailed, ObservationLimits
from netkeeper.linkedin.pacing import ScrollPlan, ScrollStep, human_delay, plan_enrichment
from netkeeper.linkedin.page_profiles import PageProfiles

NOW = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)
PRIYA, MATEO, HANA, TOMASZ = PEOPLE[0], PEOPLE[1], PEOPLE[2], PEOPLE[3]
NOT_A_CARD = b"<html>not flight</html>"


class Gate:
    """Lets every visit through."""

    def __init__(self) -> None:
        self.pauses: list[float] = []

    async def before_visit(self, number: int) -> StopReason | None:
        return None

    async def pause(self, seconds: float) -> bool:
        self.pauses.append(seconds)
        return True


@dataclass
class Visit:
    result: EnrichResult
    harvests: list[ProfileHarvest] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)

    @property
    def outcomes(self) -> list[Outcome]:
        return [harvest.outcome for harvest in self.harvests]


def target(person: Person, *, urn: str | None = None, slug: str | None = None) -> EnrichTarget:
    return EnrichTarget(person.n, slug or person.slug, urn or person.urn)


async def visit(
    site: ProfileSite,
    targets: list[EnrichTarget],
    *,
    origin: str = ORIGIN,
    limits: ObservationLimits | None = None,
    on_sleep: Callable[[float], None] | None = None,
) -> Visit:
    provider, _ = fake_provider(site)
    harvests: list[ProfileHarvest] = []
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if on_sleep is not None:
            on_sleep(seconds)

    async def on_harvest(harvest: ProfileHarvest) -> None:
        harvests.append(harvest)

    extra: dict[str, Any] = {} if limits is None else {"limits": limits}
    async with provider.run("account-1") as run:
        source = PageProfiles(
            run,
            origin=origin,
            sleep=sleep,
            landing_wait_s=0.05,
            lazy_wait_s=0.01,
            overlay_wait_s=0.05,
            **extra,
        )
        result = await run_enrichment(
            EnrichJobSpec(targets=tuple(targets), visit_budget=len(targets)),
            source,
            Gate(),
            on_harvest=on_harvest,
            rng=random.Random(7),
            clock=lambda: NOW,
        )
    return Visit(result=result, harvests=harvests, sleeps=sleeps)


def tab_of(site: ProfileSite) -> ProfileTab:
    (tab,) = site.tabs
    return tab


# --- one whole visit -------------------------------------------------------------------------


async def test_a_visit_harvests_the_profile_and_its_contact_info() -> None:
    site = ProfileSite([ProfilePage(PRIYA)])
    out = await visit(site, [target(PRIYA)])
    assert out.result.reason is StopReason.END_OF_PLAN
    (harvest,) = out.harvests
    assert harvest.outcome is Outcome.OK and harvest.details is not None
    assert harvest.details.urn == PRIYA.urn and harvest.details.public_id == PRIYA.slug
    assert harvest.details.headline == PRIYA.headline and harvest.details.location == LOCATION
    assert [p.title for p in harvest.details.positions] == ["Staff Engineer"]
    assert harvest.contact_info is not None
    assert harvest.contact_info.emails == (f"{PRIYA.slug}@example.test",)
    assert harvest.contact_info.websites == (f"https://{PRIYA.slug}.example.test",)


async def test_the_one_click_is_the_only_input_besides_navigation_and_the_wheel() -> None:
    site = ProfileSite([ProfilePage(PRIYA)])
    out = await visit(site, [target(PRIYA)])
    tab = tab_of(site)
    assert site.lookups == [
        "get_by_role:link:Contact info:True",
        "count",
        "get_attribute:href",
        "click",
    ]
    assert site.clicks == [
        (
            PRIYA.slug,
            browser_module.CONTACT_INFO_PRESS_MS,
            browser_module.CONTACT_INFO_CLICK_TIMEOUT_MS,
        )
    ]
    assert out.result.clicks == 1
    assert tab.evaluate_calls == []
    assert tab.goto_calls == [f"{ORIGIN}/in/{PRIYA.slug}/"]
    # The page's own requests, and nothing netkeeper wrote: the document, then the
    # overlay the click made the page ask for.
    assert [(m, p) for m, p, _ in site.requests] == [
        ("GET", f"/in/{PRIYA.slug}/"),
        ("POST", "/flagship-web/rsc-action/actions/navigation"),
    ]


async def test_the_page_is_scrolled_down_then_back_to_the_top_before_the_click() -> None:
    site = ProfileSite([ProfilePage(PRIYA)])
    out = await visit(site, [target(PRIYA)])
    plan = out.result.plan.steps[0].scroll
    wheels = [dy for _, dy in tab_of(site).mouse.wheels]
    down = [step.delta_px for step in plan.steps]
    assert wheels[: len(down)] == down
    back = wheels[len(down) :]
    assert back and all(dy < 0 for dy in back)
    depth = 0
    for dy in down:
        depth = max(0, depth + dy)
    assert -sum(back) > depth  # back past the top: the page stops there


async def test_a_person_pauses_before_the_click_and_the_pause_is_recorded() -> None:
    out = await visit(ProfileSite([ProfilePage(PRIYA)]), [target(PRIYA)])
    (pause,) = out.result.click_pauses_s
    assert pause is not None and 0.2 < pause < 20
    assert pause in out.sleeps


async def test_the_in_app_screen_request_is_read_like_the_document() -> None:
    site = ProfileSite([ProfilePage(PRIYA, landing="screen")])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.OK]


# --- the click: the right control, once ------------------------------------------------------


@pytest.mark.parametrize(
    ("page", "why"),
    [
        (ProfilePage(PRIYA, controls=0), "none"),
        (ProfilePage(PRIYA, controls=2), "two"),
        (ProfilePage(PRIYA, href="/in/someone-else-fake/overlay/contact-info/"), "elsewhere"),
        (ProfilePage(PRIYA, href="/in/priya/"), "the profile, not its overlay"),
        (ProfilePage(PRIYA, href="https://evil.example.test/in/x/overlay/contact-info/"), "off"),
    ],
)
async def test_a_control_that_is_not_the_one_is_never_clicked(page: ProfilePage, why: str) -> None:
    site = ProfileSite([page])
    out = await visit(site, [target(PRIYA)])
    assert site.clicks == [], why
    assert out.outcomes == [Outcome.ROUTE_CHANGED]
    assert out.result.unreadable == 1 and out.result.reason is StopReason.END_OF_PLAN
    assert "click" not in site.lookups


async def test_a_profile_under_another_urn_gets_no_click() -> None:
    """The job compares the page's id with the contact's before it touches the page."""
    site = ProfileSite([ProfilePage(PRIYA)])
    out = await visit(site, [target(PRIYA, urn=MATEO.urn)])
    assert site.clicks == [] and site.lookups == []
    (harvest,) = out.harvests
    assert harvest.outcome is Outcome.OK and harvest.contact_info is None
    assert harvest.details is not None and harvest.details.urn == PRIYA.urn
    assert out.result.click_pauses_s == (None,) and out.result.clicks == 0


async def test_a_click_that_fails_is_not_tried_again() -> None:
    site = ProfileSite([ProfilePage(PRIYA, click_error=RuntimeError("not actionable"))])
    out = await visit(site, [target(PRIYA)])
    assert len(site.clicks) == 1
    assert out.outcomes == [Outcome.ROUTE_CHANGED]


async def test_an_overlay_that_never_answers_costs_one_click_and_the_visit() -> None:
    site = ProfileSite([ProfilePage(PRIYA, overlay_answers=0)])
    out = await visit(site, [target(PRIYA)])
    assert len(site.clicks) == 1 and out.outcomes == [Outcome.ROUTE_CHANGED]


async def test_one_visit_one_click_even_when_the_overlay_answers_twice() -> None:
    site = ProfileSite([ProfilePage(PRIYA, overlay_answers=2), ProfilePage(MATEO)])
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert [slug for slug, _, _ in site.clicks] == [PRIYA.slug, MATEO.slug]
    assert out.outcomes == [Outcome.OK, Outcome.OK]
    assert out.harvests[1].contact_info is not None
    assert out.harvests[1].contact_info.emails == (f"{MATEO.slug}@example.test",)


async def test_the_source_refuses_a_second_click_on_one_visit() -> None:
    site = ProfileSite([ProfilePage(PRIYA)])
    provider, _ = fake_provider(site)

    async def no_sleep(seconds: float) -> None:
        return None

    async with provider.run("account-1") as run:
        source = PageProfiles(run, sleep=no_sleep, landing_wait_s=0.05, overlay_wait_s=0.05)
        assert (await source.open_profile(PRIYA.slug)).outcome is Outcome.OK
        details = await source.read_profile(PRIYA.slug)
        assert details.value is not None
        back = ScrollPlan(steps=(), dwell_s=0.0)
        first = await source.read_contact_info(details.value, back=back, pause_s=0.0)
        assert first.outcome is Outcome.OK
        with pytest.raises(RuntimeError, match="already clicked"):
            await source.read_contact_info(details.value, back=back, pause_s=0.0)
    assert len(site.clicks) == 1


async def test_the_source_clicks_only_for_the_profile_it_read() -> None:
    site = ProfileSite([ProfilePage(PRIYA)])
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        source = PageProfiles(run, landing_wait_s=0.05, overlay_wait_s=0.05)
        await source.open_profile(PRIYA.slug)
        details = await source.read_profile(PRIYA.slug)
        assert details.value is not None
        other = replace(details.value, public_id=MATEO.slug)
        with pytest.raises(ValueError, match="not the one this visit is on"):
            await source.read_contact_info(
                other, back=ScrollPlan(steps=(), dwell_s=0.0), pause_s=0.0
            )
    assert site.clicks == []


# --- whose answer it is ------------------------------------------------------------------------


async def test_a_redirect_to_a_renamed_slug_reads_the_landed_profile() -> None:
    renamed = replace(PRIYA, public_id="priya-renamed-fake")
    site = ProfileSite([ProfilePage(PRIYA, redirect_to=renamed.slug), ProfilePage(renamed)])
    out = await visit(site, [target(PRIYA)])
    (harvest,) = out.harvests
    assert harvest.outcome is Outcome.OK and harvest.details is not None
    assert harvest.details.public_id == renamed.slug and harvest.details.urn == PRIYA.urn
    assert [slug for slug, _, _ in site.clicks] == [renamed.slug]


async def test_a_slug_that_now_belongs_to_somebody_else_gets_no_click() -> None:
    """The slug was given up and claimed: the page is a stranger's, under their id."""
    stranger = replace(MATEO, public_id=PRIYA.slug)
    site = ProfileSite([ProfilePage(stranger)])
    out = await visit(site, [target(PRIYA)])
    (harvest,) = out.harvests
    assert harvest.details is not None and harvest.details.urn == MATEO.urn
    assert harvest.contact_info is None and site.clicks == []


async def test_a_stale_answer_from_the_last_page_is_not_this_profiles() -> None:
    """At the next navigation, the previous page's document and lazy cards arrive first.
    None of them is read as this profile's."""
    stale = [
        Stale(
            "GET",
            f"/in/{MATEO.slug}/",
            document_html(profile_payload(MATEO, location="Elsewhere")).encode(),
        ),
        Stale(
            "POST",
            "/flagship-web/rsc-action/actions/component",
            experience_payload([Role("Wrong Role", "Wrong Co", None, "2001 - 2002")]),
            f'{{"vanityName": "{MATEO.slug}"}}',
        ),
        # A lazy card that names nobody: before this profile's screen, it can only be
        # the last page's.
        Stale(
            "POST",
            "/flagship-web/rsc-action/actions/component",
            experience_payload([Role("Nameless Wrong Role", "Wrong Co", None, "2001 - 2002")]),
            "{}",
        ),
    ]
    site = ProfileSite([ProfilePage(PRIYA)], stale=stale)
    out = await visit(site, [target(PRIYA)])
    (harvest,) = out.harvests
    assert harvest.details is not None
    assert harvest.details.urn == PRIYA.urn and harvest.details.location == LOCATION
    assert [p.title for p in harvest.details.positions] == ["Staff Engineer"]


async def test_the_page_asking_for_another_profiles_overlay_is_unreadable() -> None:
    site = ProfileSite([ProfilePage(PRIYA, overlay_vanity=MATEO.slug)])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and len(site.clicks) == 1


async def test_an_overlay_that_names_another_profile_is_unreadable() -> None:
    body = contact_info_payload(PRIYA, emails=["x@example.test"], profile_slug=MATEO.slug)
    site = ProfileSite([ProfilePage(PRIYA, overlay=body)])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED]


async def test_another_navigation_the_page_makes_is_not_the_overlay() -> None:
    other = (b'0:["$","div",null,{}]\n', "com.linkedin.sdui.flagshipnav.Something")
    site = ProfileSite([ProfilePage(PRIYA, overlay_before=[other])])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.OK]


async def test_a_lazy_card_that_names_another_member_is_skipped() -> None:
    wrong = experience_payload([Role("Wrong Role", "Wrong Co", None, "2001 - 2002")])
    site = ProfileSite(
        [
            ProfilePage(
                PRIYA,
                screen=profile_payload(PRIYA, location=LOCATION, experience_inline=False),
                components=(
                    (wrong, f'{{"vanityName": "{MATEO.slug}"}}'),
                    (experience_payload([Role("Right", "Right Co", None, "2020 - 2021")]), None),
                    (NOT_A_CARD, "{}"),
                ),
            )
        ]
    )
    out = await visit(site, [target(PRIYA)])
    (harvest,) = out.harvests
    assert harvest.details is not None
    assert [p.title for p in harvest.details.positions] == ["Right"]


# --- where the tab is, and what the answers say --------------------------------------------------


@pytest.mark.parametrize(
    ("landing", "outcome"),
    [(CHECKPOINT_URL, Outcome.CHECKPOINT), (LOGIN_URL, Outcome.LOGGED_OUT)],
)
async def test_a_wall_stops_the_run_before_anything_is_read(landing: str, outcome: Outcome) -> None:
    site = ProfileSite([ProfilePage(PRIYA, landing=landing), ProfilePage(MATEO)])
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.result.reason is StopReason.RESPONSE and out.result.outcome is outcome
    assert out.harvests == [] and site.clicks == []
    assert tab_of(site).mouse.wheels == []  # a wall is not scrolled either


async def test_a_page_that_is_not_a_profile_is_unreadable() -> None:
    site = ProfileSite([ProfilePage(PRIYA, landing=f"{ORIGIN}/feed/")])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and site.clicks == []


async def test_the_documents_404_is_the_contacts_not_found() -> None:
    site = ProfileSite([ProfilePage(PRIYA, landing="404"), ProfilePage(MATEO)])
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.outcomes == [Outcome.NOT_FOUND, Outcome.OK]
    assert [slug for slug, _, _ in site.clicks] == [MATEO.slug]


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (429, Outcome.THROTTLED),
        (999, Outcome.THROTTLED),
        (500, Outcome.ROUTE_CHANGED),
        (410, Outcome.ROUTE_CHANGED),  # only a 404 is NotFound
    ],
)
async def test_a_document_that_answers_badly_stops_the_run(status: int, outcome: Outcome) -> None:
    site = ProfileSite([ProfilePage(PRIYA, landing=f"status:{status}")])
    out = await visit(site, [target(PRIYA)])
    assert out.result.reason is StopReason.RESPONSE and out.result.outcome is outcome
    assert out.harvests == []


async def test_a_wall_served_in_place_is_unreadable_and_two_stop_the_run() -> None:
    """A 200 shell at the profile's url with no screen: never read as a login wall by
    its links (#188 M1), never flagged; two in a row stop the run as route_changed."""
    people = [PRIYA, MATEO, HANA]
    site = ProfileSite([ProfilePage(p, landing="shell") for p in people])
    out = await visit(site, [target(p) for p in people])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] * MAX_UNREADABLE_IN_A_ROW
    assert out.result.reason is StopReason.RESPONSE
    assert out.result.outcome is Outcome.ROUTE_CHANGED
    assert site.clicks == []


async def test_a_changed_profile_shape_writes_nothing() -> None:
    screen = profile_payload(PRIYA, location=LOCATION, identity="none")
    site = ProfileSite([ProfilePage(PRIYA, screen=screen), ProfilePage(MATEO)])
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED, Outcome.OK]
    assert out.harvests[0].details is None and out.harvests[0].contact_info is None
    assert [slug for slug, _, _ in site.clicks] == [MATEO.slug]


async def test_a_changed_overlay_shape_writes_nothing_of_the_profile_either() -> None:
    overlay = contact_info_payload(PRIYA, email_urls=["https://example.test/not-mail"])
    site = ProfileSite([ProfilePage(PRIYA, overlay=overlay)])
    out = await visit(site, [target(PRIYA)])
    (harvest,) = out.harvests
    assert harvest.outcome is Outcome.ROUTE_CHANGED and harvest.details is None


@pytest.mark.parametrize(
    ("status", "outcome", "stops"),
    [
        (404, Outcome.ROUTE_CHANGED, False),  # never NotFound by guess
        (500, Outcome.ROUTE_CHANGED, False),
        (429, Outcome.THROTTLED, True),
    ],
)
async def test_what_the_overlays_status_means(status: int, outcome: Outcome, stops: bool) -> None:
    site = ProfileSite([ProfilePage(PRIYA, overlay_status=status)])
    out = await visit(site, [target(PRIYA)])
    if stops:
        assert out.result.outcome is outcome and out.harvests == []
    else:
        assert out.outcomes == [outcome] and out.result.unreadable == 1


async def test_a_throttled_lazy_card_stops_the_run() -> None:
    site = ProfileSite([ProfilePage(PRIYA, components=((b"", None),), component_status=429)])
    out = await visit(site, [target(PRIYA)])
    assert out.result.outcome is Outcome.THROTTLED and site.clicks == []


async def test_a_lazy_card_that_fails_is_skipped_not_the_profile() -> None:
    site = ProfileSite([ProfilePage(PRIYA, components=((b"", None),), component_status=500)])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.OK]


@pytest.mark.parametrize(
    ("after", "outcome"),
    [
        (f"{ORIGIN}/feed/", Outcome.ROUTE_CHANGED),
        (f"{ORIGIN}/in/{MATEO.slug}/", Outcome.ROUTE_CHANGED),  # another profile
        (CHECKPOINT_URL, Outcome.CHECKPOINT),
    ],
)
async def test_a_tab_that_leaves_the_profile_while_scrolling_gets_no_click(
    after: str, outcome: Outcome
) -> None:
    site = ProfileSite([ProfilePage(PRIYA, tab_after_scroll=after)])
    out = await visit(site, [target(PRIYA)])
    assert site.clicks == [] and site.lookups == []
    # The visit stopped where the tab left: no scroll back up, no reach for the control.
    assert all(dy > 0 for _, dy in tab_of(site).mouse.wheels)
    if outcome is Outcome.CHECKPOINT:
        assert out.result.outcome is Outcome.CHECKPOINT and out.harvests == []
    else:
        assert out.outcomes == [Outcome.ROUTE_CHANGED]


async def test_a_slug_that_reads_like_a_wall_is_still_a_profile() -> None:
    person = replace(PRIYA, public_id="checkpoint")
    site = ProfileSite([ProfilePage(person)])
    out = await visit(site, [target(person)])
    assert out.outcomes == [Outcome.OK]


# --- the mechanism failing ----------------------------------------------------------------------


async def test_a_body_that_could_not_be_kept_ends_the_run() -> None:
    site = ProfileSite([ProfilePage(PRIYA)])
    with pytest.raises(ObservationFailed):
        await visit(site, [target(PRIYA)], limits=ObservationLimits(max_body_bytes=10))
    assert site.clicks == []


async def test_a_tab_closed_before_the_click_ends_the_run_without_reopening_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = ProfileSite([ProfilePage(PRIYA)])
    original = browser_module.BrowserRun.scroll

    async def scroll_then_lose(self: Any, plan: ScrollPlan, **kwargs: Any) -> Any:
        outcome = await original(self, plan, **kwargs)
        if plan.steps and plan.steps[0].delta_px < 0:  # the scroll back up
            cast(ProfileTab, outcome.page).user_closed_it()
        return outcome

    monkeypatch.setattr(browser_module.BrowserRun, "scroll", scroll_then_lose)
    with pytest.raises(BrowserUnavailable):
        await visit(site, [target(PRIYA)])
    assert site.clicks == [] and len(site.tabs) == 1


async def test_a_replaced_tab_is_no_longer_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    site = ProfileSite([ProfilePage(PRIYA)])
    original = browser_module.BrowserRun.goto

    async def goto_elsewhere(self: Any, url: str) -> Any:
        page = await original(self, url)
        cast(ProfileTab, page).user_closed_it()
        return await original(self, url)  # a new tab, not the one being listened to

    monkeypatch.setattr(browser_module.BrowserRun, "goto", goto_elsewhere)
    with pytest.raises(BrowserUnavailable, match="replaced"):
        await visit(site, [target(PRIYA)])


# --- origins, logs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin", ["https://evil.example.test", "http://www.linkedin.com", "http://127.0.0.1\\@x"]
)
async def test_only_linkedin_or_loopback_may_be_read(origin: str) -> None:
    provider, _ = fake_provider(ProfileSite())
    async with provider.run("account-1") as run:
        with pytest.raises(ValueError):
            PageProfiles(run, origin=origin)


async def test_a_loopback_replica_is_read_the_same_way() -> None:
    site = ProfileSite([ProfilePage(PRIYA)], origin="http://127.0.0.1:9999")
    out = await visit(site, [target(PRIYA)], origin="http://127.0.0.1:9999")
    assert out.outcomes == [Outcome.OK]


async def test_no_slug_name_or_address_reaches_a_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    stranger = replace(MATEO, public_id=TOMASZ.slug)
    site = ProfileSite(
        [
            ProfilePage(PRIYA, controls=2),
            ProfilePage(stranger),
            ProfilePage(HANA, overlay_answers=0),
        ]
    )
    await visit(site, [target(PRIYA), target(TOMASZ), target(HANA)])
    text = caplog.text
    for secret in (PRIYA.slug, TOMASZ.slug, HANA.slug, "Priya", "Okafor", "example.test"):
        assert secret not in text
    assert "Contact info was not clicked" in text


def test_the_click_constants_are_pinned() -> None:
    assert browser_module.CONTACT_INFO_ROLE == "link"
    assert browser_module.CONTACT_INFO_NAME == "Contact info"
    assert browser_module.CONTACT_INFO_HREF_SUFFIX == "overlay/contact-info/"
    assert browser_module.CONTACT_INFO_CLICK_TIMEOUT_MS == 10_000.0
    assert browser_module.CONTACT_INFO_PRESS_MS == 90.0
    assert CONTACT_DETAILS_SCREEN_ID.endswith(".ProfileContactDetailsOverlay")


def test_a_scroll_step_type_is_what_the_back_up_uses() -> None:
    assert ScrollStep(delta_px=-300, pause_s=0.2).delta_px < 0


# --- the click method itself -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "path", "refusal"),
    [
        ({"controls": 0}, None, "no Contact info control on the page"),
        ({"controls": 2}, None, "more than one Contact info control"),
        ({"href": "/in/someone-else-fake/overlay/contact-info/"}, None, "opens something else"),
        ({"href": "/in/x/"}, None, "opens something else"),
        # Protocol-relative: the same path on another host is not this profile's.
        ({"href": f"//evil.example.test/in/{PRIYA.slug}/overlay/contact-info/"}, None, "else"),
        ({}, "/in/someone-else-fake/", "the tab is not on the profile"),
    ],
)
async def test_the_click_method_refuses_before_it_clicks(
    options: dict[str, Any], path: str | None, refusal: str
) -> None:
    site = ProfileSite([ProfilePage(PRIYA, **options)])
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        await run.goto(f"{ORIGIN}/in/{PRIYA.slug}/")
        click = await run.click_contact_info(path or f"/in/{PRIYA.slug}/", pause_s=0.0)
    assert not click.clicked
    assert click.refusal is not None and refusal in click.refusal
    assert site.clicks == [] and "click" not in site.lookups


async def test_the_click_method_pauses_first_and_rechecks_where_the_tab_is() -> None:
    site = ProfileSite([ProfilePage(PRIYA)])
    provider, _ = fake_provider(site)
    order: list[str] = []

    async def pause_while_the_tab_moves(seconds: float) -> None:
        order.append(f"pause {seconds}")
        order.extend(site.lookups)
        tab_of(site)._url = f"{ORIGIN}/feed/"  # the person, or the page, went elsewhere

    async with provider.run("account-1") as run:
        await run.goto(f"{ORIGIN}/in/{PRIYA.slug}/")
        click = await run.click_contact_info(
            f"/in/{PRIYA.slug}/", pause_s=1.25, sleep=pause_while_the_tab_moves
        )
    assert order == ["pause 1.25"]  # nothing was looked up before the pause
    assert (click.clicked, click.refusal) == (False, "the tab left the profile before the click")
    assert site.clicks == []


async def test_the_click_method_clicks_once_with_a_persons_press() -> None:
    site = ProfileSite([ProfilePage(PRIYA)])
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        await run.goto(f"{ORIGIN}/in/{PRIYA.slug.upper()}/")  # the slug as the tab spells it
        click = await run.click_contact_info(f"/in/{PRIYA.slug}/", pause_s=0.0)
    assert click.clicked and click.refusal is None
    assert site.clicks == [(PRIYA.slug, 90.0, 10_000.0)]


async def test_the_click_method_never_reopens_a_lost_tab() -> None:
    site = ProfileSite([ProfilePage(PRIYA)])
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        await run.goto(f"{ORIGIN}/in/{PRIYA.slug}/")
        tab_of(site).user_closed_it()
        with pytest.raises(BrowserUnavailable, match="went away"):
            await run.click_contact_info(f"/in/{PRIYA.slug}/", pause_s=0.0)
    assert site.clicks == [] and len(site.tabs) == 1


# --- #193 review -------------------------------------------------------------------------------


def _first_click_pause(visits: int) -> float:
    """The pause the job draws before the first visit's click, from ``visit``'s seed."""
    rng = random.Random(7)
    plan_enrichment(rng, visits)
    return human_delay(rng, median=1.5, sigma=0.5, tail_p=0.0, tail_range=(0, 0))


async def test_a_wall_during_the_pause_before_the_click_stops_the_run() -> None:
    """M1 (a): the tab moves to a checkpoint while the person pauses. The click is
    refused, and the run stops as a checkpoint, not as one unreadable profile."""
    site = ProfileSite([ProfilePage(PRIYA), ProfilePage(MATEO)])
    pause = _first_click_pause(2)

    def wall_arrives(seconds: float) -> None:
        if seconds == pause:
            tab_of(site)._url = CHECKPOINT_URL

    out = await visit(site, [target(PRIYA), target(MATEO)], on_sleep=wall_arrives)
    assert out.result.reason is StopReason.RESPONSE
    assert out.result.outcome is Outcome.CHECKPOINT
    assert out.result.final_url is not None and "/checkpoint/" in out.result.final_url
    assert out.harvests == [] and out.result.visits == 1
    assert site.clicks == [] and tab_of(site).goto_calls == [f"{ORIGIN}/in/{PRIYA.slug}/"]


@pytest.mark.parametrize(
    ("wall", "outcome"), [(CHECKPOINT_URL, Outcome.CHECKPOINT), (LOGIN_URL, Outcome.LOGGED_OUT)]
)
async def test_a_wall_after_the_click_stops_the_run(wall: str, outcome: Outcome) -> None:
    """M1 (b): the click leads to a wall and no overlay answers."""
    site = ProfileSite(
        [ProfilePage(PRIYA, tab_after_click=wall, overlay_answers=0), ProfilePage(MATEO)]
    )
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.result.reason is StopReason.RESPONSE and out.result.outcome is outcome
    assert out.harvests == [] and out.result.visits == 1
    assert [slug for slug, _, _ in site.clicks] == [PRIYA.slug]


async def test_a_wall_after_the_click_stops_the_run_even_when_an_overlay_came() -> None:
    """The overlay answered, but not readably, and the tab is on a checkpoint."""
    overlay = contact_info_payload(PRIYA, email_urls=["https://example.test/not-mail"])
    site = ProfileSite(
        [ProfilePage(PRIYA, overlay=overlay, tab_after_click=CHECKPOINT_URL), ProfilePage(MATEO)]
    )
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.result.outcome is Outcome.CHECKPOINT and out.harvests == []


@pytest.mark.parametrize(
    "request_body",
    [
        f'{{"profileUrn": "{MATEO.urn}"}}',
        f'{{"vieweeProfileId": "{MATEO.urn.rsplit(":", 1)[1]}"}}',
        f'{{"payload": {{"member": {{"profileUrn": "{MATEO.urn}"}}}}}}',
    ],
)
async def test_a_lazy_card_that_names_another_member_by_id_is_skipped(request_body: str) -> None:
    """Positions are never removed, so a wrong-person card must never be read."""
    wrong = experience_payload([Role("Wrong Role", "Wrong Co", None, "2001 - 2002")])
    right = experience_payload([Role("Right", "Right Co", None, "2020 - 2021")])
    site = ProfileSite(
        [
            ProfilePage(
                PRIYA,
                screen=profile_payload(PRIYA, location=LOCATION, experience_inline=False),
                components=(
                    (wrong, request_body),
                    (right, f'{{"profileUrn": "{PRIYA.urn}", "vanityName": "{PRIYA.slug}"}}'),
                ),
            )
        ]
    )
    out = await visit(site, [target(PRIYA)])
    (harvest,) = out.harvests
    assert harvest.details is not None
    assert [p.title for p in harvest.details.positions] == ["Right"]


async def test_a_tab_that_ends_on_another_profile_with_no_redirect_is_unreadable() -> None:
    """A stale tab, or a page that moved by itself: the profile the tab shows is not the
    one asked for, and no redirect the page received led there."""
    site = ProfileSite([ProfilePage(PRIYA, silently_to=MATEO.slug), ProfilePage(MATEO)])
    out = await visit(site, [target(PRIYA, urn=MATEO.urn)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and site.clicks == []


async def test_only_the_documents_404_is_not_found() -> None:
    """The in-app screen request answering 404 is an unreadable visit, never NotFound."""
    site = ProfileSite([ProfilePage(PRIYA, landing="screen", screen_status=404)])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and out.result.not_found == 0


async def test_the_click_method_reads_a_percent_encoded_slug() -> None:
    person = replace(PRIYA, public_id="pr\u00edya-fake")
    site = ProfileSite([ProfilePage(person)])
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        await run.goto(f"{ORIGIN}/in/pr%C3%ADya-fake/")
        click = await run.click_contact_info("/in/pr\u00edya-fake/", pause_s=0.0)
    assert click.clicked, click.refusal


async def test_the_click_method_rechecks_the_tab_after_the_pause() -> None:
    site = ProfileSite([ProfilePage(PRIYA)])
    provider, _ = fake_provider(site)

    async def the_tab_goes_away(seconds: float) -> None:
        tab_of(site).user_closed_it()

    async with provider.run("account-1") as run:
        await run.goto(f"{ORIGIN}/in/{PRIYA.slug}/")
        with pytest.raises(BrowserUnavailable, match="before the Contact info click"):
            await run.click_contact_info(f"/in/{PRIYA.slug}/", pause_s=1.0, sleep=the_tab_goes_away)
    assert site.clicks == [] and site.lookups == []


async def test_more_lazy_cards_than_a_profile_loads_is_unreadable() -> None:
    card = experience_payload([Role("Right", "Right Co", None, "2020 - 2021")])
    site = ProfileSite([ProfilePage(PRIYA, components=tuple((card, None) for _ in range(41)))])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and site.clicks == []


async def test_an_overlay_request_that_names_no_profile_is_unreadable() -> None:
    body = json.dumps({"clientArguments": {"screenId": CONTACT_DETAILS_SCREEN_ID, "payload": {}}})
    site = ProfileSite([ProfilePage(PRIYA, overlay_request=body)])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED]


async def test_a_lazy_card_naming_this_profile_in_another_case_is_kept() -> None:
    card = experience_payload([Role("Right", "Right Co", None, "2020 - 2021")])
    site = ProfileSite(
        [
            ProfilePage(
                PRIYA,
                screen=profile_payload(PRIYA, location=LOCATION, experience_inline=False),
                components=((card, json.dumps({"vanityName": PRIYA.slug.upper()})),),
            )
        ]
    )
    out = await visit(site, [target(PRIYA)])
    (harvest,) = out.harvests
    assert harvest.details is not None and [p.title for p in harvest.details.positions] == ["Right"]


async def test_a_click_on_a_tab_that_is_not_the_observed_one_ends_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = ProfileSite([ProfilePage(PRIYA)])
    original = browser_module.BrowserRun.click_contact_info

    async def click_elsewhere(self: Any, path: str, **kwargs: Any) -> Any:
        click = await original(self, path, **kwargs)
        return browser_module.ContactInfoClick(await site.new_page(), click.clicked)

    monkeypatch.setattr(browser_module.BrowserRun, "click_contact_info", click_elsewhere)
    with pytest.raises(BrowserUnavailable, match="replaced"):
        await visit(site, [target(PRIYA)])
