"""netkeeper.linkedin.enrich: the enrichment job, against a fake source and a fake gate.

No database and no browser: :class:`profile_fakes.FakeBrowser` answers from invented
profiles and records every navigation, scroll, read, and click in order, and the gate
here is a list of answers. The real source over fake pages is
``tests/test_page_profiles.py``; the runner that wires this job to budgets, heat, and
rows is ``tests/test_enrichment.py``.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from profile_fakes import (
    BAD_REQUEST,
    CHECKPOINT,
    LOGGED_OUT,
    NOT_FOUND,
    PROFILES,
    THROTTLED,
    UNRECOGNIZED,
    FakeBrowser,
    Profile,
    Scripted,
    contact_info_of,
    details_of,
)

from netkeeper.linkedin import enrich
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.enrich import (
    EnrichJobSpec,
    EnrichResult,
    EnrichTarget,
    PacingProfile,
    ProfileHarvest,
    ProgressEvent,
    StopReason,
    UnreadableCause,
    UnreadableVisit,
    run_enrichment,
    stretched,
)
from netkeeper.linkedin.observe import ObservationFailed
from netkeeper.linkedin.pacing import (
    DelayProfile,
    ScrollPlan,
    depth_after,
    human_delay,
    plan_enrichment,
    scroll_back_to_top,
)
from netkeeper.linkedin.voyager import ContactInfo, ProfileDetails

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
SEED = 7
STRANGER_URN = "urn:li:fsd_profile:ACoAAFAKE9999999"


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
        targets=tuple(EnrichTarget(p.n, p.slug, p.urn) for p in profiles),
        visit_budget=budget,
        heat_multiplier=multiplier,
    )


def _record_sleep(browser: FakeBrowser) -> Callable[[float], Awaitable[None]]:
    """A sleep that waits no real time and lands in the browser's event log."""

    async def sleep(seconds: float) -> None:
        browser.events.append(("sleep", seconds))

    return sleep


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
        browser.source(sleep=_record_sleep(browser)),
        gate or Gate(),
        on_harvest=on_harvest,
        on_progress=on_progress,
        rng=random.Random(SEED),
        clock=lambda: NOW,
    )
    return result, harvests, events


# --- spec 9.4's order, one visit at a time --------------------------------------------


async def test_each_visit_navigates_scrolls_reads_clicks_then_hands_over() -> None:
    """Navigate, scroll and dwell, read the profile, scroll back up, pause, click Contact
    info once, then the core."""
    browser = FakeBrowser.of(PROFILES[:3])
    gate = Gate()
    gate.log = browser.events

    result, harvests, _ = await _run(_spec(PROFILES[:3]), browser, gate)

    kinds = browser.kinds()
    one_visit = ["gate", "goto", "scroll", "details", "back", "sleep", "click", "harvest"]
    assert kinds == [*one_visit, "pause", *one_visit, "pause", *one_visit]
    assert browser.visited() == [p.slug for p in PROFILES[:3]]
    assert result.reason is StopReason.END_OF_PLAN
    assert result.completed == (101, 102, 103) and result.visits == 3
    assert [h.outcome for h in harvests] == [Outcome.OK] * 3
    assert result.clicks == 3


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
        emails=("priya.fake.okafor@example.test",),
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


# --- spec 9.7: stop on the first answer that is not Ok ----------------------------------

_STOPPING = [
    pytest.param(THROTTLED, Outcome.THROTTLED, id="throttled"),
    pytest.param(CHECKPOINT, Outcome.CHECKPOINT, id="checkpoint"),
    pytest.param(LOGGED_OUT, Outcome.LOGGED_OUT, id="logged-out"),
    pytest.param(BAD_REQUEST, Outcome.ROUTE_CHANGED, id="route-changed"),
]


