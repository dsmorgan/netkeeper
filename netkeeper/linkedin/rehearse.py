"""``netkeeper rehearse``: the real request pattern, against a neutral site (P2-11, CP3).

Before anyone points this tool at their own LinkedIn account, they get to watch
it work without it. A rehearsal drives the genuine attach path
(:class:`netkeeper.linkedin.browser.AttachBrowserProvider`) through the genuine
enrichment job (:func:`netkeeper.linkedin.enrich.run_enrichment`, P2-07) -- the
same navigation to a profile page, the same scroll deltas and dwell, the same
two in-page API fetches for the profile's details and contact info
(:class:`netkeeper.linkedin.fetch.PageVoyagerFetch`), the same lognormal waits
between profiles, the same bursts -- at a replica of a profile page served on
this machine's loopback, and records **every request the page made** while it
did. The job is the one a real run uses, handed a source pointed at the
replica: a rehearsal is that loop, not a copy of it.

**The site is loopback, and that is enforced here rather than trusted.**
:func:`rehearse` refuses any url whose host is not ``127.0.0.1``, ``::1``, or
``localhost``, and refuses a linkedin.com host by name first so the error says
what it means. A rehearsal is the thing that proves the tool is safe to run; a
rehearsal that could be pointed at the real site by a flag, a config value, or
a typo would prove nothing. There is no override, and there is deliberately no
parameter that could become one. (The strings ``linkedin.com`` below are
compared against, never fetched -- CLAUDE.md's offline rule.)

**Nothing here launches a browser** (ADR 0002). The rehearsal attaches to the
Chrome the user started, opens one tab, and closes that tab. It never creates a
context, never routes or intercepts a request, and never overrides anything:
requests are *observed* through ``page.on("request")``, which is a listener and
not ``page.route``, so what the log records is what the browser genuinely sent.

**Nothing here touches the database** (spec 9.10, ADR 0005): a rehearsal has no
contacts, so it needs no session. The replica's profile slugs are made up on
the spot.

:func:`serve_replica` is the neutral site itself, a small loopback server this
module can start for the length of a block: a profile-shaped page and its two
sub-resources, and invented answers in Voyager's shapes at the two profile
endpoints. It is shipped rather than left in the tests because CP3's demo has to
be one command, and because the smoke suite and the CLI then drive the same site
rather than two that could drift.

**The replica sets one cookie.** The in-page fetch reads its csrf token from the
page's own ``JSESSIONID`` cookie, the way the real client does, and never from
Python (#168). So the replica's profile page sets an invented one for
``127.0.0.1``, ``Max-Age`` two minutes, as the fetch smoke suite's fixture does.
netkeeper still writes no cookie: the site does, the ordinary way a site does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from random import Random
from typing import Any, Final, Protocol, cast
from urllib.parse import parse_qs, unquote, urlsplit

from netkeeper.linkedin.browser import SINGLE_ACCOUNT_KEY, BrowserProvider, PageLike
from netkeeper.linkedin.enrich import (
    BrowserProfiles,
    EnrichJobSpec,
    EnrichTarget,
    PacingProfile,
    ProfileHarvest,
    StopReason,
    run_enrichment,
)
from netkeeper.linkedin.fetch import PageVoyagerFetch
from netkeeper.linkedin.pacing import (
    DEFAULT_BURST_PROFILE,
    DEFAULT_DELAY_PROFILE,
    BurstProfile,
    DelayProfile,
    ScrollPlan,
)
from netkeeper.linkedin.strict_origin import NotAStrictOrigin, parse_strict_origin
from netkeeper.linkedin.voyager import CONTACT_INFO_PATH_TEMPLATE, PROFILE_PATH

log = logging.getLogger(__name__)

#: The only hosts a rehearsal may drive a tab to.
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

#: Refused by name, ahead of the loopback check, so the message says why. This
#: is a string compared against a url the caller supplied; nothing fetches it.
LINKEDIN_HOST: Final = "linkedin.com"

#: Profile-shaped paths, because the point is to rehearse the real pattern. The
#: slugs are invented; no LinkedIn profile is named here or anywhere else.
REHEARSAL_SLUGS: Final[tuple[str, ...]] = (
    "rehearsal-alex-doe",
    "rehearsal-blair-roe",
    "rehearsal-casey-poe",
    "rehearsal-devon-moe",
    "rehearsal-emery-loe",
)


class NotANeutralSite(ValueError):
    """The url a rehearsal was pointed at is not the loopback replica."""


# --- the slices of Playwright a rehearsal needs -------------------------------
# `browser.PageLike` is deliberately tiny. A rehearsal needs one more thing -- the
# event listener, to record what the tab asked for -- so it declares it here rather
# than widening the protocol every other caller shares. `on` is a listener; `route`
# (which would let netkeeper change what the browser sends) is absent from both
# protocols and is refused outright by tests/test_browser_safety.py. Replaying the
# scroll plan itself is `BrowserRun.scroll`'s job (#152): it borrows the mouse
# locally, inside `browser.py`, so this module never needs to name it either.


class RequestLike(Protocol):
    """The slice of a Playwright ``Request`` the log records."""

    @property
    def method(self) -> str: ...

    @property
    def url(self) -> str: ...

    @property
    def resource_type(self) -> str: ...


class ResponseLike(Protocol):
    """The slice of a Playwright ``Response`` the log records."""

    @property
    def status(self) -> int: ...

    @property
    def request(self) -> RequestLike: ...


class RehearsalPage(PageLike, Protocol):
    """A tab that can be listened to."""

    def on(self, event: str, handler: Callable[[Any], None]) -> None: ...


# --- what a rehearsal produces -------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """One request the page made, as the log shows it.

    Times are seconds from the start of the rehearsal, so a reader sees the
    shape of the traffic rather than a wall clock. ``finished_s`` and ``status``
    are ``None`` for a request that never came back, and ``failure`` says why
    when the browser told us.
    """

    method: str
    url: str
    resource_type: str
    started_s: float
    finished_s: float | None = None
    status: int | None = None
    failure: str | None = None

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""

    @property
    def path(self) -> str:
        split = urlsplit(self.url)
        return split.path + (f"?{split.query}" if split.query else "")

    @property
    def duration_ms(self) -> float | None:
        if self.finished_s is None:
            return None
        return (self.finished_s - self.started_s) * 1000


@dataclass(frozen=True, slots=True)
class RehearsalVisit:
    """One profile visit: where it went, how it scrolled, what it asked for, what it waited."""

    index: int
    url: str
    scroll: ScrollPlan
    requests: tuple[RequestRecord, ...]
    planned_wait_s: float | None
    waited_s: float
    burst_break: bool

    @property
    def scrolled_px(self) -> int:
        return sum(abs(step.delta_px) for step in self.scroll.steps)


@dataclass(frozen=True, slots=True)
class Rehearsal:
    """A whole rehearsal: the plan it followed, the visits it made, the requests they made."""

    site: str
    started_at: datetime
    seed: int
    time_scale: float
    burst_sizes: tuple[int, ...]
    visits: tuple[RehearsalVisit, ...]
    elapsed_s: float
    notes: tuple[str, ...] = ()
    #: Requests that landed after the last visit closed -- during the tab's
    #: own teardown, which is exactly when an unload beacon fires. Nothing
    #: groups them under a visit, so before this field existed they were
    #: recorded and then silently discarded, and the neutrality check that
    #: reads :attr:`requests` could not see them.
    trailing: tuple[RequestRecord, ...] = ()
    #: How many visits the job harvested: both in-page fetches answered and parsed.
    harvested: int = 0
    #: Why the job stopped before its last visit, or ``None`` when it made them all.
    #: Against the replica this should never be set; when it is, the log says so.
    stopped: str | None = None

    @property
    def requests(self) -> tuple[RequestRecord, ...]:
        """Every request the page made: the ones inside a visit, then any trailing ones.

        :attr:`visits` partitions requests for reading. This is the complete
        list, and it is what :func:`_assert_stayed_neutral` checks -- a
        neutrality claim must not depend on how the log happens to be grouped.
        """
        return (
            *(record for visit in self.visits for record in visit.requests),
            *self.trailing,
        )

    @property
    def hosts(self) -> tuple[str, ...]:
        """Every distinct host the page contacted, in the order first seen."""
        seen: list[str] = []
        for record in self.requests:
            if record.host and record.host not in seen:
                seen.append(record.host)
        return tuple(seen)

    @property
    def planned_wait_s(self) -> float:
        """What a run at this pacing would have waited between profiles, unscaled."""
        return sum(visit.planned_wait_s or 0.0 for visit in self.visits)

    @property
    def touched_linkedin(self) -> bool:
        """Whether any recorded request went to LinkedIn. Always false, and checked anyway."""
        return any(_is_linkedin(record.host) for record in self.requests)


async def _real_sleep(seconds: float) -> None:
    """The default sleeper. A named wrapper so the parameter's type stays one-argument."""
    await asyncio.sleep(seconds)


