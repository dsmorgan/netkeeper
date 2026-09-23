"""Make a stalled smoke test fail loudly instead of hanging the terminal.

Every test in this directory drives a real Chrome, and a real Chrome can stop
answering without closing the connection: the likeliest case is a netkeeper
window that is minimized or fully covered, which Chrome stops rendering, so a
test waiting on a scroll or a paint waits forever. The offline suite cannot
hang this way, and nothing else in pytest's default setup turns a stall into a
failure, so each test here runs under a watchdog. When it fires it says what
probably happened, dumps every thread's stack, and exits the run with status 1.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
from collections.abc import Iterator

import pytest
from _pytest.capture import CaptureManager

# Every smoke test finishes in a few seconds against a responsive Chrome; a
# minute is long enough never to fire on a slow machine and short enough that
# nobody concludes the suite is merely slow.
SMOKE_TEST_TIMEOUT_S = 60.0


def _stalled(name: str, capture: CaptureManager | None) -> None:
    # pytest captures stderr at the file-descriptor level, and the hard exit
    # below discards whatever it captured, so capture has to be let go of first
    # or this explanation is written into a buffer nobody will ever read.
    if capture is not None:
        capture.suspend_global_capture(in_=False)
    sys.stderr.write(
        f"\n\nsmoke test {name} made no progress for {SMOKE_TEST_TIMEOUT_S:.0f}s.\n"
        "The usual cause is the netkeeper Chrome window being minimized or covered:\n"
        "Chrome stops rendering it, and a test waiting on a real scroll waits forever.\n"
        "Bring that window to the front and run the suite again. Stacks follow.\n\n"
    )
    sys.stderr.flush()
    faulthandler.dump_traceback(all_threads=True)
    os._exit(1)


@pytest.fixture(autouse=True)
def _watchdog(request: pytest.FixtureRequest) -> Iterator[None]:
    capture = request.config.pluginmanager.getplugin("capturemanager")
    timer = threading.Timer(SMOKE_TEST_TIMEOUT_S, _stalled, args=(request.node.nodeid, capture))
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
