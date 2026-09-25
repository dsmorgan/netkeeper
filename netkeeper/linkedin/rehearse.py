"""``netkeeper rehearse``: enrichment's real pattern, against a neutral site (P2-11, CP3, #190).

What a rehearsal rehearses is enrichment's profile visit, as #190 built it on ADR 0006:
the navigation to a profile, the scroll, the scroll back to the top, the one click on
**Contact info**, and the requests the *page itself* makes in answer -- the profile
screen in its HTML, a lazy card as it is scrolled, the overlay's ``actions/navigation``
after the click. netkeeper sends none of those requests; it reads their answers.

Before anyone points this tool at their own LinkedIn account, they get to watch
it work without it. A rehearsal drives the genuine attach path
(:class:`netkeeper.linkedin.browser.AttachBrowserProvider`) through the genuine
enrichment job (:func:`netkeeper.linkedin.enrich.run_enrichment`) and the genuine
source (:class:`netkeeper.linkedin.page_profiles.PageProfiles`) -- the same
navigation, the same scroll deltas and dwell, the same click, the same lognormal
waits between profiles, the same bursts -- at a replica of a profile page served on
this machine's loopback, and records **every request the page made** while it did.
A rehearsal is that loop, not a copy of it.

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
A tab lost mid-rehearsal ends it, as it ends a real run: the source stops
trusting a tab it is no longer listening to.

**Nothing here touches the database** (spec 9.10, ADR 0005): a rehearsal has no
contacts, so it needs no session. The replica's profile slugs, and the URNs its
pages carry, are made up on the spot.

:func:`serve_replica` is the neutral site itself, a small loopback server this
module can start for the length of a block: a profile page carrying its screen the
way LinkedIn's does (a ``rehydrate-data`` flight payload), a stylesheet and an image,
a script that asks for a lazy card when the page is scrolled and for the overlay when
**Contact info** is clicked, and invented flight answers for both. It is shipped
rather than left in the tests because CP3's demo has to be one command, and because
the smoke suite and the CLI then drive the same site rather than two that could drift.
The replica sets no cookie: nothing reads one any more.
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
from urllib.parse import urlsplit

from netkeeper.linkedin.browser import SINGLE_ACCOUNT_KEY, BrowserProvider, PageLike
from netkeeper.linkedin.enrich import (
    EnrichJobSpec,
    EnrichTarget,
    PacingProfile,
    ProfileHarvest,
    StopReason,
    run_enrichment,
)
from netkeeper.linkedin.flagship import (
    CONTACT_DETAILS_SCREEN_ID,
    NAVIGATION_PATH,
    REHYDRATION_GLOBAL,
    REHYDRATION_SCRIPT_ID,
)
from netkeeper.linkedin.flagship_profile import (
    COMPONENT_PATH,
    PROFILE_PAGE_PREFIX,
    profile_slug,
)
from netkeeper.linkedin.pacing import (
    DEFAULT_BURST_PROFILE,
    DEFAULT_DELAY_PROFILE,
    BurstProfile,
    DelayProfile,
    ScrollPlan,
)
from netkeeper.linkedin.page_profiles import PageProfiles
from netkeeper.linkedin.strict_origin import NotAStrictOrigin, parse_strict_origin

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
    #: The pause taken before the Contact info click, as planned (unscaled); ``None``
    #: when the visit clicked nothing.
    click_pause_s: float | None = None

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
    #: How many visits the job harvested: the profile and its contact info read whole.
    harvested: int = 0
    #: Why the job stopped before its last visit, or ``None`` when it made them all.
    #: Against the replica this should never be set; when it is, the log says so.
    stopped: str | None = None
    #: Contact info clicks the job asked for: one per visit at most.
    clicks: int = 0
    #: Requests the page made before the first visit's navigation (none, normally).
    before: tuple[RequestRecord, ...] = ()

    @property
    def requests(self) -> tuple[RequestRecord, ...]:
        """Every request the page made: before, inside a visit, then any trailing ones.

        :attr:`visits` partitions requests for reading. This is the complete
        list, and it is what :func:`_assert_stayed_neutral` checks -- a
        neutrality claim must not depend on how the log happens to be grouped.
        """
        return (
            *self.before,
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
    overlay_wait_s: float | None = None,
) -> Rehearsal:
    """Drive the real visit pattern at ``site``, recording every request the page makes.

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
    gate = _RehearsalGate(sleep=sleep, time_scale=time_scale)
    harvested = 0
    # Each target is a slug on the replica, with the URN the replica's page carries
    # for it; the reference is only its position, since a rehearsal has no contacts
    # (and may visit a slug more than once).
    chosen = [slugs[index % len(slugs)] for index in range(visits)]
    spec = EnrichJobSpec(
        targets=tuple(
            EnrichTarget(index, slug, replica_urn(slug)) for index, slug in enumerate(chosen)
        ),
        visit_budget=visits,
        pacing=PacingProfile(delay=delay, burst=burst),
    )

    async with provider.run(account) as run:
        recorder.follow(_as_rehearsal_page(await run.ensure_page()))

        async def on_harvest(harvest: ProfileHarvest) -> None:
            nonlocal harvested
            if harvest.contact_info is not None:
                harvested += 1

        extra: dict[str, Any] = {} if overlay_wait_s is None else {"overlay_wait_s": overlay_wait_s}
        # A separate Random from run_enrichment's own (below): resting the pointer
        # (#192) is not part of the pacing plan, and drawing it from the same stream
        # would shift which deltas and dwells that plan draws next. Seeded from the
        # same `seed` so a rehearsal's *total* wait -- pointer rest included -- still
        # reproduces line for line, and so time_scale still divides it exactly.
        rest_rng = Random(seed)  # noqa: S311 -- reproducibility is the point, not secrecy
        source = PageProfiles(
            run, origin=base, sleep=_scaled_sleep(sleep, time_scale), rng=rest_rng, **extra
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
        before, grouped = recorder.visits()

    stopped = (
        None
        if result.reason is StopReason.END_OF_PLAN
        else result.reason.value
        + (f" ({result.outcome.value})" if result.outcome is not None else "")
    )
    made = [
        RehearsalVisit(
            index=index + 1,
            url=source.profile_url(slug),
            scroll=step.scroll,
            requests=grouped[index] if index < len(grouped) else (),
            planned_wait_s=step.delay_after_s,
            waited_s=gate.waited[index] if index < len(gate.waited) else 0.0,
            burst_break=step.burst_break,
            click_pause_s=(
                result.click_pauses_s[index] if index < len(result.click_pauses_s) else None
            ),
        )
        for index, (slug, step) in enumerate(zip(chosen, result.plan.steps, strict=False))
        if index < result.visits
    ]
    notes: list[str] = []
    if len(grouped) > len(made):
        notes.append(
            f"{len(grouped) - len(made)} profile page load(s) were not a visit of the job;"
            " their requests are listed under AFTER THE LAST VISIT"
        )
    # Taken after the `async with` block, so it includes anything the tab asked
    # for while it was being closed. That window is not hypothetical: an unload
    # beacon is precisely a request fired on teardown.
    trailing = (
        *(record for group in grouped[len(made) :] for record in group),
        *recorder.trailing(),
    )
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
        trailing=tuple(trailing),
        harvested=harvested,
        stopped=stopped,
        clicks=result.clicks,
        before=before,
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

    What the source's scroll and click pause are handed in place of the plain
    ``sleep`` a real run would use: scaling the *wait*, not the *plan*, is
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

    A visit starts with the tab's navigation to a profile page: a ``document``
    request whose path is ``/in/<slug>/``. Everything the page asks for until the
    next one is that visit's. Playwright hands the same ``Request`` object to every
    event about it, so requests are matched to their response by object identity. A
    request that never finishes simply keeps ``finished_s`` of ``None`` and shows as
    ``-`` in the log, which is information rather than a hole.
    """

    def __init__(self, clock: Callable[[], float], origin: float) -> None:
        self._clock = clock
        self._origin = origin
        self._attached: set[int] = set()
        self._groups: list[list[RequestRecord]] = [[]]
        self._where: dict[int, tuple[int, int]] = {}
        self._closed_at: int | None = None

    def follow(self, page: RehearsalPage) -> None:
        """Listen to ``page``, once."""
        key = id(page)
        if key in self._attached:
            return
        self._attached.add(key)
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_failed)

    def visits(self) -> tuple[tuple[RequestRecord, ...], list[tuple[RequestRecord, ...]]]:
        """``(before the first visit, one group per visit)``, as recorded so far.

        Whatever arrives after this call is :meth:`trailing`.
        """
        self._closed_at = len(self._groups)
        before, *groups = (tuple(group) for group in self._groups)
        return before, groups

    def trailing(self) -> tuple[RequestRecord, ...]:
        """What arrived after :meth:`visits` was taken: the tab's teardown."""
        start = self._closed_at if self._closed_at is not None else len(self._groups)
        return tuple(record for group in self._groups[start:] for record in group)

    def _now(self) -> float:
        return self._clock() - self._origin

    def _on_request(self, request: RequestLike) -> None:
        record = RequestRecord(
            method=request.method,
            url=request.url,
            resource_type=_resource_type(request),
            started_s=self._now(),
        )
        opens_visit = (
            self._closed_at is None
            and record.resource_type == "document"
            and profile_slug(urlsplit(record.url).path) is not None
        )
        if opens_visit or (self._closed_at is not None and len(self._groups) == self._closed_at):
            self._groups.append([])
        group = self._groups[-1]
        self._where[id(request)] = (len(self._groups) - 1, len(group))
        group.append(record)

    def _on_response(self, response: ResponseLike) -> None:
        self._finish(response.request, status=response.status, failure=None)

    def _on_failed(self, request: RequestLike) -> None:
        self._finish(request, status=None, failure="the browser reported the request failed")

    def _finish(self, request: RequestLike, *, status: int | None, failure: str | None) -> None:
        where = self._where.get(id(request))
        if where is None:
            return  # a request that started before we listened
        group, index = where
        record = self._groups[group][index]
        self._groups[group][index] = RequestRecord(
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

#: flagship-web's layout (#192): a fixed header at the top left, and the content scrolling
#: inside its own container rather than the window.
_REPLICA_CSS: Final = (
    b"body { margin: 0; font-family: sans-serif; }"
    b" #hdr { position: fixed; top: 0; left: 0; right: 0; height: 52px; background: #eee;"
    b" z-index: 2; }"
    b" #content { position: fixed; top: 52px; bottom: 0; left: 0; right: 0; overflow: auto; }"
    b" .tall { height: 4000px; }"
    b" #overlay { position: fixed; top: 20%; left: 20%; background: #fff; border: 1px solid;"
    b" z-index: 3; }\n"
)

_AVATAR_SVG: Final = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96">'
    b'<rect width="96" height="96" fill="#dde3ea"/></svg>\n'
)


#: Request headers the replica reports the presence of but never the value of.
REDACTED_HEADERS: Final[frozenset[str]] = frozenset(
    {"cookie", "authorization", "proxy-authorization"}
)
REDACTED: Final = "<redacted>"

#: The lazy card the replica's page asks for once it is scrolled, as a profile's does.
REPLICA_COMPONENT_ID: Final = (
    "com.linkedin.sdui.generated.profile.dsl.impl.profileCardsExperienceOnly"
)

#: How far the replica's page must be scrolled before it asks for its lazy card.
REPLICA_LAZY_AFTER_PX: Final = 200


def replica_urn(slug: str) -> str:
    """The invented URN the replica's page for ``slug`` carries. ``ACoAAREHEARSAL`` and digits."""
    fake = sum(ord(c) for c in slug) % 10_000_000
    return f"urn:li:fsd_profile:ACoAAREHEARSAL{fake:07d}"


def _replica_name(slug: str) -> tuple[str, str]:
    first, _, last = slug.removeprefix("rehearsal-").partition("-")
    return first.title() or "Rehearsal", last.title() or "Replica"


def _flight(rows: Sequence[tuple[str, str, object]]) -> bytes:
    """Flight rows, ``<id>:<tag><json>``, one per line (``I`` for an import, else no tag)."""
    lines = [f"{row}:{tag}{json.dumps(value, separators=(',', ':'))}" for row, tag, value in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _el(kind: str, props: dict[str, Any]) -> list[Any]:
    return ["$", kind, None, props]


def _navigate(screen_id: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "$type": "proto.sdui.actions.core.Navigate",
        "value": {
            "content": {
                "$case": "screen",
                "screen": {
                    "$type": "proto.sdui.actions.core.NavigateToScreen",
                    "screenId": screen_id,
                    "url": url,
                    "requestedArguments": {
                        "$type": "proto.sdui.actions.requests.RequestedArguments",
                        "payload": payload,
                    },
                },
            }
        },
    }


def replica_screen(slug: str) -> bytes:
    """The replica's profile screen, in the shape ``flagship_profile.parse_profile`` reads.

    Invented throughout: the name comes from the slug, the URN from :func:`replica_urn`,
    the headline and location name this machine. The experience card is lazy: the
    page asks for it when scrolled (:func:`replica_experience`).
    """
    first, last = _replica_name(slug)
    contact = _el(
        "$L1",
        {
            "action": {
                "actions": [
                    _navigate(
                        CONTACT_DETAILS_SCREEN_ID,
                        f"/in/{slug}/overlay/contact-info/",
                        {
                            "vanityName": slug,
                            "givenName": first,
                            "familyName": last,
                            "isVanityNameResolved": True,
                        },
                    )
                ]
            },
            "children": ["Contact info"],
        },
    )
    message = _el(
        "$L1",
        {
            "action": {
                "actions": [
                    _navigate(
                        "replica.messaging.Compose",
                        "/messaging/compose/",
                        {
                            "firstName": first,
                            "lastName": last,
                            "vanityName": slug,
                            "profileUrn": replica_urn(slug),
                        },
                    )
                ]
            },
            "children": ["Message"],
        },
    )
    runs = [
        "· 1st",
        "A rehearsal profile on this machine's loopback",
        "Loopback, Localhost",
        "·",
        contact,
        "500+ connections",
    ]
    top = _el(
        "$L2",
        {
            "componentKey": "replica-top-card",
            "viewTrackingSpecs": {"viewName": "profile-top-card"},
            "children": [
                _el(
                    "section",
                    {"children": [_el("$L1", {"textProps": {"children": [run]}}) for run in runs]},
                ),
                message,
            ],
        },
    )
    return _flight(
        [
            ("1", "I", ["replica-chunk-1", [], "default"]),
            ("2", "I", ["replica-chunk-2", [], "ClientComponent"]),
            ("3", "", top),
            ("0", "", [_el("main", {"children": ["$L3"]})]),
        ]
    )


def replica_experience(slug: str) -> bytes:
    """The replica's lazy experience card: one invented role."""
    role = _el(
        "li",
        {
            "children": [
                _el("$L1", {"textProps": {"children": [run]}})
                for run in (
                    "Rehearsal Profile",
                    "Loopback Replica Co · Full-time",
                    "Jan 2020 - Present · 6 yrs 9 mos",
                )
            ]
        },
    )
    card = _el(
        "$L2",
        {
            "componentKey": "replica-experience",
            "viewTrackingSpecs": {"viewName": "profile-card-experience"},
            "children": [
                _el("$L1", {"textProps": {"tagName": "h2", "children": ["Experience"]}}),
                _el("ul", {"children": [role]}),
            ],
        },
    )
    return _flight(
        [
            ("1", "I", ["replica-chunk-1", [], "default"]),
            ("2", "I", ["replica-chunk-2", [], "ClientComponent"]),
            ("0", "", [card]),
        ]
    )


def replica_contact_info(slug: str) -> bytes:
    """The replica's contact-info overlay: the profile link, a website, an email."""

    def link(url: str, shown: str) -> list[Any]:
        return _el(
            "$L1",
            {
                "action": {
                    "actions": [
                        {
                            "$type": "proto.sdui.actions.core.Navigate",
                            "value": {
                                "content": {
                                    "$case": "url",
                                    "url": {
                                        "$type": "proto.sdui.actions.core.NavigateToUrl",
                                        "urlValue": {"$case": "url", "url": url},
                                    },
                                }
                            },
                        }
                    ]
                },
                "children": [shown],
            },
        )

    def section(view: str, heading: str, links: list[list[Any]]) -> list[Any]:
        return _el(
            "$L1",
            {
                "viewTrackingSpecs": {"viewName": view},
                "children": _el("div", {"children": [_el("p", {"children": [heading]}), *links]}),
            },
        )

    body = _el(
        "div",
        {
            "data-testid": "replica-overlay",
            "children": [
                section(
                    "contact-your-profile",
                    "Your Profile",
                    [link(f"/in/{slug}/", f"loopback/in/{slug}")],
                ),
                section(
                    "contact-website",
                    "Website",
                    [link(f"https://{slug}.example.test/", f"{slug}.example.test")],
                ),
                section(
                    "contact-email",
                    "Email",
                    [link(f"mailto:{slug}@example.test", f"{slug}@example.test")],
                ),
            ],
        },
    )
    return _flight(
        [("1", "I", ["replica-chunk-1", [], "default"]), ("2", "", body), ("0", "", ["$L2"])]
    )


def _profile_page(slug: str) -> bytes:
    """The replica's profile page: its screen in ``rehydrate-data``, and a page to act on.

    The page's own script is what asks for the lazy card when it is scrolled and for
    the overlay when **Contact info** is clicked -- the page's requests, as on the real
    site. netkeeper only scrolls and clicks.
    """
    screen = replica_screen(slug).decode("utf-8")
    size = len(screen) // 2 + 1
    chunks = json.dumps([screen[:size], screen[size:]]).replace("</", "<\\/")
    first, last = _replica_name(slug)
    config = json.dumps(
        {
            "slug": slug,
            "first": first,
            "last": last,
            "screenId": CONTACT_DETAILS_SCREEN_ID,
            "navigation": NAVIGATION_PATH,
            "component": COMPONENT_PATH,
            "componentId": REPLICA_COMPONENT_ID,
            "lazyAfter": REPLICA_LAZY_AFTER_PX,
        }
    ).replace("</", "<\\/")
    href = f"{PROFILE_PAGE_PREFIX}{slug}/overlay/contact-info/"
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>netkeeper rehearsal replica</title>
<link rel="stylesheet" href="/static/replica.css">
</head><body>
<header id="hdr">netkeeper rehearsal replica</header>
<main id="content">
<h1 id="marker">netkeeper rehearsal replica</h1>
<img src="/static/avatar.svg" alt="" width="96" height="96">
<p>A profile-shaped page on this machine's loopback. No real profile, no real site.</p>
<p>{first} {last} &middot; <a id="contact-info" href="{href}">Contact info</a></p>
<div class="tall">a page tall enough to scroll like a person would</div>
</main>
<div id="overlay" hidden></div>
<script id="{REHYDRATION_SCRIPT_ID}">window.{REHYDRATION_GLOBAL} = {chunks};</script>
<script>
(function () {{
  var c = {config};
  var asked = false;
  var content = document.getElementById("content");
  content.addEventListener("scroll", function () {{
    if (asked || content.scrollTop < c.lazyAfter) return;
    asked = true;
    fetch(c.component + "?componentId=" + encodeURIComponent(c.componentId), {{
      method: "POST",
      headers: {{"content-type": "application/json"}},
      body: JSON.stringify({{componentId: c.componentId, vanityName: c.slug}})
    }});
  }});
  document.getElementById("contact-info").addEventListener("click", function (event) {{
    event.preventDefault();
    var body = {{
      clientArguments: {{
        requestedStateKeys: [],
        payload: {{vanityName: c.slug, givenName: c.first, familyName: c.last,
                   isVanityNameResolved: true}},
        states: [],
        screenId: c.screenId,
        knownTemplateIds: []
      }},
      isModal: true
    }};
    fetch(c.navigation + "?screenId=" + encodeURIComponent(c.screenId) + "&sduiid=replica", {{
      method: "POST",
      headers: {{"content-type": "application/json"}},
      body: JSON.stringify(body)
    }}).then(function (answer) {{ return answer.text(); }}).then(function () {{
      var overlay = document.getElementById("overlay");
      overlay.hidden = false;
      overlay.textContent = "Contact info";
    }});
  }});
}})();
</script>
</body></html>
"""
    return page.encode("utf-8")


class _ReplicaHandler(BaseHTTPRequestHandler):
    """A profile page, its stylesheet and image, and the two answers its script asks for."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        slug = profile_slug(path)
        if path.startswith("/static/replica.css"):
            self._send(_REPLICA_CSS, "text/css; charset=utf-8")
        elif path.startswith("/static/avatar.svg"):
            self._send(_AVATAR_SVG, "image/svg+xml")
        elif path.startswith("/headers"):
            self._send(json.dumps(self._echoed_headers()).encode(), "text/plain; charset=utf-8")
        elif slug is not None:
            self._send(_profile_page(slug), "text/html; charset=utf-8")
        else:
            self._send(b"not found\n", "text/plain; charset=utf-8", status=404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(min(length, 65_536)) if length > 0 else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        if path == COMPONENT_PATH:
            slug = body.get("vanityName") if isinstance(body, dict) else None
            self._send(replica_experience(str(slug or "rehearsal")), "application/octet-stream")
        elif path == NAVIGATION_PATH:
            arguments = body.get("clientArguments") if isinstance(body, dict) else None
            payload = arguments.get("payload") if isinstance(arguments, dict) else None
            slug = payload.get("vanityName") if isinstance(payload, dict) else None
            self._send(replica_contact_info(str(slug or "rehearsal")), "application/octet-stream")
        else:
            self._send(b"not found\n", "text/plain; charset=utf-8", status=404)

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

    def _send(self, body: bytes, content_type: str, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
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
    if rehearsal.before:
        lines.append("BEFORE THE FIRST VISIT")
        lines.extend(f"  {line}" for line in _request_table(rehearsal.before).splitlines())
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
    if visit.click_pause_s is not None:
        lines.append(
            f"  scrolled back to the top, paused {visit.click_pause_s:.1f}s, then clicked"
            " Contact info once"
        )
    else:
        lines.append("  clicked nothing")
    if visit.planned_wait_s is None:
        lines.append("  no wait after the last profile of the run")
    else:
        kind = "burst break" if visit.burst_break else "pause before the next profile"
        lines.append(
            f"  {kind}: {visit.planned_wait_s:.1f}s planned, {visit.waited_s:.1f}s waited here"
        )
    return lines


def _summary_lines(rehearsal: Rehearsal) -> list[str]:
    """The verdict, as far as the log can support it.

    The log is complete: the recorder listens to the run's tab from before the first
    navigation, and a tab that is lost mid-run ends the rehearsal (the source stops
    trusting a tab it is no longer listening to), so there is no reopened tab whose
    first requests the log could have missed. Requests outside every visit -- before
    the first, or during the teardown -- are listed and checked like the rest.
    """
    hosts = ", ".join(rehearsal.hosts) or "none"
    verdict = (
        "every request above went to the loopback replica. Nothing reached linkedin.com,"
        " and a rehearsal that had would have raised instead of printing this."
    )
    lines = [
        f"{len(rehearsal.visits)} visits, {len(rehearsal.requests)} requests,"
        f" {len(rehearsal.hosts)} host(s): {hosts}",
        verdict,
        f"elapsed {rehearsal.elapsed_s:.1f}s; a run at this pacing would have waited"
        f" {rehearsal.planned_wait_s:.1f}s between profiles.",
        f"{rehearsal.harvested} of {len(rehearsal.visits)} visits read the profile and its"
        f" contact info from what the page loaded, with {rehearsal.clicks} Contact info"
        " click(s) in all; netkeeper sent none of the page's requests itself.",
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
