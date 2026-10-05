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

import inspect
import json
import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlsplit

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
    navigation_timeout,
    streamed_copy,
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
    UnreadableCause,
    UnreadableVisit,
    run_enrichment,
)
from netkeeper.linkedin.flagship import CONTACT_DETAILS_SCREEN_ID, NAVIGATION_PATH
from netkeeper.linkedin.flagship_profile import COMPONENT_PATH
from netkeeper.linkedin.flight import is_whole, parse_flight
from netkeeper.linkedin.observe import (
    ObservationFailed,
    ObservationLimits,
    ResponseMatch,
    ResponseRule,
)
from netkeeper.linkedin.pacing import ScrollPlan, ScrollStep, human_delay, plan_enrichment
from netkeeper.linkedin.page_profiles import CLICK_REFUSAL_CAUSES, PageProfiles
from netkeeper.linkedin.voyager import RouteChanged

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


# --- an answer whose body cannot be read (#197) ---------------------------------------------------

LOST = Exception(
    "Protocol error (Network.getResponseBody): No resource with given identifier found"
    " for https://www.linkedin.com/in/fake-lost-slug-0000/"
)
LOST_CAUSE = "Exception (no resource)"


@pytest.mark.parametrize("landing", ["document", "screen"])
async def test_a_profile_screen_that_cannot_be_read_is_an_unreadable_visit(
    landing: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The next person is still visited: one lost answer is one unreadable profile."""
    caplog.set_level(logging.DEBUG, logger="netkeeper")
    site = ProfileSite([ProfilePage(PRIYA, landing=landing, screen_error=LOST), ProfilePage(MATEO)])
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.result.reason is StopReason.END_OF_PLAN
    assert out.outcomes == [Outcome.ROUTE_CHANGED, Outcome.OK]
    assert out.harvests[0].details is None and out.harvests[0].contact_info is None
    assert out.result.unreadable == 1
    assert out.result.lost == (f"visit 1: the profile screen could not be read ({LOST_CAUSE})",)
    assert [slug for slug, _, _ in site.clicks] == [MATEO.slug]
    assert "fake-lost-slug" not in caplog.text and "identifier" not in caplog.text
    assert f"visit 1 was unreadable: the profile screen could not be read ({LOST_CAUSE})" in (
        caplog.text
    )


async def test_a_lost_document_followed_by_a_screen_that_reads_is_a_whole_visit() -> None:
    """The page may still send the screen another way: then nothing was lost."""

    class ThenScreen(ProfileSite):
        def navigated(self, tab, url):  # type: ignore[no-untyped-def]
            super().navigated(tab, url)
            page = self.profiles[PRIYA.slug.casefold()]
            screen_url = f"{self.origin}/flagship-web{urlsplit(url).path}"
            self._send(tab, "POST", screen_url, 200, page.screen_body(), "fetch", "{}")

    site = ThenScreen([ProfilePage(PRIYA, screen_error=LOST)])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.OK] and out.result.lost == ()
    assert out.harvests[0].contact_info is not None


async def test_lost_screens_count_toward_the_unreadable_limit() -> None:
    """The same limit as every unreadable visit: two in a row stop the run as
    route_changed, which for enrichment raises no heat and flags nothing."""
    people = [PRIYA, MATEO, HANA]
    site = ProfileSite([ProfilePage(p, screen_error=LOST) for p in people])
    out = await visit(site, [target(p) for p in people])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] * MAX_UNREADABLE_IN_A_ROW
    assert out.result.reason is StopReason.RESPONSE
    assert out.result.outcome is Outcome.ROUTE_CHANGED
    assert [line.split(":")[0] for line in out.result.lost] == ["visit 1", "visit 2"]
    assert site.clicks == []


async def test_a_lost_screen_on_a_tab_that_moved_to_a_wall_stops_the_run() -> None:
    """Where the tab is still decides first."""

    class WallLaterTab(ProfileTab):
        """On the profile when the landing looks, on a checkpoint by the time the
        visit gives up on the lost screen."""

        reads = 0

        @property
        def url(self) -> str:
            self.reads += 1
            return self._url if self.reads == 1 else CHECKPOINT_URL

    class WallLater(ProfileSite):
        async def new_page(self):  # type: ignore[no-untyped-def]
            tab = WallLaterTab(self)
            self.tabs.append(tab)
            self.pages.append(tab)
            return tab

    site = WallLater([ProfilePage(PRIYA, screen_error=LOST), ProfilePage(MATEO)])
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.result.outcome is Outcome.CHECKPOINT and out.harvests == []
    assert out.result.lost == ()


async def test_a_lost_contact_info_answer_is_an_unreadable_visit_and_no_second_click(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="netkeeper")
    site = ProfileSite([ProfilePage(PRIYA, overlay_error=LOST), ProfilePage(MATEO)])
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.result.reason is StopReason.END_OF_PLAN
    assert out.outcomes == [Outcome.ROUTE_CHANGED, Outcome.OK]
    assert out.harvests[0].details is None and out.harvests[0].contact_info is None
    assert out.result.lost == (
        f"visit 1: the Contact info answer could not be read ({LOST_CAUSE})",
    )
    # One click per visit: Priya's was spent, and not tried again.
    assert [slug for slug, _, _ in site.clicks] == [PRIYA.slug, MATEO.slug]
    assert out.result.clicks == 2
    assert "fake-lost-slug" not in caplog.text


async def test_a_lost_contact_info_answer_on_a_tab_that_moved_to_a_wall_stops_the_run() -> None:
    site = ProfileSite(
        [ProfilePage(PRIYA, overlay_error=LOST, tab_after_click=LOGIN_URL), ProfilePage(MATEO)]
    )
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.result.outcome is Outcome.LOGGED_OUT and out.harvests == []
    assert [slug for slug, _, _ in site.clicks] == [PRIYA.slug]


async def test_a_profile_under_another_urn_is_never_clicked_even_after_a_lost_answer() -> None:
    """The member-id check still decides: a lost card changes nothing about whose
    profile it is."""
    site = ProfileSite([ProfilePage(PRIYA, components=((b"", None),), component_error=LOST)])
    out = await visit(site, [target(PRIYA, urn=MATEO.urn)])
    assert site.clicks == [] and out.result.mismatched == 1


async def test_a_lazy_card_that_cannot_be_read_is_skipped_not_the_profile() -> None:
    site = ProfileSite([ProfilePage(PRIYA, components=((b"", None),), component_error=LOST)])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.OK] and out.result.lost == ()


async def test_an_overlay_too_large_to_keep_still_ends_the_run() -> None:
    """Only a body the browser could not hand over is lost; one too large to keep is
    the observation failing, as before."""
    site = ProfileSite([ProfilePage(PRIYA, overlay=b"0:" + b"0" * 9000)])
    with pytest.raises(ObservationFailed):
        await visit(site, [target(PRIYA)], limits=ObservationLimits(max_body_bytes=8000))
    assert [slug for slug, _, _ in site.clicks] == [PRIYA.slug]  # it failed at the overlay


# --- a profile navigation that times out (#197) ------------------------------------------------

TIMED_OUT = "the profile could not be opened (navigation timed out)"


async def test_a_profile_navigation_that_times_out_is_an_unreadable_visit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The document broke off and the page never loaded: one unreadable visit, not
    tried again, and the next person is visited and clicked."""
    caplog.set_level(logging.DEBUG, logger="netkeeper")
    site = ProfileSite([ProfilePage(PRIYA, goto_error=navigation_timeout()), ProfilePage(MATEO)])
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.result.reason is StopReason.END_OF_PLAN
    assert out.outcomes == [Outcome.ROUTE_CHANGED, Outcome.OK]
    assert out.result.unreadable == 1 and out.result.lost == (f"visit 1: {TIMED_OUT}",)
    assert site.navigations == [f"/in/{PRIYA.slug}/", f"/in/{MATEO.slug}/"]  # no retry
    assert [slug for slug, _, _ in site.clicks] == [MATEO.slug]
    assert "Timeout 30000ms" not in caplog.text and "fake-lost-slug" not in caplog.text


async def test_navigation_timeouts_count_toward_the_unreadable_limit() -> None:
    people = [PRIYA, MATEO, HANA]
    site = ProfileSite([ProfilePage(p, goto_error=navigation_timeout()) for p in people])
    out = await visit(site, [target(p) for p in people])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] * MAX_UNREADABLE_IN_A_ROW
    assert out.result.reason is StopReason.RESPONSE
    assert out.result.outcome is Outcome.ROUTE_CHANGED
    assert len(site.navigations) == MAX_UNREADABLE_IN_A_ROW


