"""netkeeper.linkedin.rehearse: the real request pattern against a neutral site (P2-11).

Two things are being proved here, and the second matters more than the first:

1. a rehearsal drives the *genuine* enrichment job and pacing plan and records
   every request the tab made, so the log is evidence rather than decoration
   (``test_the_rehearsal_replays_the_real_pacing_plan`` compares what the tab
   was asked to do against ``plan_enrichment`` called with the same seed, step
   for step, and ``test_each_visit_is_the_enrichment_jobs_request_pattern``
   checks each visit's page view and its two in-page API fetches);
2. **a rehearsal cannot reach LinkedIn.** Three independent tests cover the
   three ways it could: the url it is pointed at
   (``test_a_rehearsal_refuses_linkedin_by_name``), any other off-machine host
   (``test_a_rehearsal_refuses_any_site_that_is_not_loopback``), and a page
   that, once loaded, asked for something off-site
   (``test_a_page_that_reached_linkedin_raises_instead_of_reporting``). The
   first two also assert the connector was never touched, so the refusal
   happens before a browser connection exists.

**Offline, and no browser.** The fakes below implement the browser protocols,
as ``tests/browser_fakes.py``'s do; they subclass those so a rehearsal cannot
quietly do something the shared fakes already refuse (a second context, closing
the user's browser). What they add is the two things a rehearsal needs and the
shared page deliberately lacks: a ``mouse`` to replay a scroll plan through and
an ``on`` to listen for requests with. The real thing is
``tests/smoke/test_rehearse_smoke.py``, opt-in behind ``NETKEEPER_BROWSER_TESTS=1``.

``serve_replica`` is exercised for real over loopback, because a neutral site
nobody ever fetched from is not evidence that the site is neutral. Nothing here
leaves this machine.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from random import Random
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from browser_fakes import FakeBrowser, FakeConnector, FakeContext, FakeMouse, FakePage
from flagship_site import FakeResponse
from profile_site import SiteRequest

from netkeeper.linkedin.browser import AttachBrowserProvider, BrowserUnavailable, PageLike
from netkeeper.linkedin.flagship import CONTACT_DETAILS_SCREEN_ID, NAVIGATION_PATH
from netkeeper.linkedin.flagship_profile import (
    COMPONENT_PATH,
    parse_contact_info,
    parse_profile,
    profile_slug,
)
from netkeeper.linkedin.pacing import (
    DEFAULT_BURST_PROFILE,
    DEFAULT_DELAY_PROFILE,
    BurstProfile,
    DelayProfile,
    depth_after,
    human_delay,
    plan_enrichment,
    rest_pointer_like_a_person,
    scroll_back_to_top,
)
from netkeeper.linkedin.rehearse import (
    LINKEDIN_HOST,
    LOOPBACK_HOSTS,
    REHEARSAL_SLUGS,
    REPLICA_COMPONENT_ID,
    NotANeutralSite,
    Rehearsal,
    _is_linkedin,
    _profile_page,
    _require_neutral,
    rehearse,
    render,
    replica_contact_info,
    replica_experience,
    replica_urn,
    serve_replica,
)

SEED = 20260923
SITE = "http://127.0.0.1:52341"
CDP = "http://127.0.0.1:9222"


# --- a browser that is not a browser -------------------------------------------


class ReplayPage(FakePage):
    """A tab that behaves like the replica's profile page, and can lose itself on cue.

    Navigating to ``/in/<slug>/`` "loads" the page: the document (the replica's own
    HTML, with its screen in ``rehydrate-data``), its stylesheet, its image, and any
    ``extra_requests``. The first scroll down makes the page ask for its lazy card,
    and the one Contact info control, found by ``get_by_role``, makes it ask for the
    overlay when clicked -- the replica script's requests, answered with the
    replica's own answers. Every request and response reaches the listeners the way
    Playwright reports them, so both the rehearsal's log and the source's observation
    read them.

    With ``context.fetch`` it really fetches each url over loopback and reports the
    status and body it got, instead of computing them. That is what the CLI test uses,
    because a computed 200 cannot tell a live replica from a port nothing is
    listening on -- and "the command starts a replica that really serves" is exactly
    what that test is for.
    """

    def __init__(self, context: ReplayContext, *, fail_at: int | None = None) -> None:
        super().__init__(context)
        self.handlers: dict[str, list[Callable[[Any], None]]] = {}
        self.fail_at = fail_at
        self.goto_count = 0
        self.replay = context
        self.slug: str | None = None
        self.asked_lazy = False
        self.clicks = 0
        self.mouse = _ReplayMouse(self)

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Callable[[Any], None]) -> None:
        self.handlers.get(event, []).remove(handler)

    async def goto(self, url: str) -> object:
        self.goto_count += 1
        if self.goto_count == self.fail_at:
            self.user_closed_it()
            raise RuntimeError("the tab went away mid-navigation")
        result = await super().goto(url)
        await self._load(url)
        return result

    async def _load(self, url: str) -> None:
        split = urlsplit(url)
        self.slug = profile_slug(split.path)
        self.asked_lazy = False
        base = f"{split.scheme}://{split.netloc}"
        await self._request("GET", url, "document")
        if self.slug is None:
            return
        await self._request("GET", f"{base}/static/replica.css", "stylesheet")
        await self._request("GET", f"{base}/static/avatar.svg", "image")
        for extra in self.replay.extra_requests:
            await self._request("GET", extra, "xhr")

    async def scrolled(self, delta_y: float) -> None:
        if self.slug is None or self.asked_lazy or delta_y <= 0:
            return
        self.asked_lazy = True
        base = self.url.split("/in/")[0]
        body = json.dumps({"componentId": REPLICA_COMPONENT_ID, "vanityName": self.slug})
        await self._request(
            "POST", f"{base}{COMPONENT_PATH}?componentId={REPLICA_COMPONENT_ID}", "fetch", body
        )

    def get_by_role(self, role: str, *, name: str, exact: bool) -> ReplayControl:
        return ReplayControl(self, (role, name, exact) == ("link", "Contact info", True))

    async def clicked(self) -> None:
        assert self.slug is not None
        self.clicks += 1
        first, _, last = self.slug.removeprefix("rehearsal-").partition("-")
        body = json.dumps(
            {
                "clientArguments": {
                    "requestedStateKeys": [],
                    "payload": {
                        "vanityName": self.slug,
                        "givenName": first.title(),
                        "familyName": last.title(),
                        "isVanityNameResolved": True,
                    },
                    "states": [],
                    "screenId": CONTACT_DETAILS_SCREEN_ID,
                    "knownTemplateIds": [],
                },
                "isModal": True,
            }
        )
        base = self.url.split("/in/")[0]
        url = f"{base}{NAVIGATION_PATH}?screenId={CONTACT_DETAILS_SCREEN_ID}&sduiid=replica"
        await self._request("POST", url, "fetch", body, status=self.replay.overlay_status)

    async def _request(
        self,
        method: str,
        url: str,
        kind: str,
        post: str | None = None,
        *,
        status: int | None = None,
    ) -> None:
        request = SiteRequest(method, url, kind, post)
        self._emit("request", request)
        answered, body = await self._answer(method, url, post)
        self._emit(
            "response",
            FakeResponse(url, status or answered, body, request),  # type: ignore[arg-type]
        )

    async def _answer(self, method: str, url: str, post: str | None) -> tuple[int, bytes]:
        """The real answer when this context fetches, the replica's own otherwise."""
        if self.replay.fetch:
            async with httpx.AsyncClient() as client:
                answer = await client.request(method, url, content=post, timeout=5)
            return answer.status_code, answer.content
        split = urlsplit(url)
        slug = profile_slug(split.path)
        if method == "GET" and slug is not None:
            return 200, _profile_page(slug)
        if split.path == COMPONENT_PATH and post is not None:
            return 200, replica_experience(json.loads(post)["vanityName"])
        if split.path == NAVIGATION_PATH and post is not None:
            vanity = json.loads(post)["clientArguments"]["payload"]["vanityName"]
            return 200, replica_contact_info(vanity)
        return 200, b""

    async def close(self) -> None:
        """Emit anything armed for the unload window, then close.

        A beacon fires when the tab goes away, which is after the last visit
        has been closed out -- the one moment nothing groups a request under a
        visit.
        """
        for target in self.replay.unload_requests:
            request = SiteRequest("POST", target, "other", None)
            self._emit("request", request)
            self._emit("response", FakeResponse(target, 204, b"", request))  # type: ignore[arg-type]
        self.close_calls += 1
        self._closed = True

    def _emit(self, event: str, payload: object) -> None:
        for handler in list(self.handlers.get(event, [])):
            handler(payload)