@pytest.mark.parametrize(("scripted", "outcome"), _STOPPING)
@pytest.mark.parametrize("call", [2, 3], ids=["profile", "contact-info"])
async def test_the_first_answer_that_is_not_ok_stops_the_run(
    scripted: Scripted, outcome: Outcome, call: int
) -> None:
    """The second profile's read (2) or its overlay (3) answers badly."""
    browser = FakeBrowser.of(PROFILES, script={call: scripted})

    result, harvests, _ = await _run(_spec(PROFILES), browser)

    assert result.reason is StopReason.RESPONSE and result.outcome is outcome
    assert result.completed == (101,)
    assert [h.contact_ref for h in harvests] == [101]
    assert browser.visited() == [PROFILES[0].slug, PROFILES[1].slug]
    assert result.visits == 2


@pytest.mark.parametrize(("scripted", "outcome"), _STOPPING)
async def test_a_navigation_that_answers_badly_stops_before_anything_is_read(
    scripted: Scripted, outcome: Outcome
) -> None:
    browser = FakeBrowser.of(PROFILES, landing={1: scripted})

    result, _, _ = await _run(_spec(PROFILES), browser)

    assert result.reason is StopReason.RESPONSE and result.outcome is outcome
    kinds = browser.kinds()
    assert kinds.count("details") == 1 and kinds.count("click") == 1  # the first profile's only
    assert "scroll" not in kinds[kinds.index("goto", 1) :]


async def test_a_checkpoints_url_is_kept_for_the_flag() -> None:
    browser = FakeBrowser.of(PROFILES, landing={0: CHECKPOINT})
    result, _, _ = await _run(_spec(PROFILES), browser)
    assert result.outcome is Outcome.CHECKPOINT
    assert result.final_url is not None and "/checkpoint/" in result.final_url


# --- NotFound is the contact's, not the run's -----------------------------------------


async def test_a_profile_not_found_is_terminal_for_the_contact_and_the_run_goes_on() -> None:
    browser = FakeBrowser.of(PROFILES, landing={1: NOT_FOUND})

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
    # A page that is not there is not scrolled, read, or clicked.
    assert browser.kinds().count("scroll") == len(PROFILES) - 1
    assert browser.clicks == [p.slug for p in PROFILES if p is not PROFILES[1]]


@pytest.mark.parametrize("call", [2, 3], ids=["profile", "contact-info"])
async def test_a_source_that_says_not_found_later_is_the_contacts_too(call: int) -> None:
    browser = FakeBrowser.of(PROFILES, script={call: NOT_FOUND})
    result, harvests, _ = await _run(_spec(PROFILES), browser)
    assert result.reason is StopReason.END_OF_PLAN and result.not_found == 1
    assert harvests[1].outcome is Outcome.NOT_FOUND


# --- whose profile it is: no click for anyone else -------------------------------------------


async def test_a_profile_under_another_urn_is_not_clicked() -> None:
    """The slug led to somebody else (a vanity url changed hands): the job reads the
    page's id, sees it is not the contact's, and never touches the page."""
    browser = FakeBrowser.of(PROFILES[:3], urns={PROFILES[1].slug: STRANGER_URN})

    result, harvests, _ = await _run(_spec(PROFILES[:3]), browser)

    assert browser.clicks == [PROFILES[0].slug, PROFILES[2].slug]
    stranger = harvests[1]
    assert stranger.outcome is Outcome.OK and stranger.contact_info is None
    assert stranger.details is not None and stranger.details.urn == STRANGER_URN
    assert result.click_pauses_s[1] is None and result.clicks == 2
    kinds = browser.kinds()
    second = kinds[kinds.index("goto", 1) : kinds.index("goto", kinds.index("goto", 1) + 1)]
    assert "back" not in second and "sleep" not in second and "click" not in second


