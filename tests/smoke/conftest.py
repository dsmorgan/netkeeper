"""Make a stalled smoke test fail instead of hanging the terminal.

Every test in this directory drives a real Chrome, and a real Chrome can stop
answering without closing the connection. The likeliest case is a netkeeper
window that is minimized or fully covered: Chrome stops rendering it, and a
test waiting on a scroll or a paint waits forever. The offline suite cannot
hang this way, and nothing in pytest's default setup turns a stall into a
failure, so two limits apply here.

The first is cooperative. Every async test runs inside ``asyncio.timeout``,
so a stall cancels the test: its ``finally`` blocks run, ``BrowserRun.close()``
closes the tab it opened in the owner's Chrome and detaches, the test fails
with the likely cause, and the session carries on to the next test and prints
its summary.

The second is a backstop for the one thing cancellation cannot interrupt, an
event loop blocked in synchronous code. It dumps every thread's stack and exits
the run. That loses the rest of the session and can leave a tab open, which is
why it waits well past the first limit and is not the normal way out.
"""

from __future__ import annotations

import asyncio
import faulthandler
import functools
import inspect
import os
import sys
import threading
from collections.abc import Awaitable, Callable, Iterator
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from _pytest.capture import CaptureManager

# Every smoke test finishes in a few seconds against a responsive Chrome.
SMOKE_TEST_TIMEOUT_S = 60.0
# Only reached when the event loop itself is blocked, so cancellation never ran.
SMOKE_BACKSTOP_S = 120.0

_STALLED = (
    "did not finish within {limit:.0f}s. The usual cause is the netkeeper Chrome "
    "window being minimized or covered: Chrome stops rendering it, and a test waiting "
    "on a real scroll waits forever. Bring that window to the front and run the suite again."
)


def _bounded(test: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(test)
    async def run(*args: Any, **kwargs: Any) -> Any:
        try:
            async with asyncio.timeout(SMOKE_TEST_TIMEOUT_S):
                return await test(*args, **kwargs)
        except TimeoutError:
            pytest.fail(f"{test.__name__} " + _STALLED.format(limit=SMOKE_TEST_TIMEOUT_S))

    return run


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    here = os.path.dirname(__file__)
    for item in items:
        if not str(item.path).startswith(here):
            continue
        # The explicit opt-out from tests/conftest.py's real-browser guard: these
        # tests drive a real, isolated Chrome. Port 9222 stays blocked even so.
        item.add_marker(pytest.mark.real_cdp)
        test = getattr(item, "obj", None)
        if inspect.iscoroutinefunction(test):
            item.obj = _bounded(test)  # type: ignore[attr-defined]


def _blocked(name: str, capture: CaptureManager | None) -> None:
    # pytest captures stderr at the file-descriptor level, and the hard exit
    # below discards whatever it captured, so capture is released first or this
    # explanation is written into a buffer nobody will ever read.
    if capture is not None:
        capture.suspend_global_capture(in_=False)
    sys.stderr.write(
        f"\n\nsmoke test {name} " + _STALLED.format(limit=SMOKE_BACKSTOP_S) + "\n"
        "The event loop was blocked, so the test could not be cancelled cleanly: a tab "
        "may still be open in that Chrome. Stacks follow.\n\n"
    )
    sys.stderr.flush()
    faulthandler.dump_traceback(all_threads=True)
    os._exit(1)


@pytest.fixture(autouse=True)
def _backstop(request: pytest.FixtureRequest) -> Iterator[None]:
    capture = request.config.pluginmanager.getplugin("capturemanager")
    timer = threading.Timer(SMOKE_BACKSTOP_S, _blocked, args=(request.node.nodeid, capture))
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
