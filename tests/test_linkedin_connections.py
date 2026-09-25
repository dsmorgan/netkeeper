"""netkeeper.linkedin.connections: the connections sync job, with no database (spec 9.4, 9.10).

Every test drives the job against :class:`voyager_pages.FakeConnectionsSource`, a
neutral, in-memory ``ConnectionsSource`` over a list of invented people: no request
shape, no classifier, no parser in the way, so these tests exercise
``run_connections_sync``'s own paging, stopping, and completeness rules -- the same
for every source -- rather than one source's translation into and out of its
transport. (Before #189, this drove ``VoyagerConnections`` over a fake Voyager
transport instead; that wrapper is retired along with the in-page connections
endpoint it read, unused since P2-17 moved a live sync onto
:class:`~netkeeper.linkedin.page_connections.PageConnections`. Its own
request/response shape is what ``test_linkedin_voyager.py`` and
``test_classify.py`` still test.) No socket is opened and nothing reaches
linkedin.com. Page sizes are small (3 or 4) so that page boundaries fall inside
the ten-person list, which is what the incremental-stop and completeness tests
need to mean anything.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from voyager_pages import CONNECTIONS_URL, PEOPLE, FakeConnectionsSource, Person, page_body

from netkeeper.linkedin import connections as job
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.connections import (
    ConnectionsPage,
    ProgressEvent,
    SourcePage,
    StopReason,
    SyncJobSpec,
    SyncMode,
    run_connections_sync,
)
from netkeeper.linkedin.voyager import ConnectionsPageResult, ConnectionSummary

FIXTURES = Path(__file__).parent / "fixtures" / "voyager"
NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)


@dataclass(slots=True)
class Gate:
    """Allows ``allow`` pages (None: all), and records what it was asked."""

    allow: int | None = None
    asked: list[int] = field(default_factory=list)
    pauses: int = 0

    async def before_page(self, number: int) -> bool:
        self.asked.append(number)
        return self.allow is None or number < self.allow

    async def between_pages(self) -> None:
        self.pauses += 1


@dataclass(slots=True)
class Sink:
    pages: list[ConnectionsPage] = field(default_factory=list)
    events: list[ProgressEvent] = field(default_factory=list)

    async def page(self, page: ConnectionsPage) -> None:
        self.pages.append(page)

    async def progress(self, event: ProgressEvent) -> None:
        self.events.append(event)

    @property
    def urns(self) -> list[str]:
        return [c.urn for page in self.pages for c in page.connections if c.urn is not None]


async def _run(
    source: FakeConnectionsSource,
    *,
    mode: SyncMode = SyncMode.FULL,
    page_size: int = 3,
    page_budget: int = 50,
    known: frozenset[str] = frozenset(),
    gate: Gate | None = None,
) -> tuple[job.SyncResult, Sink, Gate]:
    sink = Sink()
    gate = gate or Gate()
    spec = SyncJobSpec(mode=mode, page_budget=page_budget, known_urns=known, page_size=page_size)
    result = await run_connections_sync(
        spec,
        source,
        gate,
        on_page=sink.page,
        on_progress=sink.progress,
        clock=lambda: NOW,
    )
    return result, sink, gate


def _page(people: Sequence[Person], *, start: int, total: int) -> SourcePage:
    """A scripted ``Ok`` answer: exactly ``people``, at ``start``, reporting ``total`` -- for a
    page shaped differently than :class:`~voyager_pages.FakeConnectionsSource`'s default honest
    slice (a short page mid-list, an empty page, one reporting a specific total)."""
    connections = tuple(
        ConnectionSummary(
            urn=person.urn,
            public_id=person.slug,
            first_name=person.first,
            last_name=person.last,
            headline=person.headline,
            connected_at=None,
        )
        for person in people
    )
    return SourcePage(
        outcome=Outcome.OK,
        final_url=CONNECTIONS_URL,
        page=ConnectionsPageResult(
            connections=connections, start=start, count=len(connections), total=total
        ),
    )


# --- the fixture builder is the fixture ------------------------------------------


def test_the_page_builder_rebuilds_the_committed_fixture() -> None:
    """The sync tests use built pages; this ties the builder to the captured shape."""
    fixture = json.loads((FIXTURES / "connections_page.json").read_text())
    people = [
        Person(1, "Jamie", "Rivera", "Product designer at Fictional Robotics Co",
               1_690_000_000_000, public_id="jamie-fake-rivera-1a2b3c4d"),
        Person(2, "Alex", "Chen", "Staff engineer at Acme Testing Group",
               1_691_000_000_000, public_id="alex-fake-chen-5e6f7g8h"),
    ]  # fmt: skip
    assert json.loads(page_body(people, start=0, count=40, total=2)) == fixture


# --- full sync ---------------------------------------------------------------


async def test_a_full_sync_pages_to_the_end_and_is_complete() -> None:
    source = FakeConnectionsSource(list(PEOPLE))

    result, sink, gate = await _run(source)

    assert source.starts == [0, 3, 6, 9]
    assert sink.urns == [person.urn for person in PEOPLE]
    assert [page.number for page in sink.pages] == [0, 1, 2, 3]
    assert result.reason is StopReason.END_OF_LIST
    assert result.complete
    assert result.seen_urns == {person.urn for person in PEOPLE}
    assert result.total == 10 and result.pages == 4 and result.connections == 10
    assert gate.asked == [0, 1, 2, 3]
    assert gate.pauses == 3  # between pages, never before the first
    assert all(page.observed_at == NOW for page in sink.pages)


async def test_every_page_is_requested_at_the_configured_size() -> None:
    source = FakeConnectionsSource(list(PEOPLE))
    await _run(source, page_size=4)
    assert [count for _, count in source.requests] == [4, 4, 4]
    assert source.starts == [0, 4, 8]


async def test_a_list_that_ends_on_a_page_boundary_is_confirmed_by_an_empty_page() -> None:
    """A full last page is not the end: only a short page is, so one empty page follows."""
    source = FakeConnectionsSource(list(PEOPLE[:9]))
    result, _, _ = await _run(source)
    assert source.starts == [0, 3, 6, 9]
    assert result.complete


async def test_a_full_sync_ignores_known_urns_by_refusing_them() -> None:
    with pytest.raises(ValueError, match="incremental"):
        SyncJobSpec(mode=SyncMode.FULL, page_budget=5, known_urns=frozenset({PEOPLE[0].urn}))


async def test_an_empty_list_is_the_end_of_the_list() -> None:
    source = FakeConnectionsSource([])
    result, sink, _ = await _run(source)
    assert result.reason is StopReason.END_OF_LIST
    assert source.starts == [0]
    assert sink.urns == []


# --- what stops a run early, and why none of it is complete -----------------------


async def test_the_page_budget_stops_a_full_sync_and_it_is_not_complete() -> None:
    source = FakeConnectionsSource(list(PEOPLE))
    result, sink, _ = await _run(source, page_budget=2)
    assert source.starts == [0, 3]
    assert result.reason is StopReason.PAGE_BUDGET
    assert not result.complete
    assert len(sink.pages) == 2


async def test_a_zero_page_budget_fetches_nothing() -> None:
    source = FakeConnectionsSource(list(PEOPLE))
    result, _, gate = await _run(source, page_budget=0)
    assert source.requests == []
    assert gate.asked == []
    assert result.reason is StopReason.PAGE_BUDGET


async def test_the_gate_is_asked_before_each_page_and_its_refusal_fetches_nothing() -> None:
    source = FakeConnectionsSource(list(PEOPLE))
    result, sink, gate = await _run(source, gate=Gate(allow=2))
    assert gate.asked == [0, 1, 2]
    assert source.starts == [0, 3]  # the refused third page was never requested
    assert result.reason is StopReason.BUDGET
    assert not result.complete
    assert len(sink.pages) == 2


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(Outcome.THROTTLED, id="throttled"),
        pytest.param(Outcome.CHECKPOINT, id="checkpoint"),
        pytest.param(Outcome.LOGGED_OUT, id="logged-out"),
        pytest.param(Outcome.ROUTE_CHANGED, id="route-changed"),
        pytest.param(Outcome.NOT_FOUND, id="not-found"),
    ],
)
async def test_the_first_non_ok_response_stops_the_run_with_no_retry(outcome: Outcome) -> None:
    """A source's classification (spec 9.7) is tested on its own in ``test_classify.py``;
    this only needs the run's reaction to whatever a source answers."""
    final_url = "https://www.linkedin.com/x"
    source = FakeConnectionsSource(
        list(PEOPLE), script={2: SourcePage(outcome=outcome, final_url=final_url)}
    )

    result, sink, gate = await _run(source)

    assert source.starts == [0, 3, 6]  # the third page failed and nothing was asked again
    assert result.reason is StopReason.RESPONSE
    assert result.outcome is outcome
    assert result.final_url == final_url
    assert not result.complete
    assert len(sink.pages) == 2
    assert result.seen_urns == {person.urn for person in PEOPLE[:6]}
    assert gate.asked == [0, 1, 2]


