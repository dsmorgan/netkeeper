"""The harness's own tests: a throwaway list endpoint over settings_kv on a bare app."""

from __future__ import annotations

from collections.abc import Callable, Sequence

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
from .registry import ListEndpoint, array_count, paged_count


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
