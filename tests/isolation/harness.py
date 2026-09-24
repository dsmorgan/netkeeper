"""Run one list endpoint as two seeded users and an unseeded one, and compare counts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from string import Formatter

import httpx
from fastapi import FastAPI, Request
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.models import User, UserKind
from netkeeper.web.security import CLIENT_HEADER, CLIENT_HEADER_VALUE

from .registry import Body, ListEndpoint


@dataclass(frozen=True)
class FixedUser:
    """An auth provider that resolves every request to one user."""

    user_id: int

    def current_user(self, request: Request, session: Session) -> User:
        user = session.get(User, self.user_id)
        if user is None:
            raise RuntimeError(f"no user with id {self.user_id}")
        return user


@contextmanager
def acting_as(app: FastAPI, user_id: int) -> Iterator[None]:
    """Resolve every request to ``user_id`` for the block, then put the app's auth back.

    How a test acts as a second user: the local app has one auth provider, and
    swapping it is how the harness crosses users. Tests outside this package use
    it for the same reason.
    """
    previous = app.state.auth
    app.state.auth = FixedUser(user_id)
    try:
        yield
    finally:
        app.state.auth = previous


@dataclass(frozen=True)
class _Call:
    """One request the harness makes: as which user, at which path, with what, expecting what."""

    user_id: int
    url: str
    body: Body | None
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

    A ``POST`` list is called with ``endpoint.body`` (built per user when it is a
    callable) and the CSRF header; the crossed call sends the owner's body, and
    happens whenever the url *or* the body differs between the two users — a
    body naming the owner's list is as much a crossed request as a url naming
    their contact.
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
            calls.append(
                _Call(
                    user.id,
                    _url(endpoint, session, user),
                    _body(endpoint, session, user),
                    seeded,
                    seeded=True,
                )
            )
        calls.append(
            _Call(
                nobody.id,
                _url(endpoint, session, nobody),
                _body(endpoint, session, nobody),
                0,
                seeded=False,
            )
        )
    for call in calls:
        await _check(app, endpoint, call)
    seeded_calls = [call for call in calls if call.seeded]
    for viewer in seeded_calls:
        for owner in seeded_calls:
            if viewer is not owner and _crossable(viewer, owner):
                await _check_crossed(app, endpoint, viewer, owner)


def _crossable(viewer: _Call, owner: _Call) -> bool:
    """Whether asking ``viewer`` for ``owner``'s call names anything of the owner's.

    The url is one way a request names a resource; a ``POST`` list's body is the
    other, and a body built per user (``ListEndpoint.body`` as a callable) is how
    an endpoint says which of the owner's rows it is about — a list id, say. When
    both are the same for both users there is nothing to cross.
    """
    return viewer.url != owner.url or viewer.body != owner.body


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


def _body(endpoint: ListEndpoint, session: Session, user: User) -> Body | None:
    if endpoint.method.upper() == "GET":
        assert endpoint.body is None, f"{endpoint.path}: a GET list takes no body"
        return None
    if callable(endpoint.body):
        return endpoint.body(session, user)
    return endpoint.body if endpoint.body is not None else {}


async def _check(app: FastAPI, endpoint: ListEndpoint, call: _Call) -> None:
    response = await _request_as(app, call.user_id, endpoint, call.url, call.body)
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
    response = await _request_as(app, viewer.user_id, endpoint, owner.url, owner.body)
    where = f"{endpoint.path} as user {viewer.user_id} at user {owner.user_id}'s {owner.url}"
    if response.status_code == 404:
        return
    assert response.status_code == 200, f"{where}: {response.status_code} {response.text}"
    got = endpoint.count(response.json())
    assert got == 0, (
        f"{where}: expected 404 or 0 items, got {got}; is the parent lookup scoped to the user?"
    )


async def _request_as(
    app: FastAPI, user_id: int, endpoint: ListEndpoint, url: str, body: Body | None
) -> httpx.Response:
    with acting_as(app, user_id):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            if endpoint.method.upper() == "GET":
                return await client.get(url)
            return await client.request(
                endpoint.method, url, json=body, headers={CLIENT_HEADER: CLIENT_HEADER_VALUE}
            )