async def test_two_profiles_under_other_ids_in_a_row_stop_the_run() -> None:
    """One is a slug that changed hands; two in a row look like a page read wrongly, and a
    run that went on would spend its visits clicking and writing nothing."""
    others = {p.slug: f"urn:li:fsd_profile:ACoAAFAKE99999{n:02d}" for n, p in enumerate(PROFILES)}
    browser = FakeBrowser.of(PROFILES, urns={k: others[k] for k in list(others)[1:3]})

    result, harvests, events = await _run(_spec(PROFILES), browser)

    assert result.reason is StopReason.RESPONSE and result.outcome is Outcome.ROUTE_CHANGED
    assert result.mismatched == 2 and result.completed == (101, 102, 103)
    # A mismatch wrote nothing: it is not counted as harvested.
    assert (events[-1].harvested, events[-1].mismatched) == (1, 2)
    assert [h.contact_info is None for h in harvests] == [False, True, True]
    assert browser.clicks == [PROFILES[0].slug]


async def test_mismatches_and_unreadable_profiles_share_the_runs_limit() -> None:
    """Mateo's id is another's (read 2), Tomasz is unreadable (read 5), and the first
    extra person's id is another's (read 8): three suspects, never two in a row, stop
    the run before the last person."""
    extra = [Profile(201, "Extra", "One"), Profile(202, "Extra", "Two")]
    people = [*PROFILES, *extra]
    browser = FakeBrowser.of(
        people,
        urns={PROFILES[1].slug: STRANGER_URN, extra[0].slug: STRANGER_URN},
        script={5: UNRECOGNIZED},
    )

    result, _, _ = await _run(_spec(people), browser)

    assert result.reason is StopReason.RESPONSE and result.outcome is Outcome.ROUTE_CHANGED
    assert (result.mismatched, result.unreadable) == (2, 1)
    assert browser.visited() == [p.slug for p in [*PROFILES, extra[0]]]


async def test_the_run_records_each_suspect_visit_with_its_cause_and_contact() -> None:
    """#405: the same run as above, recorded visit by visit. A source that names no
    cause (this fake) is recorded as ``unknown``, never left out."""
    extra = [Profile(201, "Extra", "One"), Profile(202, "Extra", "Two")]
    people = [*PROFILES, *extra]
    browser = FakeBrowser.of(
        people,
        urns={PROFILES[1].slug: STRANGER_URN, extra[0].slug: STRANGER_URN},
        script={5: UNRECOGNIZED},
    )

    result, harvests, _ = await _run(_spec(people), browser)

    assert result.unreadable_visits == (
        UnreadableVisit(2, PROFILES[1].n, UnreadableCause.ID_MISMATCH),
        UnreadableVisit(4, PROFILES[3].n, UnreadableCause.UNKNOWN),
        UnreadableVisit(6, extra[0].n, UnreadableCause.ID_MISMATCH),
    )
    assert [h.unreadable_cause for h in harvests] == [
        None,
        UnreadableCause.ID_MISMATCH,
        None,
        UnreadableCause.UNKNOWN,
        None,
        UnreadableCause.ID_MISMATCH,
    ]


def test_a_not_found_harvest_carries_no_unreadable_cause() -> None:
    with pytest.raises(ValueError, match="no unreadable cause"):
        ProfileHarvest(
            contact_ref=1,
            requested_public_id="x",
            outcome=Outcome.NOT_FOUND,
            observed_at=NOW,
            unreadable_cause=UnreadableCause.UNKNOWN,
        )


def test_the_cause_codes_are_pinned() -> None:
    """Stored on runs: a renamed code orphans every record that already holds it."""
    assert {cause.value for cause in UnreadableCause} == {
        "profile_shape_unknown",
        "contact_info_shape_unknown",
        "landed_off_profile",
        "left_profile",
        "unexpected_profile",
        "no_profile_screen",
        "profile_screen_status",
        "too_many_lazy_cards",
        "contact_info_control_missing",
        "contact_info_control_not_alone",
        "contact_info_control_unreadable",
        "contact_info_control_elsewhere",
        "contact_info_control_unclickable",
        "contact_info_not_clicked",
        "overlay_never_answered",
        "overlay_other_profile",
        "overlay_redirected",
        "overlay_status",
        "navigation_timed_out",
        "profile_screen_lost",
        "contact_info_lost",
        "profile_status",
        "id_mismatch",
        "unknown",
    }


