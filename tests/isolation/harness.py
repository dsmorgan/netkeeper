"""Run one list endpoint as two seeded users and an unseeded one, and compare counts."""

from __future__ import annotations

from dataclasses import dataclass
from string import Formatter

import httpx
from fastapi import FastAPI, Request
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.models import User, UserKind

from .registry import ListEndpoint


@dataclass(frozen=True)
class FixedUser:
    """An auth provider that resolves every request to one user."""

    user_id: int

    def current_user(self, request: Request, session: Session) -> User:
        user = session.get(User, self.user_id)
        if user is None:
            raise RuntimeError(f"no user with id {self.user_id}")
        return user


@dataclass(frozen=True)
class _Call:
    """One request the harness makes: as which user, at which path, expecting what."""

    user_id: int
    url: str
    want: int
    seeded: bool


async def assert_isolated(app: FastAPI, endpoint: ListEndpoint) -> None:
    """Fail unless each user sees exactly the rows seeded for them.

    Creates users A and B and seeds each through ``endpoint.seed`` on
    ``app.state.session_factory``, then calls ``endpoint.path`` as A, as B, and as
    a third user with nothing seeded, swapping ``app.state.auth`` for each call.
    The unseeded user catches an endpoint that always answers for one fixed user;
    the two seeded users catch one that answers for everyone.

    A path with parameters is formatted with ``endpoint.path_params`` for each
    user, the unseeded one included. That user's request may answer ``404``
    (their resource does not exist) or ``200`` with no items (it exists and is
    empty); the seeded users' requests must answer ``200`` with their counts.
    Then each seeded user calls the other's formatted path, which must answer
    ``404`` or ``200`` with no items: an endpoint whose parent lookup is not
    scoped would hand A the rows under B's resource. When the parameter names no
    resource (both users' paths are the same) there is nothing to cross.
    """
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:  # seeds read, then write
        a = User(kind=UserKind.HOSTED, display_name="A")
        b = User(kind=UserKind.HOSTED, display_name="B")
        nobody = User(kind=UserKind.HOSTED, display_name="nobody")
        session.add_all([a, b, nobody])
        session.flush()
        calls: list[_Call] = []
        for user in (a, b):
            seeded = endpoint.seed(session, user)
            assert seeded > 0, f"{endpoint.path}: seed created no rows for user {user.id}"
            calls.append(_Call(user.id, _url(endpoint, session, user), seeded, seeded=True))
        calls.append(_Call(nobody.id, _url(endpoint, session, nobody), 0, seeded=False))
    for call in calls:
        await _check(app, endpoint, call)
    seeded_calls = [call for call in calls if call.seeded]
    for viewer in seeded_calls:
        for owner in seeded_calls:
            if viewer is not owner and viewer.url != owner.url:
                await _check_crossed(app, endpoint, viewer, owner)


def path_fields(path: str) -> list[str]:
    """The placeholder names in ``path``, in order (``["contact_id"]``)."""
    return [name for _, name, _, _ in Formatter().parse(path) if name is not None]


def _url(endpoint: ListEndpoint, session: Session, user: User) -> str:
    fields = path_fields(endpoint.path)
    if not fields:
        return endpoint.path
    assert endpoint.path_params is not None, (
        f"{endpoint.path} has path parameters {fields} but no path_params callable"
    )
    params = endpoint.path_params(session, user)
    missing = [name for name in fields if name not in params]
    assert not missing, f"{endpoint.path}: path_params gave no value for {missing}"
    return endpoint.path.format(**params)


async def _check(app: FastAPI, endpoint: ListEndpoint, call: _Call) -> None:
    response = await _get_as(app, call.user_id, call.url)
    where = f"{endpoint.path} as user {call.user_id}"
    if call.url != endpoint.path:
        where += f" ({call.url})"
    if not call.seeded and response.status_code == 404 and endpoint.path_params is not None:
        return  # the unseeded user's own resource does not exist; that is isolation too
    assert response.status_code == 200, f"{where}: {response.status_code} {response.text}"
    got = endpoint.count(response.json())
    assert got == call.want, f"{where}: expected {call.want} items, got {got}"


async def _check_crossed(app: FastAPI, endpoint: ListEndpoint, viewer: _Call, owner: _Call) -> None:
    """``viewer`` asks for ``owner``'s resource: nothing of ``owner``'s may come back."""
    response = await _get_as(app, viewer.user_id, owner.url)
    where = f"{endpoint.path} as user {viewer.user_id} at user {owner.user_id}'s {owner.url}"
    if response.status_code == 404:
        return
    assert response.status_code == 200, f"{where}: {response.status_code} {response.text}"
    got = endpoint.count(response.json())
    assert got == 0, (
        f"{where}: expected 404 or 0 items, got {got}; is the parent lookup scoped to the user?"
    )


async def _get_as(app: FastAPI, user_id: int, url: str) -> httpx.Response:
    previous = app.state.auth
    app.state.auth = FixedUser(user_id)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(url)
    finally:
        app.state.auth = previous
