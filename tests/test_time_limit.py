"""The per-test time limit (#210) fails a slow test, spares a ``slow`` one, and pins its numbers.

It also leaves out time in the garbage collector, and only performance budgets scale on a
slow machine (#472).
"""

from __future__ import annotations

import gc

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


@pytest.fixture(autouse=True)
def _unscaled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limits below are the unscaled ones, whatever this run's scale (CI sets one)."""
    monkeypatch.setattr(time_limit, "TIME_SCALE", 1.0)


INI = "[pytest]\nasyncio_default_fixture_loop_scope = function\nmarkers =\n    slow: slow\n"


def test_the_limits_are_ten_and_thirty_seconds() -> None:
    assert (time_limit.TIME_LIMIT_S, time_limit.SLOW_TIME_LIMIT_S) == (10.0, 30.0)
    assert time_limit.LIMIT_ENV == "NETKEEPER_TEST_TIME_LIMIT_S"
    assert time_limit.SCALE_ENV == "NETKEEPER_TEST_TIME_SCALE"


def test_a_test_over_the_limit_fails_and_a_slow_one_gets_longer(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 0.2 s for an ordinary test scales a slow one's limit to 0.6 s.
    monkeypatch.setenv(time_limit.LIMIT_ENV, "0.2")
    pytester.makepyfile(SLEEPS)
    pytester.makeini(INI)

    result = pytester.runpytest_inprocess("-p", "time_limit")

    result.assert_outcomes(passed=2, failed=1)
    result.stdout.fnmatch_lines(
        ["*test_dawdles passed, but took 0.*s (plus *s collecting garbage), over its 0.2s limit*"]
    )


def test_zero_turns_the_limit_off(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(time_limit.LIMIT_ENV, "0")
    pytester.makepyfile(SLEEPS)
    pytester.makeini(INI)

    pytester.runpytest_inprocess("-p", "time_limit").assert_outcomes(passed=3)


COLLECTS = """
import gc

import pytest


@pytest.fixture(scope="module")
def big_heap() -> list[list[int]]:
    # Lists, which the collector tracks (a tuple of ints it untracks): each full
    # collection walks all two million of them.
    return [[n] for n in range(2_000_000)]


def test_collects(big_heap: list[list[int]]) -> None:
    for _ in range(3):
        gc.collect()


@pytest.mark.wall_clock
def test_held() -> None:
    assert not gc.isenabled()


def test_not_held() -> None:
    assert gc.isenabled()
"""


def test_time_in_the_garbage_collector_is_not_the_tests(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collection of a big heap is the runner's cost, not the test's (#472)."""
    monkeypatch.setenv(time_limit.LIMIT_ENV, "0.05")
    pytester.makepyfile(COLLECTS)
    pytester.makeini(INI)

    pytester.runpytest_inprocess("-p", "time_limit").assert_outcomes(passed=3)


def test_the_scale_leaves_the_per_test_limit_alone(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-test limit guards a real wait, so a scaled run keeps it (#472)."""
    monkeypatch.setenv(time_limit.LIMIT_ENV, "0.2")
    monkeypatch.setattr(time_limit, "TIME_SCALE", 3.0)
    pytester.makepyfile(SLEEPS)
    pytester.makeini(INI)

    pytester.runpytest_inprocess("-p", "time_limit").assert_outcomes(passed=2, failed=1)


def test_scaled_multiplies_a_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time_limit, "TIME_SCALE", 3.0)
    assert time_limit.scaled(1.5) == 4.5


def test_the_scale_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(time_limit.SCALE_ENV, raising=False)
    assert time_limit._read_scale() == 1.0
    monkeypatch.setenv(time_limit.SCALE_ENV, "2.5")
    assert time_limit._read_scale() == 2.5
    monkeypatch.setenv(time_limit.SCALE_ENV, "0")
    with pytest.raises(ValueError, match="above 0"):
        time_limit._read_scale()


def test_a_stopwatch_leaves_out_collections() -> None:
    heap = [[n] for n in range(1_000_000)]  # lists: tracked, so the collection walks them
    watch = time_limit.Stopwatch()
    gc.collect()
    assert watch.gc > 0
    assert watch.elapsed < watch.gc
    del heap


def test_gc_held_restores_what_it_found() -> None:
    assert gc.isenabled()
    with time_limit.gc_held():
        assert not gc.isenabled()
        with time_limit.gc_held():
            assert not gc.isenabled()
        assert not gc.isenabled()
    assert gc.isenabled()