@pytest.mark.parametrize(
    ("wall", "outcome"), [(CHECKPOINT_URL, Outcome.CHECKPOINT), (LOGIN_URL, Outcome.LOGGED_OUT)]
)
async def test_a_navigation_that_times_out_on_a_wall_stops_the_run(
    wall: str, outcome: Outcome
) -> None:
    page = ProfilePage(PRIYA, goto_error=navigation_timeout(), tab_after_goto=wall)
    site = ProfileSite([page, ProfilePage(MATEO)])
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.result.reason is StopReason.RESPONSE and out.result.outcome is outcome
    assert out.harvests == [] and out.result.lost == ()


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("net::ERR_CONNECTION_REFUSED"),
        TimeoutError("not Playwright's"),
    ],
    ids=["another-error", "builtin-timeout"],
)
async def test_any_other_navigation_error_still_ends_the_run(error: Exception) -> None:
    site = ProfileSite([ProfilePage(PRIYA, goto_error=error), ProfilePage(MATEO)])
    with pytest.raises(type(error)):
        await visit(site, [target(PRIYA), target(MATEO)])


async def test_another_playwright_error_still_ends_the_run() -> None:
    from playwright.async_api import Error as PlaywrightError

    site = ProfileSite([ProfilePage(PRIYA, goto_error=PlaywrightError("net::ERR_ABORTED"))])
    with pytest.raises(PlaywrightError):
        await visit(site, [target(PRIYA)])


