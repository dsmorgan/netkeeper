"""netkeeper.linkedin.enrich: the enrichment job, against a fake tab and a fake gate.

No database and no browser: :class:`voyager_profiles.FakeBrowser` answers from
invented profiles and records every navigation, scroll, and fetch in order, and
the gate here is a list of answers. The runner that wires this job to budgets,
heat, and rows is exercised in ``tests/test_enrichment.py``.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from voyager_profiles import (
    BAD_REQUEST,
    CHECKPOINT,
    LOGGED_OUT,
    NOT_FOUND,
    PROFILES,
    THROTTLED,
    UNRECOGNIZED,
    FakeBrowser,
    Job,
    Profile,
    School,
    Scripted,
    contact_info_body,
    details_body,
)

from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.enrich import (
    BrowserProfiles,
    EnrichJobSpec,
    EnrichResult,
    EnrichTarget,
    PacingProfile,
    ProfileHarvest,
    ProgressEvent,
    StopReason,
    run_enrichment,
    stretched,
)
from netkeeper.linkedin.pacing import DelayProfile, plan_enrichment
from netkeeper.linkedin.voyager import (
    ContactInfo,
    ProfileDetails,
    parse_contact_info,
    parse_profile_details,
)

FIXTURES = Path(__file__).parent / "fixtures" / "voyager"
NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
SEED = 7


class Gate:
    """Lets every visit through unless told otherwise, and records what it was asked."""

    def __init__(
        self,
        refuse_at: dict[int, StopReason] | None = None,
        cancel_in_pause: int | None = None,
    ) -> None:
        self.refuse_at = refuse_at or {}
        self.cancel_in_pause = cancel_in_pause
        self.asked: list[int] = []
        self.pauses: list[float] = []
        self.log: list[tuple[str, object]] | None = None

    async def before_visit(self, number: int) -> StopReason | None:
        self.asked.append(number)
        if self.log is not None:
            self.log.append(("gate", number))
        return self.refuse_at.get(number)

    async def pause(self, seconds: float) -> bool:
        self.pauses.append(seconds)
        if self.log is not None:
            self.log.append(("pause", seconds))
        return self.cancel_in_pause != len(self.pauses)


def _spec(
    profiles: tuple[Profile, ...] | list[Profile] = PROFILES,
    *,
    budget: int = 100,
    multiplier: float = 1.0,
) -> EnrichJobSpec:
    return EnrichJobSpec(
        targets=tuple(EnrichTarget(p.n, p.slug) for p in profiles),
        visit_budget=budget,
        heat_multiplier=multiplier,
    )


async def _run(
    spec: EnrichJobSpec, browser: FakeBrowser, gate: Gate | None = None
) -> tuple[EnrichResult, list[ProfileHarvest], list[ProgressEvent]]:
    harvests: list[ProfileHarvest] = []
    events: list[ProgressEvent] = []

    async def on_harvest(harvest: ProfileHarvest) -> None:
        harvests.append(harvest)
        browser.events.append(("harvest", harvest.contact_ref))

    async def on_progress(event: ProgressEvent) -> None:
        events.append(event)

    result = await run_enrichment(
        spec,
        browser.source(),
        gate or Gate(),
        on_harvest=on_harvest,
        on_progress=on_progress,
        rng=random.Random(SEED),
        clock=lambda: NOW,
    )
    return result, harvests, events


# --- the fixtures and the builders agree ---------------------------------------------


def test_the_profile_builder_rebuilds_the_details_fixture() -> None:
    jamie = Profile(
        1,
        "Jamie",
        "Rivera",
        headline="Product designer at Fictional Robotics Co",
        location="Faketown, State of Example",
        jobs=(
            Job("Product Designer", "Fictional Robotics Co", start=(2022, 3)),
            Job("Associate Designer", "Prior Example Studio", start=(2019, 6), end=(2022, 2)),
        ),
        schools=(School("Fictional State University", "B.A.", "Design", 2015, 2019),),
        public_id="jamie-fake-rivera-1a2b3c4d",
    )
    fixture = (FIXTURES / "profile_details.json").read_text(encoding="utf-8")
    assert parse_profile_details(details_body(jamie)) == parse_profile_details(fixture)


def test_the_contact_info_builder_rebuilds_the_fixture() -> None:
    jamie = Profile(
        1,
        "Jamie",
        "Rivera",
        email="jamie.fake.rivera@example-mail.test",
        phones=("+1-555-0101",),
        websites=("https://jamie-fake-rivera.example.test",),
        twitter=("jamiefakerivera",),
    )
    fixture = json.loads((FIXTURES / "contact_info.json").read_text(encoding="utf-8"))
    assert json.loads(contact_info_body(jamie)) == fixture


# --- spec 9.4's order, one visit at a time --------------------------------------------


async def test_each_visit_navigates_scrolls_then_fetches_then_hands_over() -> None:
    """Spec 9.4: navigate, scroll and dwell, fetch details and contact info, then the core."""
    browser = FakeBrowser.of(PROFILES[:3])
    gate = Gate()
    gate.log = browser.events

    result, harvests, _ = await _run(_spec(PROFILES[:3]), browser, gate)

    kinds = browser.kinds()
    one_visit = ["gate", "goto", "scroll", "details", "contact_info", "harvest"]
    assert kinds == [*one_visit, "pause", *one_visit, "pause", *one_visit]
    assert browser.visited() == [p.slug for p in PROFILES[:3]]
    assert result.reason is StopReason.END_OF_PLAN
    assert result.completed == (101, 102, 103) and result.visits == 3
    assert [h.outcome for h in harvests] == [Outcome.OK] * 3


async def test_a_harvest_carries_everything_the_visit_found() -> None:
    browser = FakeBrowser.of(PROFILES[:1])
    _, harvests, _ = await _run(_spec(PROFILES[:1]), browser)

    (harvest,) = harvests
    assert harvest.contact_ref == 101 and harvest.requested_public_id == PROFILES[0].slug
    assert harvest.observed_at == NOW
    assert isinstance(harvest.details, ProfileDetails)
    assert harvest.details.urn == PROFILES[0].urn
    assert [p.title for p in harvest.details.positions] == ["Staff Data Engineer", "Data Engineer"]
    assert [e.school for e in harvest.details.education] == ["Fictional State University"]
    assert harvest.contact_info == ContactInfo(
        email="priya.fake.okafor@example.test",
        phones=("+1-555-0101",),
        websites=("https://priya-fake-okafor.example.test",),
        twitter_handles=("priyafakeokafor",),
    )


async def test_the_scroll_and_the_waits_are_the_pacing_plan() -> None:
    """The same seed plans the same visits: each scroll and each gap comes from it."""
    browser = FakeBrowser.of(PROFILES)
    gate = Gate()

    result, _, _ = await _run(_spec(PROFILES), browser, gate)

    expected = plan_enrichment(random.Random(SEED), len(PROFILES))
    assert result.plan == expected
    scrolls = [plan for kind, plan in browser.events if kind == "scroll"]
    assert scrolls == [step.scroll for step in expected.steps]
    assert gate.pauses == [step.delay_after_s for step in expected.steps[:-1]]


async def test_heat_stretches_the_delay_median() -> None:
    """Spec 9.7: while warm, ``human_delay`` medians stretch by the multiplier."""
    gate = Gate()
    await _run(_spec(PROFILES, multiplier=2.0), FakeBrowser.of(PROFILES), gate)

    warm = stretched(PacingProfile(), 2.0)
    assert warm.delay.median == 50.0
    expected = plan_enrichment(random.Random(SEED), len(PROFILES), delay=warm.delay)
    assert gate.pauses == [step.delay_after_s for step in expected.steps[:-1]]
    cold = plan_enrichment(random.Random(SEED), len(PROFILES))
    assert gate.pauses != [step.delay_after_s for step in cold.steps[:-1]]


async def test_the_configured_pacing_is_the_one_followed() -> None:
    fast = PacingProfile(delay=DelayProfile(median=5.0, tail_p=0.0))
    spec = replace(_spec(PROFILES[:3]), pacing=fast)
    gate = Gate()
    await _run(spec, FakeBrowser.of(PROFILES[:3]), gate)
    expected = plan_enrichment(random.Random(SEED), 3, delay=fast.delay)
    assert gate.pauses == [step.delay_after_s for step in expected.steps[:-1]]


# --- the gate, before each visit and never during one ----------------------------------


@pytest.mark.parametrize(
    "reason", [StopReason.BUDGET, StopReason.CANCELLED, StopReason.INACTIVE], ids=str
)
async def test_a_refusal_stops_the_run_before_the_next_navigation(reason: StopReason) -> None:
    browser = FakeBrowser.of(PROFILES)
    gate = Gate(refuse_at={2: reason})

    result, harvests, events = await _run(_spec(PROFILES), browser, gate)

    assert result.reason is reason
    assert browser.visited() == [p.slug for p in PROFILES[:2]]
    assert gate.asked == [0, 1, 2]
    assert result.completed == (101, 102) and len(harvests) == 2
    assert events[-1].stopped is reason


async def test_a_gate_may_not_stop_a_run_for_a_response() -> None:
    with pytest.raises(ValueError, match="gate may not"):
        await _run(
            _spec(PROFILES), FakeBrowser.of(PROFILES), Gate(refuse_at={0: StopReason.RESPONSE})
        )


async def test_a_cancel_during_the_wait_stops_before_the_next_visit() -> None:
    browser = FakeBrowser.of(PROFILES)
    gate = Gate(cancel_in_pause=2)

    result, _, _ = await _run(_spec(PROFILES), browser, gate)

    assert result.reason is StopReason.CANCELLED
    assert browser.visited() == [p.slug for p in PROFILES[:2]]
    assert gate.asked == [0, 1]


async def test_the_visit_budget_caps_the_run_and_says_so() -> None:
    browser = FakeBrowser.of(PROFILES)
    gate = Gate()

    result, _, _ = await _run(_spec(PROFILES, budget=2), browser, gate)

    assert result.reason is StopReason.VISIT_BUDGET
    assert result.planned == 2 and len(browser.visited()) == 2
    assert gate.asked == [0, 1]


async def test_a_zero_budget_visits_nobody() -> None:
    browser = FakeBrowser.of(PROFILES)
    result, _, _ = await _run(_spec(PROFILES, budget=0), browser)
    assert (result.reason, result.visits, browser.events) == (StopReason.VISIT_BUDGET, 0, [])


async def test_an_empty_plan_ends_at_once() -> None:
    result, _, _ = await _run(_spec(()), FakeBrowser.of(PROFILES))
    assert (result.reason, result.visits) == (StopReason.END_OF_PLAN, 0)


# --- spec 9.7: classify before parse, stop on the first non-Ok ------------------------

_STOPPING = [
    pytest.param(THROTTLED, Outcome.THROTTLED, id="throttled"),
    pytest.param(CHECKPOINT, Outcome.CHECKPOINT, id="checkpoint"),
    pytest.param(LOGGED_OUT, Outcome.LOGGED_OUT, id="logged-out"),
    pytest.param(UNRECOGNIZED, Outcome.ROUTE_CHANGED, id="route-changed"),
    pytest.param(BAD_REQUEST, Outcome.ROUTE_CHANGED, id="bad-request"),
]


@pytest.mark.parametrize(("scripted", "outcome"), _STOPPING)
@pytest.mark.parametrize("call", [2, 3], ids=["details", "contact-info"])
async def test_the_first_response_that_is_not_ok_stops_the_run(
    scripted: Scripted, outcome: Outcome, call: int
) -> None:
    """The second profile's details (fetch 2) or contact info (fetch 3) answers badly."""
    browser = FakeBrowser.of(PROFILES, script={call: scripted})

    result, harvests, _ = await _run(_spec(PROFILES), browser)

    assert result.reason is StopReason.RESPONSE and result.outcome is outcome
    assert result.completed == (101,)
    assert [h.contact_ref for h in harvests] == [101]
    assert browser.visited() == [PROFILES[0].slug, PROFILES[1].slug]
    assert result.visits == 2


