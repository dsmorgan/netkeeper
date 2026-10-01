"""Profiles and their contact info, read from what the page loads (ADR 0006, spec 9.4, #190).

:class:`PageProfiles` is the :class:`~netkeeper.linkedin.enrich.ProfileSource` a live
enrichment reads through. For each visit it does what a person does -- opens the profile,
scrolls it, scrolls back to the top, and clicks **Contact info** once -- and reads the
answers the page itself fetched on netkeeper's tab: the profile screen (in the page's own
HTML, or in the screen request an in-app navigation sends), the lazy cards the scroll made
the page load, and the overlay's ``actions/navigation`` answer the click made it load. It
sends no request of its own and intercepts, alters, or answers none. The only input it
gives the page is navigation, :meth:`~netkeeper.linkedin.browser.BrowserRun.scroll`'s
wheel replay, and :meth:`~netkeeper.linkedin.browser.BrowserRun.click_contact_info`'s one
click; the only thing it reads is
:meth:`~netkeeper.linkedin.browser.BrowserRun.observe`'s record of the tab's responses.

**One observation per visit.** Each :meth:`PageProfiles.open_profile` closes the last
visit's observation and starts a new one before it navigates, so an answer the previous
profile's page was still loading is never read as this one's. Within a visit an answer is
also checked for whose it is: a profile document or screen for another slug is skipped, a
lazy card that arrives before this profile's screen, or whose request names another member
by slug or by id, is skipped, and the overlay's answer is read
only when the page's own request asked for *this* profile's overlay and the answer's
profile link names it too.

**Where the tab is decides.** After the navigation, the scroll, the scroll back up, a
click that was refused, and an overlay that did not come or did not read, the tab's url
is classified (spec 9.7) with the profile's slug masked out
(:func:`~netkeeper.linkedin.enrich.masked`): a checkpoint or a login wall stops the run as
that; any page that is not a profile, on LinkedIn's origin, is an *unreadable* visit, and
so is a profile the tab landed on that is neither the one asked for nor one a redirect the
page received led to. A
document's HTML is never searched for wall paths (#188 review, M1): every logged-in page
links to ``/uas/logout``. So a wall served in place at the profile's url, which carries no
profile screen, is an unreadable visit too, and two of those in a row stop the run as
``RouteChanged`` without flagging the session.

**What each answer means.** The profile's document answering ``404`` is spec 9.7's
``NotFound`` for the contact; the in-app screen request answering ``404`` is only an
unreadable visit. A redirect is judged by where it points: a wall is that
wall, another profile is followed by the tab's own url, anything else is unreadable. A
throttle or a wall on any answer the visit reads stops the run. Any other non-``200``
answer for the profile itself stops the run as ``RouteChanged``; for a lazy card it is
skipped (one card failing is not the profile); for the overlay it makes the visit
unreadable, never ``NotFound`` (nothing in the capture says how LinkedIn answers an
overlay for a missing profile). A ``200`` whose body could not be kept because it was
too large or too slow is the observation failing, not LinkedIn answering:
:class:`~netkeeper.linkedin.observe.ObservationFailed` ends the run.

**An answer whose body cannot be read** (#197): the browser received it, and had no
body to hand over. For the profile's screen, the landing keeps waiting in case the page
sends the screen another way; if none reads, the visit is unreadable, with the fixed
cause (:func:`~netkeeper.linkedin.observe.unreadable_cause`) in
:attr:`~netkeeper.linkedin.enrich.Answer.lost`. For the Contact info overlay, the visit
is unreadable -- no contact info for that person this visit, and nothing is clicked
again. A lazy card is skipped, as a card that failed is. Where the tab is still decides
first: a checkpoint or a login wall there stops the run. A profile navigation that times
out (Playwright's ``TimeoutError``: the document broke off, and the page never loaded) is
an unreadable visit the same way, with the cause :data:`NAVIGATION_TIMED_OUT`, after the
same wall check; any other navigation error still ends the run, and a lost tab is still
:class:`~netkeeper.linkedin.browser.BrowserUnavailable`.

**The streamed copy** (#203, ADR 0006's amendment). Each visit observes with the body
tap (:mod:`netkeeper.linkedin.body_tap`), narrowed to the lazy cards and the overlay's
answer: the page's own client can abort those after reading them, and Chrome then has
no body to hand over. A lost lazy card or overlay is read from the copy Chrome streamed
as it arrived only when the copy is whole: the tap ended it as finished or cancelled
(``net::ERR_ABORTED``, which a cancel mid-stream gives too, so it proves nothing on its
own), and it parses as flight with its root, every row the root reaches, and no other
(a copy cut short at a row boundary lacks one). It is then read as strictly as a body.
Any other copy leaves the answer lost, as above. A harvest whose Contact info came from
a copy says so (:attr:`~netkeeper.linkedin.enrich.ProfileHarvest.contact_info_from_copy`).

**The click.** Only after the profile read whole, and only when the job has found its id
to be the contact's (:mod:`netkeeper.linkedin.enrich`). If the control is missing, not
alone, or points at another profile's overlay, nothing is clicked and the visit is
unreadable. If the overlay does not answer within :data:`OVERLAY_WAIT_S`, the visit is
unreadable; nothing is clicked again. The overlay stays open: the next visit's navigation
leaves the page.

Nothing here imports the ORM or opens a session (spec 9.10). This module holds a real tab
across real time, so it is registered in ``tests/test_browser_safety.py`` as a browser
module, never imported from a request handler (spec 9.9).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Final, cast
from urllib.parse import quote, urljoin, urlsplit

from netkeeper.linkedin.browser import (
    BrowserRun,
    BrowserUnavailable,
    PageLike,
    is_navigation_timeout,
)
from netkeeper.linkedin.classify import Outcome, classify
from netkeeper.linkedin.enrich import LINKEDIN_ORIGIN, Answer, masked
from netkeeper.linkedin.flagship import (
    CONTACT_DETAILS_SCREEN_ID,
    NAVIGATION_PATH,
    URN_PREFIX,
    rehydration_payload,
)
from netkeeper.linkedin.flagship_profile import (
    COMPONENT_ENDPOINT,
    COMPONENT_PATH,
    CONTACT_INFO_ENDPOINT,
    PROFILE_ENDPOINT,
    PROFILE_PAGE_PREFIX,
    PROFILE_SCREEN_PREFIX,
    parse_contact_info,
    parse_navigation_request,
    parse_profile,
    parse_profile_urn,
    profile_slug,
    same_slug,
)
from netkeeper.linkedin.flight import is_whole, parse_flight
from netkeeper.linkedin.observe import (
    FAILURE_REDIRECT,
    FAILURE_UNREADABLE,
    Observation,
    ObservationFailed,
    ObservationLimits,
    ObservedResponse,
    ResponseMatch,
    ResponseRule,
)
from netkeeper.linkedin.pacing import ScrollPlan
from netkeeper.linkedin.strict_origin import NotAStrictOrigin, parse_strict_origin
from netkeeper.linkedin.voyager import ContactInfo, ProfileDetails, RouteChanged

log = logging.getLogger(__name__)

#: How long, after the navigation returns, to wait for the profile's screen.
LANDING_WAIT_S: Final = 20.0

#: How long, after the scroll's dwell, to wait for a lazy card the scroll asked for. The
#: dwell itself is seconds; the captured answers took under a second.
LAZY_WAIT_S: Final = 2.0

#: How long to wait for the overlay's answer after the click. The captured one took 0.8 s.
OVERLAY_WAIT_S: Final = 10.0

#: Answers other than lazy cards the landing reads looking for the profile's screen
#: before it gives up: a page that keeps answering without one is not a profile.
MAX_LANDING_ANSWERS: Final = 8

#: Lazy cards one visit keeps. A profile loads about a dozen (#149); past this many the
#: page is not the one the capture showed, and the visit is unreadable.
MAX_COMPONENTS: Final = 40

#: A profile page loads a dozen lazy cards as it renders; the observation must hold them
#: all while the scroll runs without a drop (which would fail the run).
PROFILE_OBSERVATION_LIMITS: Final = ObservationLimits(max_pending=64)

#: The fixed cause of a profile navigation that timed out (#197).
NAVIGATION_TIMED_OUT: Final = "navigation timed out"

_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})
_STOPPING: Final = frozenset({Outcome.CHECKPOINT, Outcome.LOGGED_OUT, Outcome.THROTTLED})


class PageProfiles:
    """The profile visits of one run, from the page's own answers. One instance per run.

    ``origin`` is LinkedIn's, and a loopback origin only for the smoke suite and the
    rehearsal replica: anything else is refused. ``sleep`` waits out the scroll's pauses,
    the pointer's rest before the first scroll (#192), and the pause before the click;
    the tests pass a fast one. ``rng`` shapes that pointer rest.
    """

    def __init__(
        self,
        run: BrowserRun,
        *,
        origin: str = LINKEDIN_ORIGIN,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        landing_wait_s: float = LANDING_WAIT_S,
        lazy_wait_s: float = LAZY_WAIT_S,
        overlay_wait_s: float = OVERLAY_WAIT_S,
        limits: ObservationLimits = PROFILE_OBSERVATION_LIMITS,
        rng: random.Random | None = None,
    ) -> None:
        self._run = run
        # Shapes BrowserRun.scroll's one pointer-rest walk per tab (#192); a caller
        # that passes a seeded one gets a visit that replays from that seed.
        self._rng = rng
        self._origin = _require_origin(origin)
        self._sleep = sleep
        self._landing_wait_s = landing_wait_s
        self._lazy_wait_s = lazy_wait_s
        self._overlay_wait_s = overlay_wait_s
        self._limits = limits
        self._observation: Observation | None = None
        self._reset()

    def _reset(self) -> None:
        self._requested = ""
        self._slug = ""
        self._path = ""
        self._url = ""
        self._screen: bytes | None = None
        self._components: list[tuple[bytes, str | None]] = []
        self._redirects: list[str] = []
        self._stopped: Answer[None] | None = None
        self._clicked = False
        #: The fixed phrase for this visit's profile screen, when one arrived but
        #: its body could not be read (#197); ``None`` otherwise.
        self._lost_screen: str | None = None
        self._page: PageLike | None = None

    @property
    def origin(self) -> str:
        return self._origin

    def profile_url(self, public_id: str) -> str:
        """The profile page a visit opens: the origin and the slug, percent-encoded."""
        return f"{self._origin}{PROFILE_PAGE_PREFIX}{quote(public_id, safe='')}/"

    # --- the seam ------------------------------------------------------------------

    async def open_profile(self, public_id: str) -> Answer[None]:
        await self._end_visit()
        self._reset()
        self._requested = public_id
        match = ResponseMatch(
            origin=self._origin,
            rules=(
                ResponseRule("GET", PROFILE_PAGE_PREFIX, prefix=True),
                ResponseRule("POST", PROFILE_SCREEN_PREFIX, prefix=True),
                ResponseRule("POST", COMPONENT_PATH),
                ResponseRule("POST", NAVIGATION_PATH),
            ),
        )
        # #203: the lazy cards and the overlay's answer are streamed answers the page may
        # abort after reading them; the body tap keeps a read-only copy of those two, and
        # of nothing else the visit observes (ADR 0006's amendment).
        tapped = ResponseMatch(
            origin=self._origin,
            rules=(ResponseRule("POST", COMPONENT_PATH), ResponseRule("POST", NAVIGATION_PATH)),
        )
        self._observation = await self._run.observe(match, limits=self._limits, tap=tapped)
        try:
            page = await self._run.goto(self.profile_url(public_id))
        except Exception as exc:
            if not is_navigation_timeout(exc):
                raise
            return await self._navigation_timed_out()
        self._require_observed(page)
        self._page = page
        blocked = self._land_where(page.url)
        if blocked is not None:
            return blocked
        return await self._read_screen()

    async def scroll(self, plan: ScrollPlan) -> None:
        if self._stopped is not None:
            return
        self._stopped = await self._scroll(plan)

    async def read_profile(self, public_id: str) -> Answer[ProfileDetails]:
        if self._stopped is None:
            self._stopped = await self._absorb(self._lazy_wait_s)
        if self._stopped is not None:
            return _as(self._stopped)
        assert self._screen is not None  # open_profile answered Ok
        try:
            # The member's id first, from the screen alone: a lazy card whose request
            # names another member, by slug or by id, is never read as this one's.
            urn = parse_profile_urn(self._screen, slug=self._slug)
            kept = [
                body for body, request in self._components if _names_only(request, self._slug, urn)
            ]
            if len(kept) < len(self._components):
                log.info(
                    "enrichment: skipped %d lazy card(s) that name another member",
                    len(self._components) - len(kept),
                )
            details = parse_profile(self._screen, kept, slug=self._slug)
        except RouteChanged:
            log.warning("enrichment: the profile answered in a shape the parser does not know")
            return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True)
        return Answer(Outcome.OK, self._url, details)

    async def read_contact_info(
        self, profile: ProfileDetails, *, back: ScrollPlan, pause_s: float
    ) -> Answer[ContactInfo]:
        if self._stopped is not None:
            return _as(self._stopped)
        if self._clicked:
            raise RuntimeError("Contact info was already clicked on this visit")
        if not same_slug(profile.public_id, self._slug):
            # The job hands back the profile this visit read; anything else is a bug.
            raise ValueError("the profile is not the one this visit is on")
        blocked = await self._scroll(back)
        if blocked is not None:
            self._stopped = blocked
            return _as(blocked)
        self._clicked = True
        if self._sleep is None:
            click = await self._run.click_contact_info(self._path, pause_s=pause_s)
        else:
            click = await self._run.click_contact_info(
                self._path, pause_s=pause_s, sleep=self._sleep
            )
        self._require_observed(click.page)
        if not click.clicked:
            log.warning("enrichment: Contact info was not clicked: %s", click.refusal)
            # The tab may have left the profile for a wall during the pause: that is
            # the session's problem, not this profile's, and it stops the run.
            wall = self._wall(click.page.url)
            if wall is not None:
                self._stopped = wall
                return _as(wall)
            return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True)
        info = await self._read_overlay()
        if info.outcome is Outcome.ROUTE_CHANGED and info.unparsed:
            # An overlay that did not come, or did not read, may be a wall the click
            # led to: where the tab is now decides.
            wall = self._wall(click.page.url)
            if wall is not None:
                self._stopped = wall
                return _as(wall)
        return info

    # --- landing -------------------------------------------------------------------

    async def _read_screen(self) -> Answer[None]:
        observation = self._observation
        assert observation is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._landing_wait_s
        answers = 0
        while answers < MAX_LANDING_ANSWERS:
            response = await observation.next(max(deadline - loop.time(), 0.0))
            if response is None:
                if self._lost_screen is not None:
                    return self._screen_lost()
                log.warning("enrichment: the profile page loaded, but no profile screen arrived")
                return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True)
            path = _path(response.url)
            if path in (_path(COMPONENT_PATH), _path(NAVIGATION_PATH)):
                # Before this profile's screen, a lazy card can only be the last page's,
                # still in flight as the tab left it: never kept, but a throttle or a
                # wall on it still stops the run.
                blocked = self._take_other(response, keep=False)
                if blocked is not None:
                    return blocked
                continue
            answers += 1
            slug = _answer_slug(response)
            if response.failure == FAILURE_REDIRECT:
                blocked = self._redirect(response)
                if blocked is not None:
                    return blocked
                continue
            if slug is None or not (
                same_slug(slug, self._slug) or same_slug(slug, self._requested)
            ):
                continue  # another page's answer: not this profile's
            if response.status == 404:
                if response.method == "GET":
                    # Spec 9.7's NotFound: the profile's own document says so.
                    return Answer(Outcome.NOT_FOUND, masked(response.url))
                # The screen request's 404 is not a missing profile by anything the
                # capture showed: unreadable, never NotFound by guess.
                return Answer(Outcome.ROUTE_CHANGED, masked(response.url), unparsed=True)
            outcome = _status_outcome(response)
            if outcome is not Outcome.OK:
                return Answer(outcome, masked(response.url))
            if response.body is None:
                cause = _lost_cause(response)
                # #197: this profile's screen arrived with no body the browser could
                # hand over. The page may still send the screen another way (a
                # document is followed by the screen request); if none reads, the
                # visit is unreadable, never a failed run.
                self._lost_screen = f"the profile screen could not be read ({cause})"
                log.info("enrichment: %s; waiting for the page to send it again", self._lost_screen)
                continue
            if response.method == "GET":
                try:
                    payload = rehydration_payload(response.text() or "", endpoint=PROFILE_ENDPOINT)
                except RouteChanged:
                    return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True)
                if payload is None:
                    # No screen in the document: a page that fetches it next, or a wall
                    # served in place. The HTML is never searched for wall paths (#188
                    # M1); if no screen follows, the visit is unreadable.
                    continue
            else:
                payload = response.body
            if not same_slug(self._slug, self._requested) and not any(
                same_slug(self._slug, target) for target in self._redirects
            ):
                # The tab is on a profile nobody asked for, and no redirect the page
                # received led there: a stale tab, or a page that moved by itself.
                log.warning("enrichment: the tab is on a profile no redirect led to")
                return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True)
            self._screen = payload
            return Answer(Outcome.OK, self._url)
        if self._lost_screen is not None:
            return self._screen_lost()
        log.warning("enrichment: %d answers arrived, none the profile screen", answers)
        return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True)

    async def _navigation_timed_out(self) -> Answer[None]:
        """The profile's navigation never finished loading (#197): an unreadable visit.

        Seen when the document breaks off mid-body: Chrome never fires ``load``. The tab
        is still the one being listened to (``BrowserRun.goto`` raises a lost tab as a
        loss, not a timeout). Before the visit is called unreadable, what LinkedIn
        already said decides (#198 review, H1): where the tab is (a wall stops the run),
        then every answer already queued for this visit, read without waiting and
        classified as the landing classifies it -- a throttle, a wall, or a redirect to
        one stops the run. Only then is it one unreadable visit. It is not tried again.
        """
        observation = self._observation
        assert observation is not None
        page = cast(PageLike, observation.page)
        if page.is_closed():
            raise BrowserUnavailable(
                "the run's tab went away while a profile loaded; aborting the run"
            )
        wall = self._wall(page.url)
        if wall is not None:
            return wall
        if not self._on_origin(page.url):
            # The tab never committed to the profile (#198 review, L3): record the
            # profile asked for, masked, rather than wherever the tab still is.
            self._url = masked(self.profile_url(self._requested))
        tab = profile_slug(urlsplit(page.url).path) if self._on_origin(page.url) else None
        said = await self._queued_answers(tab)
        if said is not None:
            return said
        lost = f"the profile could not be opened ({NAVIGATION_TIMED_OUT})"
        log.info("enrichment: %s", lost)
        return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True, lost=lost)

    async def _queued_answers(self, tab: str | None) -> Answer[None] | None:
        """What the answers already queued for this visit say, read without waiting.

        The same classification as :meth:`_read_screen` (#198 review, H1): a lazy card
        or overlay's throttle or wall stops the run; a redirect is judged by
        :meth:`_redirect`; this profile's document answering ``404`` is ``NotFound``
        (its screen request's, unreadable), and any other status that is not ``200`` is
        that outcome. "This profile" is the slug asked for, any slug this visit's
        redirects led to, and ``tab``, the profile the tab itself is on (#196 item 7):
        a throttle on the renamed profile a redirect led to stops the run too. Answers
        for another profile, and ``200`` answers, say nothing here: the page never
        loaded.
        """
        observation = self._observation
        assert observation is not None
        response = await observation.next(0.0)
        while response is not None:
            path = _path(response.url)
            blocked: Answer[None] | None
            if path in (_path(COMPONENT_PATH), _path(NAVIGATION_PATH)):
                blocked = self._take_other(response, keep=False)
            elif response.failure == FAILURE_REDIRECT:
                blocked = self._redirect(response)
            else:
                blocked = None
                slug = _answer_slug(response)
                if slug is not None and (
                    self._in_chain(slug) or (tab is not None and same_slug(slug, tab))
                ):
                    if response.status == 404:
                        # As the landing reads it: the document's 404 is NotFound,
                        # the screen request's only an unreadable visit.
                        blocked = (
                            Answer(Outcome.NOT_FOUND, masked(response.url))
                            if response.method == "GET"
                            else Answer(Outcome.ROUTE_CHANGED, masked(response.url), unparsed=True)
                        )
                    else:
                        outcome = _status_outcome(response)
                        if outcome is not Outcome.OK:
                            blocked = Answer(outcome, masked(response.url))
            if blocked is not None:
                return blocked
            response = await observation.next(0.0)
        return None

    def _screen_lost(self) -> Answer[None]:
        """The visit is unreadable: its screen's body was lost and none read after it.

        Where the tab is still decides first: a wall it moved to stops the run.
        """
        assert self._lost_screen is not None and self._page is not None
        wall = self._wall(self._page.url)
        if wall is not None:
            return wall
        return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True, lost=self._lost_screen)

    def _redirect(self, response: ObservedResponse) -> Answer[None] | None:
        """A redirect: a wall is that wall, another profile is followed, else unreadable.

        A redirect to another profile is followed only when it came from this visit's
        own chain (#196 item 1): its request is for the slug asked for, or for a target
        an earlier redirect of this visit already led to. A stale redirect from another
        page leads nowhere this visit accepts; a wall on it still stops the run.
        """
        target = urljoin(response.url, response.location or "")
        outcome = classify(response.status, masked(target), "")
        if outcome in (Outcome.CHECKPOINT, Outcome.LOGGED_OUT):
            return Answer(outcome, masked(target))
        renamed = profile_slug(urlsplit(target).path) if self._on_origin(target) else None
        if renamed is not None:
            if self._in_chain(_answer_slug(response)):
                self._redirects.append(renamed)
            else:
                log.info("enrichment: skipped a redirect that another page received")
            return None  # a renamed profile: the tab's own url says where it landed
        log.warning("enrichment: the profile redirected somewhere that is not a profile")
        return Answer(Outcome.ROUTE_CHANGED, masked(target), unparsed=True)

    def _in_chain(self, slug: str | None) -> bool:
        """Whether ``slug`` is the profile asked for, or one this visit's redirects led to."""
        return slug is not None and (
            same_slug(slug, self._requested)
            or any(same_slug(slug, target) for target in self._redirects)
        )

    # --- lazy cards and the overlay --------------------------------------------------

    async def _absorb(self, wait_s: float) -> Answer[None] | None:
        """Read every answer that has arrived, waiting up to ``wait_s`` for the first."""
        observation = self._observation
        assert observation is not None
        response = await observation.next(wait_s)
        while response is not None:
            if _path(response.url) in (_path(COMPONENT_PATH), _path(NAVIGATION_PATH)):
                blocked = self._take_other(response)
                if blocked is not None:
                    return blocked
            response = await observation.next(0.0)
        return None

    def _take_other(self, response: ObservedResponse, *, keep: bool = True) -> Answer[None] | None:
        """A lazy card, kept when it is this profile's; an overlay nobody clicked for, skipped.

        A throttle or a wall on either stops the run all the same. With ``keep`` false
        (before the profile's screen arrived) a card is never kept.
        """
        if response.failure == FAILURE_REDIRECT:
            target = urljoin(response.url, response.location or "")
            outcome = classify(response.status, masked(target), "")
            if outcome in (Outcome.CHECKPOINT, Outcome.LOGGED_OUT):
                return Answer(outcome, masked(target))
            return None
        outcome = _status_outcome(response)
        if outcome in _STOPPING:
            return Answer(outcome, masked(response.url))
        if not keep or _path(response.url) != _path(COMPONENT_PATH) or outcome is not Outcome.OK:
            return None
        body = response.body
        if body is None:
            # #197: a lazy card whose body the browser could not hand over is skipped
            # like a card that failed: one card is not the profile. #203: unless the
            # body tap's streamed copy of it is whole.
            cause = _lost_cause(response)
            body = _whole_copy(response, endpoint=COMPONENT_ENDPOINT)
            if body is None:
                log.info("enrichment: skipped a lazy card that could not be read (%s)", cause)
                return None
            log.info(
                "enrichment: read a lazy card from the copy streamed as it arrived (%d bytes)",
                len(body),
            )
        if not _names_only(response.request_body, self._slug, None):
            log.info("enrichment: skipped a lazy card that names another member")
            return None
        if len(self._components) >= MAX_COMPONENTS:
            log.warning("enrichment: more lazy cards than a profile loads; unreadable")
            return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True)
        self._components.append((body, response.request_body))
        return None

    async def _read_overlay(self) -> Answer[ContactInfo]:
        observation = self._observation
        assert observation is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._overlay_wait_s
        while True:
            response = await observation.next(max(deadline - loop.time(), 0.0))
            if response is None:
                log.warning("enrichment: the Contact info overlay never answered")
                return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True)
            if _path(response.url) != _path(NAVIGATION_PATH):
                blocked = (
                    self._take_other(response)
                    if _path(response.url) == _path(COMPONENT_PATH)
                    else None
                )
                if blocked is not None:
                    return _as(blocked)
                continue
            request = parse_navigation_request(response.request_body)
            if request.screen_id != CONTACT_DETAILS_SCREEN_ID:
                continue  # another navigation the page made: not the overlay
            if request.vanity_name is None or not same_slug(request.vanity_name, self._slug):
                log.warning("enrichment: the page asked for another profile's overlay")
                return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True)
            if response.failure == FAILURE_REDIRECT:
                target = urljoin(response.url, response.location or "")
                outcome = classify(response.status, masked(target), "")
                if outcome in (Outcome.CHECKPOINT, Outcome.LOGGED_OUT):
                    return Answer(outcome, masked(target))
                return Answer(Outcome.ROUTE_CHANGED, masked(target), unparsed=True)
            outcome = _status_outcome(response)
            if outcome in _STOPPING:
                return Answer(outcome, masked(response.url))
            if outcome is not Outcome.OK:
                # Never NotFound by guess: nothing captured says how a missing
                # profile's overlay answers.
                return Answer(Outcome.ROUTE_CHANGED, masked(response.url), unparsed=True)
            body = response.body
            if body is None:
                # #197: the overlay answered, but its body could not be handed over.
                # No contact info for this person on this visit, and no second click:
                # the visit is unreadable (read_contact_info checks the tab for a wall).
                # #203: unless the body tap's streamed copy of it is whole.
                lost = f"the Contact info answer could not be read ({_lost_cause(response)})"
                body = _whole_copy(response, endpoint=CONTACT_INFO_ENDPOINT)
                if body is None:
                    log.info("enrichment: %s; not clicking again", lost)
                    return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True, lost=lost)
                log.info(
                    "enrichment: read the Contact info answer from the copy streamed as it"
                    " arrived (%d bytes)",
                    len(body),
                )
            try:
                info = parse_contact_info(body, slug=self._slug)
            except RouteChanged:
                log.warning("enrichment: the overlay answered in a shape the parser does not know")
                return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True)
            return Answer(Outcome.OK, self._url, info, from_copy=response.body is None)

    # --- the tab ----------------------------------------------------------------------

    async def _scroll(self, plan: ScrollPlan) -> Answer[None] | None:
        if self._sleep is None:
            outcome = await self._run.scroll(plan, rng=self._rng)
        else:
            outcome = await self._run.scroll(plan, sleep=self._sleep, rng=self._rng)
        self._require_observed(outcome.page)
        return self._still_here(outcome.page.url)

    def _land_where(self, url: str) -> Answer[None] | None:
        """Classify where the navigation left the tab, and remember which profile it is."""
        blocked = self._wall(url)
        if blocked is not None:
            return blocked
        slug = profile_slug(urlsplit(url).path) if self._on_origin(url) else None
        if slug is None:
            log.warning("enrichment: the tab landed somewhere that is not a profile")
            return Answer(Outcome.ROUTE_CHANGED, masked(url), unparsed=True)
        self._slug = slug
        self._path = urlsplit(url).path
        return None

    def _still_here(self, url: str) -> Answer[None] | None:
        """The tab after a scroll: still on the same profile, or the visit stops there."""
        blocked = self._wall(url)
        if blocked is not None:
            return blocked
        slug = profile_slug(urlsplit(url).path) if self._on_origin(url) else None
        if slug is None or not same_slug(slug, self._slug):
            log.warning("enrichment: the tab left the profile during the visit")
            return Answer(Outcome.ROUTE_CHANGED, masked(url), unparsed=True)
        return None

    def _wall(self, url: str) -> Answer[None] | None:
        self._url = masked(url)
        outcome = classify(200, self._url, "")
        if outcome in (Outcome.CHECKPOINT, Outcome.LOGGED_OUT):
            return Answer(outcome, self._url)
        return None

    def _on_origin(self, url: str) -> bool:
        try:
            split, want = urlsplit(url), urlsplit(self._origin)
            return (split.scheme, split.hostname, split.port) == (
                want.scheme,
                want.hostname,
                want.port,
            )
        except ValueError:
            return False

    def _require_observed(self, page: PageLike) -> None:
        """The tab a navigation, scroll, or click used must be the one being listened to."""
        observation = self._observation
        assert observation is not None
        if page is not cast(object, observation.page):
            raise BrowserUnavailable(
                "the run's tab was replaced mid-visit, so its answers are no longer"
                " observed; aborting the run rather than read part of a profile"
            )

    async def _end_visit(self) -> None:
        observation, self._observation = self._observation, None
        if observation is not None:
            await observation.close()