async def test_a_navigation_timeout_after_the_tab_was_lost_is_browser_unavailable() -> None:
    """The tab closed with the first navigation; BrowserRun reopened it and navigated
    once more, which timed out. The tab being listened to is gone: that is the
    browser failing, never an unreadable visit."""
    page = ProfilePage(PRIYA, goto_error=navigation_timeout(), goto_closes_tab=1)
    site = ProfileSite([page, ProfilePage(MATEO)])
    with pytest.raises(BrowserUnavailable):
        await visit(site, [target(PRIYA), target(MATEO)])


@pytest.mark.parametrize(
    ("page_kwargs", "outcome"),
    [
        ({"landing": "status:429"}, Outcome.THROTTLED),
        ({"landing": "status:999"}, Outcome.THROTTLED),
        ({"redirect_location": CHECKPOINT_URL}, Outcome.CHECKPOINT),
        ({"redirect_location": LOGIN_URL}, Outcome.LOGGED_OUT),
    ],
    ids=["429", "999", "redirect-to-checkpoint", "redirect-to-login"],
)
async def test_what_the_page_already_answered_decides_before_a_timeout(
    page_kwargs: dict[str, Any], outcome: Outcome
) -> None:
    """#198 review, H1: the document answered a throttle, or a redirect to a wall the
    tab never followed, and then the navigation hung. The queued answer is read before
    the timeout is called an unreadable visit: the run stops as what LinkedIn said,
    and the next person is never visited."""
    page = ProfilePage(PRIYA, goto_error=navigation_timeout(), **page_kwargs)
    site = ProfileSite([page, ProfilePage(MATEO)])
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.result.reason is StopReason.RESPONSE and out.result.outcome is outcome
    assert out.harvests == [] and out.result.lost == ()
    assert site.navigations == [f"/in/{PRIYA.slug}/"]


@pytest.mark.parametrize("move", ["redirect_to", "silently_to"])
@pytest.mark.parametrize(
    ("landing", "outcome"),
    [("status:429", Outcome.THROTTLED), ("status:999", Outcome.THROTTLED)],
    ids=["429", "999"],
)
async def test_a_throttle_on_the_renamed_profile_before_a_timeout_stops_the_run(
    landing: str, outcome: Outcome, move: str
) -> None:
    """#196 item 7: Priya's slug redirects to a renamed one (or the tab ends up on it
    with no redirect seen), the renamed profile's document answers a throttle, and then
    the navigation times out. That throttle is this visit's: the run stops, and the
    next person is never visited."""
    renamed = replace(PRIYA, public_id="priya-renamed-fake")
    first = ProfilePage(PRIYA, goto_error=navigation_timeout())
    setattr(first, move, renamed.slug)
    site = ProfileSite([first, ProfilePage(renamed, landing=landing), ProfilePage(MATEO)])
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.result.reason is StopReason.RESPONSE and out.result.outcome is outcome
    assert out.harvests == [] and out.result.lost == ()
    assert f"/in/{MATEO.slug}/" not in site.navigations


