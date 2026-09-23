"""Connections-list pages for sync tests: hand-built people, Voyager's page shape, a fake fetch.

Everyone here is invented: fake names, fake ``ACoAAFAKE`` URNs, fake slugs,
companies that do not exist, no email addresses or phone numbers. The page
shape is exactly ``tests/fixtures/voyager/connections_page.json``'s, and
``tests/test_linkedin_connections.py`` checks that :func:`page_body` rebuilds
that fixture byte for byte (as parsed JSON), so a builder that drifted from the
fixture would fail there rather than test the sync against a shape LinkedIn
never sends.

:class:`FakeVoyagerFetch` answers a ``VoyagerRequest`` from an in-memory
connections list by the request's ``start`` and ``count``, the way the real
endpoint pages, and can be told to answer one call with something else (a
throttle, a checkpoint page, an unrecognized body). It records every request
and never opens a socket.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from netkeeper.linkedin.voyager import VoyagerRequest, VoyagerResponse

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


@dataclass(slots=True)
class FakeVoyagerFetch:
    """A ``VoyagerFetch`` over an in-memory list. ``script[i]`` replaces call ``i``'s answer."""

    people: list[Person]
    script: dict[int, Scripted] = field(default_factory=dict)
    requests: list[VoyagerRequest] = field(default_factory=list)
    start_offset: int = 0  # added to the answered ``start``, to fake a page LinkedIn misnumbers
    #: What ``paging.total`` says: None tells the truth; an int, or a function of the
    #: requested ``start``, lies. Every page's ``elements`` stay honest either way.
    total: int | Callable[[int], int] | None = None

    async def __call__(self, request: VoyagerRequest) -> VoyagerResponse:
        call = len(self.requests)
        self.requests.append(request)
        scripted = self.script.get(call)
        if scripted is not None:
            return VoyagerResponse(scripted.status, scripted.body, scripted.final_url)
        start = int(request.query["start"])
        count = int(request.query["count"])
        body = page_body(
            self.people[start : start + count],
            start=start + self.start_offset,
            count=count,
            total=self._total(start),
        )
        return VoyagerResponse(200, body, CONNECTIONS_URL)

    def _total(self, start: int) -> int:
        if self.total is None:
            return len(self.people)
        if isinstance(self.total, int):
            return self.total
        return self.total(start)

    @property
    def starts(self) -> list[int]:
        return [int(request.query["start"]) for request in self.requests]
