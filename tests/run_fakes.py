"""A fake browser whose connections page loads its own answers, for the runs and ``serve`` tests.

:class:`ConnectionsContext` is a :class:`flagship_site.FlagshipSite`: a fake
connections page that "receives" its first screen when a tab navigates to it and
"sends" each pagination request when the tab is scrolled, delivering every answer to
the tab's ``response`` listeners (P2-17, ADR 0006). Nothing opens a socket; a
linkedin.com url here is a string a fake tab records, never fetched.

:func:`worker_extractor` builds the same ``ServeExtractor`` ``netkeeper serve``
builds, on this fake instead of the attach provider, so a test drives the real
app lifespan, the real scheduler, and the real worker.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

from browser_fakes import FakeBrowser, FakeConnector, FakeContext
from flagship_site import CHECKPOINT_URL, FlagshipSite
from sqlalchemy.orm import Session, sessionmaker
from voyager_pages import PEOPLE, Person

from netkeeper.config import Settings
from netkeeper.linkedin.browser import ActivityLocks, AttachBrowserProvider
from netkeeper.services import runs
from netkeeper.services.events import EventBus
from netkeeper.services.scheduled_runs import ServeExtractor
from netkeeper.worker import BrowserWorker

CDP_URL = "http://127.0.0.1:9222"


class ConnectionsContext(FlagshipSite):
    """The connections page over ``people``. ``fetches`` is every pagination request it sent.

    ``first`` is how many cards come with the page: a test that holds every sleep
    shut passes a first screen as wide as the job's page (40), so the first unit
    needs no scroll -- a scroll waits between wheel events, and a held sleep would
    park the run there instead of in the wait between pages the test is about.
    """

    def __init__(self, people: Sequence[Person] = PEOPLE, **kwargs: Any) -> None:
        super().__init__(people, **kwargs)


def fake_provider(
    context: FakeContext | None = None, *, error: Exception | None = None
) -> tuple[AttachBrowserProvider, FakeConnector]:
    """The attach provider on a fake Chrome: ``connector.attaches`` counts every attach."""
    connector = FakeConnector([FakeBrowser([context or ConnectionsContext()])], error=error)
    return AttachBrowserProvider(CDP_URL, connector=connector, locks=ActivityLocks()), connector


class Gate:
    """A ``sleep`` a test holds shut: every call waits until :meth:`open` is called."""

    def __init__(self) -> None:
        self.calls = 0
        self._open = asyncio.Event()

    def open(self) -> None:
        self._open.set()

    async def __call__(self, seconds: float) -> None:
        self.calls += 1
        await self._open.wait()


async def no_sleep(seconds: float) -> None:
    await asyncio.sleep(0)


def worker_extractor(
    provider: AttachBrowserProvider,
    settings: Settings,
    *,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]] = no_sleep,
    rng: random.Random | None = None,
) -> ServeExtractor:
    """What ``netkeeper.worker.serve_extractor`` builds, on ``provider`` and ``clock``."""

    def executor(factory: sessionmaker[Session], bus: EventBus) -> runs.RunExecutor:
        return BrowserWorker(
            provider, factory, settings.linkedin, bus=bus, clock=clock, sleep=sleep, rng=rng
        )

    return ServeExtractor(executor=executor, clock=clock, rng=rng or random.Random(0))


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class CheckpointContext(ConnectionsContext):
    """Every navigation lands on a checkpoint, the way LinkedIn redirects a flagged session."""

    LANDING = CHECKPOINT_URL

    def __init__(self, people: Sequence[Person] = PEOPLE) -> None:
        super().__init__(people, landing=CHECKPOINT_URL)
