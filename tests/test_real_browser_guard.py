"""The suite-wide guard that keeps every test away from a real Chrome (#293 review).

During the review of #293, a mutant removed the CLI's active-hours refusal, and a
test that relied on it went on to build the real attach provider on the default
``http://127.0.0.1:9222``, where the maintainer's own netkeeper Chrome was listening.
``tests/conftest.py``'s ``_no_real_browser`` now stands in front of the one place
netkeeper opens a browser connection. These tests call the real attach path with
the default URL and show the guard fires first: Playwright never starts and no
socket is opened.
"""

from __future__ import annotations

import socket
from typing import Any

import factories
import playwright.async_api
import pytest
from browser_guard import RealBrowserBlocked, is_personal_cdp
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.linkedin import activity_lock
from netkeeper.linkedin.browser import AttachBrowserProvider, PlaywrightCdpConnector
from netkeeper.models import SyncRunKind, SyncRunTrigger
from netkeeper.models.base import utcnow
from netkeeper.services import runs
from netkeeper.services.events import EventBus
from netkeeper.worker import serve_extractor

DEFAULT_CDP_URL = Settings().linkedin.cdp_url


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Record any start of Playwright, and any socket connect, without doing either."""
    calls: dict[str, list[Any]] = {"playwright": [], "socket": []}

    def playwright_spy(*args: Any, **kwargs: Any) -> None:
        calls["playwright"].append(args)
        raise AssertionError("Playwright was started")

    def connect_spy(self: socket.socket, address: Any) -> None:
        calls["socket"].append(address)
        raise AssertionError(f"a socket connect to {address!r}")

    monkeypatch.setattr(playwright.async_api, "async_playwright", playwright_spy)
    monkeypatch.setattr(socket.socket, "connect", connect_spy)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_spy)
    return calls


def test_the_default_cdp_url_is_the_personal_port() -> None:
    """What the guard protects: the address a test falls back to without a config."""
    assert DEFAULT_CDP_URL == "http://127.0.0.1:9222"


async def test_the_real_attach_on_the_default_url_is_refused_before_any_socket(
    spies: dict[str, list[Any]],
) -> None:
    provider = AttachBrowserProvider(DEFAULT_CDP_URL)  # the real connector

    with pytest.raises(RealBrowserBlocked, match=r"127\.0\.0\.1:9222"):
        await provider._attach()

    assert spies == {"playwright": [], "socket": []}


async def test_a_whole_run_on_the_default_provider_is_refused_before_any_socket(
    spies: dict[str, list[Any]],
) -> None:
    """Through ``run``: the activity lock is taken, then the attach is refused."""
    provider = AttachBrowserProvider(DEFAULT_CDP_URL)

    with pytest.raises(RealBrowserBlocked):
        async with provider.run(activity_lock.SINGLE_ACCOUNT_KEY):
            pytest.fail("attached")

    assert spies == {"playwright": [], "socket": []}


async def test_the_worker_serve_builds_is_refused_and_not_retried_later(
    session_factory: sessionmaker[Session], spies: dict[str, list[Any]]
) -> None:
    """``serve``'s own worker on the default config: the guard is not swallowed into a
    ``BrowserUnavailable`` retry; it propagates and fails the test."""
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        run_id = runs.create_run(
            session,
            user,
            SyncRunKind.CONNECTIONS_INCREMENTAL,
            trigger=SyncRunTrigger.MANUAL,
            now=utcnow(),
        ).id
        user_id = user.id
    worker = serve_extractor(Settings()).executor(session_factory, EventBus())

    with pytest.raises(RealBrowserBlocked):
        await worker.execute(run_id, user_id)

    assert spies == {"playwright": [], "socket": []}


@pytest.mark.real_cdp
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9222",
        "http://localhost:9222",
        "ws://127.0.0.1:9222/devtools/browser/x",
        "http://[::1]:9222",
        "http://192.168.1.50:9222",
        "127.0.0.1:9222",
    ],
)
async def test_opting_out_still_never_reaches_port_9222(
    spies: dict[str, list[Any]], url: str
) -> None:
    with pytest.raises(RealBrowserBlocked, match="isolated Chrome"):
        await PlaywrightCdpConnector().connect(url)

    assert spies == {"playwright": [], "socket": []}


def test_only_other_ports_count_as_isolated() -> None:
    assert is_personal_cdp("http://127.0.0.1:9222")
    assert is_personal_cdp("http://localhost")  # no port: refused rather than guessed
    assert is_personal_cdp("http://127.0.0.1:notaport")
    assert not is_personal_cdp("http://127.0.0.1:9333")


def test_a_test_that_did_not_opt_out_cannot_start_playwright() -> None:
    with pytest.raises(RealBrowserBlocked):
        playwright.async_api.async_playwright()
