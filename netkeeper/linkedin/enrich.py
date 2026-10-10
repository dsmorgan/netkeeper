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

**An answer whose body cannot be read** (#197) -- the profile's screen arrived, but
the browser had no body to hand over -- is an unreadable visit, counted toward the
same two limits, with the answer and a fixed cause in :attr:`EnrichResult.lost` for
the run's note. A wall on the tab still stops the run as that wall.

**A Contact info answer whose body was lost is not a changed route** (#405). The
profile read whole, the click went out, and the overlay answered ``200``; Chrome
just kept no body for it, and the body tap had no whole copy. That says nothing
about the page's shape, so it is a *soft* failure: the harvest carries the profile
without contact info (:attr:`ProfileHarvest.contact_info_lost`), the core writes the
profile and leaves the contact due so a later run reads Contact info again, and the
visit is recorded (:attr:`UnreadableCause.CONTACT_INFO_DEFERRED`, a line in
:attr:`EnrichResult.deferred`) without counting toward the unreadable limits. It neither
adds to nor clears the unreadable streak. Nothing is clicked again on that visit.
A systemic loss still stops the run, as ``answer_lost`` rather than ``route_changed``:
:data:`MAX_CONTACT_INFO_LOST_IN_A_ROW` lost overlays in a row, or more than half of
the run's overlays once it has clicked :data:`CONTACT_INFO_LOST_SHARE_AFTER` times.

**When the mechanism itself breaks.** A source raises when there is nothing to
classify at all: the run's tab went away (``BrowserUnavailable``), or the
observation dropped an answer or could not keep one (``ObservationFailed``). The job does not catch
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

    ANSWER_LOST = "answer_lost"
    """Too many Contact info answers arrived with no body the browser could hand over
    (#405): :data:`MAX_CONTACT_INFO_LOST_IN_A_ROW` in a row, or more than half of the
    run's overlays after :data:`CONTACT_INFO_LOST_SHARE_AFTER` clicks."""


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

#: Contact info answers lost in a row (#405) that stop the run as ``answer_lost``.
#: One lost overlay is Chrome keeping no body for one answer, about one in seven on
#: live runs; five in a row is the mechanism failing, not chance. Only visits that
#: clicked count: a successful overlay read clears the streak, and a visit that
#: clicked nothing leaves it where it was.
MAX_CONTACT_INFO_LOST_IN_A_ROW: Final = 5

#: The clicks a run makes before the share rule below applies (#405). Once a run has
#: clicked this many times, more than half of its overlays lost stops it as
#: ``answer_lost``: a loss that comes and goes never trips the in-a-row rule, and
#: without this a run could spend its visits saving profiles with no contact info.
CONTACT_INFO_LOST_SHARE_AFTER: Final = 6

#: The reasons a gate may give for refusing a visit.
GATE_REASONS: Final = frozenset({StopReason.BUDGET, StopReason.CANCELLED, StopReason.INACTIVE})


class UnreadableCause(enum.StrEnum):
    """Why one visit was unreadable, or counted toward the unreadable limits (#405).

    A fixed vocabulary for the run's record, its log, and its detail view: never a
    url, a slug, a name, or anything else the page said. Each distinct path that
    makes a visit unreadable has its own code, so a run's per-visit record says
    which one fired. Adding a code is safe; renaming one orphans the records that
    already hold it.
    """

    PROFILE_SHAPE_UNKNOWN = "profile_shape_unknown"
    """The profile answered in a shape the parser does not know."""

    CONTACT_INFO_SHAPE_UNKNOWN = "contact_info_shape_unknown"
    """The Contact info overlay answered in a shape the parser does not know."""

    LANDED_OFF_PROFILE = "landed_off_profile"
    """The navigation, or a redirect, left the tab somewhere that is not a profile."""

    LEFT_PROFILE = "left_profile"
    """The tab left the profile during the visit (a scroll, or before the click)."""

    UNEXPECTED_PROFILE = "unexpected_profile"
    """The tab is on a profile nobody asked for, and no redirect of this visit led there."""

    NO_PROFILE_SCREEN = "no_profile_screen"
    """The page loaded, but the profile's screen never arrived."""

    PROFILE_SCREEN_STATUS = "profile_screen_status"
    """The profile's screen request answered 404 (never read as a missing profile)."""

    TOO_MANY_LAZY_CARDS = "too_many_lazy_cards"
    """More lazy cards arrived than a profile loads."""

    CONTACT_INFO_CONTROL_MISSING = "contact_info_control_missing"
    """No Contact info control on the page."""

    CONTACT_INFO_CONTROL_NOT_ALONE = "contact_info_control_not_alone"
    """More than one Contact info control on the page."""

    CONTACT_INFO_CONTROL_UNREADABLE = "contact_info_control_unreadable"
    """The Contact info control could not be read."""

    CONTACT_INFO_CONTROL_ELSEWHERE = "contact_info_control_elsewhere"
    """The Contact info control opens something other than this profile's overlay."""

    CONTACT_INFO_CONTROL_UNCLICKABLE = "contact_info_control_unclickable"
    """The Contact info control could not be clicked."""

    CONTACT_INFO_NOT_CLICKED = "contact_info_not_clicked"
    """Contact info was not clicked, for a reason with no code of its own."""

    OVERLAY_NEVER_ANSWERED = "overlay_never_answered"
    """The click was sent, and the Contact info overlay never answered."""

    OVERLAY_OTHER_PROFILE = "overlay_other_profile"
    """The page asked for another profile's Contact info overlay."""

    OVERLAY_REDIRECTED = "overlay_redirected"
    """The overlay's answer redirected somewhere that is not a wall."""

    OVERLAY_STATUS = "overlay_status"
    """The overlay answered with a status that is neither Ok nor a wall."""

    NAVIGATION_TIMED_OUT = "navigation_timed_out"
    """The profile's navigation never finished loading (#197)."""

    PROFILE_SCREEN_LOST = "profile_screen_lost"
    """The profile's screen arrived, but the browser had no body to hand over (#197)."""

    CONTACT_INFO_LOST = "contact_info_lost"
    """The overlay answered, but the browser had no body to hand over (#197). What a
    source answers; since #405 the job records such a visit as
    :attr:`CONTACT_INFO_DEFERRED`, and runs before it hold this code as an
    unreadable visit."""

    CONTACT_INFO_DEFERRED = "contact_info_deferred"
    """The overlay's body was lost (:attr:`CONTACT_INFO_LOST`), so the profile was saved
    without contact info and the contact stays due (#405). Recorded, but not counted
    toward the unreadable limits."""

    PROFILE_STATUS = "profile_status"
    """The profile's page or screen answered a status that is neither Ok, NotFound, nor a
    wall (a 500, a 410). Not an unreadable visit: it stops the run at once as
    ``route_changed``, and is recorded as the visit that stopped it."""

    ID_MISMATCH = "id_mismatch"
    """The profile's own id is not the contact's URN (#190): read, not clicked, not written."""

    UNKNOWN = "unknown"
    """A source that gave no cause: a fake, or a path with no code yet."""


@dataclass(frozen=True, slots=True)
class UnreadableVisit:
    """One visit that counted toward the unreadable limits, for the run's record (#405).

    ``visit`` is the visit's number in this run, from 1. ``contact_ref`` is the core's
    own reference for the contact (its id), never anything from the page. A visit whose
    Contact info was deferred (:attr:`UnreadableCause.CONTACT_INFO_DEFERRED`, #405) is
    recorded the same way, though it does not count toward the limits.
    """

    visit: int
    contact_ref: int
    cause: UnreadableCause


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
    ``contact_info_from_copy`` is true when the Contact info was read from the body
    tap's streamed copy of the overlay's answer, not its own body (#207 review): the
    copy passed every check, but the core may still trust a thin one less.
    ``unreadable_cause`` says why a ``RouteChanged`` harvest could not be read, or is
    :attr:`UnreadableCause.ID_MISMATCH` on an ``Ok`` harvest under another id (#405).
    ``contact_info_lost`` marks an ``Ok`` harvest whose profile was read whole and whose
    Contact info answer arrived with no body (#405): it carries no contact info, its
    cause is :attr:`UnreadableCause.CONTACT_INFO_DEFERRED`, and the core writes the
    profile and leaves the contact due.
    """

    contact_ref: int
    requested_public_id: str
    outcome: Outcome
    observed_at: datetime
    details: ProfileDetails | None = None
    contact_info: ContactInfo | None = None
    contact_info_from_copy: bool = False
    unreadable_cause: UnreadableCause | None = None
    contact_info_lost: bool = False

    def __post_init__(self) -> None:
        if self.outcome is Outcome.NOT_FOUND and self.unreadable_cause is not None:
            raise ValueError("a NotFound harvest has no unreadable cause")
        deferred = self.unreadable_cause is UnreadableCause.CONTACT_INFO_DEFERRED
        if self.contact_info_lost != deferred:
            raise ValueError("a lost Contact info and its deferred cause go together")
        if self.contact_info_lost and (
            self.outcome is not Outcome.OK or self.contact_info is not None
        ):
            raise ValueError("a lost Contact info is an Ok harvest without contact info")
        if self.outcome is Outcome.OK:
            if self.details is None:
                raise ValueError("an Ok harvest carries the profile's details")
            if self.contact_info_from_copy and self.contact_info is None:
                raise ValueError("a harvest without contact info did not read it from a copy")
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
    hold. ``planned`` is how many visits the run set out to make. ``harvested``
    counts the profiles read whole for their contact; a profile under another id is
    ``mismatched`` instead, since nothing of it is written. ``contact_info_lost``
    counts the harvested ones saved without Contact info because its answer's body
    was lost (#405).
    """

    planned: int
    visited: int
    harvested: int
    not_found: int
    stopped: StopReason | None = None
    unreadable: int = 0
    mismatched: int = 0
    contact_info_lost: int = 0


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
    how many clicks the run asked for, never more than one per visit;
    ``mismatched`` how many profiles answered under another id than the contact's;
    ``lost`` one fixed line per unreadable visit whose answer's body the browser could
    not hand over (#197), naming the visit by its number in this run; ``deferred`` one
    such line per visit whose Contact info body was lost and whose profile was saved
    without it (#405, kept apart since #424); ``contact_info_lost`` how many visits
    deferred their Contact info that way, which the unreadable limits do not count;
    and
    ``copied`` one fixed line per visit whose Contact info was read from the body
    tap's streamed copy instead (#207 review), and one per visit that kept a lazy
    card read from such a copy (#196 item 12). ``unreadable_visits`` is every visit
    that counted toward the unreadable limits, unreadable or id-mismatched, with its
    fixed cause and the contact's reference, in order (#405). ``stopped_by`` is the
    visit whose answer stopped the run at once as ``RouteChanged`` (not by the
    unreadable limits), with its cause; ``None`` for any other stop.
    ``contact_info_read`` is how many Contact info answers were read and parsed
    (#424).
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
    lost: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    copied: tuple[str, ...] = ()
    unreadable_visits: tuple[UnreadableVisit, ...] = ()
    stopped_by: UnreadableVisit | None = None
    contact_info_lost: int = 0
    contact_info_read: int = 0


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
    ``lost`` is set on an unparsed answer whose body the browser could not hand
    over (#197): which answer it was and why, in fixed words, never the
    exception's message. ``from_copy`` marks an ``Ok`` value read from the body tap's
    streamed copy of an answer whose own body was lost (#203): for a profile, one
    read with at least one lazy card from a copy (#196 item 12). ``cause`` is the
    fixed code for why an unparsed answer could not be read (#405); a source that
    leaves it ``None`` is recorded as :attr:`UnreadableCause.UNKNOWN`.
    """

    outcome: Outcome
    final_url: str
    value: T | None = None
    unparsed: bool = False
    lost: str | None = None
    from_copy: bool = False
    cause: UnreadableCause | None = None


class ScrollCancelled(Exception):
    """A cancel (spec 9.9) cut a scroll short before the click it led up to (#177)."""


class ProfileSource(Protocol):
    """Where profile visits happen: :class:`~netkeeper.linkedin.page_profiles.PageProfiles`."""

    async def open_profile(self, public_id: str) -> Answer[None]:
        """Navigate to the profile page and read the screen it loads with it.

        ``Ok`` when the tab is on a profile and its screen arrived; a wall's outcome
        when it landed on one; ``NotFound`` when the page said the profile is not
        there; an unparsed ``RouteChanged`` when it landed anywhere else.
        """
        ...

    async def scroll(
        self, plan: ScrollPlan, *, cancelled: Callable[[], Awaitable[bool]] | None = None
    ) -> bool:
        """Replay ``plan`` on the profile page: the wheel events, their pauses, the dwell.

        ``cancelled``, when given, is asked between wheel events and during the dwell.
        ``False`` when it said yes and the replay stopped early (#177); ``True``
        otherwise, including when no ``cancelled`` was given."""
        ...

    async def read_profile(self, public_id: str) -> Answer[ProfileDetails]:
        """The profile, from the screen and the lazy cards the page has loaded so far."""
        ...

    async def read_contact_info(
        self,
        profile: ProfileDetails,
        *,
        back: ScrollPlan,
        pause_s: float,
        cancelled: Callable[[], Awaitable[bool]] | None = None,
    ) -> Answer[ContactInfo]:
        """Replay ``back`` (to the top), wait ``pause_s``, click **Contact info** once,
        and read the overlay's answer. Never clicks twice, never retries.

        Raises :class:`ScrollCancelled`, before any click, when ``cancelled`` said
        yes during the replay (#177)."""
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
    cancelled: Callable[[], Awaitable[bool]] | None = None,
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
    ``cancelled`` (spec 9.9) is handed to the source's scroll, so a cancel interrupts
    the scroll and its dwell instead of waiting for the visit to end (#177): the run
    then stops ``cancelled`` with that profile unread, not harvested, and not completed.
    """
    planned = min(len(spec.targets), spec.visit_budget)
    pacing = stretched(spec.pacing, spec.heat_multiplier)
    plan = plan_enrichment(
        rng, planned, delay=pacing.delay, burst=pacing.burst, scroll=pacing.scroll
    )
    completed: list[int] = []
    pauses: list[float | None] = []
    lost: list[str] = []
    deferred_lines: list[str] = []
    copied: list[str] = []
    unreadable_visits: list[UnreadableVisit] = []
    visits = harvested = not_found = unreadable = unreadable_in_a_row = clicks = mismatched = 0
    # #405: Contact info answers lost, and lost in a row among the visits that clicked.
    info_lost = info_lost_in_a_row = 0
    # #424: Contact info answers read and parsed, for the breaker across runs.
    info_read = 0

    def progress(stopped: StopReason | None = None) -> ProgressEvent:
        return ProgressEvent(
            planned=planned,
            visited=len(completed),
            harvested=harvested,
            not_found=not_found,
            stopped=stopped,
            unreadable=unreadable,
            mismatched=mismatched,
            contact_info_lost=info_lost,
        )

    async def stop(
        reason: StopReason,
        outcome: Outcome | None = None,
        final_url: str | None = None,
        stopped_by: UnreadableVisit | None = None,
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
            lost=tuple(lost),
            deferred=tuple(deferred_lines),
            copied=tuple(copied),
            unreadable_visits=tuple(unreadable_visits),
            stopped_by=stopped_by,
            contact_info_lost=info_lost,
            contact_info_read=info_read,
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
        deferred = False
        if page.outcome is Outcome.OK:
            if not await source.scroll(step.scroll, cancelled=cancelled):
                log.info("enrichment: cancelled during the scroll of visit %d", visits)
                return await stop(StopReason.CANCELLED)
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
                    try:
                        info = await source.read_contact_info(
                            details.value, back=back, pause_s=pause, cancelled=cancelled
                        )
                    except ScrollCancelled:
                        log.info("enrichment: cancelled during the scroll back to the top")
                        pauses[-1] = None  # no click was made, so no pause was waited
                        return await stop(StopReason.CANCELLED)
                    clicks += 1
                    if _contact_info_lost(info):
                        # #405: the overlay answered and Chrome kept no body. Not the
                        # route: the profile is saved without it (below), and nothing
                        # is clicked again.
                        deferred = True
                    else:
                        answers.append(info)
                        if info.outcome is Outcome.OK:
                            info_lost_in_a_row = 0
                            info_read += 1
        failed = next((a for a in answers if a.outcome is not Outcome.OK), None)
        cause: UnreadableCause | None = None
        if failed is not None and failed.outcome is Outcome.NOT_FOUND:
            outcome = Outcome.NOT_FOUND
            not_found += 1
            unreadable_in_a_row = 0
        elif failed is not None and failed.unparsed:
            outcome = Outcome.ROUTE_CHANGED
            unreadable += 1
            unreadable_in_a_row += 1
            cause = failed.cause or UnreadableCause.UNKNOWN
            if failed.lost is not None:
                # #197: the browser received the answer but had no body to hand
                # over. An unreadable visit like any other, counted the same way.
                lost.append(f"visit {visits}: {failed.lost}")
                log.info("enrichment: visit %d was unreadable: %s", visits, failed.lost)
            # #405: every unreadable visit's cause, in fixed words, whatever it was.
            log.info("enrichment: visit %d was unreadable (%s)", visits, cause.value)
        elif failed is not None:
            stopping = None
            if failed.outcome is Outcome.ROUTE_CHANGED:
                # #405: a route_changed stop at once, not by the unreadable limits: the
                # run's record names the visit and why, as it does an unreadable one.
                stopping = UnreadableVisit(
                    visits, target.contact_ref, failed.cause or UnreadableCause.UNKNOWN
                )
                log.info("enrichment: visit %d stopped the run (%s)", visits, stopping.cause.value)
            return await stop(StopReason.RESPONSE, failed.outcome, failed.final_url, stopping)
        elif mismatch:
            outcome = Outcome.OK
            mismatched += 1
            unreadable_in_a_row += 1
            cause = UnreadableCause.ID_MISMATCH
        elif deferred:
            # #405: harvested without contact info. Neither adds to the unreadable
            # streak nor clears it: the profile read, but the overlay did not.
            assert info is not None and info.lost is not None
            outcome = Outcome.OK
            harvested += 1
            info_lost += 1
            info_lost_in_a_row += 1
            cause = UnreadableCause.CONTACT_INFO_DEFERRED
            deferred_lines.append(f"visit {visits}: {info.lost}; the profile was saved without it")
            log.info(
                "enrichment: visit %d saved the profile without Contact info: %s",
                visits,
                info.lost,
            )
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
                contact_info_from_copy=info is not None and info.from_copy,
                unreadable_cause=cause,
                contact_info_lost=deferred,
            )
            if details.from_copy:
                # #196 item 12: noted only; the harvest is applied as any other.
                copied.append(f"visit {visits}: a lazy card was read from a streamed copy")
            if info is not None and info.from_copy:
                copied.append(f"visit {visits}: the Contact info was read from a streamed copy")
        else:
            harvest = ProfileHarvest(
                contact_ref=target.contact_ref,
                requested_public_id=slug,
                outcome=outcome,
                observed_at=clock(),
                unreadable_cause=cause,
            )
        if cause is not None:
            unreadable_visits.append(UnreadableVisit(visits, target.contact_ref, cause))
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
        if deferred and (
            info_lost_in_a_row >= MAX_CONTACT_INFO_LOST_IN_A_ROW
            or (clicks >= CONTACT_INFO_LOST_SHARE_AFTER and 2 * info_lost > clicks)
        ):
            # #405: lost overlays this often are the mechanism failing, not one body.
            log.warning(
                "enrichment: %d of %d Contact info answers lost (%d in a row); stopping",
                info_lost,
                clicks,
                info_lost_in_a_row,
            )
            return await stop(StopReason.ANSWER_LOST)

    if planned < len(spec.targets):
        return await stop(StopReason.VISIT_BUDGET)
    return await stop(StopReason.END_OF_PLAN)


def _contact_info_lost(info: Answer[ContactInfo]) -> bool:
    """Whether ``info`` is a Contact info answer whose body was lost (#405): unparsed,
    ``RouteChanged``, with the cause :attr:`UnreadableCause.CONTACT_INFO_LOST` and the
    fixed words of what was lost. A wall the tab moved to is the source's answer
    instead, and stops the run as before."""
    return (
        info.outcome is Outcome.ROUTE_CHANGED
        and info.unparsed
        and info.cause is UnreadableCause.CONTACT_INFO_LOST
        and info.lost is not None
    )
