"""Request sessions (``netkeeper.web.deps.get_session``): writers for methods that may write.

A handler marked :func:`~netkeeper.web.deps.read_only` opts back out (#62): a
``POST`` that only reads, because its input does not fit a query string, must
not take the SQLite write lock.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import Engine, event

from netkeeper.db import is_writer, make_session_factory
from netkeeper.web.app import API_PREFIX, discover_routers
from netkeeper.web.deps import READ_ONLY_ATTR, SAFE_METHODS, SessionDep, read_only
from netkeeper.web.security import CLIENT_HEADER, CLIENT_HEADER_VALUE

METHODS = ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]


def _probe_app(engine: Engine) -> FastAPI:
    """A bare app whose routes record whether their session is a writer."""
    router = APIRouter(prefix="/probe")

    @router.api_route("/mode", methods=METHODS, operation_id="probe_mode")
    def mode(session: SessionDep) -> dict[str, bool]:
        app.state.seen.append(is_writer(session))
        return {"writer": is_writer(session)}

    @router.api_route("/reader", methods=METHODS, operation_id="probe_reader")
    @read_only
    def reader(session: SessionDep) -> dict[str, bool]:
        app.state.seen.append(is_writer(session))
        return {"writer": is_writer(session)}

    app = FastAPI()
    app.include_router(router, prefix=API_PREFIX)
    app.state.session_factory = make_session_factory(engine)
    app.state.seen = []
    return app


@pytest.mark.parametrize("method", METHODS)
async def test_request_session_is_a_writer_unless_the_method_is_safe(
    engine: Engine, method: str
) -> None:
    app = _probe_app(engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.request(method, f"{API_PREFIX}/probe/mode")
    assert response.status_code == 200
    assert app.state.seen == [method not in SAFE_METHODS]


async def test_the_real_app_begins_immediate_for_a_post(
    client: httpx.AsyncClient, bare_engine: Engine
) -> None:
    begins: list[str] = []

    @event.listens_for(bare_engine, "before_cursor_execute")
    def record(*args: Any) -> None:
        statement: str = args[2]
        if statement.startswith("BEGIN"):
            begins.append(statement)

    assert (await client.get(f"{API_PREFIX}/me")).status_code == 200
    assert begins == ["BEGIN"]
    headers = {CLIENT_HEADER: CLIENT_HEADER_VALUE}
    assert (await client.post(f"{API_PREFIX}/tasks/ping", headers=headers)).status_code == 202
    assert begins == ["BEGIN", "BEGIN IMMEDIATE"]


# --- the read-only opt-out (#62) --------------------------------------------


@pytest.mark.parametrize("method", METHODS)
async def test_a_read_only_handler_never_gets_a_writer_session(engine: Engine, method: str) -> None:
    app = _probe_app(engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.request(method, f"{API_PREFIX}/probe/reader")
    assert response.status_code == 200
    assert app.state.seen == [False]


def test_read_only_marks_the_function_and_gives_it_back() -> None:
    def handler() -> None: ...

    assert read_only(handler) is handler
    assert getattr(handler, READ_ONLY_ATTR) is True


def test_read_only_is_used_only_on_unsafe_methods() -> None:
    """Marking a safe method would be noise: its session is a reader already."""
    marked = _marked()
    assert marked, "no route uses read_only; drop it or use it"
    assert all(method not in SAFE_METHODS for _, method in marked), marked


def test_the_read_only_routes_are_the_ones_that_only_read() -> None:
    """Listed here so a route that starts writing has to come back and change this."""
    assert {operation for operation, _ in _marked()} == {
        "query_contacts",
        "count_bulk_contacts",
        "preview_autotag_rule",
        "lint_template",
    }


def _marked() -> list[tuple[str, str]]:
    """``(operation id, method)`` for every handler carrying the read-only mark."""
    return [
        (route.operation_id or route.name, method)
        for _, router in discover_routers()
        for route in router.routes
        if isinstance(route, APIRoute) and getattr(route.endpoint, READ_ONLY_ATTR, False)
        for method in route.methods or ()
    ]


async def test_the_real_app_begins_plain_for_a_read_only_post(
    client: httpx.AsyncClient, bare_engine: Engine
) -> None:
    begins: list[str] = []

    @event.listens_for(bare_engine, "before_cursor_execute")
    def record(*args: Any) -> None:
        statement: str = args[2]
        if statement.startswith("BEGIN"):
            begins.append(statement)

    headers = {CLIENT_HEADER: CLIENT_HEADER_VALUE}
    response = await client.post(f"{API_PREFIX}/contacts/query", json={}, headers=headers)
    assert response.status_code == 200, response.text
    assert begins == ["BEGIN"], "a read-only POST must not take the SQLite write lock (#62)"
