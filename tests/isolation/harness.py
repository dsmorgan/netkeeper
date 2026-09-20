"""Run one list endpoint as two seeded users and an unseeded one, and compare counts."""

from __future__ import annotations

from dataclasses import dataclass

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


async def assert_isolated(app: FastAPI, endpoint: ListEndpoint) -> None:
    """Fail unless each user sees exactly the rows seeded for them.

    Creates users A and B and seeds each through ``endpoint.seed`` on
    ``app.state.session_factory``, then calls ``endpoint.path`` as A, as B, and as
    a third user with nothing seeded, swapping ``app.state.auth`` for each call.
    The unseeded user catches an endpoint that always answers for one fixed user;
    the two seeded users catch one that answers for everyone.
    """
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory) as session:
        a = User(kind=UserKind.HOSTED, display_name="A")
        b = User(kind=UserKind.HOSTED, display_name="B")
        nobody = User(kind=UserKind.HOSTED, display_name="nobody")
        session.add_all([a, b, nobody])
        session.flush()
        expected = {a.id: endpoint.seed(session, a), b.id: endpoint.seed(session, b)}
        for user_id, seeded in expected.items():
            assert seeded > 0, f"{endpoint.path}: seed created no rows for user {user_id}"
        expected[nobody.id] = 0
    for user_id, want in expected.items():
        got = await _count_as(app, user_id, endpoint)
        assert got == want, f"{endpoint.path} as user {user_id}: expected {want} items, got {got}"


async def _count_as(app: FastAPI, user_id: int, endpoint: ListEndpoint) -> int:
    previous = app.state.auth
    app.state.auth = FixedUser(user_id)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(endpoint.path)
    finally:
        app.state.auth = previous
    assert response.status_code == 200, (
        f"{endpoint.path} as user {user_id}: {response.status_code} {response.text}"
    )
    return endpoint.count(response.json())
