"""Enrichment, the extractor half: visit profiles, harvest them, hand the harvests back (spec 9.4).

Behind the extractor boundary (spec 9.10, ADR 0005): an :class:`EnrichJobSpec`
comes in, :class:`ProfileHarvest` values and :class:`ProgressEvent` values go
out through the callbacks the caller passes, and an :class:`EnrichResult` is
returned. Nothing here imports the models or opens a session. The core picks
who to visit (spec 9.6), maps each harvest onto the contact in
``netkeeper.crm.apply``, and wires budgets, heat, cancel, and the stored plan
around this loop in ``netkeeper.services.enrichment``.

**One visit, in spec 9.4's order.** For each target, one unit of work:

1. navigate the tab to the profile page -- a real page view, which is what the
   site sees a person do, and what loads the page's own requests;
2. scroll it like a person, then dwell (spec 9.5; the plan comes from
   :func:`netkeeper.linkedin.pacing.plan_enrichment`);
3. fetch profile details, then, after a short pause of the kind a person
   takes before opening the contact-info overlay (:data:`FETCH_GAP_MEDIAN_S`),
   contact info, through the in-page API (spec 9.3), each classified before
   anything parses it (spec 9.7);
4. hand the harvest to the core before the next visit starts, so what the
   core has written is never more than one profile behind what the run saw;
5. wait the plan's gap -- a :func:`~netkeeper.linkedin.pacing.human_delay`, or
   a burst break -- before the next profile.

**What stops a run.** The gate is asked before every visit, never partway
through one (spec 9.4, 9.9): a budget refusal, a cancel, or the end of the
active window stops the run *between* profiles, and a cancel noticed during
the wait between two profiles stops it there. The first navigation or fetch
that is not ``Ok`` stops the run, with two exceptions that are the contact's
problem rather than the run's. ``NotFound`` on a profile is terminal for that
contact only (spec 9.7), handed to the core as a not-found harvest (spec 9.8's
streak). A body that classified ``Ok`` but that the parser cannot read is an
*unreadable* harvest: one person's profile can hold a shape nobody has seen
yet, and stopping on it would leave that person at the head of every run's
queue for ever. :data:`MAX_UNREADABLE_IN_A_ROW` unreadable profiles in a row,
on different contacts, is the signal that the route itself changed, and stops
the run as ``RouteChanged``; so is :data:`MAX_UNREADABLE_PER_RUN` in one run,
scattered or not (#172). A ``400`` or a body that is not JSON at all
(classify's own ``RouteChanged``) stops the run at once. Nothing is retried, a
checkpoint least of all; like the connections job, a throttle stops the run
at once rather than taking spec 9.7's "at most 3 attempts", and the next
scheduled run is the retry, with heat raised.

**A slug is not a signal.** Spec 9.7 classifies by the url a response came
from, and a profile's url carries its slug. A person whose vanity url is
``checkpoint`` or starts with ``login`` would read as a checkpoint or a login
wall, stop every run at their name, and flag the session each time. So the
path segment that names a profile -- the one after ``/in/``, and the one after
``/identity/profiles/`` -- is masked out of a url before it is classified
(:func:`_masked`), whatever it says: the slug asked for, a renamed one the site
redirected to, in any case or encoding. A redirect to ``/checkpoint/`` still
reads as one, and a profile named like one does not.

**What navigation cannot see.** A page view is classified by where it landed,
not by what it shows: a login wall served *in place* at the profile's own url
reads as a page that loaded. The next step catches it, because the in-page
API fetch that follows answers a signed-out session with a ``401`` or a login
page, which classify reads as ``LoggedOut``. Reading the page itself for a
wall marker is left until the first live run shows what one looks like (#149).

**The source seam.** The job reads profiles through a :class:`ProfileSource`.
:class:`BrowserProfiles` is the real one, built from one browser run's own
methods and nothing else: ``navigate=run.goto``, ``scroll_page=run.scroll``
(#152), and ``fetch=PageVoyagerFetch(run)`` (#150). ``netkeeper rehearse``
builds the same class against the loopback replica, so a rehearsal is this
loop, not a copy of it.

**Classify, then parse.** Every fetched body goes through spec 9.7's
classification before a parser sees it, the discipline
:func:`netkeeper.linkedin.fetch.parse_ok` enforces. This module applies it
itself rather than importing ``parse_ok``, for two reasons: ``fetch`` is a
browser module (``tests/test_browser_safety.py``), and the job must stay
importable by the core without dragging the browser in; and the url has to be
classified with the slug masked (above), which ``parse_ok`` cannot know to do.

**When the fetch itself breaks.** ``PageVoyagerFetch`` raises
``VoyagerFetchError`` when there is no response to classify at all: no
readable csrf cookie, a tab that moved off the origin, an in-page ``fetch()``
that threw. The job does not catch it: it ends the run by exception, the
runner marks the plan aborted and re-raises, and nothing raises heat or the
session flag, since no response said anything about the session. A per-profile
skip would be wrong here: whatever broke the plumbing for one profile breaks
it for the next, and every skip would have spent a profile visit first.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol
from urllib.parse import quote

from netkeeper.linkedin.classify import Outcome, classify
from netkeeper.linkedin.pacing import (
    DEFAULT_BURST_PROFILE,
    DEFAULT_DELAY_PROFILE,
    DEFAULT_SCROLL_PROFILE,
    BurstProfile,
    DelayProfile,
    EnrichmentPlan,
    ScrollPlan,
    ScrollProfile,
    human_delay,
    plan_enrichment,
)
from netkeeper.linkedin.voyager import (
    CONTACT_INFO_ENDPOINT,
    PROFILE_ENDPOINT,
    PROFILE_PATH,
    ContactInfo,
    ProfileDetails,
    RouteChanged,
    VoyagerFetch,
    VoyagerRequest,
    contact_info_path,
    parse_contact_info,
    parse_profile_details,
    profile_query,
)

log = logging.getLogger(__name__)

#: Where a production profile visit goes. A caller passes another origin only for
#: the loopback replica (:mod:`netkeeper.linkedin.rehearse` refuses anything else).
LINKEDIN_ORIGIN: Final = "https://www.linkedin.com"


class StopReason(enum.StrEnum):
    """Why a run stopped. Only :attr:`END_OF_PLAN` means every target was visited."""

    END_OF_PLAN = "end_of_plan"
    """Every target in the spec was visited (a not-found profile counts as visited)."""

    VISIT_BUDGET = "visit_budget"
    """The spec's ``visit_budget`` visits were made and targets remain."""

    BUDGET = "budget"
    """The gate refused the next visit: the day's or week's ``profile_visits`` budget."""

    CANCELLED = "cancelled"
    """A person asked the run to stop (spec 9.9); it stopped between profiles."""

    INACTIVE = "inactive"
    """The account's active window closed (spec 9.5); it stopped between profiles."""

    RESPONSE = "response"
    """A navigation or fetch classified as something other than ``Ok`` or a
    per-contact ``NotFound``; see ``EnrichResult.outcome``."""


