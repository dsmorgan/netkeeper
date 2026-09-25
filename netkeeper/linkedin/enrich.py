"""Enrichment, the extractor half: visit profiles, harvest them, hand the harvests back (spec 9.4).

Behind the extractor boundary (spec 9.10, ADR 0005): an :class:`EnrichJobSpec`
comes in, :class:`ProfileHarvest` values and :class:`ProgressEvent` values go
out through the callbacks the caller passes, and an :class:`EnrichResult` is
returned. Nothing here imports the models or opens a session. The core picks
who to visit (spec 9.6), maps each harvest onto the contact in
``netkeeper.crm.apply``, and wires budgets, heat, cancel, and the stored plan
around this loop in ``netkeeper.services.enrichment``.

**One visit, in spec 9.4's order** (as built by #190, ADR 0006). For each target,
one unit of work:

1. navigate the tab to the profile page -- a real page view -- and read the
   profile screen the page loads with it (:meth:`ProfileSource.open_profile`);
2. scroll it like a person, then dwell (spec 9.5; the plan comes from
   :func:`netkeeper.linkedin.pacing.plan_enrichment`), and read the profile from
   the screen and the lazy cards the scroll made the page load
   (:meth:`ProfileSource.read_profile`);
3. only when the profile's own id is the contact's URN: scroll back to the top,
   pause as a person does before reaching for a link (:data:`CLICK_PAUSE_MEDIAN_S`),
   click **Contact info** once, and read the overlay's answer
   (:meth:`ProfileSource.read_contact_info`). A profile under another id gets no
   click: its harvest carries the details and no contact info, and the core
   records the mismatch and writes nothing;
4. hand the harvest to the core before the next visit starts, so what the
   core has written is never more than one profile behind what the run saw;
5. wait the plan's gap -- a :func:`~netkeeper.linkedin.pacing.human_delay`, or
   a burst break -- before the next profile.

The click is part of the visit: the one ``profile_visits`` unit the gate spent
before the navigation covers the page load, the scroll, and the click (ADR 0006;
spec 9.6 ties ``contact_info_fetches`` to visits one for one).

**What stops a run.** The gate is asked before every visit, never partway
through one (spec 9.4, 9.9): a budget refusal, a cancel, or the end of the
active window stops the run *between* profiles, and a cancel noticed during
the wait between two profiles stops it there. The first answer that is not
``Ok`` stops the run, with two exceptions that are the contact's problem rather
than the run's. ``NotFound`` on a profile is terminal for that contact only
(spec 9.7), handed to the core as a not-found harvest (spec 9.8's streak). A
visit the source could not read -- a profile answer in a shape the parser does
not know, a tab that landed somewhere other than a profile, a Contact info
control that is missing or not alone, an overlay that never answered -- is an
*unreadable* harvest: one person's profile can hold a shape nobody has seen
yet, and stopping on it would leave that person at the head of every run's
queue for ever. :data:`MAX_UNREADABLE_IN_A_ROW` unreadable profiles in a row,
on different contacts, is the signal that the route itself changed, and stops
the run as ``RouteChanged``; so is :data:`MAX_UNREADABLE_PER_RUN` in one run,
scattered or not (#172). A profile whose id is not the contact's URN counts
toward both limits as well (#190): one is a vanity url that changed hands, but
several are more likely a page the parser reads the wrong id from, and a run
that went on would spend the day's visits clicking nothing and writing nothing.
Nothing is retried, a checkpoint least of all, and a click least of all; like
the connections job, a throttle stops the run at once rather than taking spec
9.7's "at most 3 attempts", and the next scheduled run is the retry, with heat
raised.

**A slug is not a signal.** Spec 9.7 classifies by the url a response came
from, and a profile's url carries its slug. A person whose vanity url is
``checkpoint`` or starts with ``login`` would read as a checkpoint or a login
wall, stop every run at their name, and flag the session each time. So the
path segment that names a profile -- the one after ``/in/`` -- is masked out of
a url before it is classified (:func:`masked`), whatever it says: the slug
asked for, a renamed one the site redirected to, in any case or encoding. A
redirect to ``/checkpoint/`` still reads as one, and a profile named like one
does not.

**The source seam.** The job reads profiles through a :class:`ProfileSource`.
:class:`netkeeper.linkedin.page_profiles.PageProfiles` is the real one, built on
one browser run's navigation, its scroll, its observation of what the page
loads, and its one click. ``netkeeper rehearse`` builds the same class against
the loopback replica, so a rehearsal is this loop, not a copy of it.

**When the mechanism itself breaks.** A source raises when there is nothing to
classify at all: the run's tab went away (``BrowserUnavailable``), or the
observation dropped an answer (``ObservationFailed``). The job does not catch
either: it ends the run by exception, the runner marks the plan aborted and
re-raises, and nothing raises heat or the session flag, since no response said
anything about the session.
"""