@pytest.mark.parametrize(
    ("stale", "outcome"),
    [
        (Stale("POST", COMPONENT_PATH, b"", status=429), Outcome.THROTTLED),
        (Stale("POST", NAVIGATION_PATH, b"", status=999), Outcome.THROTTLED),
        (
            Stale("POST", COMPONENT_PATH, b"", status=302, headers={"location": CHECKPOINT_URL}),
            Outcome.CHECKPOINT,
        ),
        (
            Stale("POST", NAVIGATION_PATH, b"", status=302, headers={"location": LOGIN_URL}),
            Outcome.LOGGED_OUT,
        ),
    ],
    ids=["card-429", "overlay-999", "card-to-checkpoint", "overlay-to-login"],
)
async def test_a_lazy_card_or_overlay_throttle_before_a_timeout_stops_the_run(
    stale: Stale, outcome: Outcome
) -> None:
    """#196 item 8: a lazy card's or an overlay's answer, queued before the profile's
    navigation timed out, says throttle or wall. The run stops as it says, and the next
    person is never visited."""
    site = ProfileSite(
        [ProfilePage(PRIYA, goto_error=navigation_timeout()), ProfilePage(MATEO)], stale=[stale]
    )
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.result.reason is StopReason.RESPONSE and out.result.outcome is outcome
    assert out.harvests == [] and out.result.lost == ()
    assert site.navigations == [f"/in/{PRIYA.slug}/"]


async def test_a_screen_request_404_before_a_timeout_is_only_unreadable() -> None:
    """As the landing reads it: the screen request's 404 is one unreadable visit, never
    NotFound and never a stop, so the next person is still visited."""
    page = ProfilePage(PRIYA, goto_error=navigation_timeout(), landing="screen", screen_status=404)
    site = ProfileSite([page, ProfilePage(MATEO)])
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED, Outcome.OK]
    assert out.result.reason is StopReason.END_OF_PLAN and out.result.lost == ()


async def test_a_document_404_before_a_timeout_is_not_found() -> None:
    page = ProfilePage(PRIYA, goto_error=navigation_timeout(), landing="404")
    out = await visit(ProfileSite([page]), [target(PRIYA)])
    assert out.outcomes == [Outcome.NOT_FOUND]


async def test_a_timeout_records_where_the_visit_went_never_an_empty_url() -> None:
    """#198 review, L3: two timed-out visits stop the run at the limit, and the run's
    final url is the profile asked for, masked -- even when the tab never left
    ``about:blank``."""
    people = [PRIYA, MATEO]
    site = ProfileSite(
        [
            ProfilePage(p, goto_error=navigation_timeout(), tab_after_goto="about:blank")
            for p in people
        ]
    )
    out = await visit(site, [target(p) for p in people])
    assert out.result.outcome is Outcome.ROUTE_CHANGED
    assert out.result.final_url == f"{ORIGIN}/in/_/"


async def test_a_later_visit_never_inherits_an_earlier_lost_screen() -> None:
    """#198 review, L2: Priya's screen was lost; Mateo's page never sends one at all.
    Only Priya's visit is a lost answer: Mateo's is unreadable for its own reason."""
    site = ProfileSite([ProfilePage(PRIYA, screen_error=LOST), ProfilePage(MATEO, landing="shell")])
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED, Outcome.ROUTE_CHANGED]
    assert out.result.lost == (f"visit 1: the profile screen could not be read ({LOST_CAUSE})",)


def test_the_navigation_timeout_cause_is_pinned() -> None:
    from netkeeper.linkedin import page_profiles

    assert page_profiles.NAVIGATION_TIMED_OUT == "navigation timed out"


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