async def test_never_more_than_one_click_per_visit() -> None:
    people = [*PROFILES, Profile(201, "Extra", "One")]
    browser = FakeBrowser.of(people, script={2: UNRECOGNIZED}, landing={3: NOT_FOUND})
    result, _, _ = await _run(_spec(people), browser)
    assert result.clicks == len(browser.clicks) <= result.visits
    assert len(browser.clicks) == len(set(browser.clicks))


# --- the spec and the harvest are plain, checked data ---------------------------------------


def test_the_spec_refuses_what_would_make_a_run_misbehave() -> None:
    urn = PROFILES[0].urn
    with pytest.raises(ValueError, match="negative"):
        EnrichJobSpec(targets=(), visit_budget=-1)
    with pytest.raises(ValueError, match=r"at least 1\.0"):
        EnrichJobSpec(targets=(), visit_budget=1, heat_multiplier=0.5)
    with pytest.raises(ValueError, match="only once"):
        EnrichJobSpec(
            targets=(EnrichTarget(1, "a", urn), EnrichTarget(1, "b", urn)), visit_budget=2
        )
    with pytest.raises(ValueError, match="public id"):
        EnrichTarget(1, "  ", urn)
    with pytest.raises(ValueError, match="URN"):
        EnrichTarget(1, "a", " ")


def test_a_harvest_is_ok_with_its_details_or_not_found_with_nothing() -> None:
    details = details_of(PROFILES[0])
    info = contact_info_of(PROFILES[0])
    with pytest.raises(ValueError, match="details"):
        ProfileHarvest(1, "a", Outcome.OK, NOW, contact_info=info)
    with pytest.raises(ValueError, match="nothing"):
        ProfileHarvest(1, "a", Outcome.NOT_FOUND, NOW, contact_info=info)
    with pytest.raises(ValueError, match="Ok, NotFound, or RouteChanged"):
        ProfileHarvest(1, "a", Outcome.THROTTLED, NOW)
    with pytest.raises(ValueError, match="carries nothing"):
        ProfileHarvest(1, "a", Outcome.ROUTE_CHANGED, NOW, details=details)
    # A mismatch: the details, and no contact info because nothing was clicked.
    assert ProfileHarvest(1, "a", Outcome.OK, NOW, details=details).contact_info is None


async def test_progress_events_hold_counts_and_nothing_else() -> None:
    _, _, events = await _run(_spec(PROFILES[:2]), FakeBrowser.of(PROFILES[:2]))
    assert [(e.visited, e.harvested, e.stopped) for e in events] == [
        (1, 1, None),
        (2, 2, None),
        (2, 2, StopReason.END_OF_PLAN),
    ]
    assert all(e.planned == 2 for e in events)


async def test_a_broken_observation_ends_the_run_by_exception() -> None:
    """``ObservationFailed``: no answer to classify, so nothing to decide from."""
    browser = FakeBrowser.of(PROFILES)

    def plumbing_breaks(kind: str, value: object) -> None:
        if kind == "details" and browser.kinds().count("details") == 2:
            raise ObservationFailed("a matching response was dropped")

    browser.on_event = plumbing_breaks
    harvested: list[int] = []

    async def keep(harvest: ProfileHarvest) -> None:
        harvested.append(harvest.contact_ref)

    with pytest.raises(ObservationFailed):
        await run_enrichment(
            _spec(PROFILES),
            browser.source(),
            Gate(),
            on_harvest=keep,
            rng=random.Random(SEED),
        )
    assert harvested == [101]
    assert len(browser.visited()) == 2


# --- #171 review: one unreadable profile is that contact's problem (F2b) --------------------


async def test_one_unreadable_profile_is_the_contacts_and_the_run_goes_on() -> None:
    browser = FakeBrowser.of(PROFILES, script={2: UNRECOGNIZED})

    result, harvests, events = await _run(_spec(PROFILES), browser)

    assert result.reason is StopReason.END_OF_PLAN
    assert result.completed == tuple(p.n for p in PROFILES)
    unreadable = harvests[1]
    assert (unreadable.contact_ref, unreadable.outcome) == (102, Outcome.ROUTE_CHANGED)
    assert unreadable.details is None and unreadable.contact_info is None
    assert result.unreadable == 1 and events[-1].unreadable == 1
    # the profile did not read, so Contact info was never clicked on it
    assert PROFILES[1].slug not in browser.clicks


