"""The two-user isolation test (spec section 5, ADR 0005), and the harness's own tests."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from netkeeper.db import make_session_factory
from netkeeper.models import JsonValue, SettingKV, User
from netkeeper.scoping import UnscopedQueryError, install_scope_guard, scoped, unscoped
from netkeeper.services.settings_kv import set_setting
from netkeeper.web.app import API_PREFIX
from netkeeper.web.deps import CurrentUser, LocalSingleUser, SessionDep

from .discovery import list_operations
from .harness import assert_isolated
from .registry import REGISTRY, ListEndpoint, array_count, paged_count

# --- the real app -----------------------------------------------------------


def test_every_list_operation_is_registered(app: FastAPI) -> None:
    discovered = list_operations(app.openapi())
    registered = {endpoint.path for endpoint in REGISTRY}
    missing = sorted(discovered - registered)
    assert not missing, (
        f"list endpoints without an isolation test: {missing}. Add a ListEndpoint for "
        "each to REGISTRY in tests/isolation/registry.py; every list endpoint has a "
        "two-user isolation test (ADR 0005)."
    )
    stale = sorted(registered - discovered)
    assert not stale, f"registered paths that are not list operations in the schema: {stale}"


@pytest.mark.parametrize(
    "endpoint", REGISTRY or [None], ids=[e.path for e in REGISTRY] or ["registry-empty"]
)
async def test_registered_list_endpoints_are_isolated(
    endpoint: ListEndpoint | None, running_app: FastAPI
) -> None:
    if endpoint is None:
        pytest.skip("REGISTRY is empty: no list endpoints exist yet")
    await assert_isolated(running_app, endpoint)


# --- discovery on a hand-written schema -------------------------------------


def _get_json(schema: dict[str, Any]) -> dict[str, Any]:
    return {"get": {"responses": {"200": {"content": {"application/json": {"schema": schema}}}}}}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


HANDWRITTEN: dict[str, Any] = {
    "paths": {
        "/api/v1/contacts": _get_json(_ref("ContactPage")),
        "/api/v1/tags": _get_json({"type": "array", "items": _ref("Tag")}),
        "/api/v1/lists": _get_json(_ref("ListPageAlias")),
        "/api/v1/me": _get_json(_ref("User")),
        "/api/v1/contacts/{id}": {
            **_get_json(_ref("Contact")),
            "delete": {"responses": {"204": {"description": "Deleted"}}},
        },
        "/api/v1/contacts/import": {
            "post": {
                "responses": {
                    "200": {"content": {"application/json": {"schema": {"type": "array"}}}}
                }
            }
        },
        "/api/v1/stats": _get_json(
            {"type": "object", "properties": {"items": {"type": "integer"}}}
        ),
        "/api/v1/events": {"get": {"responses": {"200": {"content": {"text/event-stream": {}}}}}},
        "/api/v1/broken": _get_json(_ref("Missing")),
        "/api/v1/health": {"get": {"responses": {"200": {"description": "no content"}}}},
    },
    "components": {
        "schemas": {
            "ContactPage": {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": _ref("Contact")},
                    "total": {"type": "integer"},
                },
            },
            "ListPageAlias": _ref("ListPage"),
            "ListPage": {"type": "object", "properties": {"items": _ref("ListItems")}},
            "ListItems": {"type": "array", "items": {"type": "string"}},
            "Contact": {"type": "object", "properties": {"id": {"type": "integer"}}},
            "Tag": {"type": "object"},
            "User": {"type": "object"},
        }
    },
}


def test_list_operations_on_a_handwritten_schema() -> None:
    assert list_operations(HANDWRITTEN) == {"/api/v1/contacts", "/api/v1/tags", "/api/v1/lists"}


def test_list_operations_on_an_empty_schema() -> None:
    assert list_operations({}) == set()
    assert list_operations({"paths": {}}) == set()


def test_list_operations_survives_a_ref_cycle() -> None:
    schema = {
        "paths": {"/loop": _get_json(_ref("A"))},
        "components": {"schemas": {"A": _ref("B"), "B": _ref("A")}},
    }
    assert list_operations(schema) == set()


# --- the harness's own tests: a throwaway list endpoint over settings_kv ----


class SettingOut(BaseModel):
    key: str
    value: JsonValue


class SettingPage(BaseModel):
    items: list[SettingOut]
    total: int


Lister = Callable[[Session, User], Sequence[SettingKV]]


def _scoped_rows(session: Session, user: User) -> Sequence[SettingKV]:
    return session.scalars(scoped(user, SettingKV)).all()


def _every_users_rows(session: Session, user: User) -> Sequence[SettingKV]:
    """Wrong on purpose: everyone's rows, let through with the guard's escape hatch."""
    return session.scalars(unscoped(select(SettingKV))).all()


def _bare_rows(session: Session, user: User) -> Sequence[SettingKV]:
    """Wrong on purpose: bypasses the helper, so the guard fires inside the request."""
    return session.scalars(select(SettingKV)).all()


def _probe_app(engine: Engine, lister: Lister) -> FastAPI:
    """A bare app with the state the harness needs and list routes over settings_kv."""
    router = APIRouter(prefix="/probe")

    @router.get("/settings", operation_id="probe_list_settings")
    def list_settings(user: CurrentUser, session: SessionDep) -> SettingPage:
        rows = lister(session, user)
        items = [SettingOut(key=row.key, value=row.value) for row in rows]
        return SettingPage(items=items, total=len(items))

    @router.get("/keys", operation_id="probe_list_keys")
    def list_keys(user: CurrentUser, session: SessionDep) -> list[str]:
        return [row.key for row in lister(session, user)]

    @router.get("/count", operation_id="probe_count")
    def count(user: CurrentUser, session: SessionDep) -> dict[str, int]:
        return {"total": len(lister(session, user))}

    app = FastAPI()
    app.include_router(router, prefix=API_PREFIX)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    app.state.session_factory = factory
    app.state.auth = LocalSingleUser()
    return app


def _seed_two_settings(session: Session, user: User) -> int:
    set_setting(session, user, "theme", "dark")
    set_setting(session, user, "pace", {"per_day": 20})
    return 2


PROBE_PAGED = ListEndpoint(f"{API_PREFIX}/probe/settings", _seed_two_settings, paged_count)
PROBE_ARRAY = ListEndpoint(f"{API_PREFIX}/probe/keys", _seed_two_settings, array_count)


def test_discovery_finds_the_probe_lists(engine: Engine) -> None:
    app = _probe_app(engine, _scoped_rows)
    assert list_operations(app.openapi()) == {PROBE_PAGED.path, PROBE_ARRAY.path}


@pytest.mark.parametrize("endpoint", [PROBE_PAGED, PROBE_ARRAY], ids=["paged", "array"])
async def test_harness_passes_a_scoped_endpoint(engine: Engine, endpoint: ListEndpoint) -> None:
    await assert_isolated(_probe_app(engine, _scoped_rows), endpoint)


async def test_harness_fails_an_endpoint_that_lists_every_user(engine: Engine) -> None:
    with pytest.raises(
        AssertionError, match=r"probe/settings as user \d+: expected 2 items, got 4"
    ):
        await assert_isolated(_probe_app(engine, _every_users_rows), PROBE_PAGED)


async def test_guard_stops_an_endpoint_that_bypasses_the_helper(engine: Engine) -> None:
    with pytest.raises(UnscopedQueryError, match="SettingKV"):
        await assert_isolated(_probe_app(engine, _bare_rows), PROBE_PAGED)


async def test_harness_restores_the_auth_provider(engine: Engine) -> None:
    app = _probe_app(engine, _scoped_rows)
    before = app.state.auth
    await assert_isolated(app, PROBE_PAGED)
    assert app.state.auth is before
