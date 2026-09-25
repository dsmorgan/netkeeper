"""The connections list, read from what the page loads (ADR 0006, spec 9.3, 9.4).

:class:`PageConnections` is the :class:`~netkeeper.linkedin.connections.ConnectionsSource`
a live sync reads through. It does what a person does -- opens the connections page and
scrolls it -- and reads the answers the page itself fetched on netkeeper's tab: the first
screen (carried in the page's own HTML, or in the screen request an in-app navigation
sends) and each ``pagination`` answer the scroll made the page ask for. It sends no
request of its own, intercepts none, and changes none: the only input it gives the page
is :meth:`~netkeeper.linkedin.browser.BrowserRun.scroll`'s mouse-wheel replay, and the
only thing it reads is :meth:`~netkeeper.linkedin.browser.BrowserRun.observe`'s record of
responses the browser already received.

**One budgeted unit, as before.** :func:`~netkeeper.linkedin.connections.run_connections_sync`
calls :meth:`PageConnections.fetch_page` once per unit, after the gate has spent one
``connection_pages`` unit (spec 9.6). One call returns ``count`` connections (40 by
default, the spec's page) from the list read so far, scrolling for more only while it
has fewer: about four of the page's own ten-card answers. So the budget still counts
roughly forty connections per unit, as spec 9.6's table assumes, and one unit never
triggers more than the answers those forty need plus :data:`MAX_IDLE_SCROLLS` scrolls
that brought nothing.

**What ends the list.** Only the page's own answers do
(:attr:`~netkeeper.linkedin.flagship.ConnectionsChunk.ends_list`): a connections
answer with no cards, or a short answer that asks for no next page -- and a full
answer that asks for none once the run has seen as many distinct people as the first
screen's total (a list whose length is a multiple of ten). A page that simply stops
loading more is not an end: after :data:`MAX_IDLE_SCROLLS` scrolls that
brought nothing, the call answers ``RouteChanged``, so the run stops and ages nobody.
A short slice is returned only once the end is proven, which is what lets the job's
short-page rule stand.

**What makes a run complete.** The first screen states the total connection count, and
every slice reports it as the page's ``total``.
:attr:`~netkeeper.linkedin.connections.SyncResult.complete` then holds a full sync to
the bar it always has: the end of the list proven, and at least as many distinct URNs
seen as the total. A first screen without a total reports
0, which completes nothing. The answers must also arrive in order: each pagination
answer's ``startIndex`` has to be the one the previous answer asked for. A retried
request for a page already read is skipped; a start past it means a page went unseen,
and the call answers ``RouteChanged``.

**Classified before it is read** (spec 9.7). Where a navigation or a scroll left the tab
is classified first: a checkpoint or a login wall stops the run as that, and a tab that
has left the connections page answers ``RouteChanged``. A pagination answer that is not
``200`` is classified from its status and url, a redirect from where it pointed. No
HTML is ever searched for wall paths (#188 review, M1): every logged-in page links to
``/uas/logout`` and ``/login``, so a document without a first screen is judged by the
tab's url alone, and a wall served in place, which that cannot see, stops the run as
``RouteChanged`` -- no session flag, no heat. The first non-``Ok`` answer is sticky:
every later call returns it, except that a unit already read whole from answers that
arrived before it is still handed over. A unit is never handed over part-read.
:class:`~netkeeper.linkedin.observe.ObservationFailed` -- the mechanism failing, not
LinkedIn answering -- is handled the same way, except that the run ends by that
exception rather than an outcome: recorded failed, never complete.

**An answer whose body cannot be read** (#197). Chrome can receive a pagination
answer and still have no body to hand over: the page's own client aborted or
superseded the fetch, or the body was evicted. The answer's start is compared with
the expected one first, so a stale or repeated answer without a body is skipped like
any other. One for the expected start is logged at INFO, with the exception's class
and a fixed category but never its message, and the scroll goes on so the page can
ask again: if it does and the answer reads, the run goes on. If the page moves past
that start instead, or it is not read again within :data:`MAX_IDLE_SCROLLS` scrolls,
the call raises :class:`~netkeeper.linkedin.connections.AnswerLost`, which ends the
run as a safe incomplete stop (:attr:`~netkeeper.linkedin.connections.StopReason.ANSWER_LOST`):
not ``RouteChanged``, so it never counts toward the route-changed breaker, and never
complete, so it ages nobody. A body that could not be kept for any other reason (too
large, too slow) is still :class:`~netkeeper.linkedin.observe.ObservationFailed`.

**Cancel** lands between units, at the gate's next check (spec 9.9): one call spends a
few scrolls at most, and stopping partway through one would hand the job a short slice
it could mistake for the end of the list.

Nothing here imports the ORM or opens a session (spec 9.10). This module holds a real tab
across real time, so it is registered in ``tests/test_browser_safety.py`` as a browser
module, never imported from a request handler (spec 9.9).
"""

