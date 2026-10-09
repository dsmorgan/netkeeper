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
#: Built with a cause and never raised: it keeps the cause.
CAUSED = LookupError("built with a cause")
CAUSED.__cause__ = KeyError("the cause")

#: Two test modules for pytester: the first fills test_browser_safety's caches through
#: ``ast_caches``, the second sees them empty.
FILLS = """
import test_browser_safety as browser_safety

ast_caches = browser_safety.ast_caches


def test_fills():
    browser_safety.parse("x = 1")
    assert browser_safety._parse_cached.cache_info().currsize
"""
SEES_EMPTY = """
import test_browser_safety as browser_safety


def test_sees_empty():
    assert browser_safety._parse_cached.cache_info().currsize == 0
"""


def _compiled_entries() -> int:
    return sum(len(m.__dict__.get("_compiled_cache", ())) for m in Base.registry.mappers)


def test_the_heap_after_collection_is_frozen() -> None:
    # The imported package alone is well over this; an unfrozen run has a few hundred.
    assert gc.get_freeze_count() > 50_000


def test_every_test_empties_the_caches_after_it(request: pytest.FixtureRequest) -> None:
    assert "_per_test_caches" in request.fixturenames


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


def test_the_syntax_tree_caches_are_empty_after_a_module_that_used_them(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(test_a_fills=FILLS, test_b_sees_empty=SEES_EMPTY)
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")
    pytester.runpytest_inprocess("-p", "no:cacheprovider").assert_outcomes(passed=2)


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
    assert isinstance(CAUSED.__cause__, KeyError), "an exception never raised keeps its cause"


def test_the_postgresql_dialects_query_caches_are_dropped() -> None:
    from sqlalchemy.dialects.postgresql.base import PGDialect
    from sqlalchemy.engine.reflection import ObjectKind, ObjectScope

    assert heap._postgresql_dialect_caches(), "PGDialect no longer caches queries in lru_caches"
    dialect = PGDialect()  # type: ignore[no-untyped-call]
    dialect._table_oids_query(None, False, ObjectScope.ANY, ObjectKind.TABLE)
    assert PGDialect._table_oids_query.cache_info().currsize

    heap.clear_per_test_caches()

    assert PGDialect._table_oids_query.cache_info().currsize == 0