#: Spec 9.5 has no figure for the pause between opening a profile's details and
#: its contact-info overlay; this is a person's click, a second or two, with no
#: distraction tail. Drawn with :func:`~netkeeper.linkedin.pacing.human_delay`.
FETCH_GAP_MEDIAN_S: Final = 1.5
FETCH_GAP_SIGMA: Final = 0.5

#: Unreadable profiles in a row (on different contacts) that mean the route
#: changed rather than one person's profile being unusual. The run stops there.
MAX_UNREADABLE_IN_A_ROW: Final = 2

#: Unreadable profiles in one run, in a row or not, that stop it the same way
#: (#172). Without it, a route that fails every other profile never trips the
#: in-a-row rule and the run spends half its visits on profiles it cannot read.
#: An absolute count rather than a share of the visits: a share would stop a run
#: on its first profile, which is exactly the one unusual person the in-a-row
#: rule exists to forgive.
MAX_UNREADABLE_PER_RUN: Final = 3

#: The reasons a gate may give for refusing a visit.
GATE_REASONS: Final = frozenset({StopReason.BUDGET, StopReason.CANCELLED, StopReason.INACTIVE})


@dataclass(frozen=True, slots=True)
class EnrichTarget:
    """One contact to visit: the core's reference for it, and the slug to visit it at."""

    contact_ref: int
    li_public_id: str

    def __post_init__(self) -> None:
        if not self.li_public_id.strip():
            raise ValueError("an enrichment target needs a public id to visit")


