"""Connections sync, the extractor half: page the connections list, hand back pages (spec 9.4).

Behind the extractor boundary (spec 9.10, ADR 0005): a :class:`SyncJobSpec`
comes in, :class:`ConnectionsPage` values and :class:`ProgressEvent` values go
out through the callbacks the caller passes, and a :class:`SyncResult` is
returned. Nothing here imports the models or opens a session. The core maps
each page onto contacts in ``netkeeper.crm.apply`` and runs the edge lifecycle
of spec 9.8 there; ``netkeeper.services.connections_sync`` is the runner that
wires the two together with budgets, heat, and the session flag.

**Modes (spec 9.4).** A *full* sync pages the list to the end. An
*incremental* sync pages newest-first and stops after the first page whose
connections are all already known (the caller passes the URNs it knows in the
spec). Both stop at the end of the list, at the spec's page budget, and when
the gate refuses the next page.

**What stops a run.** Every page is one unit of work (spec 9.4, 9.9):

* the gate is asked *before* each page is fetched, never partway through one,
  and a refusal ends the run with :attr:`StopReason.BUDGET`;
* each response is classified (spec 9.7) before anything parses it, and the
  first outcome that is not ``Ok`` ends the run. Nothing is retried, a
  checkpoint least of all (:func:`netkeeper.linkedin.classify.is_retryable`
  would allow a throttled page another attempt; this job takes the stricter
  line and leaves the retry to the next scheduled run, with heat raised);
* a body the parser does not recognize is ``RouteChanged``: the run gives up on
  the endpoint for this run (spec 9.3).

**Completion.** :attr:`SyncResult.complete` is true only for a full sync that
reached the end of the list with every page ``Ok`` *and* saw at least as many
distinct connections as the list's last reported total. Only a complete full
sync may age contacts it did not see (spec 9.8); an aborted one saw part of
the list, and treating the rest as gone is how a network gets deleted. The
count check catches the other ways to end "at the end" having seen part of a
list: an empty page served before the reported total (a list the site stops
paging early), and a list that grew during the run (a connection added at the
top pushes every row down one, so the run sees one person fewer than the new
total). One shift it cannot see: a connection *removed* mid-run moves later
rows up one, and the row at the next page boundary is never served, while the
removed person, already seen, keeps the count level. That person gets one
miss they did not earn; the two-miss rule in spec 9.8 is what absorbs it.

**The source seam.** The job reads pages through a :class:`ConnectionsSource`.
:class:`VoyagerConnections` is the in-page API implementation (spec 9.3);
P2-08's DOM fallback implements the same protocol, so choosing the fallback
after a ``RouteChanged`` is a caller's decision about which source to pass,
not a change to this loop.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol

from netkeeper.linkedin.classify import Outcome, classify
from netkeeper.linkedin.voyager import (
    CONNECTIONS_DEFAULT_COUNT,
    CONNECTIONS_ENDPOINT,
    CONNECTIONS_PATH,
    ConnectionsPageResult,
    ConnectionSummary,
    RouteChanged,
    VoyagerFetch,
    VoyagerRequest,
    connections_query,
    parse_connections_page,
)

log = logging.getLogger(__name__)

#: The largest page a spec may ask for. The real client asks for 40 (spec 9.2);
#: a bigger page is a request shape the site never sees from a person.
MAX_PAGE_SIZE: Final = CONNECTIONS_DEFAULT_COUNT


class SyncMode(enum.StrEnum):
    """Spec 9.4's two connections jobs. Values match ``sync_run.kind`` minus its prefix."""

    FULL = "full"
    INCREMENTAL = "incremental"


class StopReason(enum.StrEnum):
    """Why a run stopped. Only :attr:`END_OF_LIST` can make a full sync complete."""

    END_OF_LIST = "end_of_list"
    """Every page up to the list's reported total was read."""

    CAUGHT_UP = "caught_up"
    """Incremental only: a page held nothing but already-known URNs."""

    PAGE_BUDGET = "page_budget"
    """The spec's ``page_budget`` pages were read and the list goes on."""

    BUDGET = "budget"
    """The gate refused the next page (the day's ``connection_pages`` budget)."""

    RESPONSE = "response"
    """A response classified as something other than ``Ok``; see ``SyncResult.outcome``."""