from __future__ import annotations

import logging
import random
from collections.abc import Awaitable, Callable
from typing import Final, cast
from urllib.parse import urlsplit

from netkeeper.linkedin.browser import BrowserRun, BrowserUnavailable, PageLike
from netkeeper.linkedin.classify import Outcome, classify
from netkeeper.linkedin.connections import AnswerLost, LostAnswer, SourcePage
from netkeeper.linkedin.flagship import (
    CONNECTIONS_PAGE_PATH,
    CONNECTIONS_SCREEN_PATH,
    LINKEDIN_ORIGIN,
    PAGINATION_PATH,
    SORT_NEWEST_FIRST,
    ConnectionsChunk,
    parse_connections_chunk,
    parse_pagination_request,
    rehydration_payload,
)
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
from netkeeper.linkedin.pacing import (
    DEFAULT_SCROLL_PROFILE,
    ScrollProfile,
    scroll_like_a_person,
)
from netkeeper.linkedin.strict_origin import NotAStrictOrigin, parse_strict_origin
from netkeeper.linkedin.voyager import ConnectionsPageResult, ConnectionSummary, RouteChanged

log = logging.getLogger(__name__)

#: What :class:`PageConnections` calls itself in logs and ``RouteChanged``.
PAGE_CONNECTIONS_ENDPOINT: Final = "flagship-web/connections-list"

#: How long, after a scroll's dwell, to wait for the answer it made the page ask for.
#: The captured answers took a quarter to half a second.
RESPONSE_WAIT_S: Final = 5.0

#: How long to wait for the first screen after the navigation returns.
LANDING_WAIT_S: Final = 20.0

#: Scrolls in one call that bring no new answer before the call gives up with
#: ``RouteChanged``: the page stopped loading without proving the list's end.
MAX_IDLE_SCROLLS: Final = 6

#: Answers the landing reads looking for the first screen before it gives up: a page
#: that keeps answering with documents that carry none is not the connections page.
MAX_LANDING_ANSWERS: Final = 8

_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})


