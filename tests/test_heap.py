"""tests/heap.py keeps each test process's heap small (#478).

Each piece reaches into a library's internals (a SQLAlchemy mapper's compiled cache,
FastAPI's classifier caches), so a library upgrade could quietly turn it into a no-op.
These tests fail if one does.
"""

from __future__ import annotations

import gc

import factories
import heap
import httpx
import pytest
import test_browser_safety as browser_safety
import test_extractor_boundary
import test_posture
from fastapi import FastAPI
from sqlalchemy.orm import Session

from netkeeper.models import Base

#: Raised by a test below, as a fake raises a module constant.
RAISED = LookupError("a module-level exception")


def _compiled_entries() -> int:
    return sum(len(m.__dict__.get("_compiled_cache", ())) for m in Base.registry.mappers)


def test_the_heap_after_collection_is_frozen() -> None:
    # The imported package alone is well over this; an unfrozen run has a few hundred.
    assert gc.get_freeze_count() > 50_000


def test_a_mappers_compiled_statements_are_dropped(session: Session) -> None:
    factories.make_user(session)
    session.commit()
    assert _compiled_entries() > 0, "SQLAlchemy no longer caches a mapper's INSERT there"

    heap.clear_per_test_caches()

    assert _compiled_entries() == 0


async def test_fastapis_classifier_caches_are_dropped() -> None:
    caches = heap._fastapi_dependency_caches()
    assert len(caches) >= 3, "FastAPI's dependency classifiers are no longer lru_caches"

    app = FastAPI()

    @app.get("/local")
    def local() -> dict[str, bool]:
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        assert (await client.get("/local")).status_code == 200
    assert any(c.cache_info().currsize for c in caches)  # type: ignore[attr-defined]

    heap.clear_per_test_caches()

    assert not any(c.cache_info().currsize for c in caches)  # type: ignore[attr-defined]


def test_the_syntax_tree_caches_last_one_module() -> None:
    tree = browser_safety.parse(browser_safety.read_source(browser_safety.PACKAGE / "db.py"))
    browser_safety.walk(tree)
    cached = (browser_safety.read_source, browser_safety._parse_cached, browser_safety._walk_cached)
    assert all(c.cache_info().currsize for c in cached)

    browser_safety.clear_caches()

    assert not any(c.cache_info().currsize for c in cached)
    # Every module that fills the caches empties them when it ends.
    for module in (browser_safety, test_posture, test_extractor_boundary):
        assert module.ast_caches is browser_safety.ast_caches


def _raise(error: BaseException) -> None:
    try:
        try:
            raise KeyError("first")
        except KeyError:
            raise error  # noqa: B904  (chained on purpose: the context goes too)
    except type(error):
        pass


@pytest.mark.parametrize("error", [ValueError("a parameter")], ids=["param"])
def test_a_long_lived_exception_forgets_the_test_that_raised_it(
    request: pytest.FixtureRequest, error: ValueError
) -> None:
    for each in (RAISED, error):
        _raise(each)
        assert each.__traceback__ is not None and each.__context__ is not None

    heap.pytest_runtest_teardown(request.node)

    for each in (RAISED, error):
        assert each.__traceback__ is None and each.__context__ is None
