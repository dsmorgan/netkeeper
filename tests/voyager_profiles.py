"""Profile visits for enrichment tests: invented people, Voyager's profile shapes, a fake tab.

Everyone here is invented: fake names, fake ``ACoAAFAKE`` URNs, fake slugs,
companies that do not exist, ``example.test`` addresses, ``555-01xx`` numbers.
:func:`details_body` and :func:`contact_info_body` build exactly the shapes of
``tests/fixtures/voyager/profile_details.json`` and ``contact_info.json``, and
``tests/test_linkedin_enrich.py`` checks that they rebuild those fixtures (as
parsed JSON), so a builder that drifted would fail there rather than test
enrichment against a shape LinkedIn never sends.

:class:`FakeBrowser` is one tab: ``navigate`` and ``scroll`` record what the
job asked for, and ``fetch`` answers ``VoyagerRequest`` values from an
in-memory set of profiles by slug. Any single call can be scripted to answer
something else (a throttle, a checkpoint page, a 404). Every call lands in
``events`` in order, which is what a test of the request pattern reads. Nothing
opens a socket.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from netkeeper.linkedin.enrich import BrowserProfiles
from netkeeper.linkedin.pacing import ScrollPlan
from netkeeper.linkedin.voyager import VoyagerRequest, VoyagerResponse

ORIGIN = "https://www.linkedin.com"
POSITION_TYPE = "com.linkedin.voyager.dash.identity.profile.Position"
EDUCATION_TYPE = "com.linkedin.voyager.dash.identity.profile.Education"


@dataclass(frozen=True, slots=True)
class Job:
    """One experience entry. ``end`` of ``None`` is a current position."""

    title: str
    company: str
    start: tuple[int, int | None] | None = None
    end: tuple[int, int | None] | None = None


@dataclass(frozen=True, slots=True)
class School:
    school: str
    degree: str | None = None
    field_of_study: str | None = None
    start_year: int | None = None
    end_year: int | None = None


@dataclass(frozen=True, slots=True)
class Profile:
    """One invented person's profile and contact info."""

    n: int
    first: str
    last: str
    headline: str | None = None
    location: str | None = None
    jobs: tuple[Job, ...] = ()
    schools: tuple[School, ...] = ()
    email: str | None = None
    phones: tuple[str, ...] = ()
    websites: tuple[str, ...] = ()
    twitter: tuple[str, ...] = ()
    public_id: str | None = None
    urn_prefix: str = "ACoAAFAKE"

    @property
    def urn(self) -> str:
        return f"urn:li:fsd_profile:{self.urn_prefix}{self.n:07d}"

    @property
    def slug(self) -> str:
        return self.public_id or f"{self.first.lower()}-fake-{self.last.lower()}-{self.n:04d}"


def _date(value: tuple[int, int | None]) -> dict[str, int]:
    year, month = value
    return {"year": year} if month is None else {"year": year, "month": month}


def details_body(profile: Profile) -> str:
    """A profile-details response in the normalized shape ``parse_profile_details`` reads."""
    data: dict[str, Any] = {
        "entityUrn": profile.urn,
        "publicIdentifier": profile.slug,
        "firstName": profile.first,
        "lastName": profile.last,
    }
    if profile.headline is not None:
        data["headline"] = profile.headline
    if profile.location is not None:
        data["geoLocationName"] = profile.location
    included: list[dict[str, Any]] = []
    fake_id = profile.urn.rsplit(":", 1)[1]
    for i, job in enumerate(profile.jobs, start=1):
        date_range: dict[str, Any] = {}
        if job.start is not None:
            date_range["start"] = _date(job.start)
        if job.end is not None:
            date_range["end"] = _date(job.end)
        included.append(
            {
                "entityUrn": f"urn:li:fsd_position:({fake_id},{i})",
                "$type": POSITION_TYPE,
                "title": job.title,
                "companyName": job.company,
                "dateRange": date_range,
            }
        )
    for i, school in enumerate(profile.schools, start=1):
        entity: dict[str, Any] = {
            "entityUrn": f"urn:li:fsd_education:({fake_id},{i})",
            "$type": EDUCATION_TYPE,
            "schoolName": school.school,
        }
        if school.degree is not None:
            entity["degreeName"] = school.degree
        if school.field_of_study is not None:
            entity["fieldOfStudy"] = school.field_of_study
        school_range: dict[str, Any] = {}
        if school.start_year is not None:
            school_range["start"] = {"year": school.start_year}
        if school.end_year is not None:
            school_range["end"] = {"year": school.end_year}
        entity["dateRange"] = school_range
        included.append(entity)
    return json.dumps({"data": data, "included": included})


def contact_info_body(profile: Profile) -> str:
    """A ``profileContactInfo`` response in the flat shape ``parse_contact_info`` reads."""
    body: dict[str, Any] = {"emailAddress": profile.email}
    if profile.phones:
        body["phoneNumbers"] = [{"number": number, "type": "MOBILE"} for number in profile.phones]
    if profile.websites:
        body["websites"] = [
            {"url": url, "category": {"type": "PERSONAL"}} for url in profile.websites
        ]
    if profile.twitter:
        body["twitterHandles"] = [{"name": handle} for handle in profile.twitter]
    return json.dumps(body)