class _ReplayMouse(FakeMouse):
    def __init__(self, page: ReplayPage) -> None:
        super().__init__()
        self._page = page

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await super().wheel(delta_x, delta_y)
        await self._page.scrolled(delta_y)


class ReplayControl:
    """The replica's one Contact info link, as ``get_by_role`` finds it."""

    def __init__(self, page: ReplayPage, named: bool) -> None:
        self._page = page
        self._named = named

    async def count(self) -> int:
        return 1 if self._named and self._page.slug is not None else 0

    async def get_attribute(
        self,
        name: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- Playwright's own signature
    ) -> str | None:
        return f"/in/{self._page.slug}/overlay/contact-info/" if name == "href" else None

    async def click(
        self,
        *,
        delay: float | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 -- Playwright's own signature
    ) -> None:
        await self._page.clicked()


class ReplayContext(FakeContext):
    """Hands out :class:`ReplayPage` tabs. Everything else is the shared fake's."""

    def __init__(
        self,
        *,
        fail_first_page_at: int | None = None,
        extra_requests: Sequence[str] = (),
        unload_requests: Sequence[str] = (),
        fetch: bool = False,
        overlay_status: int | None = None,
    ) -> None:
        super().__init__()
        self.fail_first_page_at = fail_first_page_at
        self.extra_requests = tuple(extra_requests)
        self.unload_requests = tuple(unload_requests)
        self.fetch = fetch
        self.overlay_status = overlay_status
        self.replays: list[ReplayPage] = []

    async def new_page(self) -> PageLike:
        self.new_page_calls += 1
        page = ReplayPage(self, fail_at=self.fail_first_page_at if not self.replays else None)
        self.pages.append(page)
        self.replays.append(page)
        return page