@dataclass(frozen=True, slots=True)
class PacingProfile:
    """Spec 9.10's "pacing profile": the delay, burst, and scroll parameters of a run."""

    delay: DelayProfile = DEFAULT_DELAY_PROFILE
    burst: BurstProfile = DEFAULT_BURST_PROFILE
    scroll: ScrollProfile = DEFAULT_SCROLL_PROFILE


@dataclass(frozen=True, slots=True)
class EnrichJobSpec:
    """What the core asks for (spec 9.10's ``EnrichJobSpec``).

    ``targets`` is the ordered list to visit, pinned contacts first (spec 9.6);
    the job never reorders it. ``visit_budget`` is the most profiles this run
    may visit; 0 is allowed and visits nothing. ``heat_multiplier`` (>= 1.0,
    spec 9.7) stretches the delay median between profiles while heat is warm;
    the core has already shrunk ``visit_budget`` by it.
    """

    targets: tuple[EnrichTarget, ...]
    visit_budget: int
    pacing: PacingProfile = field(default_factory=PacingProfile)
    heat_multiplier: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", tuple(self.targets))
        if self.visit_budget < 0:
            raise ValueError("visit_budget must not be negative")
        if self.heat_multiplier < 1.0:
            raise ValueError("heat_multiplier must be at least 1.0")
        refs = [target.contact_ref for target in self.targets]
        if len(set(refs)) != len(refs):
            raise ValueError("a contact may appear in an enrichment plan only once")


@dataclass(frozen=True, slots=True)
class ProfileHarvest:
    """What one visit found (spec 9.10's ``ProfileHarvest``), for ``crm/apply.py`` to map.

    ``outcome`` is ``Ok`` (``details`` and ``contact_info`` both present),
    ``NotFound`` (both ``None``: the profile is not there, spec 9.8's streak),
    or ``RouteChanged`` (both ``None``: the profile answered in a shape the
    parser could not read; the core records the attempt and waits before
    visiting again).
    ``requested_public_id`` is the slug the core asked the job to visit, which
    may differ from ``details.public_id`` after a vanity-url change; deciding
    whether the profile found is the contact asked for is the core's, against
    the URN it holds. ``observed_at`` is when the visit finished, aware.
    """

    contact_ref: int
    requested_public_id: str
    outcome: Outcome
    observed_at: datetime
    details: ProfileDetails | None = None
    contact_info: ContactInfo | None = None

    def __post_init__(self) -> None:
        if self.outcome is Outcome.OK:
            if self.details is None or self.contact_info is None:
                raise ValueError("an Ok harvest carries both the details and the contact info")
        elif self.outcome in (Outcome.NOT_FOUND, Outcome.ROUTE_CHANGED):
            if self.details is not None or self.contact_info is not None:
                raise ValueError(f"a {self.outcome.value} harvest carries nothing")
        else:
            raise ValueError(
                f"a harvest is Ok, NotFound, or RouteChanged, not {self.outcome.value}"
            )


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One step of a run, for ``sync_run.progress_json`` and the SSE stream (spec 9.10).

    Counts only: no names, no slugs, nothing a log or a browser tab should not
    hold. ``planned`` is how many visits the run set out to make.
    """

    planned: int
    visited: int
    harvested: int
    not_found: int
    stopped: StopReason | None = None
    unreadable: int = 0


@dataclass(frozen=True, slots=True)
class EnrichResult:
    """How a run ended.

    ``completed`` is every target the run finished with, in order: harvested
    or not found, handed to the core either way. A resumed plan skips exactly
    these. ``visits`` counts navigations, including the one whose response
    stopped the run. ``outcome`` and ``final_url`` describe that response when
    ``reason`` is :attr:`StopReason.RESPONSE` (the core raises heat or the
    session flag from them); ``final_url`` has the visited slug masked out.
    ``plan`` is the pacing plan the run followed, for a report to show, and
    ``fetch_gaps_s`` the pause taken between each visit's two fetches, one entry
    per visit in order, ``None`` for a visit whose second fetch was never made.
    """

    reason: StopReason
    planned: int
    visits: int
    completed: tuple[int, ...]
    not_found: int = 0
    outcome: Outcome | None = None
    final_url: str | None = None
    plan: EnrichmentPlan = field(default_factory=lambda: EnrichmentPlan(steps=(), burst_sizes=()))
    unreadable: int = 0
    fetch_gaps_s: tuple[float | None, ...] = ()


# --- the source seam ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer[T]:
    """What a source answered for one request: the classification, and the value when ``Ok``.

    ``value`` is ``None`` for every outcome but ``Ok``, and for an ``Ok``
    response whose body did not parse: then ``outcome`` is ``RouteChanged`` and
    ``unparsed`` is true, which is what tells one unreadable profile from an
    endpoint that answered ``400`` or not JSON at all. ``final_url`` is where
    the response came from, with the profile's own path segment masked out.
    """

    outcome: Outcome
    final_url: str
    value: T | None = None
    unparsed: bool = False


class ProfileSource(Protocol):
    """Where profile visits happen. P2-08's DOM fallback implements the fetches too."""

    async def open_profile(self, public_id: str) -> Answer[None]:
        """Navigate to the profile page; ``Ok`` unless the page landed on a checkpoint or login."""
        ...

    async def scroll(self, plan: ScrollPlan) -> None:
        """Replay ``plan`` on the profile page: the wheel events, their pauses, the dwell."""
        ...

    async def fetch_details(self, public_id: str) -> Answer[ProfileDetails]:
        """The profile's details: classified, and parsed only when ``Ok``."""
        ...

    async def fetch_contact_info(self, public_id: str) -> Answer[ContactInfo]:
        """The contact-info overlay's contents: classified, and parsed only when ``Ok``."""
        ...