@dataclass(frozen=True, slots=True)
class SyncJobSpec:
    """What the core asks for (spec 9.10's ``SyncJobSpec``).

    ``known_urns`` is for incremental mode only, and a full sync that carries
    some is refused: a full sync must not stop early on them. ``page_budget`` is
    the most pages this run may fetch; 0 is allowed and fetches nothing.
    """

    mode: SyncMode
    page_budget: int
    known_urns: frozenset[str] = frozenset()
    page_size: int = CONNECTIONS_DEFAULT_COUNT

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", SyncMode(self.mode))
        object.__setattr__(self, "known_urns", frozenset(self.known_urns))
        if self.page_budget < 0:
            raise ValueError("page_budget must not be negative")
        if not 1 <= self.page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        if self.mode is SyncMode.FULL and self.known_urns:
            raise ValueError("known_urns is for incremental syncs only (spec 9.10)")


@dataclass(frozen=True, slots=True)
class ConnectionsPage:
    """One page of the connections list, as the core maps it (spec 9.10's ``ConnectionsPage``).

    ``number`` counts pages within this run from 0. ``observed_at`` is when the
    page arrived, timezone-aware; the core records it as each field's
    observation time.
    """

    mode: SyncMode
    number: int
    start: int
    total: int
    connections: tuple[ConnectionSummary, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One step of a run, for ``sync_run.progress_json`` and the SSE stream (spec 9.10).

    Counts only: no names, no URNs, nothing a log or a browser tab should not
    hold.
    """

    mode: SyncMode
    pages: int
    connections: int
    total: int | None
    stopped: StopReason | None = None


@dataclass(frozen=True, slots=True)
class SyncResult:
    """How a run ended, and what it saw.

    ``seen_urns`` is every URN on every page the run read; the core ages
    contacts outside it, and only when :attr:`complete` is true. ``outcome`` and
    ``final_url`` describe the response that stopped the run when ``reason`` is
    :attr:`StopReason.RESPONSE` (the core raises heat or the session flag from
    them), and are ``None`` otherwise.
    """

    mode: SyncMode
    reason: StopReason
    pages: int
    seen_urns: frozenset[str]
    total: int | None
    outcome: Outcome | None = None
    final_url: str | None = None
    connections: int = 0

    @property
    def complete(self) -> bool:
        """A full sync that read the whole list: the only run that may age anyone (spec 9.8)."""
        return (
            self.mode is SyncMode.FULL
            and self.reason is StopReason.END_OF_LIST
            and self.total is not None
            and len(self.seen_urns) >= self.total
        )


# --- the source seam ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourcePage:
    """What a source answered for one page: the classification, and the page when it was ``Ok``.

    ``page`` is ``None`` for every outcome but ``Ok``, and for an ``Ok``
    response whose body did not parse (then ``outcome`` is ``RouteChanged``).
    """

    outcome: Outcome
    final_url: str
    page: ConnectionsPageResult | None = None


class ConnectionsSource(Protocol):
    """Where pages of the connections list come from. P2-08's DOM fallback implements this too."""

    @property
    def endpoint(self) -> str:
        """A name for logs and ``RouteChanged``: which endpoint this reads."""
        ...

    async def fetch_page(self, *, start: int, count: int) -> SourcePage:
        """One page starting at offset ``start``: classified, and parsed only when ``Ok``."""
        ...


@dataclass(frozen=True, slots=True)
class VoyagerConnections:
    """The in-page API as a :class:`ConnectionsSource` (spec 9.3).

    ``fetch`` is the ``VoyagerFetch`` the browser side provides (#150).
    ``headers`` are sent on every request; the fetch helper merges what it reads
    from the live page (``csrf-token``, ``x-li-track``) on its side.
    """

    fetch: VoyagerFetch
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        return CONNECTIONS_ENDPOINT

    async def fetch_page(self, *, start: int, count: int) -> SourcePage:
        request = VoyagerRequest(
            path=CONNECTIONS_PATH,
            query=connections_query(start=start, count=count),
            headers=self.headers,
        )
        response = await self.fetch(request)
        outcome = classify(response.status, response.final_url, response.body)
        if outcome is not Outcome.OK:
            # Never parsed: a checkpoint page is not a connections page (#150's done-when).
            return SourcePage(outcome=outcome, final_url=response.final_url)
        try:
            page = parse_connections_page(response.body)
        except RouteChanged:
            return SourcePage(outcome=Outcome.ROUTE_CHANGED, final_url=response.final_url)
        return SourcePage(outcome=Outcome.OK, final_url=response.final_url, page=page)


# --- the gate ----------------------------------------------------------------


class PageGate(Protocol):
    """The core's say over each unit of work. Budgets and pacing live behind this.

    :meth:`before_page` runs before every fetch, never during one; ``False``
    stops the run (spec 9.4, 9.9). :meth:`between_pages` runs after a page is
    handed over and before the next is asked for: the human-like pause.
    """

    async def before_page(self, number: int) -> bool: ...

    async def between_pages(self) -> None: ...


PageSink = Callable[[ConnectionsPage], Awaitable[None]]
ProgressSink = Callable[[ProgressEvent], Awaitable[None]]


async def _no_progress(event: ProgressEvent) -> None:
    return None


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- the job -----------------------------------------------------------------


async def run_connections_sync(
    spec: SyncJobSpec,
    source: ConnectionsSource,
    gate: PageGate,
    *,
    on_page: PageSink,
    on_progress: ProgressSink = _no_progress,
    clock: Callable[[], datetime] = _utcnow,
) -> SyncResult:
    """Run one connections sync to its stopping point and say why it stopped.

    ``on_page`` receives each page as soon as it is read, before the next one is
    fetched, so what the core writes is never more than one page behind what
    the run saw. An exception from ``on_page`` propagates and ends the run; a
    run that ends by exception has no :class:`SyncResult`, so nothing downstream
    can mistake it for complete.
    """
    seen: set[str] = set()
    pages = 0
    connections = 0
    total: int | None = None
    start = 0

    def finish(
        reason: StopReason, outcome: Outcome | None = None, final_url: str | None = None
    ) -> SyncResult:
        return SyncResult(
            mode=spec.mode,
            reason=reason,
            pages=pages,
            seen_urns=frozenset(seen),
            total=total,
            outcome=outcome,
            final_url=final_url,
            connections=connections,
        )

    async def stopped(result: SyncResult) -> SyncResult:
        await on_progress(
            ProgressEvent(
                mode=spec.mode,
                pages=pages,
                connections=connections,
                total=total,
                stopped=result.reason,
            )
        )
        log.info(
            "connections %s sync stopped: %s after %d pages (%d connections)%s",
            spec.mode.value,
            result.reason.value,
            pages,
            connections,
            f", {result.outcome.value}" if result.outcome is not None else "",
        )
        return result

    while True:
        if pages >= spec.page_budget:
            return await stopped(finish(StopReason.PAGE_BUDGET))
        if pages > 0:
            await gate.between_pages()
        if not await gate.before_page(pages):
            return await stopped(finish(StopReason.BUDGET))

        answer = await source.fetch_page(start=start, count=spec.page_size)
        if answer.outcome is not Outcome.OK or answer.page is None:
            outcome = Outcome.ROUTE_CHANGED if answer.outcome is Outcome.OK else answer.outcome
            return await stopped(finish(StopReason.RESPONSE, outcome, answer.final_url))
        result = answer.page
        if result.start != start:
            # A page we did not ask for: an offset we cannot trust is a route we do not know.
            log.warning(
                "connections: asked for offset %d, %s answered %d",
                start,
                source.endpoint,
                result.start,
            )
            return await stopped(
                finish(StopReason.RESPONSE, Outcome.ROUTE_CHANGED, answer.final_url)
            )

        page = ConnectionsPage(
            mode=spec.mode,
            number=pages,
            start=result.start,
            total=result.total,
            connections=result.connections,
            observed_at=clock(),
        )
        await on_page(page)
        pages += 1
        connections += len(result.connections)
        total = result.total
        urns = {connection.urn for connection in result.connections}
        seen.update(urns)
        await on_progress(
            ProgressEvent(mode=spec.mode, pages=pages, connections=connections, total=total)
        )

        start += len(result.connections)
        if not result.connections or start >= result.total:
            return await stopped(finish(StopReason.END_OF_LIST))
        if spec.mode is SyncMode.INCREMENTAL and urns <= spec.known_urns:
            return await stopped(finish(StopReason.CAUGHT_UP))
