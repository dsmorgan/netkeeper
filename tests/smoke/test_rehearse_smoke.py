"""``netkeeper rehearse`` against a real Chrome. Opt in with ``NETKEEPER_BROWSER_TESTS=1``.

Skipped by default, because it needs a browser the developer started and the
rest of the suite stays offline. The only site involved is the loopback replica
``netkeeper.linkedin.rehearse.serve_replica`` starts -- the same one the command
starts -- and nothing here knows a LinkedIn URL.

What it proves that ``tests/test_rehearse.py`` cannot, because that one drives
fakes: that ``page.on("request")`` and ``page.mouse.wheel`` are really what
Playwright offers, that the request log really fills from a real page load
(the document, its stylesheet, its image), that the requests carry the
browser's own headers because nothing overrides them, and that a whole
rehearsal adds no browser process and no context.

Start Chrome first with the command ``netkeeper browser launch`` prints.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest

from netkeeper.linkedin.browser import AttachBrowserProvider, BrowserUnavailable
from netkeeper.linkedin.rehearse import NotANeutralSite, rehearse, render, serve_replica

pytestmark = pytest.mark.skipif(
    os.environ.get("NETKEEPER_BROWSER_TESTS") != "1",
    reason="browser smoke suite: start Chrome, then set NETKEEPER_BROWSER_TESTS=1",
)

CDP_URL = os.environ.get("NETKEEPER_CDP_URL", "http://127.0.0.1:9222")
SEED = 20260923


@pytest.fixture(scope="module")
def site() -> Iterator[str]:
    """The same loopback replica `netkeeper rehearse` starts for itself."""
    with serve_replica() as base:
        yield base


@pytest.fixture
def provider() -> AttachBrowserProvider:
    return AttachBrowserProvider(CDP_URL)


async def test_a_rehearsal_logs_what_a_real_page_load_asked_for(
    provider: AttachBrowserProvider, site: str
) -> None:
    """A real Chrome, a real page, a real request log. Fast pacing so the suite finishes."""
    try:
        rehearsal = await rehearse(provider, site=site, visits=2, seed=SEED, time_scale=200.0)
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeeper browser launch` prints.")

    assert len(rehearsal.visits) == 2
    assert rehearsal.hosts == ("127.0.0.1",)
    assert not rehearsal.touched_linkedin
    kinds = {record.resource_type for record in rehearsal.requests}
    # A real load fetches the document and the two sub-resources the replica names.
    assert "document" in kinds
    assert {"stylesheet", "image"} <= kinds
    for record in rehearsal.requests:
        assert record.status == 200, record
        assert record.duration_ms is not None
    assert "VISIT 1" in render(rehearsal)


async def test_the_rehearsal_scrolls_the_real_page(
    provider: AttachBrowserProvider, site: str
) -> None:
    """``page.mouse.wheel`` is really there, and the replica is really tall enough."""
    rehearsal = await rehearse(provider, site=site, visits=1, seed=SEED, time_scale=200.0)
    visit = rehearsal.visits[0]

    assert visit.scroll.steps
    assert visit.scrolled_px > 0

    async with provider.run() as run:
        page = await run.goto(f"{site}/in/rehearsal-alex-doe/")
        height = await page.evaluate("() => document.body.scrollHeight")
    assert height > 1000, "the replica is too short to scroll like a person"


async def test_the_rehearsals_requests_carry_the_browsers_own_headers(
    provider: AttachBrowserProvider, site: str
) -> None:
    """Nothing overrides the UA or the client hints, so the rehearsal is the real pattern."""
    async with provider.run() as run:
        page = await run.goto(f"{site}/headers")
        sent = json.loads(await page.evaluate("() => document.body.textContent"))
        fingerprint_ua = await page.evaluate("() => navigator.userAgent")

    assert sent["user-agent"] == fingerprint_ua
    assert "Headless" not in sent["user-agent"], "run the smoke suite against a windowed Chrome"
    assert sent.get("sec-ch-ua"), "Chrome's client hints did not reach the request"


async def test_a_rehearsal_adds_no_browser_context(
    provider: AttachBrowserProvider, site: str
) -> None:
    """ADR 0002 from the outside: the rehearsal leaves the browser as it found it."""
    async with provider.run() as run:
        contexts_before = len(run.browser.contexts)
        pages_before = len(run.context.pages)

    await rehearse(provider, site=site, visits=2, seed=SEED, time_scale=200.0)

    async with provider.run() as run:
        assert len(run.browser.contexts) == contexts_before
        assert len(run.context.pages) == pages_before + 1  # only this run's own tab


async def test_a_real_rehearsal_still_refuses_linkedin(provider: AttachBrowserProvider) -> None:
    """The refusal is not a property of the fakes. Nothing is fetched: it raises first."""
    with pytest.raises(NotANeutralSite, match="never touches LinkedIn"):
        await rehearse(provider, site="https://www.linkedin.com", visits=1, seed=SEED)