class PageConnections:
    """The connections list from the page's own answers. One instance per run.

    ``origin`` is LinkedIn's, and a loopback origin only for the smoke suite's
    replica: anything else is refused. ``require_newest_first`` refuses a list the
    page sorts any other way than by recently added -- an incremental sync, which
    stops at the first page of connections it knows, is only right newest first.
    ``rng``, ``scroll_profile``, and ``sleep`` shape the scroll exactly as they do for
    :meth:`~netkeeper.linkedin.browser.BrowserRun.scroll`; the tests pass fast ones.
    """

    def __init__(
        self,
        run: BrowserRun,
        *,
        origin: str = LINKEDIN_ORIGIN,
        require_newest_first: bool = False,
        rng: random.Random | None = None,
        scroll_profile: ScrollProfile = DEFAULT_SCROLL_PROFILE,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        response_wait_s: float = RESPONSE_WAIT_S,
        landing_wait_s: float = LANDING_WAIT_S,
        max_idle_scrolls: int = MAX_IDLE_SCROLLS,
        limits: ObservationLimits | None = None,
    ) -> None:
        self._run = run
        self._origin = _require_origin(origin)
        self._require_newest_first = require_newest_first
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 -- pacing, not crypto
        self._scroll_profile = scroll_profile
        self._sleep = sleep
        self._response_wait_s = response_wait_s
        self._landing_wait_s = landing_wait_s
        self._max_idle = max_idle_scrolls
        self._limits = limits
        self._observation: Observation | None = None
        self._cards: list[ConnectionSummary] = []
        self._next_start: int | None = None
        self._total = 0
        self._landed = False
        self._ended = False
        self._stopped: SourcePage | None = None
        self._failed: ObservationFailed | AnswerLost | None = None
        #: The start of a pagination answer that arrived unreadable and has not been
        #: read since (#197), with its fixed cause; ``None`` while nothing is lost.
        self._lost: tuple[int, str] | None = None
        self._url = ""

    @property
    def endpoint(self) -> str:
        return PAGE_CONNECTIONS_ENDPOINT

    @property
    def page_url(self) -> str:
        """The connections page this source opens."""
        return f"{self._origin}{CONNECTIONS_PAGE_PATH}"

    async def fetch_page(self, *, start: int, count: int) -> SourcePage:
        if self._failed is not None and len(self._cards) < start + count:
            raise self._failed
        if self._stopped is None and self._failed is None:
            try:
                blocked = await self._read_until(start + count)
            except RouteChanged:
                blocked = SourcePage(Outcome.ROUTE_CHANGED, self._url)
            except (ObservationFailed, AnswerLost) as exc:
                # The mechanism failed, or an answer was lost (#197), not LinkedIn
                # answering: there is no outcome to report, so the run ends by this
                # exception (never complete, ageing nobody). A unit already read whole
                # before it is still handed over, the same as before a RouteChanged;
                # the next call raises it.
                self._failed = exc
                blocked = None
                if len(self._cards) < start + count:
                    raise
            if blocked is not None:
                self._stopped = blocked
        if self._stopped is not None and len(self._cards) < start + count:
            # The first non-Ok answer is sticky, and only a unit this source had already
            # read whole, before it, is still handed over: a stop never becomes a short
            # slice the job could mistake for the end of the list.
            return self._stopped
        selected = tuple(self._cards[start : start + count])
        return SourcePage(
            outcome=Outcome.OK,
            final_url=self._url,
            page=ConnectionsPageResult(
                connections=selected, start=start, count=count, total=self._total
            ),
        )

    async def _read_until(self, needed: int) -> SourcePage | None:
        """Land, then scroll until ``needed`` cards are read or the list's end is proven.

        Returns the first answer that is not ``Ok``, or ``None``. Cards read from whole
        answers before it are kept: an answer that arrived after the unit was already
        full does not take the unit back, it stops the next one.
        """
        if not self._landed:
            blocked = await self._land()
            if blocked is not None:
                return blocked
        blocked = await self._absorb(wait_s=0.0)
        if blocked is not None:
            return blocked
        idle = 0
        while len(self._cards) < needed and not self._ended:
            if idle >= self._max_idle and self._lost is not None:
                raise self._answer_lost(f"it was not read again within {idle} scrolls")
            if idle >= self._max_idle:
                log.warning(
                    "connections: %d scrolls brought no new answer before the list"
                    " proved its end; stopping without trusting it",
                    idle,
                )
                return SourcePage(Outcome.ROUTE_CHANGED, self._url)
            before = (len(self._cards), self._ended)
            lost_before = self._lost
            blocked = await self._scroll()
            if blocked is None:
                blocked = await self._absorb(wait_s=self._response_wait_s)
            if blocked is not None:
                return blocked
            if lost_before is None and self._lost is not None:
                # An answer was just lost (#197): the page gets MAX_IDLE_SCROLLS
                # scrolls, from here, to ask for it again.
                idle = 0
            elif (len(self._cards), self._ended) == before:
                idle += 1
        return None

    # --- landing: the navigation and the first screen -----------------------------

    async def _land(self) -> SourcePage | None:
        self._landed = True
        match = ResponseMatch(
            origin=self._origin,
            rules=(
                ResponseRule("GET", CONNECTIONS_PAGE_PATH),
                ResponseRule("POST", CONNECTIONS_SCREEN_PATH),
                ResponseRule("POST", PAGINATION_PATH),
            ),
        )
        self._observation = await self._run.observe(match, limits=self._limits)
        page = await self._run.goto(self.page_url)
        self._require_observed(page)
        blocked = self._where(page.url)
        if blocked is not None:
            return blocked
        observation = self._observation
        for _answer in range(MAX_LANDING_ANSWERS):
            response = await observation.next(self._landing_wait_s)
            if response is None:
                log.warning("connections: the page loaded, but no first screen arrived")
                return SourcePage(Outcome.ROUTE_CHANGED, self._url)
            if _path(response.url) == _path(PAGINATION_PATH):
                log.warning("connections: a page of the list arrived before its first screen")
                return SourcePage(Outcome.ROUTE_CHANGED, self._url)
            outcome = _outcome(response)
            if outcome is not Outcome.OK:
                return SourcePage(outcome, response.location or response.url)
            if response.body is None:
                raise _not_kept(response)
            payload: bytes | None
            if response.method == "GET":
                payload = rehydration_payload(response.text() or "")
                if payload is None:
                    # No first screen in the document: a page that fetches its screen
                    # next, or a wall. Where the tab is decides which, by its url alone
                    # (#188 review, M1): the HTML itself is never searched for wall
                    # paths, because every logged-in page links to /uas/logout, and
                    # reading that as a login wall would flag the session and end the
                    # run with nothing. A wall served in place at the connections url
                    # stays unrecognized: no first screen ever comes, and the run stops
                    # as RouteChanged, ageing nobody and flagging nothing.
                    blocked = self._where(page.url)
                    if blocked is not None:
                        return blocked
                    continue
            else:
                payload = response.body
            assert payload is not None  # a response without a body was refused above
            chunk = parse_connections_chunk(payload, endpoint=self.endpoint, expected_start=0)
            if not chunk.cards and chunk.total != 0:
                # A first screen with no cards is an empty list only when it says the
                # list is empty; otherwise the cards come some other way now.
                log.warning("connections: the first screen has no cards and no zero total")
                return SourcePage(Outcome.ROUTE_CHANGED, self._url)
            self._total = chunk.total or 0
            self._take(chunk)
            if self._total == 0:
                log.warning(
                    "connections: the first screen states no total; this run cannot complete"
                )
            return None
        log.warning("connections: %d answers arrived, none the first screen", MAX_LANDING_ANSWERS)
        return SourcePage(Outcome.ROUTE_CHANGED, self._url)

    # --- the scroll, and the answers it brought ------------------------------------

    async def _scroll(self) -> SourcePage | None:
        plan = scroll_like_a_person(
            self._rng,
            steps_range=self._scroll_profile.steps_range,
            delta_range_px=self._scroll_profile.delta_range_px,
            pause_range_s=self._scroll_profile.pause_range_s,
            back_up_p=self._scroll_profile.back_up_p,
            back_up_delta_range_px=self._scroll_profile.back_up_delta_range_px,
            dwell_median_s=self._scroll_profile.dwell_median_s,
            dwell_sigma=self._scroll_profile.dwell_sigma,
        )
        # `rng` shares this source's own stream with the scroll plan above, on
        # purpose: the pointer-rest walk (#192) is spent from the same seed a
        # caller gives this source, so the whole read -- not just the wheel
        # deltas and dwells -- reproduces identically for a fixed seed (the same
        # promise :mod:`netkeeper.linkedin.pacing` makes for everything else it
        # draws). Sharing costs nothing correctness-wise: it only decides which
        # otherwise-equally-valid `ScrollPlan` each later scroll draws, since the
        # rest walk's few extra draws land before them once, on the first call.
        # `tests/test_page_connections.py`'s incremental-stop test pins the bound
        # this shifted rather than the exact one a particular seed happened to
        # land on before, which is what a test asserting the real invariant
        # (stopped well short of the list's actual end) should have done from
        # the start.
        if self._sleep is None:
            outcome = await self._run.scroll(plan, rng=self._rng)
        else:
            outcome = await self._run.scroll(plan, sleep=self._sleep, rng=self._rng)
        self._require_observed(outcome.page)
        return self._where(outcome.page.url)

    async def _absorb(self, *, wait_s: float) -> SourcePage | None:
        """Read every answer that has arrived, waiting up to ``wait_s`` for the first."""
        observation = self._observation
        assert observation is not None
        response = await observation.next(wait_s)
        while response is not None:
            try:
                blocked = self._take_answer(response)
            except RouteChanged:
                blocked = SourcePage(Outcome.ROUTE_CHANGED, self._url)
            if blocked is not None:
                return blocked
            response = await observation.next(0.0)
        return None

    def _take_answer(self, response: ObservedResponse) -> SourcePage | None:
        if _path(response.url) != _path(PAGINATION_PATH):
            # A second first screen (the client refetching it) says nothing new.
            return None
        request = parse_pagination_request(response.request_body)
        if request is None:
            return None  # another pager on the same path: not the connections list
        # A status or a redirect is classified whatever start it answered: a
        # checkpoint on a duplicate is still a checkpoint. Only a 200 whose body
        # could not be kept waits for the start comparison below (#197).
        outcome = _outcome(response)
        if outcome is not Outcome.OK:
            return SourcePage(outcome, response.location or response.url)
        if self._require_newest_first and request.sort != SORT_NEWEST_FIRST:
            log.warning("connections: the list is not sorted by recently added; stopping")
            return SourcePage(Outcome.ROUTE_CHANGED, self._url)
        if self._ended:
            return None
        expected = self._next_start
        assert expected is not None  # set by every chunk taken before this one
        if request.start_index < expected:
            # Stale or retried, with or without a body: already read, never fatal.
            log.info("connections: the page asked for a page it already had; skipped")
            return None
        if request.start_index > expected:
            if self._lost is not None:
                # The page moved on past the answer it lost (#197): not a changed
                # route, a page this run could not read.
                raise self._answer_lost("the page moved past it")
            log.warning(
                "connections: a page of the list went unseen (asked from %d, expected %d)",
                request.start_index,
                expected,
            )
            return SourcePage(Outcome.ROUTE_CHANGED, self._url)
        if response.body is None:
            if response.failure != FAILURE_UNREADABLE:
                raise _not_kept(response)
            # #197: the browser received the answer but could not hand its body
            # over -- the page's own client aborted or superseded the fetch, or the
            # body was evicted. Not LinkedIn answering: keep scrolling so the page
            # can ask for it again, and give up safely if it does not.
            cause = response.cause or "unknown"
            self._lost = (request.start_index, cause)
            log.info(
                "connections: the page's answer for start %d could not be read (%s);"
                " scrolling on for the page to ask again",
                request.start_index,
                cause,
            )
            return None
        if self._lost is not None:
            log.info(
                "connections: the page asked again for start %d, and it read",
                request.start_index,
            )
            self._lost = None
        self._take(
            parse_connections_chunk(
                response.body, endpoint=self.endpoint, expected_start=request.start_index
            )
        )
        return None

    def _take(self, chunk: ConnectionsChunk) -> None:
        self._cards.extend(chunk.cards)
        if chunk.ends_list:
            self._ended = True
        elif chunk.next_start is None and 0 < self._total <= self._distinct():
            # A full answer that asks for no next page is the end of a list whose length
            # is a multiple of ten -- but only once the run has seen as many distinct
            # people as the first screen's total. Short of that it proves nothing, and
            # the page stopping there still ends the run as RouteChanged.
            self._ended = True
        self._next_start = (
            chunk.next_start if chunk.next_start is not None else chunk.start + len(chunk.cards)
        )
        log.info(
            "connections: read %d at %d%s",
            len(chunk.cards),
            chunk.start,
            ", the end of the list" if self._ended else "",
        )

    def _answer_lost(self, ending: str) -> AnswerLost:
        """The lost answer as the exception that ends the run incomplete (#197)."""
        assert self._lost is not None
        start, cause = self._lost
        return AnswerLost(LostAnswer(start=start, cause=cause, ending=ending))

    def _distinct(self) -> int:
        return len({card.urn for card in self._cards})

    # --- where the tab is -----------------------------------------------------------

    def _require_observed(self, page: PageLike) -> None:
        """The tab a navigation or scroll used must be the one being listened to."""
        observation = self._observation
        assert observation is not None
        if page is not cast(object, observation.page):
            raise BrowserUnavailable(
                "the run's tab was replaced mid-read, so its answers are no longer"
                " observed; aborting the run rather than read part of the list"
            )

    def _where(self, url: str) -> SourcePage | None:
        """Classify where the tab is (spec 9.7): a wall, somewhere else, or the list."""
        self._url = url
        outcome = classify(200, url, "")
        if outcome in (Outcome.CHECKPOINT, Outcome.LOGGED_OUT):
            return SourcePage(outcome, url)
        split = urlsplit(url)
        want = urlsplit(self._origin)
        on_page = (split.scheme, split.hostname, split.port) == (
            want.scheme,
            want.hostname,
            want.port,
        ) and _path(split.path) == _path(CONNECTIONS_PAGE_PATH)
        if not on_page:
            log.warning("connections: the tab left the connections page; stopping")
            return SourcePage(Outcome.ROUTE_CHANGED, url)
        return None