async def test_a_stale_redirect_from_another_page_leads_nowhere_this_visit_accepts() -> None:
    """#196 item 1: as the tab leaves Mateo's page, a redirect Mateo's document received
    (to Hana) arrives first; then the tab ends on Hana's profile with no redirect of its
    own. That stale redirect did not come from the slug asked for or from any target of
    this visit's own redirects, so it does not make Hana's profile this visit's."""
    stale = [
        Stale(
            "GET",
            f"/in/{MATEO.slug}/",
            b"",
            status=301,
            headers={"location": f"{ORIGIN}/in/{HANA.slug}/"},
        )
    ]
    site = ProfileSite([ProfilePage(PRIYA, silently_to=HANA.slug), ProfilePage(HANA)], stale=stale)
    out = await visit(site, [target(PRIYA, urn=HANA.urn)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and site.clicks == []


@pytest.mark.parametrize(
    ("wall", "outcome"), [(CHECKPOINT_URL, Outcome.CHECKPOINT), (LOGIN_URL, Outcome.LOGGED_OUT)]
)
async def test_a_stale_redirect_to_a_wall_still_stops_the_run(wall: str, outcome: Outcome) -> None:
    """#309 review, M8: a redirect another page received is never followed, but a wall
    is a wall whoever's request it answered -- the wall check comes before the chain
    check, so the run stops and the next person is never visited."""
    stale = [Stale("GET", f"/in/{MATEO.slug}/", b"", status=302, headers={"location": wall})]
    site = ProfileSite([ProfilePage(PRIYA), ProfilePage(HANA)], stale=stale)
    out = await visit(site, [target(PRIYA), target(HANA)])
    assert out.result.reason is StopReason.RESPONSE and out.result.outcome is outcome
    assert out.harvests == [] and site.clicks == []
    assert f"/in/{HANA.slug}/" not in site.navigations


async def test_a_redirect_chain_is_followed_through_each_of_its_own_targets() -> None:
    """The other side of #196 item 1: Priya's slug redirects to a renamed slug, which
    redirects again. The second redirect's request is the first's target, so the
    profile it lands on is this visit's."""
    first = replace(PRIYA, public_id="priya-renamed-fake")
    second = replace(PRIYA, public_id="priya-renamed-again-fake")
    site = ProfileSite(
        [
            ProfilePage(PRIYA, redirect_to=first.slug),
            ProfilePage(first, redirect_to=second.slug),
            ProfilePage(second),
        ]
    )
    out = await visit(site, [target(PRIYA)])
    (harvest,) = out.harvests
    assert harvest.outcome is Outcome.OK and harvest.details is not None
    assert harvest.details.public_id == second.slug
    assert [slug for slug, _, _ in site.clicks] == [second.slug]


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


# --- #203: what the first live run showed ------------------------------------------------------


async def test_the_top_card_logs_once_per_visit(caplog: pytest.LogCaptureFixture) -> None:
    """The id is read before the lazy cards are sorted; the top card is read once."""
    caplog.set_level(logging.DEBUG, logger="netkeeper")
    screen = profile_payload(PRIYA, location=LOCATION, extra_top_runs=["Some Short Run"])
    site = ProfileSite([ProfilePage(PRIYA, screen=screen)])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.OK]
    lines = [r for r in caplog.records if "runs before its link" in r.getMessage()]
    assert len(lines) == 1


async def test_a_linkedin_website_keeps_the_visit_and_the_rest_of_the_overlay() -> None:
    """Run 9 on #31: a website on LinkedIn's own host made three of five visits
    unreadable. It is a site a person listed, or their own profile, never a new shape."""
    overlay = contact_info_payload(
        PRIYA,
        emails=[f"{PRIYA.slug}@example.test"],
        website_urls=[
            f"https://www.linkedin.com/in/{PRIYA.slug}/",
            "https://www.linkedin.com/company/fictional-robotics-co/",
        ],
    )
    site = ProfileSite([ProfilePage(PRIYA, overlay=overlay)])
    out = await visit(site, [target(PRIYA)])
    (harvest,) = out.harvests
    assert harvest.outcome is Outcome.OK and harvest.contact_info is not None
    assert harvest.contact_info.emails == (f"{PRIYA.slug}@example.test",)
    assert harvest.contact_info.websites == (
        "https://www.linkedin.com/company/fictional-robotics-co/",
    )


# --- #203: the body tap, for the lazy cards and the overlay -----------------------------------

LAZY_ROLES = (Role("Staff Engineer", "Fictional Robotics Co", "Full-time", "Aug 2021 - Present"),)


def _lazy_page(how: str, **extra: Any) -> ProfilePage:
    """Priya's profile with her experience in a lazy card whose body is lost."""
    return ProfilePage(
        PRIYA,
        screen=profile_payload(PRIYA, location=LOCATION, experience_inline=False),
        components=((experience_payload(LAZY_ROLES), None),),
        component_error=LOST,
        component_streamed=how,
        **extra,
    )


async def test_a_lost_lazy_card_reads_from_its_whole_streamed_copy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="netkeeper")
    site = ProfileSite([_lazy_page("whole")], tap=True)
    out = await visit(site, [target(PRIYA)])
    (harvest,) = out.harvests
    assert harvest.outcome is Outcome.OK and harvest.details is not None
    assert [p.title for p in harvest.details.positions] == ["Staff Engineer"]
    assert "read a lazy card from the copy streamed as it arrived" in caplog.text
    assert "fake-lost-slug" not in caplog.text
    # #196 item 12: the run notes it; the harvest is applied like any other.
    assert out.result.copied == ("visit 1: a lazy card was read from a streamed copy",)
    assert harvest.contact_info is not None and not harvest.contact_info_from_copy