class Ticker:
    """A monotonic clock that advances a fixed amount per read, so timings are exact."""

    def __init__(self, step: float = 0.01) -> None:
        self.step = step
        self.reads = 0

    def __call__(self) -> float:
        value = self.reads * self.step
        self.reads += 1
        return value


class Sleeper:
    """A sleeper that records what it was asked to wait and waits none of it."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.waits)


def _setup(
    *,
    fail_first_page_at: int | None = None,
    extra_requests: Sequence[str] = (),
    unload_requests: Sequence[str] = (),
    overlay_status: int | None = None,
) -> tuple[AttachBrowserProvider, ReplayContext, FakeConnector]:
    context = ReplayContext(
        fail_first_page_at=fail_first_page_at,
        extra_requests=extra_requests,
        unload_requests=unload_requests,
        overlay_status=overlay_status,
    )
    connector = FakeConnector([FakeBrowser([context])])
    return AttachBrowserProvider(CDP, connector=connector), context, connector


async def _rehearse(
    *,
    site: str = SITE,
    visits: int = 3,
    time_scale: float = 1.0,
    fail_first_page_at: int | None = None,
    extra_requests: Sequence[str] = (),
    unload_requests: Sequence[str] = (),
    delay: DelayProfile = DEFAULT_DELAY_PROFILE,
    burst: BurstProfile = DEFAULT_BURST_PROFILE,
) -> tuple[Rehearsal, ReplayContext, FakeConnector, Sleeper]:
    provider, context, connector = _setup(
        fail_first_page_at=fail_first_page_at,
        extra_requests=extra_requests,
        unload_requests=unload_requests,
    )
    sleeper = Sleeper()
    rehearsal = await rehearse(
        provider,
        site=site,
        visits=visits,
        seed=SEED,
        time_scale=time_scale,
        delay=delay,
        burst=burst,
        sleep=sleeper,
        clock=Ticker(),
    )
    return rehearsal, context, connector, sleeper


# --- it records what the page did ------------------------------------------------


async def test_a_rehearsal_logs_every_request_the_page_made() -> None:
    rehearsal, _, _, _ = await _rehearse(visits=3)

    assert len(rehearsal.visits) == 3
    assert [visit.index for visit in rehearsal.visits] == [1, 2, 3]
    # Per visit: the page and its two sub-resources, the lazy card the scroll made the
    # page ask for, and the overlay the one click made it ask for (#190).
    assert len(rehearsal.requests) == 15
    kinds = [record.resource_type for record in rehearsal.visits[0].requests]
    assert kinds == ["document", "stylesheet", "image", "fetch", "fetch"]
    methods = [record.method for record in rehearsal.visits[0].requests]
    assert methods == ["GET", "GET", "GET", "POST", "POST"]
    assert rehearsal.harvested == 3 and rehearsal.stopped is None and rehearsal.clicks == 3
    for record in rehearsal.requests:
        assert record.status == 200
        assert record.duration_ms is not None and record.duration_ms > 0
        assert record.host == "127.0.0.1"


async def test_each_visit_goes_to_a_profile_shaped_path() -> None:
    rehearsal, _, _, _ = await _rehearse(visits=3)

    paths = [visit.url for visit in rehearsal.visits]
    assert paths == [f"{SITE}/in/{slug}/" for slug in REHEARSAL_SLUGS[:3]]
    assert rehearsal.visits[0].requests[0].path == f"/in/{REHEARSAL_SLUGS[0]}/"


async def test_the_requests_of_one_visit_stay_with_that_visit() -> None:
    """A flat log would pass the count above; a log nobody can read by visit would not."""
    rehearsal, _, _, _ = await _rehearse(visits=3)

    for visit in rehearsal.visits:
        assert len(visit.requests) == 5
        assert visit.requests[0].url == visit.url


async def test_the_host_summary_lists_only_the_replica() -> None:
    rehearsal, _, _, _ = await _rehearse(visits=2)

    assert rehearsal.hosts == ("127.0.0.1",)
    assert not rehearsal.touched_linkedin


async def test_a_second_host_is_named_rather_than_folded_into_the_first() -> None:
    """A log that always printed the replica's host would hide an off-site request.

    ``localhost`` is loopback and is not LinkedIn, so this is the case that gets
    *reported* rather than refused -- and a fixture with only one host in it
    could never catch a summary that hardcoded that host.
    """
    rehearsal, _, _, _ = await _rehearse(
        visits=1, extra_requests=("http://localhost:60001/analytics",)
    )
    text = render(rehearsal)

    assert rehearsal.hosts == ("127.0.0.1", "localhost")
    assert "2 host(s): 127.0.0.1, localhost" in text
    assert "/analytics" in text
    assert not rehearsal.touched_linkedin


# --- it cannot reach LinkedIn -------------------------------------------------------


@pytest.mark.parametrize(
    "site",
    [
        "https://www.linkedin.com",
        "http://linkedin.com:8080",
        "https://LINKEDIN.COM",
        "https://sub.domain.linkedin.com",
    ],
)
async def test_a_rehearsal_refuses_linkedin_by_name(site: str) -> None:
    """Refused before a browser connection exists, and with a message that says why."""
    provider, _, connector = _setup()

    with pytest.raises(NotANeutralSite, match="never touches LinkedIn"):
        await rehearse(provider, site=site, seed=SEED, sleep=Sleeper(), clock=Ticker())

    assert connector.attaches == 0


@pytest.mark.parametrize(
    "site",
    [
        # #168's F3: --site is a bare origin now, full stop. A url that carries a
        # path is refused before the linkedin-by-name check ever runs (a url this
        # strict cannot be fooled into approving by a differently-parsed path), so
        # these read as "not a bare origin", not specifically "is LinkedIn" -- both
        # of these still ultimately keep the rehearsal off LinkedIn either way.
        "https://www.linkedin.com/in/someone",
        "https://LINKEDIN.COM/feed",
        "https://sub.domain.linkedin.com/x",
    ],
)
async def test_a_linkedin_url_with_a_path_is_refused_as_not_a_bare_origin(site: str) -> None:
    provider, _, connector = _setup()

    with pytest.raises(NotANeutralSite, match="not a bare origin"):
        await rehearse(provider, site=site, seed=SEED, sleep=Sleeper(), clock=Ticker())

    assert connector.attaches == 0


async def test_a_rehearsal_refuses_the_whatwg_backslash_userinfo_trick() -> None:
    """F3 (#168 review): the exact url that reads as loopback to ``urlsplit`` and as
    LinkedIn to a real browser -- see ``strict_origin``'s docstring. Before this fix,
    ``_require_neutral`` read ``urlsplit(...).hostname`` alone and approved this,
    which would have let ``netkeeper rehearse --site`` navigate a real Chrome to
    ``www.linkedin.com``.
    """
    provider, _, connector = _setup()

    with pytest.raises(NotANeutralSite):
        await rehearse(
            provider,
            site="http://www.linkedin.com\\@127.0.0.1:8080",
            seed=SEED,
            sleep=Sleeper(),
            clock=Ticker(),
        )

    assert connector.attaches == 0


async def test_a_rehearsal_refuses_a_loopback_lookalike_host() -> None:
    """F3/N3: 'localhost' must match exactly, never as a substring of a longer host."""
    provider, _, connector = _setup()

    with pytest.raises(NotANeutralSite, match="loopback"):
        await rehearse(
            provider,
            site="http://localhost.evil.example:8080",
            seed=SEED,
            sleep=Sleeper(),
            clock=Ticker(),
        )

    assert connector.attaches == 0


@pytest.mark.parametrize(
    "site",
    [
        "http://example.test",
        "https://198.51.100.7:9222",
        "http://127.0.0.1.evil.test",
        "file:///tmp/replica.html",
        "ftp://127.0.0.1/x",
        "not a url",
    ],
)
async def test_a_rehearsal_refuses_any_site_that_is_not_loopback(site: str) -> None:
    provider, _, connector = _setup()

    with pytest.raises(NotANeutralSite):
        await rehearse(provider, site=site, seed=SEED, sleep=Sleeper(), clock=Ticker())

    assert connector.attaches == 0


async def test_a_page_that_reached_linkedin_raises_instead_of_reporting() -> None:
    """The url check covers where a rehearsal navigates. This covers where the page went.

    The fake page below really does emit a request to a LinkedIn host -- emits,
    never fetches -- so a rehearsal that stopped checking its own log would
    return a report claiming success, and this test would catch it.
    """
    with pytest.raises(NotANeutralSite, match="not neutral"):
        await _rehearse(visits=1, extra_requests=("https://www.linkedin.com/li/track",))


async def test_a_beacon_fired_during_teardown_is_logged_and_checked() -> None:
    """The one window nothing groups a request under a visit, which is when beacons fire.

    Before ``Rehearsal.trailing`` existed, a request arriving after the last
    ``close_visit()`` was recorded and then silently dropped: it never reached
    ``requests``, so the neutrality check could not see it and the log printed
    an unqualified "nothing reached linkedin.com" over the top of it.
    """
    rehearsal, _, _, _ = await _rehearse(
        visits=2, unload_requests=("http://127.0.0.1:52341/beacon",)
    )
    text = render(rehearsal)

    assert len(rehearsal.trailing) == 1
    assert rehearsal.trailing[0].path == "/beacon"
    assert rehearsal.trailing[0].method == "POST"
    assert rehearsal.trailing[0] in rehearsal.requests, "trailing requests are not in the log"
    assert "AFTER THE LAST VISIT" in text
    assert "/beacon" in text


async def test_a_beacon_to_linkedin_during_teardown_raises() -> None:
    """The gap that mattered: a request outside every visit still has to be checked.

    ``requests`` is what ``_assert_stayed_neutral`` reads, so this passes only
    because trailing requests are part of it -- a neutrality claim must not
    depend on how the log happens to be grouped. The fake emits; nothing is
    fetched.
    """
    with pytest.raises(NotANeutralSite, match="not neutral"):
        await _rehearse(visits=1, unload_requests=("https://www.linkedin.com/li/track",))


async def test_a_run_with_no_beacon_has_nothing_trailing() -> None:
    """Without this, a report that swept every request into `trailing` would pass above."""
    rehearsal, _, _, _ = await _rehearse(visits=2)

    assert rehearsal.trailing == ()
    assert "AFTER THE LAST VISIT" not in render(rehearsal)


def test_the_three_spellings_of_this_machine_are_accepted() -> None:
    for host in ("127.0.0.1", "localhost", "::1"):
        url = f"http://{host}:8080" if host != "::1" else "http://[::1]:8080"
        assert _require_neutral(url) == url


def test_a_trailing_slash_is_trimmed_so_paths_do_not_double_up() -> None:
    assert _require_neutral("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"


@pytest.mark.parametrize("site", ["http://[::1", "http://[", "https://[::1]:notaport"])
async def test_a_url_too_malformed_to_parse_is_refused_not_raised_through(site: str) -> None:
    """Every refusal is a NotANeutralSite, including one urlsplit itself chokes on.

    A refusal arriving as some other exception type is one the caller's except
    clause does not catch, and the caller is the CLI turning it into a message
    rather than a traceback.
    """
    provider, _, connector = _setup()

    with pytest.raises(NotANeutralSite):
        await rehearse(provider, site=site, seed=SEED, sleep=Sleeper(), clock=Ticker())

    assert connector.attaches == 0


def test_the_replica_never_echoes_a_credential_header() -> None:
    """The smoke suite fetches /headers, and a failing assertion prints the payload.

    The replica is on loopback and the browser is the owner's own, so an echoed
    `Cookie` is their real LinkedIn session landing in test output -- the one
    thing preflight goes out of its way never to read (spec 9.1).
    """
    with serve_replica() as base:
        echoed = httpx.get(
            f"{base}/headers",
            headers={
                "Cookie": "li_at=SECRET-SESSION-VALUE",
                "Authorization": "Bearer SECRET-TOKEN",
                "User-Agent": "Chrome/140.0.7339.80",
            },
            timeout=5,
        ).text

    assert "SECRET-SESSION-VALUE" not in echoed
    assert "SECRET-TOKEN" not in echoed
    assert '"cookie": "<redacted>"' in echoed
    assert '"authorization": "<redacted>"' in echoed
    # The names still arrive, which is what the smoke suite actually checks for.
    assert "Chrome/140.0.7339.80" in echoed


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("linkedin.com", True),
        ("www.linkedin.com", True),
        ("LinkedIn.Com", True),
        ("www.linkedin.com.", True),  # a fully qualified name still resolves there
        ("mylinkedin.com", False),
        ("linkedin.com.example.test", False),  # that is example.test, not LinkedIn
        ("127.0.0.1", False),
    ],
)
def test_the_linkedin_matcher_reads_labels_not_substrings(host: str, expected: bool) -> None:
    assert _is_linkedin(host) is expected


# --- it uses the real pacing, not an imitation of it ----------------------------------


async def test_the_rehearsal_replays_the_real_pacing_plan() -> None:
    """Step for step against ``plan_enrichment`` with the same seed.

    This is what makes the log evidence: the scroll deltas, the dwell, and the
    waits are the ones a run would use, not a rehearsal-only shortcut.
    """
    expected = plan_enrichment(Random(SEED), 4)
    rehearsal, context, _, _ = await _rehearse(visits=4)

    assert rehearsal.burst_sizes == expected.burst_sizes
    assert [visit.scroll for visit in rehearsal.visits] == [step.scroll for step in expected.steps]
    assert [visit.planned_wait_s for visit in rehearsal.visits] == [
        step.delay_after_s for step in expected.steps
    ]
    # Each visit's plan, then its scroll back to the top before the click.
    rng = Random(SEED)
    plan_enrichment(rng, 4)
    replayed: list[tuple[int, int]] = []
    for step in expected.steps:
        replayed += [(0, s.delta_px) for s in step.scroll.steps]
        human_delay(rng, median=1.5, sigma=0.5, tail_p=0.0, tail_range=(0, 0))
        back = scroll_back_to_top(rng, depth_after(step.scroll))
        replayed += [(0, s.delta_px) for s in back.steps]
    assert context.replays[0].mouse.wheels == replayed


async def test_the_rehearsal_follows_the_pacing_it_is_given_not_the_library_default() -> None:
    """The fidelity claim, made falsifiable.

    ``PacingSettings``' defaults equal the pacing module's constants field for
    field, so a rehearsal that ignored its arguments would agree with one that
    honored them on every default config -- and diverge silently the moment
    somebody edits ``config.toml``. This passes a median far from the default
    and checks the waits actually moved.
    """
    brisk = DelayProfile(median=2.0, sigma=0.01, tail_p=0.0, tail_range=(0.0, 0.0))
    expected = plan_enrichment(Random(SEED), 4, delay=brisk, burst=DEFAULT_BURST_PROFILE)

    rehearsal, _, _, _ = await _rehearse(visits=4, delay=brisk)
    default_rehearsal, _, _, _ = await _rehearse(visits=4)

    assert [visit.planned_wait_s for visit in rehearsal.visits] == [
        step.delay_after_s for step in expected.steps
    ]
    assert rehearsal.planned_wait_s < default_rehearsal.planned_wait_s / 5
    assert rehearsal.planned_wait_s > 0


async def test_the_rehearsal_follows_the_burst_profile_it_is_given() -> None:
    tight = BurstProfile(size_range=(2, 2), break_range_s=(11.0, 11.0))

    rehearsal, _, _, _ = await _rehearse(visits=6, burst=tight, time_scale=1000.0)

    assert rehearsal.burst_sizes == (2, 2, 2)
    breaks = [visit.planned_wait_s for visit in rehearsal.visits if visit.burst_break]
    assert breaks == [11.0, 11.0]


async def test_every_wait_is_one_the_plan_asked_for() -> None:
    expected = plan_enrichment(Random(SEED), 4)
    rehearsal, _, _, sleeper = await _rehearse(visits=4)

    planned = sum(
        sum(step.pause_s for step in visit.scroll.steps)
        + visit.scroll.dwell_s
        + (visit.delay_after_s or 0.0)
        for visit in expected.steps
    )
    # Each visit's pause before the click, and its scroll back to the top: drawn from
    # the same seed, after the plan (``run_enrichment``'s own order).
    rng = Random(SEED)
    plan_enrichment(rng, 4)
    for step in expected.steps:
        planned += human_delay(rng, median=1.5, sigma=0.5, tail_p=0.0, tail_range=(0, 0))
        back = scroll_back_to_top(rng, depth_after(step.scroll))
        planned += sum(s.pause_s for s in back.steps) + back.dwell_s
    # And #192's one pointer rest before the run's first scroll: the rehearsal's own
    # `Random(seed)`, so it draws the walk `rest_pointer_like_a_person` would here.
    planned += sum(step.pause_s for step in rest_pointer_like_a_person(Random(SEED)).steps)
    assert all(visit.click_pause_s is not None for visit in rehearsal.visits)
    assert math.isclose(sleeper.total, planned, rel_tol=1e-9)


async def test_time_scale_divides_the_waits_and_not_the_plan() -> None:
    """A scaled rehearsal waits less and still reports what a real run would wait."""
    _, _, _, real = await _rehearse(visits=3, time_scale=1.0)
    scaled_rehearsal, _, _, scaled = await _rehearse(visits=3, time_scale=100.0)
    unscaled, _, _, _ = await _rehearse(visits=3, time_scale=1.0)

    assert math.isclose(scaled.total, real.total / 100, rel_tol=1e-9)
    assert math.isclose(scaled_rehearsal.planned_wait_s, unscaled.planned_wait_s, rel_tol=1e-9)
    for visit in scaled_rehearsal.visits:  # each visit reports the wait it really did
        planned = visit.planned_wait_s or 0.0
        assert math.isclose(visit.waited_s, planned / 100, rel_tol=1e-9)
    assert scaled_rehearsal.visits[0].waited_s > 0
    assert "SCALED" in render(scaled_rehearsal)
    assert "SCALED" not in render(unscaled)


async def test_a_long_rehearsal_breaks_into_bursts() -> None:
    """Spec 9.5: 8 to 15 profiles, then 5 to 20 minutes off. A run of 25 has to break."""
    rehearsal, _, _, _ = await _rehearse(visits=25, time_scale=1_000.0)

    assert sum(rehearsal.burst_sizes) == 25
    assert len(rehearsal.burst_sizes) >= 2
    breaks = [visit for visit in rehearsal.visits if visit.burst_break]
    assert breaks
    for visit in breaks:
        assert visit.planned_wait_s is not None
        assert 300.0 <= visit.planned_wait_s <= 1200.0


async def test_the_last_visit_has_nothing_to_wait_for() -> None:
    rehearsal, _, _, _ = await _rehearse(visits=3)

    assert rehearsal.visits[-1].planned_wait_s is None
    assert rehearsal.visits[-1].waited_s == 0.0
    assert all(visit.planned_wait_s is not None for visit in rehearsal.visits[:-1])


# --- it behaves like every other browser path -------------------------------------


async def test_a_rehearsal_attaches_once_opens_one_tab_and_closes_it() -> None:
    rehearsal, context, connector, _ = await _rehearse(visits=3)

    assert connector.attaches == 1
    assert connector.detaches == 1
    assert context.new_page_calls == 1
    assert context.open_pages == []
    assert rehearsal.notes == ()


async def test_a_rehearsal_never_launches_a_browser() -> None:
    """The fakes raise on a second context or a close; ADR 0002 from the outside."""
    provider, context, _ = _setup()

    await rehearse(provider, site=SITE, visits=2, seed=SEED, sleep=Sleeper(), clock=Ticker())

    assert not hasattr(provider, "launch")
    assert provider.mode == "attach"
    assert len(context.pages) == 1  # one tab for the run, no second context anywhere


async def test_a_tab_lost_mid_rehearsal_ends_it_as_it_ends_a_run() -> None:
    """A reopened tab is not the one being listened to: the source stops trusting it,
    and the rehearsal ends by exception rather than print a log with a hole in it."""
    with pytest.raises(BrowserUnavailable, match="replaced"):
        await _rehearse(visits=3, fail_first_page_at=2)


async def test_a_rehearsal_refuses_a_run_with_nothing_in_it() -> None:
    provider, _, connector = _setup()

    with pytest.raises(ValueError, match="at least one visit"):
        await rehearse(provider, site=SITE, visits=0, seed=SEED, sleep=Sleeper())
    with pytest.raises(ValueError, match="time_scale must be positive"):
        await rehearse(provider, site=SITE, seed=SEED, time_scale=0, sleep=Sleeper())

    assert connector.attaches == 0


# --- the log a person reads --------------------------------------------------------


async def test_the_rendered_log_shows_every_request_with_its_timing() -> None:
    rehearsal, _, _, _ = await _rehearse(visits=2)
    text = render(rehearsal)

    assert "VISIT 1" in text and "VISIT 2" in text
    assert "TIME" in text and "METHOD" in text and "STATUS" in text and "PATH" in text
    for record in rehearsal.requests:
        assert record.path in text
    assert f"seed        {SEED}" in text
    assert "2 visits, 10 requests, 1 host(s): 127.0.0.1" in text
    assert "clicked Contact info once" in text
    assert "2 of 2 visits read the profile and its contact info" in text
    assert "2 Contact info click(s)" in text
    assert "Nothing reached linkedin.com" in text


async def test_the_rendered_log_says_what_a_real_run_would_have_waited() -> None:
    rehearsal, _, _, _ = await _rehearse(visits=3)
    text = render(rehearsal)

    assert f"would have waited {rehearsal.planned_wait_s:.1f}s" in text
    assert "scrolled" in text and "dwelled" in text


# --- the neutral site itself ---------------------------------------------------------


def test_the_replica_serves_a_profile_shaped_page_on_loopback() -> None:
    """Fetched for real over loopback: a site nobody ever asked for proves nothing."""
    with serve_replica() as base:
        assert base.startswith("http://127.0.0.1:")
        page = httpx.get(f"{base}/in/rehearsal-alex-doe/", timeout=5)
        css = httpx.get(f"{base}/static/replica.css", timeout=5)
        svg = httpx.get(f"{base}/static/avatar.svg", timeout=5)

    assert page.status_code == 200
    assert "netkeeper rehearsal replica" in page.text
    assert 'href="/static/replica.css"' in page.text
    assert 'href="/in/rehearsal-alex-doe/overlay/contact-info/">Contact info</a>' in page.text
    assert 'id="rehydrate-data"' in page.text
    assert LINKEDIN_HOST not in page.text
    assert "set-cookie" not in page.headers  # nothing reads a cookie any more
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert svg.status_code == 200 and svg.headers["content-type"] == "image/svg+xml"


def test_the_replica_stops_listening_when_the_block_ends() -> None:
    with serve_replica() as base:
        assert httpx.get(f"{base}/in/rehearsal-alex-doe/", timeout=5).status_code == 200

    with pytest.raises(httpx.HTTPError):
        httpx.get(base, timeout=2)


def test_the_replica_refuses_to_bind_anywhere_but_loopback() -> None:
    """The address below is the one that would expose the replica to the network."""
    with pytest.raises(NotANeutralSite, match="loopback only"), serve_replica("0.0.0.0"):
        pass


# --- constants, pinned to literals ------------------------------------------------


def test_loopback_hosts_are_the_three_spellings_of_this_machine() -> None:
    assert {"127.0.0.1", "::1", "localhost"} == LOOPBACK_HOSTS


def test_the_refused_host_is_linkedin_com() -> None:
    assert LINKEDIN_HOST == "linkedin.com"


def test_the_rehearsal_slugs_are_invented() -> None:
    """No real profile is named anywhere, and every slug says so on its face."""
    assert all(slug.startswith("rehearsal-") for slug in REHEARSAL_SLUGS)
    assert len(set(REHEARSAL_SLUGS)) == len(REHEARSAL_SLUGS)


async def test_each_visit_is_the_enrichment_jobs_pattern() -> None:
    """#190: the page view, the lazy card the scroll made the page ask for, and the
    overlay the one click made it ask for -- the page's requests, none of netkeeper's."""
    rehearsal, context, _, _ = await _rehearse(visits=2)

    for visit in rehearsal.visits:
        slug = visit.url.rstrip("/").rsplit("/", 1)[1]
        paths = [record.path for record in visit.requests]
        assert paths[0] == f"/in/{slug}/"
        assert paths[3].startswith(f"{COMPONENT_PATH}?")
        assert paths[4].startswith(f"{NAVIGATION_PATH}?screenId=")
        assert visit.click_pause_s is not None
    (page,) = context.replays
    assert page.clicks == 2 and page.evaluate_calls == []


async def test_a_replica_that_stops_answering_is_named_in_the_log() -> None:
    """A rehearsal whose job stopped early says so rather than pretending it visited them all."""
    provider, _, _ = _setup(overlay_status=429)
    rehearsal = await rehearse(
        provider, site=SITE, visits=3, seed=SEED, sleep=Sleeper(), clock=Ticker()
    )

    assert rehearsal.stopped == "response (throttled)"
    assert rehearsal.harvested == 0 and len(rehearsal.visits) == 1
    assert "stopped early: response (throttled)" in render(rehearsal)


def test_the_replica_answers_in_the_shapes_the_parsers_read() -> None:
    """Served for real: a rehearsal that loaded garbage would stop at visit one."""
    from netkeeper.linkedin.flagship import rehydration_payload

    slug = REHEARSAL_SLUGS[0]
    with serve_replica() as base:
        page = httpx.get(f"{base}/in/{slug}/", timeout=5)
        card = httpx.post(
            f"{base}{COMPONENT_PATH}",
            content=json.dumps({"vanityName": slug}),
            timeout=5,
        )
        overlay = httpx.post(
            f"{base}{NAVIGATION_PATH}",
            content=json.dumps({"clientArguments": {"payload": {"vanityName": slug}}}),
            timeout=5,
        )
        missing = httpx.post(f"{base}/elsewhere", timeout=5)

    screen = rehydration_payload(page.text)
    assert screen is not None
    parsed = parse_profile(screen, [card.content], slug=slug)
    assert (parsed.public_id, parsed.first_name, parsed.last_name) == (slug, "Alex", "Doe")
    assert parsed.urn == replica_urn(slug) and parsed.urn.startswith(
        "urn:li:fsd_profile:ACoAAREHEARSAL"
    )
    assert [p.title for p in parsed.positions] == ["Rehearsal Profile"]
    assert parse_contact_info(overlay.content, slug=slug).emails == (f"{slug}@example.test",)
    assert missing.status_code == 404


async def test_a_rehearsal_that_fails_still_closes_its_tab() -> None:
    """A job that raises between two profiles: its own exception comes out, and the
    tab the rehearsal opened is closed, the context left as it was."""
    provider, context, _ = _setup()
    waits = 0

    async def breaks_on_a_later_wait(seconds: float) -> None:
        nonlocal waits
        waits += 1
        if waits == 12:
            raise RuntimeError("the rehearsal broke mid-run")

    with pytest.raises(RuntimeError, match="broke mid-run"):
        await rehearse(
            provider,
            site=SITE,
            visits=3,
            seed=SEED,
            sleep=breaks_on_a_later_wait,
            clock=Ticker(),
        )

    (page,) = context.replays
    assert page.is_closed()