#: A handful of invented people with varied, partly missing, profiles.
PROFILES: tuple[Profile, ...] = (
    Profile(
        101,
        "Priya",
        "Okafor",
        headline="Staff data engineer at Fictional Robotics Co",
        location="Faketown, State of Example",
        jobs=(
            Job("Staff Data Engineer", "Fictional Robotics Co", start=(2023, 4)),
            Job("Data Engineer", "Placeholder Partners", start=(2019, 1), end=(2023, 3)),
        ),
        schools=(School("Fictional State University", "B.S.", "Statistics", 2012, 2016),),
        email="priya.fake.okafor@example.test",
        phones=("+1-555-0101",),
        websites=("https://priya-fake-okafor.example.test",),
        twitter=("priyafakeokafor",),
    ),
    Profile(
        102,
        "Mateo",
        "Lindqvist",
        headline="Head of design at Acme Testing Group",
        location="Sampleville",
        jobs=(Job("Head of Design", "Acme Testing Group", start=(2021, 6)),),
        websites=("https://github.com/mateo-fake",),
    ),
    Profile(103, "Hana", "Brennan"),  # a profile with almost nothing on it
    Profile(
        104,
        "Tomasz",
        "Adeyemi",
        headline="Recruiter at Placeholder Partners",
        jobs=(Job("Recruiter", "Placeholder Partners", start=(2020, None)),),
        email="tomasz.fake@example.test",
    ),
    Profile(
        105,
        "Aiko",
        "Villanueva",
        headline="Founder, Imaginary Analytics",
        location="Mocktown",
        jobs=(Job("Founder", "Imaginary Analytics", start=(2018, 9)),),
        phones=("555-0105",),
    ),
)


@dataclass(frozen=True, slots=True)
class Scripted:
    """What to answer instead of the profile, for one call."""

    status: int
    body: str
    final_url: str | None = None


THROTTLED = Scripted(429, "Too many requests")
CHECKPOINT = Scripted(
    200,
    "<html><body>Let's do a quick security check</body></html>",
    "https://www.linkedin.com/checkpoint/challenge/AgFAKE?ctx=secret-token",
)
LOGGED_OUT = Scripted(200, "<html>Sign in</html>", "https://www.linkedin.com/authwall?trk=x")
#: JSON, so it classifies ``Ok``, but not an object: neither parser recognizes it.
UNRECOGNIZED = Scripted(200, json.dumps([{"somethingElse": []}]))
NOT_FOUND = Scripted(404, "{}")
BAD_REQUEST = Scripted(400, "{}")


@dataclass(frozen=True, slots=True)
class Landed:
    """The tab after a navigation, as far as the job reads it: where it landed."""

    url: str


@dataclass(slots=True)
class FakeBrowser:
    """One tab over an in-memory set of profiles. See the module docstring.

    ``events`` records ``("goto", url)``, ``("scroll", plan)``,
    ``("details", slug)`` and ``("contact_info", slug)`` in the order they
    happened. ``script[i]`` replaces the answer of fetch call ``i`` (counting
    both endpoints); ``redirect`` maps a slug to the url a navigation to it
    lands on. ``on_event`` runs after every event is recorded, so a test can
    act mid-run (set the cancel flag, spend budget elsewhere).
    """

    profiles: dict[str, Profile]
    script: dict[int, Scripted] = field(default_factory=dict)
    redirect: dict[str, str] = field(default_factory=dict)
    events: list[tuple[str, object]] = field(default_factory=list)
    fetches: int = 0
    on_event: Callable[[str, object], None] | None = None

    @classmethod
    def of(cls, profiles: tuple[Profile, ...] | list[Profile], **kwargs: Any) -> FakeBrowser:
        return cls(profiles={p.slug: p for p in profiles}, **kwargs)

    def source(self, origin: str = ORIGIN) -> BrowserProfiles:
        return BrowserProfiles(
            navigate=self.navigate, scroll_page=self.scroll, fetch=self.fetch, origin=origin
        )

    def _record(self, kind: str, value: object) -> None:
        self.events.append((kind, value))
        if self.on_event is not None:
            self.on_event(kind, value)

    async def navigate(self, url: str) -> Landed:
        self._record("goto", url)
        slug = unquote(urlsplit(url).path.split("/")[2])
        return Landed(self.redirect.get(slug, url))

    async def scroll(self, plan: ScrollPlan) -> None:
        self._record("scroll", plan)

    async def fetch(self, request: VoyagerRequest) -> VoyagerResponse:
        call = self.fetches
        self.fetches += 1
        if request.path.endswith("/profileContactInfo"):
            slug = request.path.split("/")[-2]
            kind = "contact_info"
        else:
            slug = request.query["memberIdentity"]
            kind = "details"
        self._record(kind, slug)
        url = f"{ORIGIN}{request.path}"
        scripted = self.script.get(call)
        if scripted is not None:
            return VoyagerResponse(scripted.status, scripted.body, scripted.final_url or url)
        profile = self.profiles.get(slug)
        if profile is None:
            return VoyagerResponse(404, "{}", url)
        body = details_body(profile) if kind == "details" else contact_info_body(profile)
        return VoyagerResponse(200, body, url)

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]

    def visited(self) -> list[str]:
        """The slugs navigated to, in order."""
        return [
            unquote(urlsplit(str(url)).path.split("/")[2])
            for kind, url in self.events
            if kind == "goto"
        ]