async def test_an_unreadable_landing_is_the_contacts_too() -> None:
    """A tab that landed off the profile, or a page with no screen: one unreadable visit."""
    browser = FakeBrowser.of(PROFILES[:3], landing={1: UNRECOGNIZED})
    result, harvests, _ = await _run(_spec(PROFILES[:3]), browser)
    assert result.reason is StopReason.END_OF_PLAN and result.unreadable == 1
    assert [h.outcome for h in harvests] == [Outcome.OK, Outcome.ROUTE_CHANGED, Outcome.OK]


async def test_two_unreadable_profiles_in_a_row_mean_the_route_changed() -> None:
    """Reads 0-1 are Priya; 2 is Mateo's profile; 3 is Hana's (Mateo is never clicked)."""
    browser = FakeBrowser.of(PROFILES, script={2: UNRECOGNIZED, 3: UNRECOGNIZED})

    result, harvests, _ = await _run(_spec(PROFILES), browser)

    assert result.reason is StopReason.RESPONSE and result.outcome is Outcome.ROUTE_CHANGED
    assert result.completed == (101, 102, 103)  # both unreadable ones handed over first
    assert [h.outcome for h in harvests[1:]] == [Outcome.ROUTE_CHANGED] * 2
    assert len(browser.visited()) == 3


async def test_a_readable_profile_between_two_unreadable_ones_resets_the_count() -> None:
    """Mateo's profile (read 2) and Tomasz's (read 5) fail; Hana (3, 4) between is fine."""
    browser = FakeBrowser.of(PROFILES, script={2: UNRECOGNIZED, 5: UNRECOGNIZED})

    result, _, _ = await _run(_spec(PROFILES), browser)

    assert result.reason is StopReason.END_OF_PLAN and result.unreadable == 2
    # one entry per visit; no click after an unreadable profile
    assert [pause is None for pause in result.click_pauses_s] == [False, True, False, True, False]


async def test_an_unreadable_overlay_is_the_contacts_too() -> None:
    browser = FakeBrowser.of(PROFILES[:2], script={1: UNRECOGNIZED})
    result, harvests, _ = await _run(_spec(PROFILES[:2]), browser)
    assert result.reason is StopReason.END_OF_PLAN
    assert [h.outcome for h in harvests] == [Outcome.ROUTE_CHANGED, Outcome.OK]
    assert harvests[0].details is None  # the profile is not written without its overlay


def test_the_unreadable_limits_are_two_in_a_row_and_three_a_run() -> None:
    assert enrich.MAX_UNREADABLE_IN_A_ROW == 2
    assert enrich.MAX_UNREADABLE_PER_RUN == 3


async def test_a_not_found_between_two_unreadable_profiles_resets_the_count() -> None:
    """#172 N04: Mateo unreadable (read 2), Hana not found (read 3), Tomasz unreadable (4).

    A NotFound is a profile that answered; it breaks the run of unreadable ones, so
    the run goes on to Aiko rather than calling the route changed.
    """
    browser = FakeBrowser.of(PROFILES, script={2: UNRECOGNIZED, 3: NOT_FOUND, 4: UNRECOGNIZED})

    result, harvests, _ = await _run(_spec(PROFILES), browser)

    assert result.reason is StopReason.END_OF_PLAN
    assert (result.unreadable, result.not_found) == (2, 1)
    assert [h.outcome for h in harvests[1:4]] == [
        Outcome.ROUTE_CHANGED,
        Outcome.NOT_FOUND,
        Outcome.ROUTE_CHANGED,
    ]
    assert len(browser.visited()) == len(PROFILES)