from __future__ import annotations

import enum
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol

from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.pacing import (
    DEFAULT_BURST_PROFILE,
    DEFAULT_DELAY_PROFILE,
    DEFAULT_SCROLL_PROFILE,
    BurstProfile,
    DelayProfile,
    EnrichmentPlan,
    ScrollPlan,
    ScrollProfile,
    depth_after,
    human_delay,
    plan_enrichment,
    scroll_back_to_top,
)
from netkeeper.linkedin.voyager import ContactInfo, ProfileDetails

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
    """An answer classified as something other than ``Ok``, a per-contact
    ``NotFound``, or an unreadable profile; see ``EnrichResult.outcome``."""


#: The pause a person takes between reaching the top of a profile and clicking
#: **Contact info**: a second or two, with no distraction tail. Spec 9.5 has no
#: figure for it. Drawn with :func:`~netkeeper.linkedin.pacing.human_delay`.
CLICK_PAUSE_MEDIAN_S: Final = 1.5
CLICK_PAUSE_SIGMA: Final = 0.5

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
    """One contact to visit: the core's reference for it, the slug to visit it at, and
    the URN the core holds for it.

    ``li_urn`` is what the profile's own id must be before the job clicks anything
    on the page: a slug can change hands, and the Contact info click is for this
    contact's profile only. The core checks the same URN again before it writes
    (``crm/apply.py``).
    """

    contact_ref: int
    li_public_id: str
    li_urn: str

    def __post_init__(self) -> None:
        if not self.li_public_id.strip():
            raise ValueError("an enrichment target needs a public id to visit")
        if not self.li_urn.strip():
            raise ValueError("an enrichment target needs the URN its profile must have")


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

    ``outcome`` is ``Ok`` (``details`` present), ``NotFound`` (nothing: the profile
    is not there, spec 9.8's streak), or ``RouteChanged`` (nothing: the visit could
    not be read; the core records the attempt and waits before visiting again).
    An ``Ok`` harvest's ``contact_info`` is ``None`` only when the job did not click
    **Contact info** because the profile's id was not the contact's URN: the core
    finds the same mismatch and writes nothing.
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
            if self.details is None:
                raise ValueError("an Ok harvest carries the profile's details")
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

    ``completed`` is every target the run finished with, in order: harvested,
    not found, or unreadable, handed to the core either way. A resumed plan skips
    exactly these. ``visits`` counts navigations, including the one whose answer
    stopped the run. ``outcome`` and ``final_url`` describe that answer when
    ``reason`` is :attr:`StopReason.RESPONSE` (the core raises heat or the
    session flag from them); ``final_url`` has the visited slug masked out.
    ``plan`` is the pacing plan the run followed, for a report to show;
    ``click_pauses_s`` the pause before each visit's Contact info click, one entry
    per visit in order, ``None`` for a visit that clicked nothing; ``clicks``
    how many clicks the run asked for, never more than one per visit; and
    ``mismatched`` how many profiles answered under another id than the contact's.
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
    click_pauses_s: tuple[float | None, ...] = ()
    clicks: int = 0
    mismatched: int = 0


# --- the source seam ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer[T]:
    """What a source answered for one step: the classification, and the value when ``Ok``.

    ``value`` is ``None`` for every outcome but ``Ok``. ``unparsed`` marks a
    ``RouteChanged`` that is this one visit's, not the route's: the page answered,
    but not in a shape the parser knows, or the tab was not where a profile should
    be, or the Contact info control was not there to click once. That is what tells
    an unreadable profile from an answer that stops the run. ``final_url`` is where
    the answer came from, with the profile's own path segment masked out.
    """

    outcome: Outcome
    final_url: str
    value: T | None = None
    unparsed: bool = False


