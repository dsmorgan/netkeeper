"""Wall-clock limits that fail a real hang or slowdown, not a loaded CI runner (#210, #472).

Two kinds of limit live here.

**The per-test limit (#210).** Nothing in the offline suite waits on a network, a
browser, or a person, so every test there runs in well under a second on its own; the
one reason a test would take tens of seconds is a real ``asyncio`` wait (a landing,
response, or pacing timeout) that its fake clock or fast sleep does not cover. The suite
once took eleven minutes, 400 s of it in one test sitting out a 20 s landing wait per
profile visit. So a test whose body runs longer than :data:`TIME_LIMIT_S` fails, naming
the limit and where to look. A test marked ``slow`` (the whole-week simulations, which
are CPU work on purpose) gets :data:`SLOW_TIME_LIMIT_S` instead, and ``make test-fast``
skips it. The browser smoke suite under ``tests/smoke/`` drives a real Chrome and has
its own limits, so it is exempt.

**A test's own timing assertions** (a render refused within a second, a write that did
not sit out the busy timeout). Use :class:`Stopwatch` for the elapsed time and
:func:`scaled` for a performance budget.

Both kinds leave out the time the garbage collector held the process. Each xdist worker
ends up holding about three million objects (the imported package, session caches,
leftovers of thousands of tests), and on a CI runner one full collection of that heap
takes 2-3 s. It lands wherever an allocation happens to trigger it, so a correct test
used to fail a 1 s or 2 s limit at random (#472). Collections are timed with
:data:`gc.callbacks` and subtracted. Where a limit is an ``asyncio`` timeout or a SQLite
busy timeout, which a stopwatch cannot adjust, mark the test ``wall_clock``: its body
runs with automatic collection held (:func:`gc_held`), and the collection runs after it.

``NETKEEPER_TEST_TIME_LIMIT_S``, read when the run starts, sets the per-test limit for
an ordinary test (a ``slow`` one gets three times it), and ``0`` turns the guard off.
``NETKEEPER_TEST_TIME_SCALE`` multiplies every budget, the per-test limit included, for
a machine that is slower than a laptop (CI sets it). Keep a limit that stands for
"did not wait out a real timeout" unscaled, and below that timeout, so a scaled run
still fails the wait it guards against.
"""

from __future__ import annotations

import gc
import os
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

#: A test's own body, setup and teardown excluded. Measured under full ``-n auto`` load
#: (#220), the slowest ordinary tests take 4-5 s on a contended 4-core machine and went
#: over 5 s on a busy 10-core one; alone they take about 2 s. Ten seconds is twice the
#: worst seen, and still fails the 20 s landing wait this guard was written to catch.
TIME_LIMIT_S = 10.0
#: A ``slow`` test's body: the simulations take a few seconds of CPU each, up to about
#: 10 s under load.
SLOW_TIME_LIMIT_S = 30.0
LIMIT_ENV = "NETKEEPER_TEST_TIME_LIMIT_S"
SCALE_ENV = "NETKEEPER_TEST_TIME_SCALE"

_SMOKE = Path(__file__).parent / "smoke"
_BASE = pytest.StashKey[float]()
_GC_IN_CALL = pytest.StashKey[float]()


def _read_scale() -> float:
    scale = float(os.environ.get(SCALE_ENV, "1") or "1")
    if scale <= 0:
        raise ValueError(f"{SCALE_ENV} must be above 0, not {scale:g}")
    return scale


#: Read once, at import, so a test that sets the variable changes nothing mid-run.
TIME_SCALE = _read_scale()


def scaled(seconds: float) -> float:
    """``seconds`` times :data:`TIME_SCALE`: a performance budget for this machine."""
    return seconds * TIME_SCALE


# --- time spent in the garbage collector ----------------------------------------------


class _CollectorClock:
    """Adds up the wall time of every collection, in any thread, since import."""

    def __init__(self) -> None:
        self.total = 0.0
        self._started: float | None = None

    def __call__(self, phase: str, info: dict[str, Any]) -> None:
        now = time.perf_counter()
        if phase == "start":
            self._started = now
        elif self._started is not None:
            self.total += now - self._started
            self._started = None


_collector = _CollectorClock()
gc.callbacks.append(_collector)


def gc_seconds() -> float:
    """How long the garbage collector has held the process since the run started."""
    return _collector.total


class Stopwatch:
    """Elapsed wall time, less the time the garbage collector held the process."""

    def __init__(self) -> None:
        self._started = time.perf_counter()
        self._gc_started = gc_seconds()

    @property
    def gc(self) -> float:
        """The collections' share of the wall time so far."""
        return gc_seconds() - self._gc_started

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._started - self.gc

    def __str__(self) -> str:
        return f"{self.elapsed:.2f}s (plus {self.gc:.2f}s collecting garbage)"


@contextmanager
def gc_held() -> Iterator[None]:
    """No automatic collection inside the block; the collection it put off runs next.

    A block that frees what it allocates by reference counting, as a test does, grows
    the heap by little. Nested use keeps the outer block's setting.
    """
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


# --- the per-test limit -----------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    # Read once for the run, so a test that sets the variable changes nothing mid-run.
    config.stash[_BASE] = float(os.environ.get(LIMIT_ENV, TIME_LIMIT_S))
    config.addinivalue_line(
        "markers",
        "wall_clock: the body runs with automatic garbage collection held, for a test whose"
        " limit is an asyncio or SQLite timeout (tests/time_limit.py)",
    )


def limit_for(item: pytest.Item) -> float | None:
    """The limit ``item``'s body runs under, or ``None`` when it has none."""
    if _SMOKE in Path(str(item.path)).parents:
        return None
    base = item.config.stash[_BASE]
    if base <= 0:
        return None
    if item.get_closest_marker("slow") is not None:
        base *= SLOW_TIME_LIMIT_S / TIME_LIMIT_S
    return scaled(base)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    started = gc_seconds()
    try:
        if item.get_closest_marker("wall_clock") is not None:
            with gc_held():
                return (yield)
        return (yield)
    finally:
        item.stash[_GC_IN_CALL] = gc_seconds() - started


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if report.when != "call" or not report.passed:
        return report
    limit = limit_for(item)
    collecting = item.stash.get(_GC_IN_CALL, 0.0)
    if limit is None or report.duration - collecting <= limit:
        return report
    report.outcome = "failed"
    report.longrepr = (
        f"{item.nodeid} passed, but took {report.duration - collecting:.1f}s"
        f" (plus {collecting:.1f}s collecting garbage), over its {limit:g}s limit"
        f" (tests/time_limit.py). A real asyncio wait its fake clock or sleep does not"
        f" cover is the usual cause: run it with --durations and pass the wait a test"
        f" value. Set {SCALE_ENV} to scale the limit on a slow machine."
    )
    return report