@pytest.mark.parametrize("how", ["half", "rows", "orphan", "none"])
async def test_a_lost_lazy_card_without_a_whole_copy_is_still_skipped(
    how: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="netkeeper")
    site = ProfileSite([_lazy_page(how)], tap=True)
    out = await visit(site, [target(PRIYA)])
    (harvest,) = out.harvests
    assert harvest.outcome is Outcome.OK and harvest.details is not None
    assert harvest.details.positions == ()
    assert out.result.copied == ()
    assert "skipped a lazy card that could not be read" in caplog.text
    assert "read a lazy card from the copy" not in caplog.text
    not_whole = "the streamed copy of a lost answer is not whole; not used"
    assert (not_whole in caplog.text) is (how != "none")


@pytest.mark.parametrize("body", ["card", "overlay"])
def test_every_row_cut_of_a_fixture_parses_or_not_but_is_never_whole(body: str) -> None:
    """The fixtures write rows child first, root last, as React's ``outlineModel``
    does; ``rows`` reorders them root first. Either way no cut copy is whole."""
    whole = (
        experience_payload(LAZY_ROLES)
        if body == "card"
        else contact_info_payload(PRIYA, emails=["priya.fake@example.test"])
    )
    assert is_whole(parse_flight(whole, endpoint="test"), endpoint="test")
    for ordered in (whole, streamed_copy(whole, "reordered")):
        assert ordered is not None
        lines = ordered.splitlines(keepends=True)
        for cut in range(1, len(lines)):
            prefix = b"".join(lines[:cut])
            try:
                payload = parse_flight(prefix, endpoint="test")
            except RouteChanged:
                continue
            assert not is_whole(payload, endpoint="test"), (body, cut)


async def test_a_cut_overlay_copy_before_its_email_row_is_never_read() -> None:
    """The #207 review's case: child rows first, cut before the email's row. The copy
    parses, and without the root would have read as a person who shares nothing."""
    overlay = contact_info_payload(PRIYA, emails=[f"{PRIYA.slug}@example.test"])
    lines = overlay.splitlines(keepends=True)
    email_row = next(i for i, line in enumerate(lines) if b"mailto:" in line)
    cut = b"".join(lines[:email_row])
    site = ProfileSite(
        [ProfilePage(PRIYA, overlay=overlay, overlay_error=LOST, overlay_streamed="custom")],
        tap=True,
    )
    site.custom_copies[overlay] = cut
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and out.harvests[0].contact_info is None


async def test_a_lost_overlay_reads_from_its_whole_streamed_copy() -> None:
    site = ProfileSite(
        [ProfilePage(PRIYA, overlay_error=LOST, overlay_streamed="whole"), ProfilePage(MATEO)],
        tap=True,
    )
    out = await visit(site, [target(PRIYA), target(MATEO)])
    assert out.outcomes == [Outcome.OK, Outcome.OK] and out.result.lost == ()
    info = out.harvests[0].contact_info
    assert info is not None and info.emails == (f"{PRIYA.slug}@example.test",)
    assert [slug for slug, _, _ in site.clicks] == [PRIYA.slug, MATEO.slug]
    # The harvest says its Contact info came from a copy, and the run notes it.
    assert [h.contact_info_from_copy for h in out.harvests] == [True, False]
    assert out.result.copied == ("visit 1: the Contact info was read from a streamed copy",)


@pytest.mark.parametrize("how", ["half", "rows", "orphan", "none"])
async def test_a_lost_overlay_without_a_whole_copy_is_still_unreadable(how: str) -> None:
    site = ProfileSite([ProfilePage(PRIYA, overlay_error=LOST, overlay_streamed=how)], tap=True)
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED]
    assert out.result.lost == (
        f"visit 1: the Contact info answer could not be read ({LOST_CAUSE})",
    )
    assert [slug for slug, _, _ in site.clicks] == [PRIYA.slug]  # never clicked again


