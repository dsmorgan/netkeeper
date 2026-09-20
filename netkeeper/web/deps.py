"""Request-scoped dependencies: the session, the current user, the bus, the runner.

The auth provider here is the P0-09 slice this app needs for ``/me``; the scoping
helper and the runtime query guard follow in that item (spec section 5, ADR 0005).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Protocol

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.models import User, UserKind
from netkeeper.services.events import EventBus
from netkeeper.services.tasks import TaskRunner


class AuthProvider(Protocol):
    """Resolves the user a request acts as. One v1 implementation: :class:`LocalSingleUser`."""

    def current_user(self, request: Request, session: Session) -> User: ...


class LocalSingleUser:
    """No login: the only ``local`` user is the current user."""

    def current_user(self, request: Request, session: Session) -> User:
        user = session.scalars(
            select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)
        ).first()
        if user is None:
            raise HTTPException(
                status_code=500,
                detail="no local user exists; run `netkeeper db upgrade` to create it",
            )
        return user


def get_session(request: Request) -> Iterator[Session]:
    """One session per request: commits when the handler returns, rolls back on error.

    Declared with ``scope="function"`` below so the session closes before the
    response is sent. A streaming response (the SSE stream) would otherwise hold a
    SQLite read transaction open for as long as the client stays connected.
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    with session_scope(factory) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session, scope="function")]


def current_user(request: Request, session: SessionDep) -> User:
    auth: AuthProvider = request.app.state.auth
    return auth.current_user(request, session)


CurrentUser = Annotated[User, Depends(current_user)]


def get_bus(request: Request) -> EventBus:
    bus: EventBus = request.app.state.bus
    return bus


def get_tasks(request: Request) -> TaskRunner:
    tasks: TaskRunner = request.app.state.tasks
    return tasks


Bus = Annotated[EventBus, Depends(get_bus)]
Tasks = Annotated[TaskRunner, Depends(get_tasks)]