async def rehearse(
    provider: BrowserProvider,
    *,
    site: str,
    visits: int = 3,
    seed: int,
    account: str = SINGLE_ACCOUNT_KEY,
    time_scale: float = 1.0,
    delay: DelayProfile = DEFAULT_DELAY_PROFILE,
    burst: BurstProfile = DEFAULT_BURST_PROFILE,
    sleep: Callable[[float], Awaitable[None]] = _real_sleep,
    clock: Callable[[], float] = time.monotonic,
    now: datetime | None = None,
    slugs: Sequence[str] = REHEARSAL_SLUGS,
) -> Rehearsal:
    """Drive the real request pattern at ``site``, recording every request the page makes.

    ``site`` must be a loopback url (:func:`serve_replica` provides one);
    anything else raises :class:`NotANeutralSite`, and a linkedin.com host
    raises it with a message saying so. ``seed`` makes the pacing plan
    reproducible, so two rehearsals of the same seed compare line for line.

    ``time_scale`` divides every wait. At ``1.0`` the rehearsal waits exactly
    what a run would, which is the honest demo and takes about as long as the
    pacing says; a smaller value is for a test or an impatient second look, and
    the report records it so a scaled rehearsal can never be mistaken for a real
    one. The *planned* waits are recorded unscaled either way, so the log
    always shows what the real thing would do.

    ``delay`` and ``burst`` are the pacing the rehearsal actually follows. They
    default to the pacing module's own constants, which today equal
    ``[linkedin.pacing]``'s defaults field for field -- so a caller that leaves
    them out looks right in every test and rehearses the wrong pacing the
    moment somebody edits ``config.toml``. ``netkeeper rehearse`` passes the
    loaded settings through :func:`netkeeper.services.pacing.profiles`, because
    a rehearsal that shows 25-second medians while the config asks for 5 is
    worse than no rehearsal: its entire value is fidelity.

    Raises whatever the browser raises: a rehearsal that cannot attach is a
    failure worth seeing, not something to paper over. Nothing here retries and
    nothing launches a browser (ADR 0002).
    """
    base = _require_neutral(site)
    if visits < 1:
        raise ValueError("a rehearsal needs at least one visit")
    if time_scale <= 0:
        raise ValueError("time_scale must be positive")
    if not slugs:
        raise ValueError("a rehearsal needs at least one profile slug to visit")

    started_at = datetime.now(UTC) if now is None else now
    origin = clock()
    recorder = _Recorder(clock, origin)
    notes: list[str] = []
    urls: list[str] = []
    grouped: list[tuple[RequestRecord, ...]] = []
    gate = _RehearsalGate(sleep=sleep, time_scale=time_scale)
    harvested = 0
    # Each target is a slug on the replica; the reference is only its position,
    # since a rehearsal has no contacts (and may visit a slug more than once).
    spec = EnrichJobSpec(
        targets=tuple(EnrichTarget(index, slugs[index % len(slugs)]) for index in range(visits)),
        visit_budget=visits,
        pacing=PacingProfile(delay=delay, burst=burst),
    )

    async with provider.run(account) as run:
        recorder.follow(_as_rehearsal_page(await run.ensure_page()))

        async def navigate(url: str) -> PageLike:
            # A visit's requests run from its navigation to the next one's, so the
            # wait after a profile is filed under that profile, as it happens.
            if urls:
                grouped.append(recorder.close_visit())
            else:
                recorder.open_visit()
            urls.append(url)
            page = await run.goto(url)
            if recorder.follow(_as_rehearsal_page(page)):
                notes.append(
                    f"the tab was reopened before visit {len(urls)}; requests it made"
                    " before the listeners were reattached are not in this log"
                )
            return page

        async def scroll(plan: ScrollPlan) -> object:
            scrolled = await run.scroll(plan, sleep=_scaled_sleep(sleep, time_scale))
            if recorder.follow(_as_rehearsal_page(scrolled.page)):
                notes.append(
                    f"the tab had already been lost before visit {len(urls)} could be"
                    " scrolled and was reopened first; requests it made before the"
                    " listeners were reattached are not in this log"
                )
            return scrolled

        async def on_harvest(harvest: ProfileHarvest) -> None:
            nonlocal harvested
            harvested += 1

        source = BrowserProfiles(
            navigate=navigate,
            scroll_page=scroll,
            fetch=PageVoyagerFetch(run, origin=base),
            origin=base,
        )
        # Not a cryptographic use: the seed is printed in the log precisely so a
        # rehearsal can be repeated line for line, which is the opposite of what a
        # secure generator is for.
        result = await run_enrichment(
            spec,
            source,
            gate,
            on_harvest=on_harvest,
            rng=Random(seed),  # noqa: S311
            clock=lambda: started_at,
        )
        if urls:
            grouped.append(recorder.close_visit())

    stopped = (
        None
        if result.reason is StopReason.END_OF_PLAN
        else result.reason.value
        + (f" ({result.outcome.value})" if result.outcome is not None else "")
    )
    made = [
        RehearsalVisit(
            index=index + 1,
            url=url,
            scroll=step.scroll,
            requests=requests,
            planned_wait_s=step.delay_after_s,
            waited_s=gate.waited[index] if index < len(gate.waited) else 0.0,
            burst_break=step.burst_break,
        )
        for index, (url, requests, step) in enumerate(
            zip(urls, grouped, result.plan.steps, strict=False)
        )
    ]

    # Taken after the `async with` block, so it includes anything the tab asked
    # for while it was being closed. That window is not hypothetical: an unload
    # beacon is precisely a request fired on teardown.
    trailing = recorder.close_visit()
    if trailing:
        notes.append(
            f"{len(trailing)} request(s) arrived after the last profile visit, during the"
            " tab's teardown; they are listed under AFTER THE LAST VISIT and are checked"
            " for neutrality like every other request"
        )
    rehearsal = Rehearsal(
        site=base,
        started_at=started_at,
        seed=seed,
        time_scale=time_scale,
        burst_sizes=result.plan.burst_sizes,
        visits=tuple(made),
        elapsed_s=clock() - origin,
        notes=tuple(notes),
        trailing=trailing,
        harvested=harvested,
        stopped=stopped,
    )
    _assert_stayed_neutral(rehearsal)
    return rehearsal


