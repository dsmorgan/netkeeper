"""Request sessions (``netkeeper.web.deps.get_session``): writers for methods that may write."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from sqlalchemy import Engine, event

from netkeeper.db import is_writer, make_session_factory
from netkeeper.web.app import API_PREFIX
from netkeeper.web.deps import SAFE_METHODS, SessionDep
from netkeeper.web.security import CLIENT_HEADER, CLIENT_HEADER_VALUE

METHODS = ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]


def _probe_app(engine: Engine) -> FastAPI:
    """A bare app whose one route records whether its session is a writer."""
    router = APIRouter(prefix="/probe")

    @router.api_route("/mode", methods=METHODS, operation_id="probe_mode")
    def mode(session: SessionDep) -> dict[str, bool]:
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
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
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
