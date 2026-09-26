"""Fail an offline test that runs too long, so real-time waits never creep back (#210).

Nothing in the offline suite waits on a network, a browser, or a person, so every test
there runs in well under a second on its own; the one reason a test would take tens of
seconds is a real ``asyncio`` wait (a landing, response, or pacing timeout) that its
fake clock or fast sleep does not cover. The suite once took eleven minutes, 400 s of
it in one test sitting out a 20 s landing wait per profile visit.

So a test whose body runs longer than :data:`TIME_LIMIT_S` fails, naming the limit and
where to look. A test marked ``slow`` (the whole-week simulations, which are CPU work
on purpose) gets :data:`SLOW_TIME_LIMIT_S` instead, and ``make test-fast`` skips it.
The browser smoke suite under ``tests/smoke/`` drives a real Chrome and has its own
limits, so it is exempt.

``NETKEEPER_TEST_TIME_LIMIT_S``, read when the run starts, scales both limits for a
slow machine: its value is the limit for an ordinary test, and ``0`` turns the guard off.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

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

_SMOKE = Path(__file__).parent / "smoke"
_BASE = pytest.StashKey[float]()


def pytest_configure(config: pytest.Config) -> None:
    # Read once for the run, so a test that sets the variable changes nothing mid-run.
    config.stash[_BASE] = float(os.environ.get(LIMIT_ENV, TIME_LIMIT_S))


def limit_for(item: pytest.Item) -> float | None:
    """The limit ``item``'s body runs under, or ``None`` when it has none."""
    if _SMOKE in Path(str(item.path)).parents:
        return None
    base = item.config.stash[_BASE]
    if base <= 0:
        return None
    if item.get_closest_marker("slow") is not None:
        return base * SLOW_TIME_LIMIT_S / TIME_LIMIT_S
    return base


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if report.when != "call" or not report.passed:
        return report
    limit = limit_for(item)
    if limit is None or report.duration <= limit:
        return report
    report.outcome = "failed"
    report.longrepr = (
        f"{item.nodeid} passed, but took {report.duration:.1f}s, over its {limit:g}s limit"
        f" (tests/time_limit.py). A real asyncio wait its fake clock or sleep does not"
        f" cover is the usual cause: run it with --durations and pass the wait a test"
        f" value. Set {LIMIT_ENV} to scale the limit on a slow machine."
    )
    return report