async def test_a_checkpoint_is_never_parsed_and_its_url_is_kept_for_the_flag() -> None:
    """Parsed, the checkpoint page's HTML would be ``RouteChanged``; classified first it is not."""
    browser = FakeBrowser.of(PROFILES, script={0: CHECKPOINT})
    result, _, _ = await _run(_spec(PROFILES), browser)
    assert result.outcome is Outcome.CHECKPOINT
    assert result.final_url is not None and "/checkpoint/" in result.final_url


@pytest.mark.parametrize(
    ("landed", "outcome"),
    [
        ("https://www.linkedin.com/checkpoint/challenge/AgFAKE", Outcome.CHECKPOINT),
        ("https://www.linkedin.com/authwall?trk=x", Outcome.LOGGED_OUT),
        ("https://www.linkedin.com/login?session_redirect=x", Outcome.LOGGED_OUT),
    ],
)
async def test_a_navigation_that_lands_on_a_wall_stops_before_any_fetch(
    landed: str, outcome: Outcome
) -> None:
    browser = FakeBrowser.of(PROFILES, redirect={PROFILES[1].slug: landed})

    result, _, _ = await _run(_spec(PROFILES), browser)

    assert result.reason is StopReason.RESPONSE and result.outcome is outcome
    assert browser.kinds().count("details") == 1  # the first profile's only
    assert "scroll" not in browser.kinds()[browser.kinds().index("goto", 1) :]


