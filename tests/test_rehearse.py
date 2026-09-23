"""netkeeper.linkedin.rehearse: the real request pattern against a neutral site (P2-11).

Two things are being proved here, and the second matters more than the first:

1. a rehearsal drives the *genuine* pacing plan and records every request the
   tab made, so the log is evidence rather than decoration
   (``test_the_rehearsal_replays_the_real_pacing_plan`` compares what the tab
   was asked to do against ``plan_enrichment`` called with the same seed, step
   for step);
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

import math
from collections.abc import Callable, Sequence
from random import Random
from typing import Any

import httpx
import pytest
from browser_fakes import FakeBrowser, FakeConnector, FakeContext, FakePage

from netkeeper.linkedin.browser import AttachBrowserProvider, PageLike
from netkeeper.linkedin.pacing import (
    DEFAULT_BURST_PROFILE,
    DEFAULT_DELAY_PROFILE,
    BurstProfile,
    DelayProfile,
    plan_enrichment,
)
from netkeeper.linkedin.rehearse import (
    LINKEDIN_HOST,
    LOOPBACK_HOSTS,
    REHEARSAL_SLUGS,
    NotANeutralSite,
    Rehearsal,
    _is_linkedin,
    _require_neutral,
    rehearse,
    render,
    serve_replica,
)

SEED = 20260923
SITE = "http://127.0.0.1:52341"
CDP = "http://127.0.0.1:9222"


# --- a browser that is not a browser -------------------------------------------


class FakeRequest:
    """What Playwright hands a ``request`` listener."""

    def __init__(self, url: str, *, method: str = "GET", resource_type: str = "document") -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type


class FakeResponse:
    """What Playwright hands a ``response`` listener."""

    def __init__(self, request: FakeRequest, status: int = 200) -> None:
        self.request = request
        self.status = status


class ReplayPage(FakePage):
    """A tab that emits the requests a page load makes, and can lose itself on cue.

    ``mouse`` comes from the shared :class:`FakePage` base now that
    ``BrowserRun.scroll`` (#152) is what replays a scroll plan against it, rather
    than this module's own (formerly separate) ``_replay_scroll``.

    With ``context.fetch`` it really fetches each url over loopback and reports
    the status it got, instead of fabricating one. That is what the CLI test
    uses, because a fabricated 200 cannot tell a live replica from a port
    nothing is listening on -- and "the command starts a replica that really
    serves" is exactly what that test is for.
    """

    def __init__(self, context: ReplayContext, *, fail_at: int | None = None) -> None:
        super().__init__(context)
        self.handlers: dict[str, list[Callable[[Any], None]]] = {}
        self.fail_at = fail_at
        self.goto_count = 0
        self._extra: tuple[str, ...] = context.extra_requests
        self._on_close: tuple[str, ...] = context.unload_requests
        self._fetch = context.fetch

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    async def goto(self, url: str) -> object:
        self.goto_count += 1
        if self.goto_count == self.fail_at:
            self.user_closed_it()
            raise RuntimeError("the tab went away mid-navigation")
        result = await super().goto(url)
        self._load(url)
        return result

    def _load(self, url: str) -> None:
        """The requests a real profile page makes: the document, its css, its image."""
        base = url.split("/in/")[0]
        for target, kind in (
            (url, "document"),
            (f"{base}/static/replica.css", "stylesheet"),
            (f"{base}/static/avatar.svg", "image"),
            *((extra, "xhr") for extra in self._extra),
        ):
            request = FakeRequest(target, resource_type=kind)
            self._emit("request", request)
            self._emit("response", FakeResponse(request, status=self._status(target)))

    def _status(self, url: str) -> int:
        """The real status when this context fetches, a fabricated 200 otherwise."""
        if not self._fetch:
            return 200
        return httpx.get(url, timeout=5).status_code

    async def close(self) -> None:
        """Emit anything armed for the unload window, then close.

        A beacon fires when the tab goes away, which is after the last visit
        has been closed out -- the one moment nothing groups a request under a
        visit.
        """
        for target in self._on_close:
            request = FakeRequest(target, method="POST", resource_type="other")
            self._emit("request", request)
            self._emit("response", FakeResponse(request, status=204))
        self.close_calls += 1
        self._closed = True

    def _emit(self, event: str, payload: object) -> None:
        for handler in self.handlers.get(event, []):
            handler(payload)


class ReplayContext(FakeContext):
    """Hands out :class:`ReplayPage` tabs. Everything else is the shared fake's."""

    def __init__(
        self,
        *,
        fail_first_page_at: int | None = None,
        extra_requests: Sequence[str] = (),
        unload_requests: Sequence[str] = (),
        fetch: bool = False,
    ) -> None:
        super().__init__()
        self.fail_first_page_at = fail_first_page_at
        self.extra_requests = tuple(extra_requests)
        self.unload_requests = tuple(unload_requests)
        self.fetch = fetch
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
) -> tuple[AttachBrowserProvider, ReplayContext, FakeConnector]:
    context = ReplayContext(
        fail_first_page_at=fail_first_page_at,
        extra_requests=extra_requests,
        unload_requests=unload_requests,
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
    assert len(rehearsal.requests) == 9  # document, stylesheet, image, per visit
    kinds = [record.resource_type for record in rehearsal.visits[0].requests]
    assert kinds == ["document", "stylesheet", "image"]
    for record in rehearsal.requests:
        assert record.method == "GET"
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
        assert len(visit.requests) == 3
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
        "https://www.linkedin.com/in/someone",
        "http://linkedin.com:8080",
        "https://LINKEDIN.COM/feed",
        "https://sub.domain.linkedin.com/x",
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


async def test_the_summary_stops_claiming_the_whole_run_once_the_log_is_incomplete() -> None:
    """After a tab loss the reopened tab navigates before the listeners reattach.

    The note was already there; the sentence above it still said "every request
    above" and "nothing reached linkedin.com" without qualification, which is
    the over-claiming this module exists to avoid.
    """
    lost, _, _, _ = await _rehearse(visits=3, fail_first_page_at=2)
    complete, _, _, _ = await _rehearse(visits=3)

    lost_text, complete_text = render(lost), render(complete)

    assert "nothing *in this log* reached linkedin.com" in lost_text
    assert "some requests are missing from this log" in lost_text
    assert "every request above went to the loopback replica" not in lost_text
    # And the unqualified claim is still made when the log really is complete.
    assert "every request above went to the loopback replica" in complete_text
    assert "missing from this log" not in complete_text


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
    wheels = context.replays[0].mouse.wheels
    assert wheels == [(0, step.delta_px) for plan in expected.steps for step in plan.scroll.steps]


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
    _, _, _, sleeper = await _rehearse(visits=4)

    planned = sum(
        sum(step.pause_s for step in visit.scroll.steps)
        + visit.scroll.dwell_s
        + (visit.delay_after_s or 0.0)
        for visit in expected.steps
    )
    assert math.isclose(sleeper.total, planned, rel_tol=1e-9)


async def test_time_scale_divides_the_waits_and_not_the_plan() -> None:
    """A scaled rehearsal waits less and still reports what a real run would wait."""
    _, _, _, real = await _rehearse(visits=3, time_scale=1.0)
    scaled_rehearsal, _, _, scaled = await _rehearse(visits=3, time_scale=100.0)
    unscaled, _, _, _ = await _rehearse(visits=3, time_scale=1.0)

    assert math.isclose(scaled.total, real.total / 100, rel_tol=1e-9)
    assert math.isclose(scaled_rehearsal.planned_wait_s, unscaled.planned_wait_s, rel_tol=1e-9)
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


async def test_a_tab_lost_mid_rehearsal_is_recovered_and_said_so() -> None:
    """The run carries on, and the log says the listeners missed what the old tab did."""
    rehearsal, context, _, _ = await _rehearse(visits=3, fail_first_page_at=2)

    assert len(context.replays) == 2
    assert len(rehearsal.visits) == 3
    assert any("the tab was reopened" in note for note in rehearsal.notes)
    assert "note: the tab was reopened" in render(rehearsal)


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
    assert "2 visits, 6 requests, 1 host(s): 127.0.0.1" in text
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
    assert LINKEDIN_HOST not in page.text
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert svg.status_code == 200 and svg.headers["content-type"] == "image/svg+xml"


def test_the_replica_stops_listening_when_the_block_ends() -> None:
    with serve_replica() as base:
        assert httpx.get(base, timeout=5).status_code == 200

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