async def test_a_whole_copy_of_another_profiles_overlay_is_still_refused() -> None:
    """A copy is read as strictly as a body: it must name this profile."""
    other = contact_info_payload(PRIYA, emails=["x@example.test"], profile_slug=MATEO.slug)
    site = ProfileSite(
        [ProfilePage(PRIYA, overlay=other, overlay_error=LOST, overlay_streamed="whole")],
        tap=True,
    )
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and out.harvests[0].contact_info is None


async def test_the_tap_streams_only_lazy_cards_and_the_overlay() -> None:
    """Never the profile's document or screen: the tap's match is narrower than the
    visit's. The session sends the two read-only methods only, and is detached."""
    site = ProfileSite([_lazy_page("whole", landing="screen")], tap=True)
    await visit(site, [target(PRIYA)])
    assert site.cdp is not None and site.cdp.detached
    assert {method for method, _ in site.cdp.sent} == {
        "Network.enable",
        "Network.streamResourceContent",
    }
    asked = {
        request_id: path
        for request_id, (_, path, _) in zip(
            (f"fake.{n}" for n in range(1, len(site.requests) + 1)), site.requests, strict=True
        )
    }
    streamed = {urlsplit(asked[request_id]).path for request_id in site.streamed_ids}
    assert streamed == {COMPONENT_PATH, NAVIGATION_PATH}
    assert any(path.startswith("/flagship-web/in/") for path in asked.values())
    assert any(path.startswith("/in/") for path in asked.values())


async def test_a_browser_without_cdp_sessions_reads_as_before() -> None:
    site = ProfileSite([_lazy_page("whole")])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.OK] and out.harvests[0].details is not None
    assert out.harvests[0].details.positions == ()


@pytest.mark.parametrize(
    "tapped",
    [
        ResponseMatch(origin="http://127.0.0.1:9", rules=(ResponseRule("POST", COMPONENT_PATH),)),
        ResponseMatch(origin=ORIGIN, rules=(ResponseRule("POST", "/flagship-web/other"),)),
        ResponseMatch(origin=ORIGIN, rules=(ResponseRule("GET", COMPONENT_PATH),)),
    ],
)
async def test_a_tap_may_only_narrow_its_observation(tapped: ResponseMatch) -> None:
    site = ProfileSite(tap=True)
    provider, _ = fake_provider(site)
    match = ResponseMatch(
        origin=ORIGIN,
        rules=(ResponseRule("POST", COMPONENT_PATH), ResponseRule("POST", NAVIGATION_PATH)),
    )
    async with provider.run("account-1") as run:
        untapped = await run.observe(match)
        await untapped.close()
        with pytest.raises(ValueError, match="only narrow"):
            await run.observe(match, tap=tapped)
        before = site.cdp
        assert before is None  # no tap asked for, then one refused before it opened
        narrower = ResponseMatch(origin=ORIGIN, rules=(ResponseRule("POST", NAVIGATION_PATH),))
        observation = await run.observe(match, tap=narrower)
        opened = site.cdp
        assert opened is not None
        await observation.close()
        assert opened.detached


# --- #405: each unreadable path has its own cause on the run's record ------------------------