class Landed(Protocol):
    """What a navigation hands back: the tab, whose ``url`` is where it landed."""

    @property
    def url(self) -> str: ...


Navigate = Callable[[str], Awaitable[Landed]]
"""Drive the tab to a url and return it (``BrowserRun.goto``); its ``url`` is after redirects."""

Scroll = Callable[[ScrollPlan], Awaitable[object]]
"""Replay a scroll plan on the tab (``BrowserRun.scroll``, #152)."""


@dataclass(frozen=True, slots=True)
class BrowserProfiles:
    """The real :class:`ProfileSource`: one tab's navigation, its scroll, and the in-page API.

    ``navigate`` and ``scroll`` drive the run's tab; ``fetch`` is the
    ``VoyagerFetch`` bound to the same run (``PageVoyagerFetch``, #150).
    ``origin`` is :data:`LINKEDIN_ORIGIN` for a real run and the loopback
    replica's for a rehearsal. ``headers`` go on every fetch; the fetch helper
    adds what it reads from the live page.
    """

    navigate: Navigate
    scroll_page: Scroll
    fetch: VoyagerFetch
    origin: str = LINKEDIN_ORIGIN
    headers: Mapping[str, str] = field(default_factory=dict)

    def profile_url(self, public_id: str) -> str:
        return f"{self.origin.rstrip('/')}/in/{quote(public_id, safe='')}/"

    async def open_profile(self, public_id: str) -> Answer[None]:
        page = await self.navigate(self.profile_url(public_id))
        masked = _masked(page.url)
        return Answer(outcome=_navigation_outcome(masked), final_url=masked)

    async def scroll(self, plan: ScrollPlan) -> None:
        await self.scroll_page(plan)

    async def fetch_details(self, public_id: str) -> Answer[ProfileDetails]:
        request = VoyagerRequest(
            path=PROFILE_PATH, query=profile_query(public_id), headers=self.headers
        )
        return await self._fetch(request, public_id, parse_profile_details, PROFILE_ENDPOINT)

    async def fetch_contact_info(self, public_id: str) -> Answer[ContactInfo]:
        request = VoyagerRequest(path=contact_info_path(public_id), headers=self.headers)
        return await self._fetch(request, public_id, parse_contact_info, CONTACT_INFO_ENDPOINT)

    async def _fetch[T](
        self,
        request: VoyagerRequest,
        public_id: str,
        parser: Callable[[str], T],
        endpoint: str,
    ) -> Answer[T]:
        response = await self.fetch(request)
        masked = _masked(response.final_url)
        outcome = classify(response.status, masked, response.body)
        if outcome is not Outcome.OK:
            # Never parsed: a checkpoint page is not a profile (#150's gate).
            return Answer(outcome=outcome, final_url=masked)
        try:
            value = parser(response.body)
        except RouteChanged:
            log.warning("enrichment: %s answered a shape its parser does not know", endpoint)
            return Answer(outcome=Outcome.ROUTE_CHANGED, final_url=masked, unparsed=True)
        return Answer(outcome=Outcome.OK, final_url=masked, value=value)