# --- NotFound is the contact's, not the run's -----------------------------------------


@pytest.mark.parametrize("call", [2, 3], ids=["details", "contact-info"])
async def test_not_found_is_terminal_for_the_contact_and_the_run_goes_on(call: int) -> None:
    browser = FakeBrowser.of(PROFILES, script={call: NOT_FOUND})

    result, harvests, events = await _run(_spec(PROFILES), browser)

    assert result.reason is StopReason.END_OF_PLAN
    assert result.completed == tuple(p.n for p in PROFILES)
    assert result.not_found == 1
    gone = harvests[1]
    assert (gone.contact_ref, gone.outcome, gone.details, gone.contact_info) == (
        102,
        Outcome.NOT_FOUND,
        None,
        None,
    )
    assert all(h.outcome is Outcome.OK for h in harvests if h is not gone)
    assert events[-1].not_found == 1 and events[-1].harvested == len(PROFILES) - 1


async def test_a_details_not_found_skips_the_contact_info_fetch() -> None:
    browser = FakeBrowser.of(PROFILES[:2], script={0: NOT_FOUND})
    await _run(_spec(PROFILES[:2]), browser)
    assert [k for k in browser.kinds() if k in ("details", "contact_info")] == [
        "details",
        "details",
        "contact_info",
    ]