def _cause_cases() -> list[Any]:
    card = experience_payload([Role("Right", "Right Co", None, "2020 - 2021")])
    cases: list[tuple[ProfilePage, UnreadableCause]] = [
        (
            ProfilePage(PRIYA, screen=profile_payload(PRIYA, location=LOCATION, identity="none")),
            UnreadableCause.PROFILE_SHAPE_UNKNOWN,
        ),
        (
            ProfilePage(
                PRIYA,
                overlay=contact_info_payload(PRIYA, email_urls=["https://example.test/not-mail"]),
            ),
            UnreadableCause.CONTACT_INFO_SHAPE_UNKNOWN,
        ),
        (ProfilePage(PRIYA, landing=f"{ORIGIN}/feed/"), UnreadableCause.LANDED_OFF_PROFILE),
        (ProfilePage(PRIYA, tab_after_scroll=f"{ORIGIN}/feed/"), UnreadableCause.LEFT_PROFILE),
        (ProfilePage(PRIYA, silently_to=MATEO.slug), UnreadableCause.UNEXPECTED_PROFILE),
        (ProfilePage(PRIYA, landing="shell"), UnreadableCause.NO_PROFILE_SCREEN),
        (
            ProfilePage(PRIYA, landing="screen", screen_status=404),
            UnreadableCause.PROFILE_SCREEN_STATUS,
        ),
        (
            ProfilePage(PRIYA, components=tuple((card, None) for _ in range(41))),
            UnreadableCause.TOO_MANY_LAZY_CARDS,
        ),
        (ProfilePage(PRIYA, controls=0), UnreadableCause.CONTACT_INFO_CONTROL_MISSING),
        (ProfilePage(PRIYA, controls=2), UnreadableCause.CONTACT_INFO_CONTROL_NOT_ALONE),
        (
            ProfilePage(PRIYA, href="/in/someone-else-fake/overlay/contact-info/"),
            UnreadableCause.CONTACT_INFO_CONTROL_ELSEWHERE,
        ),
        (
            ProfilePage(PRIYA, click_error=RuntimeError("not actionable")),
            UnreadableCause.CONTACT_INFO_CONTROL_UNCLICKABLE,
        ),
        (ProfilePage(PRIYA, overlay_answers=0), UnreadableCause.OVERLAY_NEVER_ANSWERED),
        (ProfilePage(PRIYA, overlay_vanity=MATEO.slug), UnreadableCause.OVERLAY_OTHER_PROFILE),
        (ProfilePage(PRIYA, overlay_status=500), UnreadableCause.OVERLAY_STATUS),
        (
            ProfilePage(PRIYA, goto_error=navigation_timeout()),
            UnreadableCause.NAVIGATION_TIMED_OUT,
        ),
        (ProfilePage(PRIYA, screen_error=LOST), UnreadableCause.PROFILE_SCREEN_LOST),
        (ProfilePage(PRIYA, overlay_error=LOST), UnreadableCause.CONTACT_INFO_LOST),
    ]
    return [pytest.param(page, cause, id=cause.value) for page, cause in cases]


@pytest.mark.parametrize(("page", "cause"), _cause_cases())
async def test_each_unreadable_path_records_its_own_cause(
    page: ProfilePage, cause: UnreadableCause, caplog: pytest.LogCaptureFixture
) -> None:
    """#405: the run's record says which path made the visit unreadable, by a fixed code
    and the contact's reference, and the log line says the same; nothing from the page."""
    caplog.set_level(logging.INFO, logger="netkeeper")
    site = ProfileSite([page, ProfilePage(MATEO)])
    out = await visit(
        site, [target(PRIYA, urn=MATEO.urn if page.silently_to else None), target(MATEO)]
    )
    assert out.outcomes[0] is Outcome.ROUTE_CHANGED
    assert out.harvests[0].unreadable_cause is cause
    assert out.harvests[1].unreadable_cause is None
    assert out.result.unreadable_visits == (UnreadableVisit(1, PRIYA.n, cause),)
    assert f"visit 1 was unreadable ({cause.value})" in caplog.text
    assert PRIYA.slug not in caplog.text


async def test_a_profile_under_another_id_is_recorded_as_a_mismatch() -> None:
    site = ProfileSite([ProfilePage(PRIYA), ProfilePage(MATEO)])
    out = await visit(site, [target(PRIYA, urn=MATEO.urn), target(MATEO)])
    assert out.outcomes == [Outcome.OK, Outcome.OK]
    assert out.harvests[0].unreadable_cause is UnreadableCause.ID_MISMATCH
    assert out.result.unreadable_visits == (
        UnreadableVisit(1, PRIYA.n, UnreadableCause.ID_MISMATCH),
    )


def test_every_click_refusal_the_browser_gives_has_its_own_cause() -> None:
    """A refusal phrase added to ``click_contact_info`` must be given a cause here too,
    or its visits read as the catch-all ``contact_info_not_clicked``."""
    source = inspect.getsource(browser_module.BrowserRun.click_contact_info)
    phrases = set(re.findall(r'ContactInfoClick\(page, False, "([^"]+)"\)', source))
    assert phrases, "found no refusal phrases; the pattern no longer matches the source"
    assert phrases == set(CLICK_REFUSAL_CAUSES)
    controls = {
        "no Contact info control on the page": "contact_info_control_missing",
        "more than one Contact info control": "contact_info_control_not_alone",
    }
    for phrase, code in controls.items():
        assert CLICK_REFUSAL_CAUSES[phrase].value == code
