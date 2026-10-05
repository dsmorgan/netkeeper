"""Profile visits for enrichment tests: invented people and a fake profile source.

Everyone here is invented: fake names, fake ``ACoAAFAKE`` URNs, fake slugs,
companies that do not exist, ``example.test`` addresses, ``555-01xx`` numbers.
:func:`details_of` and :func:`contact_info_of` are what a visit to each person
harvests, as the parsers in :mod:`netkeeper.linkedin.flagship_profile` would hand
them back (#190); ``tests/test_page_profiles.py`` drives the real source over real
flight payloads, and this module stands in for it where a test is about the job or
the runner, not the page.

:class:`FakeBrowser` is one tab behind the :class:`~netkeeper.linkedin.enrich.ProfileSource`
seam: ``open_profile`` records the navigation, ``scroll`` the plan,
``read_profile`` answers the person's details, and ``read_contact_info`` records the
scroll back up, waits the pause through the ``sleep`` it was given, records the one
click, and answers the contact info. Any step can be scripted to answer something
else (a throttle, a checkpoint, an unreadable shape, a not-found profile). Every step
lands in ``events`` in order, which is what a test of the visit pattern reads.
Nothing opens a socket.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.enrich import Answer, UnreadableCause, masked
from netkeeper.linkedin.pacing import ScrollPlan
from netkeeper.linkedin.voyager import (
    ContactInfo,
    EducationEntry,
    PositionEntry,
    ProfileDetails,
)

ORIGIN = "https://www.linkedin.com"


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


def details_of(profile: Profile) -> ProfileDetails:
    """What a visit to ``profile`` reads from the page: the profile's details."""
    return ProfileDetails(
        urn=profile.urn,
        public_id=profile.slug,
        first_name=profile.first,
        last_name=profile.last,
        headline=profile.headline,
        location=profile.location,
        positions=tuple(
            PositionEntry(
                title=job.title,
                company=job.company,
                start_year=job.start[0] if job.start else None,
                start_month=job.start[1] if job.start else None,
                end_year=job.end[0] if job.end else None,
                end_month=job.end[1] if job.end else None,
            )
            for job in profile.jobs
        ),
        education=tuple(
            EducationEntry(
                school=school.school,
                degree=school.degree,
                field_of_study=school.field_of_study,
                start_year=school.start_year,
                end_year=school.end_year,
            )
            for school in profile.schools
        ),
    )


def contact_info_of(profile: Profile) -> ContactInfo:
    """What the Contact info overlay shows for ``profile``."""
    return ContactInfo(
        emails=() if profile.email is None else (profile.email,),
        phones=profile.phones,
        websites=profile.websites,
        twitter_handles=profile.twitter,
    )


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
    """What to answer instead of the profile, for one step."""

    outcome: Outcome
    final_url: str = f"{ORIGIN}/in/_/"
    unparsed: bool = False
    lost: str | None = None  # #197: the answer's body could not be read
    cause: UnreadableCause | None = None  # #405: why an unparsed answer could not be read


THROTTLED = Scripted(Outcome.THROTTLED)
CHECKPOINT = Scripted(
    Outcome.CHECKPOINT, "https://www.linkedin.com/checkpoint/challenge/AgFAKE?ctx=secret-token"
)
LOGGED_OUT = Scripted(Outcome.LOGGED_OUT, "https://www.linkedin.com/authwall?trk=x")
#: The page answered, in a shape the parser does not know: one unreadable profile.
UNRECOGNIZED = Scripted(Outcome.ROUTE_CHANGED, unparsed=True)
NOT_FOUND = Scripted(Outcome.NOT_FOUND)
#: A ``RouteChanged`` that is the route's, not the profile's (a 400, a 500): stops the run.
BAD_REQUEST = Scripted(Outcome.ROUTE_CHANGED)


async def _no_sleep(seconds: float) -> None:
    return None