def _outcome(response: ObservedResponse) -> Outcome:
    """What one observed answer means (spec 9.7), before anything parses it.

    A redirect is classified by where it pointed. A ``200`` is ``Ok``, whether or not
    its body could be kept: which answer it was decides what a missing body means
    (a duplicate's is skipped, a lost one's is waited out, #197), so the caller
    checks the body after that, never here. Any other status is
    classified from its status and url, never its body (#188 review, M1): an error
    page is HTML with the site's own navigation in it, and its Sign-out link to
    ``/uas/logout`` is not a login wall.
    """
    if response.failure == FAILURE_REDIRECT:
        target = response.location or ""
        outcome = classify(response.status, target, "")
        return (
            outcome
            if outcome in (Outcome.CHECKPOINT, Outcome.LOGGED_OUT)
            else (Outcome.ROUTE_CHANGED)
        )
    if response.status == 200:
        return Outcome.OK
    return classify(response.status, response.url, "")


def _not_kept(response: ObservedResponse) -> ObservationFailed:
    """A ``200`` answer the run needed whose body the observation could not keep."""
    return ObservationFailed(f"an answer of the page could not be kept: {response.failure}")


def _path(url: str) -> str:
    path = urlsplit(url).path if "://" in url else url
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


def _require_origin(origin: str) -> str:
    """LinkedIn's origin, or this machine's loopback for the smoke suite. Nothing else."""
    try:
        parsed = parse_strict_origin(origin)
    except NotAStrictOrigin as exc:
        raise ValueError(f"{origin!r} is not an origin this source may read") from exc
    if str(parsed) == LINKEDIN_ORIGIN:
        return LINKEDIN_ORIGIN
    if parsed.scheme in ("http", "https") and parsed.host in _LOOPBACK_HOSTS:
        return str(parsed)
    raise ValueError(
        f"the connections list is read from {LINKEDIN_ORIGIN!r}, or this machine's own"
        f" loopback for tests, never {origin!r}"
    )