async def test_a_checkpoint_on_the_first_page_is_never_retried() -> None:
    source = FakeConnectionsSource(
        list(PEOPLE),
        script={0: SourcePage(outcome=Outcome.CHECKPOINT, final_url=CONNECTIONS_URL)},
    )
    result, sink, _ = await _run(source)
    assert len(source.requests) == 1
    assert result.outcome is Outcome.CHECKPOINT
    assert sink.pages == []


async def test_a_page_at_an_offset_nobody_asked_for_is_a_changed_route() -> None:
    source = FakeConnectionsSource(list(PEOPLE), start_offset=1)
    result, sink, _ = await _run(source)
    assert result.outcome is Outcome.ROUTE_CHANGED
    assert sink.pages == []


async def test_an_empty_page_before_the_total_is_not_complete() -> None:
    """A list that stops paging short of its own total ends the run but ages nobody."""
    empty_early = _page([], start=6, total=10)
    source = FakeConnectionsSource(list(PEOPLE), script={2: empty_early})

    result, _, _ = await _run(source)

    assert source.starts == [0, 3, 6]
    assert result.reason is StopReason.END_OF_LIST
    assert len(result.seen_urns) == 6 and result.total == 10
    assert not result.complete


async def test_a_list_that_grew_during_the_run_is_not_complete() -> None:
    """A connection accepted mid-run lands at the top, behind the run: seen 10 of 11."""
    people = list(PEOPLE)
    source = FakeConnectionsSource(people)
    newcomer = Person(111, "Zanele", "Oyelaran", "Analyst at Pretend Freight", 1_701_000_000_000)

    async def grow(page: ConnectionsPage) -> None:
        if page.number == 0:
            people.insert(0, newcomer)

    result = await run_connections_sync(
        SyncJobSpec(mode=SyncMode.FULL, page_budget=50, page_size=3),
        source,
        Gate(),
        on_page=grow,
        clock=lambda: NOW,
    )

    assert result.reason is StopReason.END_OF_LIST
    assert newcomer.urn not in result.seen_urns
    assert result.seen_urns == {person.urn for person in PEOPLE}
    assert result.total == 11
    assert not result.complete