def _assert_stayed_neutral(rehearsal: Rehearsal) -> None:
    """The last line of defence: no recorded request may have gone to LinkedIn.

    The url check at the top of :func:`rehearse` covers where the rehearsal
    *navigates*. This covers where the page then *went* -- a replica that
    served a page referencing an off-site resource, or a redirect, would show up
    here and nowhere else. It raises rather than warns: a rehearsal whose whole
    purpose is to prove nothing reached LinkedIn must not be able to return a
    report saying otherwise.
    """
    offenders = sorted({record.host for record in rehearsal.requests if _is_linkedin(record.host)})
    if offenders:
        raise NotANeutralSite(
            "a rehearsal reached " + ", ".join(offenders) + "; the replica is not neutral"
        )


def _scaled_sleep(
    sleep: Callable[[float], Awaitable[None]], time_scale: float
) -> Callable[[float], Awaitable[None]]:
    """``sleep``, with every wait it is asked for divided by ``time_scale`` first.

    What :meth:`~netkeeper.linkedin.browser.BrowserRun.scroll` is handed in place of
    the plain ``sleep`` a real run would use: scaling the *wait*, not the *plan*, is
    ``rehearse``'s whole point (a scaled rehearsal still records what a run at this
    pacing would actually wait -- see :class:`_RehearsalGate`, which does the same
    division for the pause between profiles).
    """

    async def scaled(seconds: float) -> None:
        await sleep(seconds / time_scale)

    return scaled


