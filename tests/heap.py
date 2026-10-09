"""Keep each test process's heap small, so a full garbage collection stays short (#478).

A full (generation 2) collection walks every object the process tracks. Before this
plugin, each xdist worker held about three million objects by the time
``test_web_events.py`` ran, and one full collection of that took 2-3 s on a CI runner,
landing on whichever test allocated at the wrong moment (#472). ``tests/time_limit.py``
keeps that pause out of the timed tests; this plugin shrinks the pause itself:

* **The heap that exists after collection is frozen.** The imported package, the
  test modules and pytest's own items live for the whole run, so
  :func:`pytest_collection_finish` moves them out of every later collection with
  :func:`gc.freeze`.
* **Caches keyed by a per-test object are emptied after each test.** SQLAlchemy keeps a
  compiled ``INSERT``/``UPDATE``/``DELETE`` per mapper, keyed by the dialect, and every
  test's engine has its own dialect. In one long run those entries pin up to 100
  dialects per mapper, each with its type caches and the tables it compiled: most of
  the heap's growth. FastAPI keeps an endpoint's and a dependency's classification in
  module-level caches keyed by the function, which pins a test's app when the test
  defines its endpoints inside a function (``tests/test_web_deps.py``). An entry for
  another test's engine or function is never looked up again, so emptying them costs
  nothing; a running app keeps one engine and its own endpoints, so neither cache
  grows there.

``tests/test_browser_safety.py`` keeps its parsed syntax trees for one module at a
time and drops them at the module's end (see ``ast_caches`` there).
"""

from __future__ import annotations

import gc
from collections.abc import Iterator

import pytest
from fastapi.dependencies import models as fastapi_dependency_models

from netkeeper.models import Base


def pytest_collection_finish(session: pytest.Session) -> None:
    gc.collect()
    gc.freeze()


def _fastapi_dependency_caches() -> list[object]:
    """FastAPI's ``lru_cache``-wrapped classifiers, found by shape, not by private name."""
    return [
        value
        for value in vars(fastapi_dependency_models).values()
        if callable(getattr(value, "cache_clear", None))
    ]


def clear_per_test_caches() -> None:
    """Empty the caches whose entries outlive the test that made them."""
    for mapper in Base.registry.mappers:
        # A memoized property: only a mapper that has written a row has the cache.
        compiled = mapper.__dict__.get("_compiled_cache")
        if compiled is not None:
            compiled.clear()
    for cached in _fastapi_dependency_caches():
        cached.cache_clear()  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _per_test_caches() -> Iterator[None]:
    yield
    clear_per_test_caches()
