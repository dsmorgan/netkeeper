"""Connections-list pages for sync tests: hand-built people, Voyager's page shape, a fake source.

Everyone here is invented: fake names, fake ``ACoAAFAKE`` URNs, fake slugs,
companies that do not exist, no email addresses or phone numbers. The page
shape is exactly ``tests/fixtures/voyager/connections_page.json``'s, and
``tests/test_linkedin_connections.py`` checks that :func:`page_body` rebuilds
that fixture byte for byte (as parsed JSON), so a builder that drifted from the
fixture would fail there rather than test the sync against a shape LinkedIn
never sends.

:class:`FakeConnectionsSource` is a neutral, in-memory
:class:`~netkeeper.linkedin.connections.ConnectionsSource` with no request
shape of its own, for tests that drive ``run_connections_sync`` or
``services.connections_sync.sync_connections`` and only need pages, not a
transport (``tests/test_linkedin_connections.py``,
``tests/test_connections_sync.py``). It reuses :func:`classify` and
:func:`~netkeeper.linkedin.voyager.parse_connections_page` to interpret a
scripted :class:`Scripted` answer, so the fixtures below (``THROTTLED``,
``CHECKPOINT``, ...) drive it exactly as they drove ``VoyagerConnections``
before #189 retired that wrapper along with the in-page connections endpoint
it read (unused since P2-17's move onto what the page loads).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from netkeeper.linkedin.classify import Outcome, classify
from netkeeper.linkedin.connections import SourcePage
from netkeeper.linkedin.voyager import (
    ConnectionsPageResult,
    ConnectionSummary,
    RouteChanged,
    parse_connections_page,
)

CONNECTIONS_URL = "https://www.linkedin.com/voyager/api/relationships/dash/connections"


@dataclass(frozen=True, slots=True)
class Person:
    """One invented connection."""

    n: int
    first: str
    last: str
    headline: str | None
    created_ms: int | None = 1_690_000_000_000
    public_id: str | None = None
    #: The URN scheme; a test swaps it to fake LinkedIn renumbering every profile.
    urn_prefix: str = "ACoAAFAKE"

    @property
    def urn(self) -> str:
        return f"urn:li:fsd_profile:{self.urn_prefix}{self.n:07d}"

    @property
    def slug(self) -> str:
        return self.public_id or f"{self.first.lower()}-fake-{self.last.lower()}-{self.n:04d}"


#: Ten invented people at different (invented) companies, newest connection first.
PEOPLE: tuple[Person, ...] = (
    Person(101, "Priya", "Okafor", "Data engineer at Fictional Robotics Co", 1_700_000_000_000),
    Person(102, "Mateo", "Lindqvist", "Head of design at Acme Testing Group", 1_699_000_000_000),
    Person(103, "Hana", "Brennan", None, 1_698_000_000_000),
    Person(104, "Tomasz", "Adeyemi", "Recruiter at Placeholder Partners", 1_697_000_000_000),
    Person(105, "Aiko", "Villanueva", "Founder, Imaginary Analytics", 1_696_000_000_000),
    Person(106, "Luca", "Haddad", "SRE at Nonexistent Networks", 1_695_000_000_000),
    Person(107, "Sofia", "Kowalczyk", "PM at Example Widgets Ltd", 1_694_000_000_000),
    Person(108, "Kwame", "Ferreira", "Staff engineer at Sample Systems", None),
    Person(109, "Ingrid", "Moreau", "Counsel at Hypothetical Holdings", 1_692_000_000_000),
    Person(110, "Ravi", "Nakamura", "CTO at Madeup Mobility", 1_691_000_000_000),
)


def page_body(people: Sequence[Person], *, start: int, count: int, total: int) -> str:
    """One connections page in the normalized shape ``parse_connections_page`` reads."""
    elements: list[dict[str, Any]] = []
    included: list[dict[str, Any]] = []
    for person in people:
        element: dict[str, Any] = {"*connectedMemberResolutionResult": person.urn}
        if person.created_ms is not None:
            element["createdAt"] = person.created_ms
        elements.append(element)
        profile: dict[str, Any] = {
            "entityUrn": person.urn,
            "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
            "publicIdentifier": person.slug,
            "firstName": person.first,
            "lastName": person.last,
        }
        if person.headline is not None:
            profile["headline"] = person.headline
        included.append(profile)
    return json.dumps(
        {
            "data": {
                "elements": elements,
                "paging": {"start": start, "count": count, "total": total},
            },
            "included": included,
        }
    )


@dataclass(frozen=True, slots=True)
class Scripted:
    """What to answer instead of a page, for one call."""

    status: int
    body: str
    final_url: str = CONNECTIONS_URL


THROTTLED = Scripted(429, "Too many requests")
CHECKPOINT = Scripted(
    200,
    "<html><body>Let's do a quick security check</body></html>",
    "https://www.linkedin.com/checkpoint/challenge/AgFAKE?ctx=secret-token",
)
LOGGED_OUT = Scripted(200, "<html>Sign in</html>", "https://www.linkedin.com/authwall?trk=x")
UNRECOGNIZED = Scripted(200, json.dumps({"data": {"somethingElse": []}}))


def _from_person(person: Person) -> ConnectionSummary:
    """One invented person as the row a live source would report."""
    return ConnectionSummary(
        urn=person.urn,
        public_id=person.slug,
        first_name=person.first,
        last_name=person.last,
        headline=person.headline,
        connected_at=(
            None
            if person.created_ms is None
            else datetime.fromtimestamp(person.created_ms / 1000, tz=UTC)
        ),
    )


@dataclass(slots=True)
class FakeConnectionsSource:
    """A neutral ``ConnectionsSource`` over an in-memory people list (#189 item 4).

    Answers page ``start:start+count`` from ``people`` directly -- no request
    shape, no transport -- so a test that drives ``run_connections_sync`` or
    ``services.connections_sync.sync_connections`` through this exercises
    those loops' own paging, budget, heat, and aging rules, the same for every
    source, rather than one source's translation into and out of its
    transport. This is what replaced driving those tests through
    ``VoyagerConnections`` over :class:`FakeVoyagerFetch`: the fixtures below
    (``THROTTLED``, ``CHECKPOINT``, ...) still work unchanged, because
    ``script[i]`` still classifies a :class:`Scripted` answer the ordinary way
    (spec 9.7) and parses an ``Ok`` one the ordinary way
    (:func:`~netkeeper.linkedin.voyager.parse_connections_page`) -- only the
    honest, unscripted path skips both, building the page straight from
    ``people``. ``script[i]`` also accepts a hand-built ``SourcePage``
    directly, for a page shaped differently than the default honest slice (a
    short page mid-list, an empty page, one reporting the wrong start).

    ``total`` is what an honest page reports: ``None`` tells the truth
    (``len(people)``); an int, or a function of the requested ``start``, lies,
    the way spec 9.8's "a total that lies" tests need. ``start_offset``
    reports every honest page's own ``start`` shifted from what was asked, the
    way a route that skipped or repeated an offset would.
    """

    people: list[Person]
    script: dict[int, Scripted | SourcePage] = field(default_factory=dict)
    start_offset: int = 0
    total: int | Callable[[int], int] | None = None
    requests: list[tuple[int, int]] = field(default_factory=list, init=False)

    @property
    def endpoint(self) -> str:
        return "fake-connections"

    @property
    def starts(self) -> list[int]:
        return [start for start, _ in self.requests]

    async def fetch_page(self, *, start: int, count: int) -> SourcePage:
        index = len(self.requests)
        self.requests.append((start, count))
        scripted = self.script.get(index)
        if scripted is not None:
            return scripted if isinstance(scripted, SourcePage) else _classify_scripted(scripted)
        people = self.people[start : start + count]
        return SourcePage(
            outcome=Outcome.OK,
            final_url=CONNECTIONS_URL,
            page=ConnectionsPageResult(
                connections=tuple(_from_person(p) for p in people),
                start=start + self.start_offset,
                count=count,
                total=self._total(start),
            ),
        )

    def _total(self, start: int) -> int:
        if self.total is None:
            return len(self.people)
        if isinstance(self.total, int):
            return self.total
        return self.total(start)


def _classify_scripted(scripted: Scripted) -> SourcePage:
    """A :class:`Scripted` answer, read the way ``VoyagerConnections`` used to: classify
    first (spec 9.7), and only an ``Ok`` status is ever handed to the parser."""
    outcome = classify(scripted.status, scripted.final_url, scripted.body)
    if outcome is not Outcome.OK:
        return SourcePage(outcome=outcome, final_url=scripted.final_url)
    try:
        page = parse_connections_page(scripted.body)
    except RouteChanged:
        return SourcePage(outcome=Outcome.ROUTE_CHANGED, final_url=scripted.final_url)
    return SourcePage(outcome=Outcome.OK, final_url=scripted.final_url, page=page)
