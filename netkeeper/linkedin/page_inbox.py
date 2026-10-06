"""The LinkedIn inbox, read from what the messaging page loads (P4-01, #380, ADR 0006).

:class:`PageInbox` is the :class:`~netkeeper.linkedin.inbox.InboxSource` a live inbox poll
reads through. It does what a person does -- opens ``/messaging/``, scrolls the list for
older conversations, and opens a thread by going to its address -- and reads only the
answers the page itself fetched on netkeeper's tab
(:mod:`netkeeper.linkedin.messaging_shapes` parses them). It sends no request of its
own, intercepts none, and changes none. The only input it gives the page is
:meth:`~netkeeper.linkedin.browser.BrowserRun.scroll`'s mouse-wheel replay and
:meth:`~netkeeper.linkedin.browser.BrowserRun.goto`'s navigation: **no click, no typing,
no new input** (``tests/test_browser_safety.py``).

**Where the tab landed is classified first** (spec 9.7): a checkpoint or a login wall
raises :class:`~netkeeper.linkedin.inbox.InboxReadStopped` (the runner sets the session
flag), a throttle raises it too (the runner raises heat), and a tab somewhere other
than the inbox stops as ``RouteChanged``. No html is searched for wall paths.

**A parse failure is a stop, never a quiet "no replies".** Every answer the source
needs parses whole or raises ``RouteChanged``, which this class turns into
``InboxReadStopped(ROUTE_CHANGED)``: the runner ends the poll ``aborted``, and #417
treats only a ``completed`` poll as fresh. A list answer the page never loaded, a
list that stops growing before it proves its end (:data:`MAX_IDLE_SCROLLS` scrolls with
nothing new, the connections sync's own limit), a page of the list that went unseen
(a request whose anchor is not where the last answer left off), a list that is not
newest first, and a body the observation could not keep
(:class:`~netkeeper.linkedin.observe.ObservationFailed`) are all stops.

**What ``complete`` means.** True only when the list read proved it: it reached a
conversation whose last activity is at or before ``spec.since``, or the end of the list
(a category page with no next cursor or no conversations: assumed, not captured). Within
the first ``spec.max_conversations`` conversations only; stopping at the bound first is
false.

**Opening threads** (the 2026-10-03 decision). The list carries only the last message,
so a thread is opened by navigation to its own page, never a click, only for a
conversation in ``spec.open_threads_for`` that the list showed one-to-one with activity
after ``since``, and at most :data:`~netkeeper.linkedin.inbox.MAX_THREADS_OPENED` per
poll, newest first. A thread answer the page loaded on its own counts and costs no
navigation. A thread opened for its replies that yields no parsed answer (a lost body, a non-200, a
renamed field) stops the poll. A one-to-one item that carries no message, whose thread was
asked for and not opened (the cap), makes the read incomplete. A third sender in a
one-to-one thread is an unknown shape and stops the poll.

**Nothing identifying is logged**: counts only, never a url, an id, or a text.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final, cast
from urllib.parse import urlsplit

from netkeeper.linkedin.browser import BrowserRun, BrowserUnavailable, PageLike
from netkeeper.linkedin.classify import Outcome, classify
from netkeeper.linkedin.flagship import LINKEDIN_ORIGIN
from netkeeper.linkedin.inbox import (
    MAX_THREADS_OPENED,
    InboxConversation,
    InboxDelta,
    InboxJobSpec,
    InboxMessage,
    InboxReadStopped,
)
from netkeeper.linkedin.messaging_shapes import (
    BY_CATEGORY,
    BY_SYNC_TOKEN,
    CONVERSATIONS_QUERY,
    GRAPHQL_PATH,
    LIST_CATEGORY,
    MESSAGES_BY_SYNC_TOKEN,
    MESSAGES_QUERY,
    MESSAGING_PAGE_PATH,
    THREAD_PATH_PREFIX,
    Kind,
    ListItem,
    answer_field,
    conversation_parts,
    parse_conversation_list,
    parse_thread,
    query_name,
    request_category,
    request_conversation,
    request_has_sync_token,
    request_last_updated_before,
    request_mailbox,
    request_next_cursor,
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
from netkeeper.linkedin.pacing import (
    DEFAULT_SCROLL_PROFILE,
    ScrollProfile,
    scroll_like_a_person,
)
from netkeeper.linkedin.strict_origin import NotAStrictOrigin, parse_strict_origin
from netkeeper.linkedin.voyager import RouteChanged

log = logging.getLogger(__name__)

#: How long, after a scroll's dwell, to wait for the answer it made the page ask for.
RESPONSE_WAIT_S: Final = 5.0

#: How long to wait for the list after the navigation returns.
LANDING_WAIT_S: Final = 20.0

#: How long to wait for a thread's answer after navigating to it.
THREAD_WAIT_S: Final = 15.0

#: Scrolls in one read that bring nothing new before the read stops as ``RouteChanged``:
#: the connections sync's own limit.
MAX_IDLE_SCROLLS: Final = 6

#: The pause before each thread navigation, in seconds (a person reading, then choosing).
THREAD_PAUSE_S: Final = (4.0, 10.0)

#: What the poll's scroll rests the pointer over (#439): a link to a conversation. The
#: wheel scrolls the nearest scrollable ancestor of what is under the pointer, which for
#: a conversation link is the list pane, not the thread pane beside it. A selector for
#: one read-only geometry lookup, never a click: nothing is run in the page, and the link
#: is only hovered at its center as a bare point.
CONVERSATION_LINK: Final = f'a[href*="{THREAD_PATH_PREFIX}"]'

_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(slots=True)
class _Seen:
    """What the page's answers added up to so far."""

    owner: str | None = None
    #: Every conversation seen, latest version of each, by conversation urn.
    latest: dict[str, ListItem] = field(default_factory=dict)
    #: The on-load list and the older pages, in the order they arrived: the contiguous
    #: newest-first run the completeness proof rests on.
    run: list[ListItem] = field(default_factory=list)
    first: bool = False
    ended: bool = False
    older_started: bool = False
    expected_cursor: str | None = None
    cursors: set[str] = field(default_factory=set)
    threads: dict[str, dict[str, InboxMessage]] = field(default_factory=dict)