def _navigation_outcome(url: str) -> Outcome:
    """Spec 9.7 for a page view: where it landed is all a navigation says.

    A page's HTML is not an API body, so only the url is classified: a
    checkpoint or login redirect reads as one, and anything else is a page
    that loaded. (Handing :func:`classify` an empty JSON object as the body is
    how its url rules are asked on their own.)
    """
    return classify(200, url, "{}")


#: A path segment that names a profile: the one after ``/in/`` (the profile page)
#: or after ``/identity/profiles/`` (the contact-info path). Case-insensitive, and
#: up to the next ``/``, ``?`` or ``#``, so it takes a raw, percent-encoded, or
#: renamed slug whole and never anything past it.
_PROFILE_SEGMENT: Final = re.compile(r"(/(?:in|identity/profiles)/)[^/?#]+", re.IGNORECASE)


def _masked(url: str) -> str:
    """``url`` with every path segment that names a profile replaced by ``_``.

    See the module docstring: a slug is somebody's name, never a signal about
    the session, and one that happens to read ``checkpoint`` or ``login...``
    must not classify as one -- the slug asked for, or a renamed one the site
    redirected to. Only the segment itself is masked, never the word wherever
    it appears: masking ``checkpoint`` everywhere would turn a real redirect to
    ``/checkpoint/lg/login`` into a login wall, or ``/checkpoint/...`` into no
    wall at all, the one misreading that must never happen. Only the path is
    touched; a query or fragment is left exactly as it came.
    """
    head, sep, tail = url.partition("?")
    if not sep:
        head, sep, tail = url.partition("#")
    return _PROFILE_SEGMENT.sub(r"\1_", head) + sep + tail


# --- the gate ----------------------------------------------------------------


class VisitGate(Protocol):
    """The core's say over each visit. Budgets, cancel, and active hours live behind this.

    :meth:`before_visit` runs before every visit, never during one, and returns
    ``None`` to let it go ahead or one of :data:`GATE_REASONS` to stop the run
    (spec 9.4, 9.9). :meth:`pause` waits out the gap between two profiles and
    returns ``False`` when a cancel arrived during it (spec 9.9's "sliced
    cooldowns"); the run then stops before the next visit.
    """

    async def before_visit(self, number: int) -> StopReason | None: ...

    async def pause(self, seconds: float) -> bool: ...


HarvestSink = Callable[[ProfileHarvest], Awaitable[None]]
ProgressSink = Callable[[ProgressEvent], Awaitable[None]]


async def _no_progress(event: ProgressEvent) -> None:
    return None


async def _real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def stretched(pacing: PacingProfile, multiplier: float) -> PacingProfile:
    """``pacing`` with its delay median stretched by heat's ``multiplier`` (spec 9.7)."""
    delay = pacing.delay
    return PacingProfile(
        delay=DelayProfile(
            median=delay.median * multiplier,
            sigma=delay.sigma,
            tail_p=delay.tail_p,
            tail_range=delay.tail_range,
        ),
        burst=pacing.burst,
        scroll=pacing.scroll,
    )


# --- the job -----------------------------------------------------------------


