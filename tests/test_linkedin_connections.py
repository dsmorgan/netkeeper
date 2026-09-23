"""netkeeper.linkedin.connections: the connections sync job, with no database (spec 9.4, 9.10).

Every test drives the job against :class:`voyager_pages.FakeVoyagerFetch`, an
in-memory list of invented people paged the way the real endpoint pages. No
socket is opened and nothing reaches linkedin.com. Page sizes are small (3 or
4) so that page boundaries fall inside the ten-person list, which is what the
incremental-stop and completeness tests need to mean anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from voyager_pages import (
    CHECKPOINT,
    LOGGED_OUT,
    PEOPLE,
    THROTTLED,
    UNRECOGNIZED,
    FakeVoyagerFetch,
    Person,
    Scripted,
    page_body,
)

from netkeeper.linkedin import connections as job
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.connections import (
    ConnectionsPage,
    ProgressEvent,
    SourcePage,
    StopReason,
    SyncJobSpec,
    SyncMode,
    VoyagerConnections,
    run_connections_sync,
)
from netkeeper.linkedin.voyager import CONNECTIONS_PATH, parse_connections_page

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
        return [c.urn for page in self.pages for c in page.connections]


async def _run(
    fetch: FakeVoyagerFetch,
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
        VoyagerConnections(fetch),
        gate,
        on_page=sink.page,
        on_progress=sink.progress,
        clock=lambda: NOW,
    )
    return result, sink, gate


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
    fetch = FakeVoyagerFetch(list(PEOPLE))

    result, sink, gate = await _run(fetch)

    assert fetch.starts == [0, 3, 6, 9]
    assert sink.urns == [person.urn for person in PEOPLE]
    assert [page.number for page in sink.pages] == [0, 1, 2, 3]
    assert result.reason is StopReason.END_OF_LIST
    assert result.complete
    assert result.seen_urns == {person.urn for person in PEOPLE}
    assert result.total == 10 and result.pages == 4 and result.connections == 10
    assert gate.asked == [0, 1, 2, 3]
    assert gate.pauses == 3  # between pages, never before the first
    assert all(page.observed_at == NOW for page in sink.pages)


async def test_every_request_is_the_connections_endpoint_at_the_asked_size() -> None:
    fetch = FakeVoyagerFetch(list(PEOPLE))
    await _run(fetch, page_size=4)
    assert {request.path for request in fetch.requests} == {CONNECTIONS_PATH}
    assert [request.query["count"] for request in fetch.requests] == ["4", "4", "4"]
    assert fetch.starts == [0, 4, 8]


async def test_a_list_that_ends_on_a_page_boundary_needs_no_empty_page() -> None:
    fetch = FakeVoyagerFetch(list(PEOPLE[:9]))
    result, _, _ = await _run(fetch)
    assert fetch.starts == [0, 3, 6]
    assert result.complete


async def test_a_full_sync_ignores_known_urns_by_refusing_them() -> None:
    with pytest.raises(ValueError, match="incremental"):
        SyncJobSpec(mode=SyncMode.FULL, page_budget=5, known_urns=frozenset({PEOPLE[0].urn}))


async def test_an_empty_list_is_the_end_of_the_list() -> None:
    fetch = FakeVoyagerFetch([])
    result, sink, _ = await _run(fetch)
    assert result.reason is StopReason.END_OF_LIST
    assert fetch.starts == [0]
    assert sink.urns == []


# --- what stops a run early, and why none of it is complete -----------------------


async def test_the_page_budget_stops_a_full_sync_and_it_is_not_complete() -> None:
    fetch = FakeVoyagerFetch(list(PEOPLE))
    result, sink, _ = await _run(fetch, page_budget=2)
    assert fetch.starts == [0, 3]
    assert result.reason is StopReason.PAGE_BUDGET
    assert not result.complete
    assert len(sink.pages) == 2


async def test_a_zero_page_budget_fetches_nothing() -> None:
    fetch = FakeVoyagerFetch(list(PEOPLE))
    result, _, gate = await _run(fetch, page_budget=0)
    assert fetch.requests == []
    assert gate.asked == []
    assert result.reason is StopReason.PAGE_BUDGET


async def test_the_gate_is_asked_before_each_page_and_its_refusal_fetches_nothing() -> None:
    fetch = FakeVoyagerFetch(list(PEOPLE))
    result, sink, gate = await _run(fetch, gate=Gate(allow=2))
    assert gate.asked == [0, 1, 2]
    assert fetch.starts == [0, 3]  # the refused third page was never requested
    assert result.reason is StopReason.BUDGET
    assert not result.complete
    assert len(sink.pages) == 2


@pytest.mark.parametrize(
    ("scripted", "outcome"),
    [
        pytest.param(THROTTLED, Outcome.THROTTLED, id="throttled"),
        pytest.param(CHECKPOINT, Outcome.CHECKPOINT, id="checkpoint"),
        pytest.param(LOGGED_OUT, Outcome.LOGGED_OUT, id="logged-out"),
        pytest.param(UNRECOGNIZED, Outcome.ROUTE_CHANGED, id="route-changed"),
        pytest.param(Scripted(404, "{}"), Outcome.NOT_FOUND, id="not-found"),
        pytest.param(Scripted(500, "oops"), Outcome.ROUTE_CHANGED, id="server-error"),
    ],
)
async def test_the_first_non_ok_response_stops_the_run_with_no_retry(
    scripted: Scripted, outcome: Outcome
) -> None:
    fetch = FakeVoyagerFetch(list(PEOPLE), script={2: scripted})

    result, sink, gate = await _run(fetch)

    assert fetch.starts == [0, 3, 6]  # the third page failed and nothing was asked again
    assert result.reason is StopReason.RESPONSE
    assert result.outcome is outcome
    assert result.final_url == scripted.final_url
    assert not result.complete
    assert len(sink.pages) == 2
    assert result.seen_urns == {person.urn for person in PEOPLE[:6]}
    assert gate.asked == [0, 1, 2]


async def test_a_checkpoint_on_the_first_page_is_never_retried() -> None:
    fetch = FakeVoyagerFetch(list(PEOPLE), script={0: CHECKPOINT})
    result, sink, _ = await _run(fetch)
    assert len(fetch.requests) == 1
    assert result.outcome is Outcome.CHECKPOINT
    assert sink.pages == []


async def test_a_non_ok_response_never_reaches_the_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """A checkpoint page is classified, not parsed (#150's done-when, spec 9.7)."""
    parsed: list[str] = []

    def spy(body: str) -> object:
        parsed.append(body)
        return parse_connections_page(body)

    monkeypatch.setattr(job, "parse_connections_page", spy)
    for scripted in (THROTTLED, CHECKPOINT, LOGGED_OUT):
        source = VoyagerConnections(FakeVoyagerFetch(list(PEOPLE), script={0: scripted}))
        answer = await source.fetch_page(start=0, count=3)
        assert answer.page is None
    assert parsed == []
    answer = await VoyagerConnections(FakeVoyagerFetch(list(PEOPLE))).fetch_page(start=0, count=3)
    assert answer.page is not None and len(parsed) == 1


async def test_a_page_at_an_offset_nobody_asked_for_is_a_changed_route() -> None:
    fetch = FakeVoyagerFetch(list(PEOPLE), start_offset=1)
    result, sink, _ = await _run(fetch)
    assert result.outcome is Outcome.ROUTE_CHANGED
    assert sink.pages == []


async def test_an_empty_page_before_the_total_is_not_complete() -> None:
    """A list that stops paging short of its own total ends the run but ages nobody."""
    empty_early = Scripted(200, page_body([], start=6, count=3, total=10))
    fetch = FakeVoyagerFetch(list(PEOPLE), script={2: empty_early})

    result, _, _ = await _run(fetch)

    assert fetch.starts == [0, 3, 6]
    assert result.reason is StopReason.END_OF_LIST
    assert len(result.seen_urns) == 6 and result.total == 10
    assert not result.complete


async def test_a_list_that_grew_during_the_run_is_not_complete() -> None:
    """A connection accepted mid-run lands at the top, behind the run: seen 10 of 11."""
    people = list(PEOPLE)
    fetch = FakeVoyagerFetch(people)
    newcomer = Person(111, "Zanele", "Oyelaran", "Analyst at Pretend Freight", 1_701_000_000_000)

    async def grow(page: ConnectionsPage) -> None:
        if page.number == 0:
            people.insert(0, newcomer)

    result = await run_connections_sync(
        SyncJobSpec(mode=SyncMode.FULL, page_budget=50, page_size=3),
        VoyagerConnections(fetch),
        Gate(),
        on_page=grow,
        clock=lambda: NOW,
    )

    assert result.reason is StopReason.END_OF_LIST
    assert newcomer.urn not in result.seen_urns
    assert result.seen_urns == {person.urn for person in PEOPLE}
    assert result.total == 11
    assert not result.complete


async def test_a_removal_during_the_run_skips_a_row_the_count_cannot_see() -> None:
    """The documented limit: the skipped person looks missing from a complete sync.

    ``tests/test_crm_apply.py`` shows why that is survivable: one miss never
    disconnects anyone.
    """
    people = list(PEOPLE)
    fetch = FakeVoyagerFetch(people)

    async def remove_a_seen_one(page: ConnectionsPage) -> None:
        if page.number == 0:
            del people[0]

    result = await run_connections_sync(
        SyncJobSpec(mode=SyncMode.FULL, page_budget=50, page_size=3),
        VoyagerConnections(fetch),
        Gate(),
        on_page=remove_a_seen_one,
        clock=lambda: NOW,
    )

    assert PEOPLE[3].urn not in result.seen_urns  # moved up to offset 2, already read
    assert result.complete


# --- incremental sync ------------------------------------------------------------


def _known(*people: Person) -> frozenset[str]:
    return frozenset(person.urn for person in people)


async def test_incremental_stops_at_the_first_page_that_is_all_known() -> None:
    """Three new people on top, then a page of known ones: two pages and stop.

    Page 0 holds the three newest, all unknown; page 1 holds three known ones,
    so the run reads it and stops before page 2.
    """
    fetch = FakeVoyagerFetch(list(PEOPLE))
    known = _known(*PEOPLE[3:])  # everyone but the three newest

    result, sink, _ = await _run(fetch, mode=SyncMode.INCREMENTAL, known=known)

    assert fetch.starts == [0, 3]
    assert result.reason is StopReason.CAUGHT_UP
    assert not result.complete
    assert sink.urns == [person.urn for person in PEOPLE[:6]]


async def test_incremental_keeps_going_past_a_page_with_one_unknown_urn() -> None:
    """One unknown person in the middle of page 1 keeps page 2 coming."""
    fetch = FakeVoyagerFetch(list(PEOPLE))
    known = _known(*PEOPLE[1:4], *PEOPLE[5:])  # PEOPLE[0] (page 0) and PEOPLE[4] (page 1) are new

    result, _, _ = await _run(fetch, mode=SyncMode.INCREMENTAL, known=known)

    assert fetch.starts == [0, 3, 6]
    assert result.reason is StopReason.CAUGHT_UP


async def test_incremental_with_nothing_known_reads_to_the_end_and_is_still_not_complete() -> None:
    """Reading every page in incremental mode ages nobody: only a full sync may."""
    fetch = FakeVoyagerFetch(list(PEOPLE))
    result, _, _ = await _run(fetch, mode=SyncMode.INCREMENTAL, known=frozenset())
    assert fetch.starts == [0, 3, 6, 9]
    assert result.reason is StopReason.END_OF_LIST
    assert not result.complete


async def test_incremental_with_everything_known_reads_one_page() -> None:
    fetch = FakeVoyagerFetch(list(PEOPLE))
    result, sink, _ = await _run(fetch, mode=SyncMode.INCREMENTAL, known=_known(*PEOPLE))
    assert fetch.starts == [0]
    assert result.reason is StopReason.CAUGHT_UP
    assert len(sink.pages) == 1  # the page is still handed over: headlines refresh


# --- progress ------------------------------------------------------------------------


async def test_progress_counts_pages_and_says_why_it_stopped() -> None:
    fetch = FakeVoyagerFetch(list(PEOPLE), script={2: THROTTLED})
    _, sink, _ = await _run(fetch)
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