async def test_a_removal_during_the_run_is_caught_by_the_largest_total() -> None:
    """A removal skips a row at a page boundary; the first page's total still counts it."""
    people = list(PEOPLE)
    source = FakeConnectionsSource(people)

    async def remove_a_seen_one(page: ConnectionsPage) -> None:
        if page.number == 0:
            del people[0]

    result = await run_connections_sync(
        SyncJobSpec(mode=SyncMode.FULL, page_budget=50, page_size=3),
        source,
        Gate(),
        on_page=remove_a_seen_one,
        clock=lambda: NOW,
    )

    assert PEOPLE[3].urn not in result.seen_urns  # moved up to offset 2, already read
    assert result.reason is StopReason.END_OF_LIST
    assert (len(result.seen_urns), result.total, result.max_total) == (9, 9, 10)
    assert not result.complete


# --- incremental sync ------------------------------------------------------------


def _known(*people: Person) -> frozenset[str]:
    return frozenset(person.urn for person in people)


async def test_incremental_stops_at_the_first_page_that_is_all_known() -> None:
    """Three new people on top, then a page of known ones: two pages and stop.

    Page 0 holds the three newest, all unknown; page 1 holds three known ones,
    so the run reads it and stops before page 2.
    """
    source = FakeConnectionsSource(list(PEOPLE))
    known = _known(*PEOPLE[3:])  # everyone but the three newest

    result, sink, _ = await _run(source, mode=SyncMode.INCREMENTAL, known=known)

    assert source.starts == [0, 3]
    assert result.reason is StopReason.CAUGHT_UP
    assert not result.complete
    assert sink.urns == [person.urn for person in PEOPLE[:6]]


async def test_incremental_keeps_going_past_a_page_with_one_unknown_urn() -> None:
    """One unknown person in the middle of page 1 keeps page 2 coming."""
    source = FakeConnectionsSource(list(PEOPLE))
    known = _known(*PEOPLE[1:4], *PEOPLE[5:])  # PEOPLE[0] (page 0) and PEOPLE[4] (page 1) are new

    result, _, _ = await _run(source, mode=SyncMode.INCREMENTAL, known=known)

    assert source.starts == [0, 3, 6]
    assert result.reason is StopReason.CAUGHT_UP