class _RehearsalGate:
    """The enrichment job's gate, for a rehearsal: every visit goes ahead, every wait is kept.

    A rehearsal has no budget to spend and no cancel flag to read; what it keeps
    is the wait itself, divided by ``time_scale`` (the *planned* wait stays in the
    job's plan, unscaled, for the log).
    """

    def __init__(self, *, sleep: Callable[[float], Awaitable[None]], time_scale: float) -> None:
        self._sleep = sleep
        self._time_scale = time_scale
        self.waited: list[float] = []

    async def before_visit(self, number: int) -> StopReason | None:
        return None

    async def pause(self, seconds: float) -> bool:
        scaled = seconds / self._time_scale
        await self._sleep(scaled)
        self.waited.append(scaled)
        return True


def _as_rehearsal_page(page: PageLike) -> RehearsalPage:
    """A ``BrowserRun`` tab, seen through the wider slice a rehearsal drives.

    ``BrowserRun`` hands back the narrow :class:`PageLike` every caller shares.
    A real Playwright page satisfies :class:`RehearsalPage` as well, and so do
    the test fakes -- mypy checks both against it -- but the narrowing itself
    cannot be expressed without a cast.
    """
    return cast(RehearsalPage, page)


class _Recorder:
    """Listens to one tab and files each request under the visit it happened during.

    Playwright hands the same ``Request`` object to every event about it, so
    requests are matched to their response by object identity. A request that
    never finishes simply keeps ``finished_s`` of ``None`` and shows as ``-``
    in the log, which is information rather than a hole.
    """

    def __init__(self, clock: Callable[[], float], origin: float) -> None:
        self._clock = clock
        self._origin = origin
        self._attached: set[int] = set()
        self._open: list[RequestRecord] = []
        self._by_request: dict[int, int] = {}

    def follow(self, page: RehearsalPage) -> bool:
        """Listen to ``page``. True when this is a tab we had not seen before."""
        key = id(page)
        if key in self._attached:
            return False
        self._attached.add(key)
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_failed)
        return bool(len(self._attached) > 1)

    def open_visit(self) -> None:
        self._open = []
        self._by_request = {}

    def close_visit(self) -> tuple[RequestRecord, ...]:
        records, self._open = self._open, []
        self._by_request = {}
        return tuple(records)

    def _now(self) -> float:
        return self._clock() - self._origin

    def _on_request(self, request: RequestLike) -> None:
        self._by_request[id(request)] = len(self._open)
        self._open.append(
            RequestRecord(
                method=request.method,
                url=request.url,
                resource_type=_resource_type(request),
                started_s=self._now(),
            )
        )

    def _on_response(self, response: ResponseLike) -> None:
        self._finish(response.request, status=response.status, failure=None)

    def _on_failed(self, request: RequestLike) -> None:
        self._finish(request, status=None, failure="the browser reported the request failed")

    def _finish(self, request: RequestLike, *, status: int | None, failure: str | None) -> None:
        index = self._by_request.get(id(request))
        if index is None:
            # A request that started before this visit opened, or on a tab we
            # stopped following. Recording it under the wrong visit would be
            # worse than leaving it out of the timing.
            return
        record = self._open[index]
        self._open[index] = RequestRecord(
            method=record.method,
            url=record.url,
            resource_type=record.resource_type,
            started_s=record.started_s,
            finished_s=self._now(),
            status=status,
            failure=failure,
        )