# --- a slug is not a signal ---------------------------------------------------------------


@pytest.mark.parametrize(
    "slug", ["checkpoint", "challenge", "login-fake-person", "loginova-fake", "authwall-fan"]
)
async def test_a_slug_that_reads_like_a_wall_is_still_a_profile(slug: str) -> None:
    """The profile url and the contact-info path both carry the slug."""
    person = Profile(150, "Wren", "Sample", headline="Tester", public_id=slug)
    browser = FakeBrowser.of([person])

    result, harvests, _ = await _run(_spec([person]), browser)

    assert result.reason is StopReason.END_OF_PLAN
    assert [h.outcome for h in harvests] == [Outcome.OK]


@pytest.mark.parametrize(
    ("slug", "landed", "outcome"),
    [
        ("checkpoint", "https://www.linkedin.com/checkpoint/lg/login", Outcome.CHECKPOINT),
        ("checkpoint", "https://www.linkedin.com/checkpoint/lg/x", Outcome.CHECKPOINT),
        ("challenge", "https://www.linkedin.com/checkpoint/challenge/x", Outcome.CHECKPOINT),
        ("login", "https://www.linkedin.com/login?trk=x", Outcome.LOGGED_OUT),
        (
            "authwall",
            "https://www.linkedin.com/authwall?redirect=/in/authwall/",
            Outcome.LOGGED_OUT,
        ),
    ],
)
async def test_a_real_wall_is_still_a_wall_for_a_slug_like_one(
    slug: str, landed: str, outcome: Outcome
) -> None:
    """The mask takes the slug's own path segment and nothing else: the wall's path stays."""
    person = Profile(150, "Wren", "Sample", public_id=slug)
    wall = Scripted(200, "<html>check</html>", landed)
    browser = FakeBrowser.of([person], script={0: wall})
    navigated = FakeBrowser.of([person], redirect={slug: landed})

    result, _, _ = await _run(_spec([person]), browser)
    at_navigation, _, _ = await _run(_spec([person]), navigated)

    assert result.outcome is outcome
    assert at_navigation.outcome is outcome


