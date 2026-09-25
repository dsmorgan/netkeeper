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
lazy card that arrives before this profile's screen, or whose request names another member,
is skipped, and the overlay's answer is read
only when the page's own request asked for *this* profile's overlay and the answer's
profile link names it too.

**Where the tab is decides.** After the navigation, the scroll, and the scroll back up,
the tab's url is classified (spec 9.7) with the profile's slug masked out
(:func:`~netkeeper.linkedin.enrich.masked`): a checkpoint or a login wall stops the run as
that; any page that is not a profile, on LinkedIn's origin, is an *unreadable* visit. A
document's HTML is never searched for wall paths (#188 review, M1): every logged-in page
links to ``/uas/logout``. So a wall served in place at the profile's url, which carries no
profile screen, is an unreadable visit too, and two of those in a row stop the run as
``RouteChanged`` without flagging the session.

**What each answer means.** The profile's document answering ``404`` is spec 9.7's
``NotFound`` for the contact. A redirect is judged by where it points: a wall is that
wall, another profile is followed by the tab's own url, anything else is unreadable. A
throttle or a wall on any answer the visit reads stops the run. Any other non-``200``
answer for the profile itself stops the run as ``RouteChanged``; for a lazy card it is
skipped (one card failing is not the profile); for the overlay it makes the visit
unreadable, never ``NotFound`` (nothing in the capture says how LinkedIn answers an
overlay for a missing profile). A ``200`` whose body could not be kept is the observation
failing, not LinkedIn answering: :class:`~netkeeper.linkedin.observe.ObservationFailed`
ends the run.

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
from collections.abc import Awaitable, Callable
from typing import Final, cast
from urllib.parse import quote, urljoin, urlsplit

from netkeeper.linkedin.browser import BrowserRun, BrowserUnavailable, PageLike
from netkeeper.linkedin.classify import Outcome, classify
from netkeeper.linkedin.enrich import LINKEDIN_ORIGIN, Answer, masked
from netkeeper.linkedin.flagship import (
    CONTACT_DETAILS_SCREEN_ID,
    NAVIGATION_PATH,
    rehydration_payload,
)
from netkeeper.linkedin.flagship_profile import (
    COMPONENT_PATH,
    PROFILE_ENDPOINT,
    PROFILE_PAGE_PREFIX,
    PROFILE_SCREEN_PREFIX,
    parse_contact_info,
    parse_navigation_request,
    parse_profile,
    profile_slug,
    same_slug,
)
from netkeeper.linkedin.observe import (
    FAILURE_REDIRECT,
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

_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})
_STOPPING: Final = frozenset({Outcome.CHECKPOINT, Outcome.LOGGED_OUT, Outcome.THROTTLED})


class PageProfiles:
    """The profile visits of one run, from the page's own answers. One instance per run.

    ``origin`` is LinkedIn's, and a loopback origin only for the smoke suite and the
    rehearsal replica: anything else is refused. ``sleep`` waits out the scroll's pauses
    and the pause before the click; the tests pass a fast one.
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
    ) -> None:
        self._run = run
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
        self._components: list[bytes] = []
        self._stopped: Answer[None] | None = None
        self._clicked = False

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
        self._observation = await self._run.observe(match, limits=self._limits)
        page = await self._run.goto(self.profile_url(public_id))
        self._require_observed(page)
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
            details = parse_profile(self._screen, self._components, slug=self._slug)
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
            return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True)
        return await self._read_overlay()

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
                return Answer(Outcome.NOT_FOUND, masked(response.url))
            outcome = _status_outcome(response)
            if outcome is not Outcome.OK:
                return Answer(outcome, masked(response.url))
            assert response.body is not None  # _status_outcome refused a 200 without one
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
            self._screen = payload
            return Answer(Outcome.OK, self._url)
        log.warning("enrichment: %d answers arrived, none the profile screen", answers)
        return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True)

    def _redirect(self, response: ObservedResponse) -> Answer[None] | None:
        """A redirect: a wall is that wall, another profile is followed, else unreadable."""
        target = urljoin(response.url, response.location or "")
        outcome = classify(response.status, masked(target), "")
        if outcome in (Outcome.CHECKPOINT, Outcome.LOGGED_OUT):
            return Answer(outcome, masked(target))
        if self._on_origin(target) and profile_slug(urlsplit(target).path) is not None:
            return None  # a renamed profile: the tab's own url says where it landed
        log.warning("enrichment: the profile redirected somewhere that is not a profile")
        return Answer(Outcome.ROUTE_CHANGED, masked(target), unparsed=True)

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
        if not _names_only(response.request_body, self._slug):
            log.info("enrichment: skipped a lazy card that names another member")
            return None
        if len(self._components) >= MAX_COMPONENTS:
            log.warning("enrichment: more lazy cards than a profile loads; unreadable")
            return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True)
        assert response.body is not None
        self._components.append(response.body)
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
            assert response.body is not None
            try:
                info = parse_contact_info(response.body, slug=self._slug)
            except RouteChanged:
                log.warning("enrichment: the overlay answered in a shape the parser does not know")
                return Answer(Outcome.ROUTE_CHANGED, self._url, unparsed=True)
            return Answer(Outcome.OK, self._url, info)

    # --- the tab ----------------------------------------------------------------------

    async def _scroll(self, plan: ScrollPlan) -> Answer[None] | None:
        if self._sleep is None:
            outcome = await self._run.scroll(plan)
        else:
            outcome = await self._run.scroll(plan, sleep=self._sleep)
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
    return Answer(answer.outcome, answer.final_url, unparsed=answer.unparsed)


def _status_outcome(response: ObservedResponse) -> Outcome:
    """What a non-redirect answer's status says (spec 9.7), never its body (#188 M1).

    A ``200`` whose body could not be kept is the observation failing, not LinkedIn.
    """
    if response.status == 200:
        if response.body is None:
            raise ObservationFailed(f"an answer of the page could not be kept: {response.failure}")
        return Outcome.OK
    return classify(response.status, masked(response.url), "")


def _answer_slug(response: ObservedResponse) -> str | None:
    """The profile a document or screen answer is for, from its own path."""
    path = urlsplit(response.url).path
    if response.method == "GET":
        return profile_slug(path)
    if path.startswith(PROFILE_SCREEN_PREFIX):
        return profile_slug(path[len(PROFILE_SCREEN_PREFIX) - len(PROFILE_PAGE_PREFIX) :])
    return None


def _names_only(request_body: str | None, slug: str) -> bool:
    """Whether a lazy card's request names no member but this profile's.

    The capture did not record these requests' bodies, so a body that names nobody is
    accepted; one whose ``vanityName`` is another slug is not.
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