async def test_three_scattered_unreadable_profiles_stop_the_run() -> None:
    """#172: every other profile unreadable never trips the in-a-row rule; the third
    unreadable profile in one run stops it, as the route having changed.

    Reads: Priya 0 (unreadable), Mateo 1-2, Hana 3 (unreadable), Tomasz 4-5, Aiko 6
    (unreadable). The two people after Aiko are never visited.
    """
    people = [*PROFILES, Profile(201, "Extra", "One"), Profile(202, "Extra", "Two")]
    browser = FakeBrowser.of(people, script={0: UNRECOGNIZED, 3: UNRECOGNIZED, 6: UNRECOGNIZED})

    result, _, _ = await _run(_spec(people), browser)

    assert result.reason is StopReason.RESPONSE and result.outcome is Outcome.ROUTE_CHANGED
    assert result.unreadable == enrich.MAX_UNREADABLE_PER_RUN == 3
    assert browser.visited() == [p.slug for p in PROFILES]


# --- the pause and the scroll back up before the click ------------------------------------------


def test_the_click_pause_is_a_second_and_a_half() -> None:
    assert (enrich.CLICK_PAUSE_MEDIAN_S, enrich.CLICK_PAUSE_SIGMA) == (1.5, 0.5)


async def test_a_person_scrolls_back_up_and_pauses_before_the_click() -> None:
    browser = FakeBrowser.of(PROFILES[:3])

    result, _, _ = await _run(_spec(PROFILES[:3]), browser)

    sleeps = [float(str(value)) for kind, value in browser.events if kind == "sleep"]
    assert sleeps == [p for p in result.click_pauses_s if p is not None] and len(sleeps) == 3
    assert all(0 < pause < 15 for pause in sleeps)
    kinds = browser.kinds()
    for i, kind in enumerate(kinds):
        if kind == "sleep":
            assert (kinds[i - 2], kinds[i - 1], kinds[i + 1]) == ("details", "back", "click")
    plan = plan_enrichment(random.Random(SEED), 3)
    assert result.plan == plan  # drawn after the plan
    # Each visit's pause, then its scroll back up, drawn from the same rng after the plan.
    rng = random.Random(SEED)
    plan_enrichment(rng, 3)
    backs = [value for kind, value in browser.events if kind == "back"]
    for step, pause, back in zip(plan.steps, sleeps, backs, strict=True):
        assert pause == human_delay(rng, median=1.5, sigma=0.5, tail_p=0.0, tail_range=(0, 0))
        assert back == scroll_back_to_top(rng, depth_after(step.scroll))
        assert isinstance(back, ScrollPlan)
        assert -sum(s.delta_px for s in back.steps) > depth_after(step.scroll) or (
            depth_after(step.scroll) == 0 and not back.steps
        )


# --- every profile segment is masked, whatever it says (#171 review, F3) -------------------------


@pytest.mark.parametrize(
    ("url", "masked"),
    [
        ("https://x.test/in/abc/", "https://x.test/in/_/"),
        # the whole segment, however it is spelled: nothing of a slug survives into
        # the url a session flag stores
        ("https://x.test/in/login%C3%A9-fake/", "https://x.test/in/_/"),
        ("https://x.test/in/loginé-fake.1_2~/", "https://x.test/in/_/"),
        ("https://x.test/in/abc", "https://x.test/in/_"),
        ("https://x.test/IN/checkpoint/", "https://x.test/IN/_/"),
        # only the segment itself, never the path after it
        ("https://x.test/in/abc/overlay/checkpoint/", "https://x.test/in/_/overlay/checkpoint/"),
        ("https://x.test/checkpoint/in/abc", "https://x.test/checkpoint/in/_"),
        ("https://x.test/login-in/abc", "https://x.test/login-in/abc"),  # "/in/" must be whole
        # a query or fragment is left exactly as it came
        ("https://x.test/authwall?r=/in/login-x/", "https://x.test/authwall?r=/in/login-x/"),
        ("https://x.test/in/abc/#/in/def", "https://x.test/in/_/#/in/def"),
    ],
)
def test_the_mask_takes_the_profile_segment_and_nothing_else(url: str, masked: str) -> None:
    assert enrich.masked(url) == masked