class PageInbox:
    """The inbox poll's source over the page's own answers. One instance per run.

    ``origin`` is LinkedIn's, and a loopback origin only for the smoke suite's replica.
    ``rng``, ``scroll_profile``, and ``sleep`` shape the scroll and the pauses the way
    they do for :class:`~netkeeper.linkedin.page_connections.PageConnections`.
    """

    def __init__(
        self,
        run: BrowserRun,
        *,
        origin: str = LINKEDIN_ORIGIN,
        rng: random.Random | None = None,
        scroll_profile: ScrollProfile = DEFAULT_SCROLL_PROFILE,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        response_wait_s: float = RESPONSE_WAIT_S,
        landing_wait_s: float = LANDING_WAIT_S,
        thread_wait_s: float = THREAD_WAIT_S,
        max_idle_scrolls: int = MAX_IDLE_SCROLLS,
        thread_pause_s: tuple[float, float] = THREAD_PAUSE_S,
        limits: ObservationLimits | None = None,
    ) -> None:
        self._run = run
        self._origin = _require_origin(origin)
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 -- pacing, not crypto
        self._scroll_profile = scroll_profile
        self._sleep = sleep
        self._response_wait_s = response_wait_s
        self._landing_wait_s = landing_wait_s
        self._thread_wait_s = thread_wait_s
        self._max_idle = max_idle_scrolls
        self._thread_pause_s = thread_pause_s
        self._limits = limits
        self._observation: Observation | None = None  # set by read()
        self._seen = _Seen()
        self._url = ""
        self.threads_opened = 0

    @property
    def page_url(self) -> str:
        return f"{self._origin}{MESSAGING_PAGE_PATH}"

    async def read(self, spec: InboxJobSpec) -> InboxDelta:
        try:
            return await self._read(spec)
        except RouteChanged:
            # Already logged by RouteChanged itself (a shape, never a value). The poll
            # stops: nothing partial is handed on.
            raise InboxReadStopped(Outcome.ROUTE_CHANGED, final_url=self._url) from None

    # --- the read ---------------------------------------------------------------------

    async def _read(self, spec: InboxJobSpec) -> InboxDelta:
        match = ResponseMatch(
            origin=self._origin,
            rules=(
                ResponseRule("GET", GRAPHQL_PATH),
                ResponseRule("GET", MESSAGING_PAGE_PATH),
                ResponseRule("GET", THREAD_PATH_PREFIX, prefix=True),
            ),
        )
        self._observation = await self._run.observe(match, limits=self._limits)
        page = await self._run.goto(self.page_url)
        self._require_observed(page)
        self._where(page.url, thread=False)
        await self._land()
        await self._absorb(wait_s=0.0)
        await self._scroll_until_enough(spec)
        considered, complete = self._considered(spec)
        await self._open_threads(spec, considered)
        await self._absorb(wait_s=0.0)
        return self._delta(spec, complete)

    async def _land(self) -> None:
        observation = self._observation
        assert observation is not None
        while not self._seen.first:
            response = await observation.next(self._landing_wait_s)
            if response is None:
                log.warning("inbox: the page loaded, but no conversation list arrived")
                raise RouteChanged("inbox", "no conversation list arrived")
            self._take(response)

    async def _scroll_until_enough(self, spec: InboxJobSpec) -> None:
        idle = 0
        while not self._enough(spec):
            if idle >= self._max_idle:
                log.warning(
                    "inbox: %d scrolls brought no new answer before the list proved its end;"
                    " stopping without trusting it",
                    idle,
                )
                raise RouteChanged("inbox", "the list stopped loading before its end")
            before = (len(self._seen.run), self._seen.ended)
            await self._scroll()
            await self._absorb(wait_s=self._response_wait_s)
            idle = idle + 1 if (len(self._seen.run), self._seen.ended) == before else 0

    def _reached(self, spec: InboxJobSpec) -> bool:
        since = spec.since
        return since is not None and any(i.last_activity_at <= since for i in self._seen.run)

    def _enough(self, spec: InboxJobSpec) -> bool:
        return (
            self._seen.ended or self._reached(spec) or len(self._seen.run) >= spec.max_conversations
        )

    def _considered(self, spec: InboxJobSpec) -> tuple[list[ListItem], bool]:
        """The conversations within the bound, newest first, and whether the list proved it.

        Items strictly older than ``since`` only prove the read reached it; they are not
        returned. Conversations only a refresh named are added when active since then.
        """
        seen = self._seen
        considered: list[ListItem] = []
        reached = False
        bounded = False
        for index, item in enumerate(seen.run):
            if index >= spec.max_conversations:
                bounded = True
                break
            if spec.since is not None and item.last_activity_at <= spec.since:
                reached = True
                if item.last_activity_at == spec.since:
                    considered.append(seen.latest[item.conversation_urn])
                break
            considered.append(seen.latest[item.conversation_urn])
        complete = reached or (seen.ended and not bounded)
        named = {i.conversation_urn for i in seen.run}
        for urn, item in seen.latest.items():
            if urn not in named and (spec.since is None or item.last_activity_at >= spec.since):
                considered.append(item)
        # A conversation the run named at an old place may have moved up in a refresh.
        unique = {i.conversation_urn: seen.latest[i.conversation_urn] for i in considered}
        considered = list(unique.values())
        considered.sort(key=lambda i: i.last_activity_at, reverse=True)
        return considered, complete

    # --- threads -------------------------------------------------------------------------

    async def _open_threads(self, spec: InboxJobSpec, considered: list[ListItem]) -> None:
        wanted = [
            item
            for item in considered
            if item.kind is Kind.ONE_TO_ONE
            and item.conversation_urn in spec.open_threads_for
            and item.conversation_urn not in self._seen.threads
            and (spec.since is None or item.last_activity_at > spec.since)
        ][:MAX_THREADS_OPENED]
        observation = self._observation
        assert observation is not None
        for item in wanted:
            if self.threads_opened >= MAX_THREADS_OPENED:
                break
            await self._pause()
            self.threads_opened += 1
            page = await self._run.goto(f"{self._origin}{item.thread_path}")
            self._require_observed(page)
            self._where(page.url, thread=True)
            while item.conversation_urn not in self._seen.threads:
                response = await observation.next(self._thread_wait_s)
                if response is None:
                    # A lost body, a non-200 and a renamed field all end here or earlier:
                    # a thread opened for its replies that yields nothing parsed is a stop.
                    log.warning("inbox: a thread opened but loaded no readable messages")
                    raise RouteChanged("inbox", "an opened thread loaded no readable answer")
                self._take(response)

    async def _pause(self) -> None:
        low, high = self._thread_pause_s
        seconds = self._rng.uniform(low, high)
        if self._sleep is None:
            await asyncio.sleep(seconds)
        else:
            await self._sleep(seconds)

    # --- the delta ------------------------------------------------------------------------

    def _delta(self, spec: InboxJobSpec, complete: bool) -> InboxDelta:
        considered, _ = self._considered(spec)
        conversations: list[InboxConversation] = []
        skipped_group = skipped_other = unread = 0
        for item in considered:
            if item.kind is Kind.GROUP:
                skipped_group += 1
                continue
            if item.kind is Kind.OTHER:
                skipped_other += 1
                continue
            assert item.counterpart_urn is not None
            messages: dict[str, InboxMessage] = {}
            if item.last_message is not None:
                messages[item.last_message.message_urn] = item.last_message
            messages.update(self._seen.threads.get(item.conversation_urn, {}))
            strangers = {m.sender_urn for m in messages.values()} - {
                item.owner_urn,
                item.counterpart_urn,
            }
            if strangers:
                # The list said two participants: a third sender is an unknown shape, not
                # a group to skip (a reply of the contact's could be hiding in it).
                raise RouteChanged("inbox", "a one-to-one conversation has a third sender")
            if not messages:
                # A one-to-one item with no message at all: counted. If its thread was
                # asked for and not opened (the cap), the read is not complete.
                unread += 1
                if item.conversation_urn in spec.open_threads_for:
                    complete = False
                continue
            conversations.append(
                InboxConversation(
                    conversation_urn=item.conversation_urn,
                    counterpart_urn=item.counterpart_urn,
                    last_activity_at=item.last_activity_at,
                    messages=tuple(sorted(messages.values(), key=lambda m: m.at)),
                )
            )
        log.info(
            "inbox: read %d conversations, %d with no message, skipped %d group and %d other,"
            " opened %d threads, complete=%s",
            len(conversations),
            unread,
            skipped_group,
            skipped_other,
            self.threads_opened,
            complete,
        )
        return InboxDelta(
            conversations=tuple(conversations),
            skipped_group=skipped_group,
            skipped_other=skipped_other,
            complete=complete,
            owner_urn=self._seen.owner,
        )

    # --- the page's answers -----------------------------------------------------------------

    async def _absorb(self, *, wait_s: float) -> None:
        observation = self._observation
        assert observation is not None
        response = await observation.next(wait_s)
        while response is not None:
            self._take(response)
            response = await observation.next(0.0)

    def _take(self, response: ObservedResponse) -> None:
        path = urlsplit(response.url).path
        if path != GRAPHQL_PATH:
            outcome = _outcome(response)  # a messaging document: only its status matters
            if outcome is not Outcome.OK:
                raise InboxReadStopped(outcome, final_url=response.location or self._url)
            return
        name = query_name(response.url)
        ours = name in (CONVERSATIONS_QUERY, MESSAGES_QUERY)
        outcome = _outcome(response)
        if outcome is not Outcome.OK:
            if ours or outcome in (Outcome.THROTTLED, Outcome.CHECKPOINT, Outcome.LOGGED_OUT):
                raise InboxReadStopped(outcome, final_url=response.location or self._url)
            return  # an unrelated query's error says nothing about the list
        if not ours:
            return
        if response.body is None:
            raise ObservationFailed(f"an answer of the page could not be kept: {response.failure}")
        mailbox = request_mailbox(response.url)
        if mailbox is not None:
            self._agree_owner(mailbox)
        field = answer_field(response.body)
        if name == CONVERSATIONS_QUERY and field in (BY_SYNC_TOKEN, BY_CATEGORY):
            self._take_list(response)
        elif name == MESSAGES_QUERY and field == MESSAGES_BY_SYNC_TOKEN:
            self._take_thread(response)

    def _agree_owner(self, owner: str) -> None:
        seen = self._seen
        if seen.owner is None:
            seen.owner = owner
        elif seen.owner != owner:
            raise RouteChanged("inbox", "the answers name more than one mailbox owner")

    def _take_list(self, response: ObservedResponse) -> None:
        assert response.body is not None
        seen = self._seen
        answer = parse_conversation_list(response.body)
        for item in answer.items:
            self._agree_owner(item.owner_urn)
        refresh = answer.field == BY_SYNC_TOKEN and (
            request_has_sync_token(response.url) or seen.first
        )
        for item in answer.items:
            known = seen.latest.get(item.conversation_urn)
            if known is None or item.last_activity_at >= known.last_activity_at:
                seen.latest[item.conversation_urn] = item
        if refresh:
            return  # changed conversations only: never a contiguous run
        if answer.field == BY_SYNC_TOKEN:
            seen.first = True
            self._extend(answer.items)
            return
        if not seen.first:
            raise RouteChanged("inbox", "an older page of the list arrived before the list")
        if request_category(response.url) != LIST_CATEGORY:
            raise RouteChanged("inbox", "an older page of the list is not of the primary inbox")
        cursor = request_next_cursor(response.url)
        if cursor is None:
            before = request_last_updated_before(response.url)
            if before is None:
                raise RouteChanged("inbox", "an older page names neither a cursor nor a time")
            if seen.older_started:
                return  # the first older page asked again: already read
            oldest = min((i.last_activity_at for i in seen.run), default=None)
            if oldest is not None and before < round(oldest.timestamp() * 1000):
                raise RouteChanged("inbox", "a page of the list went unseen (a gap before it)")
            seen.older_started = True
        else:
            if cursor in seen.cursors:
                return  # a retried request for a page already read
            if cursor != seen.expected_cursor:
                raise RouteChanged("inbox", "a page of the list went unseen (a cursor skipped)")
            seen.cursors.add(cursor)
        self._extend(answer.items)
        seen.expected_cursor = answer.next_cursor
        if answer.next_cursor is None or not answer.items:
            seen.ended = True

    def _extend(self, items: tuple[ListItem, ...]) -> None:
        run = self._seen.run
        for item in items:
            if run and item.last_activity_at > run[-1].last_activity_at:
                raise RouteChanged("inbox", "the list is not newest first")
            run.append(item)

    def _take_thread(self, response: ObservedResponse) -> None:
        assert response.body is not None
        seen = self._seen
        conversation = request_conversation(response.url)
        if conversation is None:
            raise RouteChanged("inbox", "a thread request names no conversation")
        owner, _thread = conversation_parts(conversation)
        self._agree_owner(owner)
        answer = parse_thread(response.body, owner_urn=owner, conversation_urn=conversation)
        held = seen.threads.setdefault(conversation, {})
        for message in answer.messages:
            held[message.message_urn] = message

    # --- the tab ---------------------------------------------------------------------------------

    async def _scroll(self) -> None:
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
        if self._sleep is None:
            outcome = await self._run.scroll(plan, rng=self._rng, rest_over=CONVERSATION_LINK)
        else:
            outcome = await self._run.scroll(
                plan, sleep=self._sleep, rng=self._rng, rest_over=CONVERSATION_LINK
            )
        self._require_observed(outcome.page)
        self._where(outcome.page.url, thread=False)

    def _require_observed(self, page: PageLike) -> None:
        observation = self._observation
        assert observation is not None
        if page is not cast(object, observation.page):
            raise BrowserUnavailable(
                "the run's tab was replaced mid-read, so its answers are no longer"
                " observed; aborting the run rather than read part of the inbox"
            )

    def _where(self, url: str, *, thread: bool) -> None:
        """Classify where the tab is (spec 9.7): a wall stops the run; elsewhere is a change."""
        self._url = url
        outcome = classify(200, url, "")
        if outcome in (Outcome.CHECKPOINT, Outcome.LOGGED_OUT):
            raise InboxReadStopped(outcome, final_url=url)
        split = urlsplit(url)
        want = urlsplit(self._origin)
        same_origin = (split.scheme, split.hostname, split.port) == (
            want.scheme,
            want.hostname,
            want.port,
        )
        path = _trim(split.path)
        on_page = path == _trim(MESSAGING_PAGE_PATH) or (
            thread and split.path.startswith(THREAD_PATH_PREFIX)
        )
        if not (same_origin and on_page):
            log.warning("inbox: the tab is not on the messaging page; stopping")
            raise InboxReadStopped(Outcome.ROUTE_CHANGED, final_url=url)


def _outcome(response: ObservedResponse) -> Outcome:
    """What one observed answer means (spec 9.7), from its status and url, never its body."""
    if response.failure == FAILURE_REDIRECT:
        outcome = classify(response.status, response.location or "", "")
        return (
            outcome
            if outcome in (Outcome.CHECKPOINT, Outcome.LOGGED_OUT)
            else Outcome.ROUTE_CHANGED
        )
    if response.status == 200:
        return Outcome.OK
    return classify(response.status, response.url, "")


def _trim(path: str) -> str:
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
        f"the inbox is read from {LINKEDIN_ORIGIN!r}, or this machine's own"
        f" loopback for tests, never {origin!r}"
    )


__all__ = ["PageInbox"]