async def run_enrichment(
    spec: EnrichJobSpec,
    source: ProfileSource,
    gate: VisitGate,
    *,
    on_harvest: HarvestSink,
    rng: random.Random,
    on_progress: ProgressSink = _no_progress,
    clock: Callable[[], datetime] = _utcnow,
    sleep: Callable[[float], Awaitable[None]] = _real_sleep,
) -> EnrichResult:
    """Visit the spec's targets in order, to its stopping point, and say why it stopped.

    ``on_harvest`` receives each harvest as soon as its visit finishes, before
    the gate is asked about the next one. An exception from it propagates and
    ends the run: a run that ends by exception has no :class:`EnrichResult`,
    and the target it was handing over is not in anybody's completed list.
    ``rng`` draws the pacing plan (:func:`~netkeeper.linkedin.pacing.plan_enrichment`),
    then each pause between a visit's two fetches; a seeded one replays both.
    ``sleep`` waits out that pause (the waits *between* profiles are the gate's).
    """
    planned = min(len(spec.targets), spec.visit_budget)
    pacing = stretched(spec.pacing, spec.heat_multiplier)
    plan = plan_enrichment(
        rng, planned, delay=pacing.delay, burst=pacing.burst, scroll=pacing.scroll
    )
    completed: list[int] = []
    gaps: list[float | None] = []
    visits = harvested = not_found = unreadable = unreadable_in_a_row = 0

    def progress(stopped: StopReason | None = None) -> ProgressEvent:
        return ProgressEvent(
            planned=planned,
            visited=len(completed),
            harvested=harvested,
            not_found=not_found,
            stopped=stopped,
            unreadable=unreadable,
        )

    async def stop(
        reason: StopReason, outcome: Outcome | None = None, final_url: str | None = None
    ) -> EnrichResult:
        await on_progress(progress(reason))
        log.info(
            "enrichment stopped: %s after %d of %d planned visits (%d harvested, %d not found)%s",
            reason.value,
            len(completed),
            planned,
            harvested,
            not_found,
            f", {outcome.value}" if outcome is not None else "",
        )
        return EnrichResult(
            reason=reason,
            planned=planned,
            visits=visits,
            completed=tuple(completed),
            not_found=not_found,
            outcome=outcome,
            final_url=final_url,
            plan=plan,
            unreadable=unreadable,
            fetch_gaps_s=tuple(gaps),
        )

    for index, step in enumerate(plan.steps):
        if index > 0:
            gap = plan.steps[index - 1].delay_after_s or 0.0
            if not await gate.pause(gap):
                return await stop(StopReason.CANCELLED)
        refusal = await gate.before_visit(index)
        if refusal is not None:
            if refusal not in GATE_REASONS:
                raise ValueError(f"a gate may not stop a run for {refusal.value}")
            return await stop(refusal)

        target = spec.targets[index]
        slug = target.li_public_id
        visits += 1
        page = await source.open_profile(slug)
        if page.outcome is not Outcome.OK:
            return await stop(StopReason.RESPONSE, page.outcome, page.final_url)
        await source.scroll(step.scroll)

        details = await source.fetch_details(slug)
        info: Answer[ContactInfo] | None = None
        gaps.append(None)
        if details.outcome is Outcome.OK:
            gap = human_delay(
                rng, median=FETCH_GAP_MEDIAN_S, sigma=FETCH_GAP_SIGMA, tail_p=0.0, tail_range=(0, 0)
            )
            gaps[-1] = gap
            await sleep(gap)
            info = await source.fetch_contact_info(slug)
        answers = [answer for answer in (details, info) if answer is not None]
        failed = next((a for a in answers if a.outcome is not Outcome.OK), None)
        if failed is not None and failed.outcome is Outcome.NOT_FOUND:
            outcome = Outcome.NOT_FOUND
            not_found += 1
            unreadable_in_a_row = 0
        elif failed is not None and failed.unparsed:
            outcome = Outcome.ROUTE_CHANGED
            unreadable += 1
            unreadable_in_a_row += 1
        elif failed is not None:
            return await stop(StopReason.RESPONSE, failed.outcome, failed.final_url)
        else:
            outcome = Outcome.OK
            harvested += 1
            unreadable_in_a_row = 0
        if outcome is Outcome.OK:
            assert info is not None and details.value is not None and info.value is not None
            harvest = ProfileHarvest(
                contact_ref=target.contact_ref,
                requested_public_id=slug,
                outcome=Outcome.OK,
                observed_at=clock(),
                details=details.value,
                contact_info=info.value,
            )
        else:
            harvest = ProfileHarvest(
                contact_ref=target.contact_ref,
                requested_public_id=slug,
                outcome=outcome,
                observed_at=clock(),
            )
        await on_harvest(harvest)
        completed.append(target.contact_ref)
        await on_progress(progress())
        if outcome is Outcome.ROUTE_CHANGED and (
            unreadable_in_a_row >= MAX_UNREADABLE_IN_A_ROW or unreadable >= MAX_UNREADABLE_PER_RUN
        ):
            assert failed is not None
            return await stop(StopReason.RESPONSE, Outcome.ROUTE_CHANGED, failed.final_url)

    if planned < len(spec.targets):
        return await stop(StopReason.VISIT_BUDGET)
    return await stop(StopReason.END_OF_PLAN)