def _resource_type(request: RequestLike) -> str:
    try:
        return request.resource_type
    except (AttributeError, NotImplementedError):  # pragma: no cover - defensive
        return "other"


def _require_neutral(site: str) -> str:
    """``site``, rebuilt and canonical, if it is a loopback url; raise otherwise.

    Every way this says no is a :class:`NotANeutralSite`, including a url too
    malformed to parse, or one shaped so that Python's ``urlsplit`` and a real
    browser's URL parser would read it two different ways -- ``site`` first goes
    through :func:`~netkeeper.linkedin.strict_origin.parse_strict_origin`, which
    refuses a backslash, userinfo, or anything past the authority before that
    differential can matter (see that module's docstring; a #168 review found this
    function could be fooled into approving a url a real Chrome would resolve
    straight to ``www.linkedin.com``). The linkedin-block and the loopback-require
    below both read the *rebuilt* origin, never the original string. A refusal that
    arrives as some other exception type is a refusal a caller's ``except`` clause
    does not catch, and the caller here is the CLI turning it into a message rather
    than a traceback.
    """
    try:
        origin = parse_strict_origin(site)
    except NotAStrictOrigin as exc:
        raise NotANeutralSite(
            f"{site!r} is not a url a rehearsal can be pointed at: {exc}"
        ) from exc
    if origin.scheme not in ("http", "https"):
        raise NotANeutralSite(
            f"a rehearsal site must be an http(s) url on this machine's loopback, got {site!r}"
        )
    if _is_linkedin(origin.host):
        raise NotANeutralSite(
            "a rehearsal never touches LinkedIn. It exists so the request pattern can be"
            f" seen before anything real is, so it refuses {origin.host!r} and runs against"
            " the loopback replica instead (`netkeeper rehearse` starts one for you)"
        )
    if origin.host not in LOOPBACK_HOSTS:
        allowed = ", ".join(sorted(LOOPBACK_HOSTS))
        raise NotANeutralSite(
            f"a rehearsal site must be on this machine's loopback ({allowed}), got {origin.host!r}"
        )
    return str(origin)