async def test_the_masked_url_never_carries_the_slug() -> None:
    person = Profile(150, "Wren", "Sample", public_id="wren-fake-sample")
    browser = FakeBrowser.of([person], script={0: THROTTLED})
    result, _, _ = await _run(_spec([person]), browser)
    assert result.final_url is not None and "wren-fake-sample" not in result.final_url


# --- the browser source ---------------------------------------------------------------------


async def test_the_profile_url_is_the_origin_and_the_encoded_slug() -> None:
    browser = FakeBrowser.of([])
    source = browser.source("http://127.0.0.1:8765/")
    assert source.profile_url("josé-fake") == "http://127.0.0.1:8765/in/jos%C3%A9-fake/"
    assert (
        BrowserProfiles(navigate=browser.navigate, scroll_page=browser.scroll, fetch=browser.fetch)
        .profile_url("a-b")
        .startswith("https://www.linkedin.com/in/a-b/")
    )


async def test_the_fetches_are_the_voyager_profile_requests() -> None:
    seen: list[tuple[str, dict[str, str]]] = []
    browser = FakeBrowser.of(PROFILES[:1])
    inner = browser.fetch

    async def recording(request):  # type: ignore[no-untyped-def]
        seen.append((request.path, dict(request.query)))
        return await inner(request)

    source = BrowserProfiles(navigate=browser.navigate, scroll_page=browser.scroll, fetch=recording)
    slug = PROFILES[0].slug
    assert (await source.fetch_details(slug)).outcome is Outcome.OK
    assert (await source.fetch_contact_info(slug)).outcome is Outcome.OK
    (details_path, query), (info_path, info_query) = seen
    assert details_path == "/voyager/api/identity/dash/profiles"
    assert query["memberIdentity"] == slug and query["q"] == "memberIdentity"
    assert info_path == f"/voyager/api/identity/profiles/{slug}/profileContactInfo"
    assert info_query == {}


# --- the spec and the harvest are plain, checked data ---------------------------------------


def test_the_spec_refuses_what_would_make_a_run_misbehave() -> None:
    with pytest.raises(ValueError, match="negative"):
        EnrichJobSpec(targets=(), visit_budget=-1)
    with pytest.raises(ValueError, match=r"at least 1\.0"):
        EnrichJobSpec(targets=(), visit_budget=1, heat_multiplier=0.5)
    with pytest.raises(ValueError, match="only once"):
        EnrichJobSpec(targets=(EnrichTarget(1, "a"), EnrichTarget(1, "b")), visit_budget=2)
    with pytest.raises(ValueError, match="public id"):
        EnrichTarget(1, "  ")


def test_a_harvest_is_ok_with_both_halves_or_not_found_with_neither() -> None:
    details = parse_profile_details(details_body(PROFILES[0]))
    info = parse_contact_info(contact_info_body(PROFILES[0]))
    with pytest.raises(ValueError, match="both"):
        ProfileHarvest(1, "a", Outcome.OK, NOW, details=details)
    with pytest.raises(ValueError, match="nothing"):
        ProfileHarvest(1, "a", Outcome.NOT_FOUND, NOW, contact_info=info)
    with pytest.raises(ValueError, match="Ok or NotFound"):
        ProfileHarvest(1, "a", Outcome.THROTTLED, NOW)