def _as[T](answer: Answer[None]) -> Answer[T]:
    """A visit's stop, as the answer to whichever step asked."""
    return Answer(answer.outcome, answer.final_url, unparsed=answer.unparsed, lost=answer.lost)


def _status_outcome(response: ObservedResponse) -> Outcome:
    """What a non-redirect answer's status says (spec 9.7), never its body (#188 M1).

    A ``200`` is ``Ok`` whether or not its body could be kept: the caller decides
    what a missing body means for the answer it is reading (#197), through
    :func:`_lost_cause`.
    """
    if response.status == 200:
        return Outcome.OK
    return classify(response.status, masked(response.url), "")


def _lost_cause(response: ObservedResponse) -> str:
    """The fixed cause of a ``200`` answer without a body, when the body was lost (#197).

    Only a body the browser could not hand over is a lost answer. One that could not
    be kept for any other reason (too large, too slow) is still the observation
    failing, and ends the run by :class:`~netkeeper.linkedin.observe.ObservationFailed`.
    """
    if response.failure != FAILURE_UNREADABLE:
        raise ObservationFailed(f"an answer of the page could not be kept: {response.failure}")
    return response.cause or "unknown"


def _whole_copy(response: ObservedResponse, *, endpoint: str) -> bytes | None:
    """The body tap's streamed copy of a lost answer, when it is whole (#203), else ``None``.

    The tap hands over only a copy whose stream finished, or was cancelled with
    ``net::ERR_ABORTED`` (#202), within the body limit. That cancel does not prove the
    copy is whole: the page's own client cancelling after it read everything gives it,
    and so do a cancel mid-stream and a navigation. So the copy must also parse as
    flight and be whole by its own structure (:func:`~netkeeper.linkedin.flight.is_whole`:
    its root, and every row the root reaches, and no row it does not). The caller then
    reads it as strictly as a body.
    """
    streamed = response.streamed
    if streamed is None:
        return None
    try:
        payload = parse_flight(streamed, endpoint=endpoint)
        whole = is_whole(payload, endpoint=endpoint)
    except RouteChanged:
        whole = False
    if not whole:
        log.info("enrichment: the streamed copy of a lost answer is not whole; not used")
        return None
    return streamed