async def test_incremental_with_nothing_known_reads_to_the_end_and_is_still_not_complete() -> None:
    """Reading every page in incremental mode ages nobody: only a full sync may."""
    source = FakeConnectionsSource(list(PEOPLE))
    result, _, _ = await _run(source, mode=SyncMode.INCREMENTAL, known=frozenset())
    assert source.starts == [0, 3, 6, 9]
    assert result.reason is StopReason.END_OF_LIST
    assert not result.complete


async def test_incremental_with_everything_known_reads_one_page() -> None:
    source = FakeConnectionsSource(list(PEOPLE))
    result, sink, _ = await _run(source, mode=SyncMode.INCREMENTAL, known=_known(*PEOPLE))
    assert source.starts == [0]
    assert result.reason is StopReason.CAUGHT_UP
    assert len(sink.pages) == 1  # the page is still handed over: headlines refresh


# --- progress ------------------------------------------------------------------------


async def test_progress_counts_pages_and_says_why_it_stopped() -> None:
    source = FakeConnectionsSource(
        list(PEOPLE),
        script={2: SourcePage(outcome=Outcome.THROTTLED, final_url=CONNECTIONS_URL)},
    )
    _, sink, _ = await _run(source)
    assert [(e.pages, e.connections, e.stopped) for e in sink.events] == [
        (1, 3, None),
        (2, 6, None),
        (2, 6, StopReason.RESPONSE),
    ]
    assert all(e.total == 10 for e in sink.events)


def test_progress_events_carry_counts_and_nothing_that_names_a_person() -> None:
    names = {f.name for f in job.ProgressEvent.__dataclass_fields__.values()}
    assert names == {"mode", "pages", "connections", "total", "stopped"}


# --- the spec ------------------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 41])
def test_a_page_size_outside_what_the_client_asks_for_is_refused(size: int) -> None:
    with pytest.raises(ValueError, match="page_size"):
        SyncJobSpec(mode=SyncMode.FULL, page_budget=1, page_size=size)


def test_the_largest_page_is_forty() -> None:
    assert job.MAX_PAGE_SIZE == 40


def test_a_negative_page_budget_is_refused() -> None:
    with pytest.raises(ValueError, match="page_budget"):
        SyncJobSpec(mode=SyncMode.INCREMENTAL, page_budget=-1)


async def test_an_ok_answer_with_no_page_is_a_changed_route_not_an_empty_list() -> None:
    """A source that says ``Ok`` and hands back nothing must not read as the end of the list."""

    class Hollow:
        endpoint = "hollow"

        async def fetch_page(self, *, start: int, count: int) -> SourcePage:
            return SourcePage(outcome=Outcome.OK, final_url="https://www.linkedin.com/x")

    sink = Sink()
    result = await run_connections_sync(
        SyncJobSpec(mode=SyncMode.FULL, page_budget=5),
        Hollow(),
        Gate(),
        on_page=sink.page,
        clock=lambda: NOW,
    )
    assert result.outcome is Outcome.ROUTE_CHANGED
    assert result.reason is StopReason.RESPONSE
    assert not result.complete


# --- a total that lies ---------------------------------------------------------------
# Every other fixture here reports ``total=len(people)``. These serve honest pages
# under a dishonest ``paging.total``, which is the one number the run cannot check.


def _hundred() -> list[Person]:
    return [*PEOPLE, *(Person(300 + i, f"Given{i}", f"Family{i}", None) for i in range(90))]


async def test_a_total_of_zero_neither_ends_the_run_nor_completes_it() -> None:
    source = FakeConnectionsSource(_hundred(), total=0)

    result, sink, _ = await _run(source, page_size=40)

    # The short page at 80 reaches no reported total (there is none), so one more
    # request confirms the end with an empty page.
    assert source.starts == [0, 40, 80, 100]
    assert len(sink.urns) == 100
    assert result.reason is StopReason.END_OF_LIST
    assert not result.complete


async def test_a_total_that_lies_low_cannot_cut_the_run_short() -> None:
    """Total 40 of 100: the run still reads all 100, so it is complete about what it saw."""
    source = FakeConnectionsSource(_hundred(), total=40)

    result, _, _ = await _run(source, page_size=40)

    assert source.starts == [0, 40, 80]
    assert len(result.seen_urns) == 100
    assert result.complete


async def test_a_trailing_empty_page_cannot_lower_the_bar() -> None:
    """Page 0 says 100; page 1 comes back empty and says 0. Forty seen is not the list."""
    empty_says_zero = _page([], start=40, total=0)
    source = FakeConnectionsSource(_hundred(), script={1: empty_says_zero})

    result, _, _ = await _run(source, page_size=40)

    assert source.starts == [0, 40]
    assert result.reason is StopReason.END_OF_LIST
    assert (result.total, result.max_total) == (0, 100)
    assert not result.complete


