"""A fake browser that answers connections pages in-page, for the runs and ``serve`` tests (P2-10).

:class:`ConnectionsContext` is a :class:`browser_fakes.FakeContext` whose tabs
answer the in-page Voyager fetch the way the real endpoint pages: it reads the
url out of the script ``PageVoyagerFetch`` hands to ``page.evaluate`` and
returns that page of invented people (``voyager_pages``). Nothing opens a
socket; a linkedin.com url here is a string a fake tab records, never fetched.

:func:`worker_extractor` builds the same ``ServeExtractor`` ``netkeeper serve``
builds, on this fake instead of the attach provider, so a test drives the real
app lifespan, the real scheduler, and the real worker.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from browser_fakes import FakeBrowser, FakeConnector, FakeContext, FakePage
from sqlalchemy.orm import Session, sessionmaker
from voyager_pages import CONNECTIONS_URL, PEOPLE, Person, page_body

from netkeeper.config import Settings
from netkeeper.linkedin.browser import ActivityLocks, AttachBrowserProvider
from netkeeper.services import runs
from netkeeper.services.events import EventBus
from netkeeper.services.scheduled_runs import ServeExtractor
from netkeeper.worker import BrowserWorker

CDP_URL = "http://127.0.0.1:9222"

_FETCH_URL = re.compile(r'fetch\("([^"]+)"')


class ConnectionsContext(FakeContext):
    """Tabs whose in-page fetch pages through ``people`` like the connections endpoint."""

    def __init__(self, people: Sequence[Person] = PEOPLE) -> None:
        super().__init__()
        self.people = list(people)
        self.fetches: list[str] = []

    def answer(self, expression: str) -> dict[str, Any]:
        match = _FETCH_URL.search(expression)
        assert match is not None, "not a PageVoyagerFetch script"
        url = json.loads(f'"{match.group(1)}"')
        self.fetches.append(url)
        query = parse_qs(urlsplit(url).query)
        start = int(query["start"][0])
        count = int(query["count"][0])
        body = page_body(
            self.people[start : start + count], start=start, count=count, total=len(self.people)
        )
        return {"status": 200, "body": body, "url": CONNECTIONS_URL}

    async def new_page(self) -> Any:
        page = await super().new_page()
        assert isinstance(page, FakePage)
        context = self

        async def evaluate(expression: str) -> Any:
            page.evaluate_calls.append(expression)
            return context.answer(expression)

        page.evaluate = evaluate  # type: ignore[method-assign]
        return page


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

    LANDING = "https://www.linkedin.com/checkpoint/challenge/AgFAKE?ctx=invented"

    async def new_page(self) -> Any:
        page = await super().new_page()
        real_goto = page.goto

        async def goto(url: str) -> object:
            await real_goto(url)
            page._url = self.LANDING
            return None

        page.goto = goto
        return page