@dataclass(slots=True)
class FakeBrowser:
    """One tab over an in-memory set of profiles. See the module docstring.

    ``events`` records ``("goto", url)``, ``("scroll", plan)``, ``("details", slug)``,
    ``("back", plan)``, and ``("click", slug)`` in the order
    they happened. ``script[i]`` replaces the answer of read ``i``, counting the
    profile read and the contact-info read of every visit (two per visit that clicks,
    the way the in-page fetches were once counted); ``landing[v]`` replaces visit
    ``v``'s navigation. ``redirect`` maps a slug to the url a navigation to it lands
    on; ``urns`` gives a slug's profile another URN (a slug that changed hands).
    ``on_event`` runs after every event is recorded, so a test can act mid-run (set
    the cancel flag, spend budget elsewhere).
    """

    profiles: dict[str, Profile]
    script: dict[int, Scripted] = field(default_factory=dict)
    landing: dict[int, Scripted] = field(default_factory=dict)
    redirect: dict[str, str] = field(default_factory=dict)
    urns: dict[str, str] = field(default_factory=dict)
    events: list[tuple[str, object]] = field(default_factory=list)
    fetches: int = 0
    navigations: int = 0
    on_event: Callable[[str, object], None] | None = None
    sleep: Callable[[float], Awaitable[None]] = _no_sleep
    _slug: str = ""

    @classmethod
    def of(cls, profiles: tuple[Profile, ...] | list[Profile], **kwargs: Any) -> FakeBrowser:
        return cls(profiles={p.slug: p for p in profiles}, **kwargs)

    def source(self, sleep: Callable[[float], Awaitable[None]] | None = None) -> FakeBrowser:
        """This fake is its own source; ``sleep`` waits out the pause before each click."""
        if sleep is not None:
            self.sleep = sleep
        return self

    def _record(self, kind: str, value: object) -> None:
        self.events.append((kind, value))
        if self.on_event is not None:
            self.on_event(kind, value)

    async def open_profile(self, public_id: str) -> Answer[None]:
        url = f"{ORIGIN}/in/{quote(public_id, safe='')}/"
        self._record("goto", url)
        visit = self.navigations
        self.navigations += 1
        landed = self.redirect.get(public_id, url)
        scripted = self.landing.get(visit)
        if scripted is not None:
            return Answer(
                scripted.outcome,
                masked(scripted.final_url),
                unparsed=scripted.unparsed,
                lost=scripted.lost,
                cause=scripted.cause,
            )
        self._slug = unquote(urlsplit(landed).path.split("/")[2])
        if self._slug not in self.profiles:
            return Answer(Outcome.NOT_FOUND, masked(landed))
        return Answer(Outcome.OK, masked(landed))

    async def scroll(self, plan: ScrollPlan) -> None:
        self._record("scroll", plan)

    async def read_profile(self, public_id: str) -> Answer[ProfileDetails]:
        self._record("details", self._slug)
        call = self.fetches
        self.fetches += 1
        scripted = self.script.get(call)
        if scripted is not None:
            return Answer(
                scripted.outcome,
                masked(scripted.final_url),
                unparsed=scripted.unparsed,
                lost=scripted.lost,
                cause=scripted.cause,
            )
        details = details_of(self.profiles[self._slug])
        if self._slug in self.urns:
            details = ProfileDetails(
                urn=self.urns[self._slug],
                public_id=details.public_id,
                first_name=details.first_name,
                last_name=details.last_name,
                headline=details.headline,
                location=details.location,
                positions=details.positions,
                education=details.education,
            )
        return Answer(Outcome.OK, f"{ORIGIN}/in/_/", details)

    async def read_contact_info(
        self, profile: ProfileDetails, *, back: ScrollPlan, pause_s: float
    ) -> Answer[ContactInfo]:
        self._record("back", back)
        await self.sleep(pause_s)
        self._record("click", self._slug)
        call = self.fetches
        self.fetches += 1
        scripted = self.script.get(call)
        if scripted is not None:
            return Answer(
                scripted.outcome,
                masked(scripted.final_url),
                unparsed=scripted.unparsed,
                lost=scripted.lost,
                cause=scripted.cause,
            )
        return Answer(Outcome.OK, f"{ORIGIN}/in/_/", contact_info_of(self.profiles[self._slug]))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]

    @property
    def clicks(self) -> list[object]:
        return [value for kind, value in self.events if kind == "click"]

    def visited(self) -> list[str]:
        """The slugs navigated to, in order."""
        return [
            unquote(urlsplit(str(url)).path.split("/")[2])
            for kind, url in self.events
            if kind == "goto"
        ]