async def test_an_honest_trailing_empty_page_reporting_zero_still_completes() -> None:
    """80 people, total 80 until the empty page at 80 says 0: all 80 were seen."""
    source = FakeConnectionsSource(_hundred()[:80], total=lambda start: 0 if start >= 80 else 80)
    result, _, _ = await _run(source, page_size=40)
    assert source.starts == [0, 40, 80]
    assert result.complete


# --- a short page in the middle of the list ------------------------------------------


async def test_a_short_page_before_the_total_is_read_past_not_the_end() -> None:
    """Offset 40 serves 39 of 40; the run carries on from 79 and completes."""
    people = _hundred()
    under_filled = _page(people[40:79], start=40, total=100)
    source = FakeConnectionsSource(people, script={1: under_filled})

    result, _, _ = await _run(source, page_size=40)

    assert source.starts == [0, 40, 79]
    assert result.reason is StopReason.END_OF_LIST
    assert len(result.seen_urns) == 100
    assert result.complete


async def test_a_short_page_that_reaches_the_total_is_the_end() -> None:
    source = FakeConnectionsSource(_hundred())
    result, _, _ = await _run(source, page_size=40)
    assert source.starts == [0, 40, 80]  # 80 + 20 reaches 100: no extra request
    assert result.complete


async def test_endless_full_pages_stop_at_the_page_budget() -> None:
    """A source that never runs out (or a total that never arrives) is bounded."""
    endless = [Person(1000 + i, f"Given{i}", f"Family{i}", None) for i in range(1000)]
    source = FakeConnectionsSource(endless, total=0)

    result, _, _ = await _run(source, page_size=40, page_budget=5)

    assert len(source.requests) == 5
    assert result.reason is StopReason.PAGE_BUDGET
    assert not result.complete


async def test_every_slug_on_every_page_is_reported() -> None:
    source = FakeConnectionsSource(list(PEOPLE))
    result, _, _ = await _run(source)
    assert result.seen_public_ids == {person.slug for person in PEOPLE}


# --- a source that fell back never completes ---------------------------------------
# FallbackConnectionsSource went with #187's review (nothing wired it). The rule it
# fed stays in the loop: a page a source marks ``switched`` makes the run incomplete,
# whatever the totals say, for any future source that falls back.


@dataclass(slots=True)
class ScriptedSource:
    """A ``ConnectionsSource`` that answers ``answers[i]`` on its ``i``-th call."""

    name: str
    answers: list[SourcePage]
    calls: list[tuple[int, int]] = field(default_factory=list)

    @property
    def endpoint(self) -> str:
        return self.name

    async def fetch_page(self, *, start: int, count: int) -> SourcePage:
        index = len(self.calls)
        self.calls.append((start, count))
        return self.answers[index]


def _summary(n: int, urn: str | None) -> ConnectionSummary:
    return ConnectionSummary(
        urn=urn,
        public_id=f"person-{n:04d}",
        first_name=f"Given{n}",
        last_name=f"Family{n}",
        headline=None,
        connected_at=None,
    )


def _ok(connections: Sequence[ConnectionSummary], *, start: int, total: int) -> SourcePage:
    return SourcePage(
        outcome=Outcome.OK,
        final_url=CONNECTIONS_URL,
        page=ConnectionsPageResult(
            connections=tuple(connections), start=start, count=len(connections), total=total
        ),
    )


ROUTE_CHANGED = SourcePage(outcome=Outcome.ROUTE_CHANGED, final_url=CONNECTIONS_URL)


async def test_a_page_from_a_source_that_fell_back_makes_the_run_incomplete() -> None:
    """#173 review, F5(a), kept for any source that reports ``switched``: the first
    page already reached max_total by URN, and the second came from a secondary
    source -- the run reaches the end, and is still never complete."""
    source = ScriptedSource(
        "switching",
        [
            _ok([_summary(0, "urn:0"), _summary(1, "urn:1")], start=0, total=2),
            replace(_ok([], start=2, total=0), switched=True),
        ],
    )

    result = await run_connections_sync(
        SyncJobSpec(mode=SyncMode.FULL, page_budget=50, page_size=2),
        source,
        Gate(),
        on_page=Sink().page,
        clock=lambda: NOW,
    )

    assert result.reason is StopReason.END_OF_LIST
    assert result.source_switched and not result.complete