async def test_progress_events_hold_counts_and_nothing_else() -> None:
    _, _, events = await _run(_spec(PROFILES[:2]), FakeBrowser.of(PROFILES[:2]))
    assert [(e.visited, e.harvested, e.stopped) for e in events] == [
        (1, 1, None),
        (2, 2, None),
        (2, 2, StopReason.END_OF_PLAN),
    ]
    assert all(e.planned == 2 for e in events)


# --- the real browser pieces fit the seam (#150, #152) -----------------------------------


async def test_a_browser_runs_own_methods_are_the_source() -> None:
    """``run.goto``, ``run.scroll``, and ``PageVoyagerFetch(run)`` are the source, unwrapped.

    Over the shared fakes: the tab is a ``FakePage``, and its in-page ``evaluate``
    answers every fetch with one profile's body. Nothing is fetched from anywhere.
    """
    import functools

    from browser_fakes import FakeBrowser as FakeChrome
    from browser_fakes import FakeConnector, FakeContext, FakePage

    from netkeeper.linkedin.browser import ActivityLocks, AttachBrowserProvider
    from netkeeper.linkedin.enrich import Navigate, Scroll
    from netkeeper.linkedin.fetch import PageVoyagerFetch

    context = FakeContext(
        evaluate_result={
            "status": 200,
            "body": details_body(PROFILES[0]),
            "url": "https://www.linkedin.com/voyager/api/identity/dash/profiles",
        }
    )
    provider = AttachBrowserProvider(
        "http://127.0.0.1:9222",
        connector=FakeConnector([FakeChrome([context])]),
        locks=ActivityLocks(),
    )
    waits: list[float] = []

    async def no_wait(seconds: float) -> None:
        waits.append(seconds)

    harvests: list[ProfileHarvest] = []

    async def keep(harvest: ProfileHarvest) -> None:
        harvests.append(harvest)

    async with provider.run("local") as run:
        navigate: Navigate = run.goto  # the protocol is the method, as it stands
        scroll: Scroll = run.scroll
        assert navigate is not None and scroll is not None
        source = BrowserProfiles(
            navigate=run.goto,
            scroll_page=functools.partial(run.scroll, sleep=no_wait),
            fetch=PageVoyagerFetch(run),
        )
        result = await run_enrichment(
            _spec(PROFILES[:1]), source, Gate(), on_harvest=keep, rng=random.Random(SEED)
        )

    (page,) = context.pages
    assert isinstance(page, FakePage)
    assert page.goto_calls == [f"https://www.linkedin.com/in/{PROFILES[0].slug}/"]
    assert len(page.mouse.wheels) == len(result.plan.steps[0].scroll.steps)
    assert len(page.evaluate_calls) == 2  # details, then contact info
    assert result.reason is StopReason.END_OF_PLAN
    assert harvests[0].details is not None and harvests[0].details.urn == PROFILES[0].urn


async def test_a_broken_fetch_ends_the_run_by_exception() -> None:
    """``VoyagerFetchError``: no response to classify, so nothing to decide from. The run ends."""
    from netkeeper.linkedin.fetch import VoyagerFetchError

    browser = FakeBrowser.of(PROFILES)

    def plumbing_breaks(kind: str, value: object) -> None:
        if kind == "details" and browser.kinds().count("details") == 2:
            raise VoyagerFetchError("no live JSESSIONID cookie readable on this page")

    browser.on_event = plumbing_breaks
    harvested: list[int] = []

    async def keep(harvest: ProfileHarvest) -> None:
        harvested.append(harvest.contact_ref)

    with pytest.raises(VoyagerFetchError):
        await run_enrichment(
            _spec(PROFILES), browser.source(), Gate(), on_harvest=keep, rng=random.Random(SEED)
        )
    assert harvested == [101]
    assert len(browser.visited()) == 2
