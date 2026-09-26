"""The per-test time limit (#210) fails a slow test, spares a ``slow`` one, and pins its numbers."""

from __future__ import annotations

import pytest
import time_limit

SLEEPS = """
import time

import pytest


def test_quick() -> None:
    pass


def test_dawdles() -> None:
    time.sleep(0.3)


@pytest.mark.slow
def test_simulates() -> None:
    time.sleep(0.3)
"""

INI = "[pytest]\nasyncio_default_fixture_loop_scope = function\nmarkers =\n    slow: slow\n"


def test_the_limits_are_ten_and_thirty_seconds() -> None:
    assert (time_limit.TIME_LIMIT_S, time_limit.SLOW_TIME_LIMIT_S) == (10.0, 30.0)
    assert time_limit.LIMIT_ENV == "NETKEEPER_TEST_TIME_LIMIT_S"


def test_a_test_over_the_limit_fails_and_a_slow_one_gets_longer(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 0.2 s for an ordinary test scales a slow one's limit to 0.6 s.
    monkeypatch.setenv(time_limit.LIMIT_ENV, "0.2")
    pytester.makepyfile(SLEEPS)
    pytester.makeini(INI)

    result = pytester.runpytest_inprocess("-p", "time_limit")

    result.assert_outcomes(passed=2, failed=1)
    result.stdout.fnmatch_lines(["*test_dawdles passed, but took 0.*s, over its 0.2s limit*"])


def test_zero_turns_the_limit_off(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(time_limit.LIMIT_ENV, "0")
    pytester.makepyfile(SLEEPS)
    pytester.makeini(INI)

    pytester.runpytest_inprocess("-p", "time_limit").assert_outcomes(passed=3)