def _is_linkedin(host: str) -> bool:
    """Whether ``host`` is linkedin.com or a subdomain of it, matched on labels."""
    host = host.lower().rstrip(".")
    return host == LINKEDIN_HOST or host.endswith(f".{LINKEDIN_HOST}")


# --- the neutral site ----------------------------------------------------------

_PROFILE_PAGE: Final = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>netkeeper rehearsal replica</title>
<link rel="stylesheet" href="/static/replica.css">
</head><body>
<h1 id="marker">netkeeper rehearsal replica</h1>
<img src="/static/avatar.svg" alt="" width="96" height="96">
<p>A profile-shaped page on this machine's loopback. No real profile, no real site.</p>
<div class="tall">a page tall enough to scroll like a person would</div>
</body></html>
"""

_REPLICA_CSS: Final = b".tall { height: 4000px; } body { font-family: sans-serif; }\n"

_AVATAR_SVG: Final = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96">'
    b'<rect width="96" height="96" fill="#dde3ea"/></svg>\n'
)


#: Request headers the replica reports the presence of but never the value of.
REDACTED_HEADERS: Final[frozenset[str]] = frozenset(
    {"cookie", "authorization", "proxy-authorization"}
)
REDACTED: Final = "<redacted>"


#: The csrf cookie the replica's profile page sets, so the in-page fetch has one to
#: read (see the module docstring). Invented, and gone two minutes after the last
#: profile page set it.
REPLICA_COOKIE: Final = 'JSESSIONID="ajax:netkeeper-rehearsal-replica"; Path=/; Max-Age=120'


def replica_voyager(path: str) -> bytes | None:
    """The replica's answer at a Voyager path, or ``None`` when ``path`` is not one.

    Invented people in the shapes :mod:`netkeeper.linkedin.voyager` parses, named
    after the slug asked for: fake URNs, ``example.test`` addresses, a company that
    does not exist. Shared with the offline tests so the fake tab answers exactly
    what the served replica does.
    """
    split = urlsplit(path)
    prefix, _, suffix = CONTACT_INFO_PATH_TEMPLATE.partition("{public_id}")
    if split.path.startswith(prefix) and split.path.endswith(suffix):
        slug = unquote(split.path[len(prefix) : -len(suffix)])
        return json.dumps(
            {
                "emailAddress": f"{slug}@example.test",
                "websites": [
                    {"url": f"https://{slug}.example.test", "category": {"type": "PERSONAL"}}
                ],
            }
        ).encode()
    if split.path == PROFILE_PATH:
        slug = parse_qs(split.query).get("memberIdentity", ["rehearsal"])[0]
        first, _, last = slug.removeprefix("rehearsal-").partition("-")
        fake = sum(ord(c) for c in slug) % 10_000_000
        return json.dumps(
            {
                "data": {
                    "entityUrn": f"urn:li:fsd_profile:ACoAAREHEARSAL{fake:07d}",
                    "publicIdentifier": slug,
                    "firstName": first.title() or "Rehearsal",
                    "lastName": last.title() or "Replica",
                    "headline": "A rehearsal profile on this machine's loopback",
                },
                "included": [
                    {
                        "entityUrn": f"urn:li:fsd_position:(ACoAAREHEARSAL{fake:07d},1)",
                        "$type": "com.linkedin.voyager.dash.identity.profile.Position",
                        "title": "Rehearsal Profile",
                        "companyName": "Loopback Replica Co",
                        "dateRange": {"start": {"year": 2020, "month": 1}},
                    }
                ],
            }
        ).encode()
    return None


class _ReplicaHandler(BaseHTTPRequestHandler):
    """A profile page, a stylesheet, an image, and the two profile endpoints a visit fetches."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        voyager = replica_voyager(self.path)
        if voyager is not None:
            self._send(voyager, "application/json")
        elif self.path.startswith("/static/replica.css"):
            self._send(_REPLICA_CSS, "text/css; charset=utf-8")
        elif self.path.startswith("/static/avatar.svg"):
            self._send(_AVATAR_SVG, "image/svg+xml")
        elif self.path.startswith("/headers"):
            self._send(json.dumps(self._echoed_headers()).encode(), "text/plain; charset=utf-8")
        else:
            self._send(_PROFILE_PAGE, "text/html; charset=utf-8", cookie=REPLICA_COOKIE)

    def _echoed_headers(self) -> dict[str, str]:
        """The request's headers, with anything bearing a credential redacted.

        The smoke suite fetches this to prove the tab sends the browser's own
        user agent and client hints, and a failing assertion prints the whole
        payload. The replica is on loopback and the profile is the user's own,
        so ``Cookie`` here would be their real session cookie landing in test
        output -- the one thing preflight goes out of its way never to read
        (spec 9.1, CLAUDE.md). Nothing in netkeeper needs their values to check
        that a header arrived, so the name is kept and the value is not.
        """
        return {
            name.lower(): (REDACTED if name.lower() in REDACTED_HEADERS else value)
            for name, value in self.headers.items()
        }

    def _send(self, body: bytes, content_type: str, *, cookie: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the replica's own access log out of the rehearsal's output."""


@contextmanager
def serve_replica(host: str = "127.0.0.1") -> Iterator[str]:
    """The neutral site, on a loopback port the OS picks, for the length of the block.

    Bound to loopback only, so nothing outside this machine can reach it, and
    torn down when the block ends. Yields the base url to hand
    :func:`rehearse`.
    """
    if host not in LOOPBACK_HOSTS:
        raise NotANeutralSite(f"the replica binds to loopback only, not {host!r}")
    server = ThreadingHTTPServer((host, 0), _ReplicaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="netkeeper-replica")
    thread.start()
    log.debug("rehearsal replica listening on %s:%d", host, server.server_port)
    try:
        yield f"http://{host}:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --- the log a person reads ------------------------------------------------------


def render(rehearsal: Rehearsal) -> str:
    """The request log as the artifact CP3 is judged on: readable first, parseable second."""
    lines = [
        "netkeeper rehearse: the real request pattern, against a neutral site",
        "",
        f"  site        {rehearsal.site}  (loopback only; linkedin.com is refused)",
        f"  started     {rehearsal.started_at:%Y-%m-%d %H:%M:%S UTC}",
        f"  visits      {len(rehearsal.visits)} in"
        f" {len(rehearsal.burst_sizes)} burst(s) of {_burst_text(rehearsal.burst_sizes)}",
        f"  seed        {rehearsal.seed}",
        f"  pacing      {_scale_text(rehearsal.time_scale)}",
        "",
    ]
    for visit in rehearsal.visits:
        lines.extend(_visit_lines(visit))
        lines.append("")
    if rehearsal.trailing:
        lines.append("AFTER THE LAST VISIT  (the tab's own teardown)")
        lines.extend(f"  {line}" for line in _request_table(rehearsal.trailing).splitlines())
        lines.append("")
    lines.extend(_summary_lines(rehearsal))
    return "".join(f"{line}\n" for line in lines)


def _request_table(records: Sequence[RequestRecord]) -> str:
    """The request log's one table shape, so every block of it reads the same."""
    rows = [
        (
            f"+{record.started_s:.3f}s",
            record.method,
            "-" if record.status is None else str(record.status),
            record.resource_type,
            "-" if record.duration_ms is None else f"{record.duration_ms:.0f} ms",
            record.path,
        )
        for record in records
    ]
    return _table(("TIME", "METHOD", "STATUS", "KIND", "TOOK", "PATH"), rows)


def _visit_lines(visit: RehearsalVisit) -> list[str]:
    lines = [f"VISIT {visit.index}  {visit.url}"]
    if visit.requests:
        lines.extend(f"  {line}" for line in _request_table(visit.requests).splitlines())
    else:
        lines.append("  (the page made no requests)")
    failures = [record for record in visit.requests if record.failure is not None]
    lines.extend(f"  failed: {record.path}: {record.failure}" for record in failures)
    lines.append(
        f"  scrolled {len(visit.scroll.steps)} times over {visit.scrolled_px} px,"
        f" dwelled {visit.scroll.dwell_s:.1f}s"
    )
    if visit.planned_wait_s is None:
        lines.append("  no wait after the last profile of the run")
    else:
        kind = "burst break" if visit.burst_break else "pause before the next profile"
        lines.append(
            f"  {kind}: {visit.planned_wait_s:.1f}s planned, {visit.waited_s:.1f}s waited here"
        )
    return lines


def _summary_lines(rehearsal: Rehearsal) -> list[str]:
    """The verdict, qualified exactly as far as the log can support it.

    An unqualified "nothing reached linkedin.com" is a claim about the run. It
    is only true of the *log* once something is known to be missing from the
    log -- after a tab loss, the navigation on the reopened tab happens before
    the listeners are reattached. Printing the absolute claim above a note
    saying the log is incomplete is the shape of over-claiming this whole
    module exists to avoid, so the claim shrinks when the note appears.
    """
    hosts = ", ".join(rehearsal.hosts) or "none"
    complete = not rehearsal.notes
    verdict = (
        "every request above went to the loopback replica. Nothing reached linkedin.com,"
        " and a rehearsal that had would have raised instead of printing this."
        if complete
        else "nothing *in this log* reached linkedin.com, and a rehearsal whose log showed"
        " otherwise would have raised instead of printing this -- but some requests are"
        " missing from this log; see the notes below."
    )
    lines = [
        f"{len(rehearsal.visits)} visits, {len(rehearsal.requests)} requests,"
        f" {len(rehearsal.hosts)} host(s): {hosts}",
        verdict,
        f"elapsed {rehearsal.elapsed_s:.1f}s; a run at this pacing would have waited"
        f" {rehearsal.planned_wait_s:.1f}s between profiles.",
        f"{rehearsal.harvested} of {len(rehearsal.visits)} visits harvested the profile's"
        " details and contact info through the in-page API.",
    ]
    if rehearsal.stopped is not None:
        lines.append(
            f"the enrichment job stopped early: {rehearsal.stopped}. A real run would have"
            " stopped at the same point."
        )
    lines.extend(f"note: {note}" for note in rehearsal.notes)
    return lines


def _burst_text(sizes: tuple[int, ...]) -> str:
    return ", ".join(str(size) for size in sizes) or "0"


def _scale_text(time_scale: float) -> str:
    if time_scale == 1.0:
        return "real (every wait is what a run would wait)"
    return (
        f"SCALED: every wait divided by {time_scale:g}. This is not what a run"
        " waits; the planned figures below are."
    )


def _table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    if not rows:
        return ""
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows, strict=True)]
    lines = []
    for row in (headers, *rows):
        cells = (cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        lines.append("  ".join(cells).rstrip())
    return "".join(f"{line}\n" for line in lines)