class ProfileSource(Protocol):
    """Where profile visits happen: :class:`~netkeeper.linkedin.page_profiles.PageProfiles`."""

    async def open_profile(self, public_id: str) -> Answer[None]:
        """Navigate to the profile page and read the screen it loads with it.

        ``Ok`` when the tab is on a profile and its screen arrived; a wall's outcome
        when it landed on one; ``NotFound`` when the page said the profile is not
        there; an unparsed ``RouteChanged`` when it landed anywhere else.
        """
        ...

    async def scroll(self, plan: ScrollPlan) -> None:
        """Replay ``plan`` on the profile page: the wheel events, their pauses, the dwell."""
        ...

    async def read_profile(self, public_id: str) -> Answer[ProfileDetails]:
        """The profile, from the screen and the lazy cards the page has loaded so far."""
        ...

    async def read_contact_info(
        self, profile: ProfileDetails, *, back: ScrollPlan, pause_s: float
    ) -> Answer[ContactInfo]:
        """Replay ``back`` (to the top), wait ``pause_s``, click **Contact info** once,
        and read the overlay's answer. Never clicks twice, never retries."""
        ...


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


#: A path segment that names a profile: the one after ``/in/``. Case-insensitive, and
#: up to the next ``/``, ``?`` or ``#``, so it takes a raw, percent-encoded, or renamed
#: slug whole and never anything past it.
_PROFILE_SEGMENT: Final = re.compile(r"(/in/)[^/?#]+", re.IGNORECASE)


def masked(url: str) -> str:
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
) -> EnrichResult:
    """Visit the spec's targets in order, to its stopping point, and say why it stopped.

    ``on_harvest`` receives each harvest as soon as its visit finishes, before
    the gate is asked about the next one. An exception from it propagates and
    ends the run: a run that ends by exception has no :class:`EnrichResult`,
    and the target it was handing over is not in anybody's completed list.
    ``rng`` draws the pacing plan (:func:`~netkeeper.linkedin.pacing.plan_enrichment`),
    then, for each visit that clicks, the pause before the click and the scroll
    back to the top; a seeded one replays all of it. The waits *between* profiles
    are the gate's; the pause before a click is the source's to wait out.
    """
    planned = min(len(spec.targets), spec.visit_budget)
    pacing = stretched(spec.pacing, spec.heat_multiplier)
    plan = plan_enrichment(
        rng, planned, delay=pacing.delay, burst=pacing.burst, scroll=pacing.scroll
    )
    completed: list[int] = []
    pauses: list[float | None] = []
    visits = harvested = not_found = unreadable = unreadable_in_a_row = clicks = mismatched = 0

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
            click_pauses_s=tuple(pauses),
            clicks=clicks,
            mismatched=mismatched,
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
        pauses.append(None)
        page = await source.open_profile(slug)
        answers: list[Answer[None] | Answer[ProfileDetails] | Answer[ContactInfo]] = [page]
        details: Answer[ProfileDetails] | None = None
        info: Answer[ContactInfo] | None = None
        mismatch = False
        if page.outcome is Outcome.OK:
            await source.scroll(step.scroll)
            details = await source.read_profile(slug)
            answers.append(details)
            if details.outcome is Outcome.OK:
                assert details.value is not None
                if details.value.urn != target.li_urn:
                    # Another person's profile, or this one under another id: no click.
                    # The core sees the same mismatch and writes nothing.
                    mismatch = True
                    log.warning(
                        "enrichment: the profile for contact %d is not theirs; not clicking",
                        target.contact_ref,
                    )
                else:
                    pause = human_delay(
                        rng,
                        median=CLICK_PAUSE_MEDIAN_S,
                        sigma=CLICK_PAUSE_SIGMA,
                        tail_p=0.0,
                        tail_range=(0, 0),
                    )
                    back = scroll_back_to_top(rng, depth_after(step.scroll))
                    pauses[-1] = pause
                    clicks += 1
                    info = await source.read_contact_info(details.value, back=back, pause_s=pause)
                    answers.append(info)
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
        elif mismatch:
            outcome = Outcome.OK
            harvested += 1
            mismatched += 1
            unreadable_in_a_row += 1
        else:
            outcome = Outcome.OK
            harvested += 1
            unreadable_in_a_row = 0
        if outcome is Outcome.OK:
            assert details is not None and details.value is not None
            harvest = ProfileHarvest(
                contact_ref=target.contact_ref,
                requested_public_id=slug,
                outcome=Outcome.OK,
                observed_at=clock(),
                details=details.value,
                contact_info=None if info is None else info.value,
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
        if (outcome is Outcome.ROUTE_CHANGED or mismatch) and (
            unreadable_in_a_row >= MAX_UNREADABLE_IN_A_ROW
            or unreadable + mismatched >= MAX_UNREADABLE_PER_RUN
        ):
            last = failed if failed is not None else details
            assert last is not None
            return await stop(StopReason.RESPONSE, Outcome.ROUTE_CHANGED, last.final_url)

    if planned < len(spec.targets):
        return await stop(StopReason.VISIT_BUDGET)
    return await stop(StopReason.END_OF_PLAN)