def _answer_slug(response: ObservedResponse) -> str | None:
    """The profile a document or screen answer is for, from its own path."""
    path = urlsplit(response.url).path
    if response.method == "GET":
        return profile_slug(path)
    if path.startswith(PROFILE_SCREEN_PREFIX):
        return profile_slug(path[len(PROFILE_SCREEN_PREFIX) - len(PROFILE_PAGE_PREFIX) :])
    return None


def _names_only(request_body: str | None, slug: str, urn: str | None) -> bool:
    """Whether a lazy card's request names no member but this profile's.

    The capture did not record these requests' bodies, so a body that names nobody is
    accepted; one whose ``vanityName`` is another slug is not, and neither is one whose
    ``profileUrn`` or ``vieweeProfileId`` is another member's id than ``urn`` (when the
    profile's id is known). Positions are upserted and never removed, so a card read
    as the wrong person's could never be taken back.
    """
    try:
        request = json.loads(request_body) if request_body else None
    except (ValueError, RecursionError):
        return True
    stack: list[object] = [request]
    seen = 0
    while stack and seen < 10_000:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            vanity = node.get("vanityName")
            if isinstance(vanity, str) and not same_slug(vanity, slug):
                return False
            if urn is not None:
                named = node.get("profileUrn")
                if isinstance(named, str) and named != urn:
                    return False
                viewee = node.get("vieweeProfileId")
                if isinstance(viewee, str) and f"{URN_PREFIX}{viewee}" != urn:
                    return False
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return True


def _path(url: str) -> str:
    path = urlsplit(url).path if "://" in url else url
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


def _require_origin(origin: str) -> str:
    """LinkedIn's origin, or this machine's loopback for tests and rehearsals. Nothing else."""
    try:
        parsed = parse_strict_origin(origin)
    except NotAStrictOrigin as exc:
        raise ValueError(f"{origin!r} is not an origin this source may read") from exc
    if str(parsed) == LINKEDIN_ORIGIN:
        return LINKEDIN_ORIGIN
    if parsed.scheme in ("http", "https") and parsed.host in _LOOPBACK_HOSTS:
        return str(parsed)
    raise ValueError(
        f"profiles are read from {LINKEDIN_ORIGIN!r}, or this machine's own loopback for"
        f" tests, never {origin!r}"
    )
