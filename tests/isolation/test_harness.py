"""The harness's own tests: a throwaway list endpoint over settings_kv on a bare app."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
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
from .harness import assert_isolated, path_fields
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


def _probe_app(engine: Engine, lister: Lister, *, missing_is_404: bool = False) -> FastAPI:
    """A bare app with the state the harness needs and list routes over settings_kv.

    ``missing_is_404`` makes the parameterized route answer ``404`` when the user
    has no key under the prefix, the way a route under a parent resource does
    when the parent is not the user's.
    """
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

    @router.get("/prefix/{prefix}/keys", operation_id="probe_list_prefixed_keys")
    def list_prefixed_keys(prefix: str, user: CurrentUser, session: SessionDep) -> list[str]:
        keys = [row.key for row in lister(session, user) if row.key.startswith(prefix)]
        if not keys and missing_is_404:
            raise HTTPException(status_code=404, detail="no such prefix")
        return keys

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


def _seed_one_themed(session: Session, user: User) -> int:
    """The same two settings; one of them starts with ``the``."""
    _seed_two_settings(session, user)
    return 1


def _theme_prefix(session: Session, user: User) -> dict[str, str]:
    return {"prefix": "the"}


PROBE_PAGED = ListEndpoint(f"{API_PREFIX}/probe/settings", _seed_two_settings, paged_count)
PROBE_ARRAY = ListEndpoint(f"{API_PREFIX}/probe/keys", _seed_two_settings, array_count)
PROBE_PREFIXED = ListEndpoint(
    f"{API_PREFIX}/probe/prefix/{{prefix}}/keys",
    _seed_one_themed,
    array_count,
    path_params=_theme_prefix,
)


def test_discovery_finds_the_probe_lists(engine: Engine) -> None:
    app = _probe_app(engine, _scoped_rows)
    assert list_operations(app.openapi()) == {
        PROBE_PAGED.path,
        PROBE_ARRAY.path,
        PROBE_PREFIXED.path,
    }


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


# --- parameterized paths ----------------------------------------------------


def test_path_fields_names_the_placeholders() -> None:
    assert path_fields("/api/v1/contacts/{contact_id}/interactions") == ["contact_id"]
    assert path_fields("/api/v1/a/{x}/b/{y}") == ["x", "y"]
    assert path_fields("/api/v1/contacts") == []


@pytest.mark.parametrize("missing_is_404", [False, True], ids=["empty-list", "404"])
async def test_harness_formats_a_parameterized_path_per_user(
    engine: Engine, missing_is_404: bool
) -> None:
    """The unseeded user may get an empty list or a 404 for a resource of their own."""
    app = _probe_app(engine, _scoped_rows, missing_is_404=missing_is_404)
    await assert_isolated(app, PROBE_PREFIXED)


async def test_harness_fails_a_parameterized_endpoint_that_lists_every_user(
    engine: Engine,
) -> None:
    with pytest.raises(AssertionError, match=r"prefix/the/keys\): expected 1 items, got 2"):
        await assert_isolated(_probe_app(engine, _every_users_rows), PROBE_PREFIXED)


async def test_harness_does_not_accept_404_for_a_seeded_user(engine: Engine) -> None:
    """A 404 is only isolation for the unseeded user; for a seeded one it is a broken route."""
    app = _probe_app(engine, _scoped_rows, missing_is_404=True)
    endpoint = ListEndpoint(
        PROBE_PREFIXED.path,
        _seed_one_themed,
        array_count,
        path_params=lambda session, user: {"prefix": "nothing-starts-with-this"},
    )
    with pytest.raises(AssertionError, match=r"as user \d+ \(.*\): 404"):
        await assert_isolated(app, endpoint)


async def test_harness_requires_path_params_for_a_parameterized_path(engine: Engine) -> None:
    endpoint = ListEndpoint(PROBE_PREFIXED.path, _seed_one_themed, array_count)
    with pytest.raises(AssertionError, match=r"path parameters \['prefix'\] but no path_params"):
        await assert_isolated(_probe_app(engine, _scoped_rows), endpoint)


async def test_harness_requires_a_value_for_every_placeholder(engine: Engine) -> None:
    endpoint = ListEndpoint(
        PROBE_PREFIXED.path,
        _seed_one_themed,
        array_count,
        path_params=lambda session, user: {"other": "x"},
    )
    with pytest.raises(AssertionError, match=r"no value for \['prefix'\]"):
        await assert_isolated(_probe_app(engine, _scoped_rows), endpoint)
