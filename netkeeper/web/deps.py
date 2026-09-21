"""Request-scoped dependencies: the session, the current user, the bus, the runner.

The auth provider is the request-side half of P0-09 (spec section 5, ADR 0005);
the scoping helper and the runtime query guard are :mod:`netkeeper.scoping`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Annotated, Any, Protocol

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm.confirmation import Signer
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


# Methods that never write (RFC 9110 "safe methods"). Every other request gets a
# writer session, so on SQLite it takes the write lock before its first read.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Attribute :func:`read_only` sets on a handler, and :func:`writes` reads back.
READ_ONLY_ATTR = "__netkeeper_read_only__"


def read_only[Endpoint: Callable[..., Any]](endpoint: Endpoint) -> Endpoint:
    """Mark a ``POST`` (or other unsafe method) handler that only reads (#62).

    A query whose input does not fit a query string has to be a ``POST`` even
    though it writes nothing: the contacts query carries a filter tree, the
    auto-tag preview carries a pattern. Without this mark such a request takes a
    writer session, and on SQLite that is ``BEGIN IMMEDIATE`` — the contacts
    table's live filter would block every other writer while it scans the
    address book.

    Put it under the route decorator, so the router registers the marked
    function::

        @router.post("/contacts/query")
        @read_only
        def query_contacts(...) -> ContactPage: ...

    The mark is a claim about the handler, not a guard on the session. A marked
    handler that calls a service writer gets ``RuntimeError`` from that
    service's ``_require_writer``, which is most of the surface; but a raw
    ``session.add()`` and flush is not caught anywhere and will commit, on a
    connection that never took the write lock. So the review question for a new
    mark is "does every path through this handler only read", and
    ``tests/test_web_deps.py`` keeps the list of marked routes, so adding one is
    a visible change. ``tests/test_web_deps.py`` also fails a marked route whose
    method is safe already, which would only be noise.
    """
    setattr(endpoint, READ_ONLY_ATTR, True)
    return endpoint


def writes(request: Request) -> bool:
    """Whether ``request`` needs a writer session: an unsafe method, not :func:`read_only`.

    The matched route is on the ASGI scope by the time dependencies are solved,
    so the mark is read from the handler itself and never depends on the order
    dependencies resolve in.
    """
    if request.method in SAFE_METHODS:
        return False
    endpoint = getattr(request.scope.get("route"), "endpoint", None)
    return not getattr(endpoint, READ_ONLY_ATTR, False)


def get_session(request: Request) -> Iterator[Session]:
    """One session per request: commits when the handler returns, rolls back on error.

    A request whose method may write (anything but the safe methods, unless the
    handler is :func:`read_only`) gets a writer session
    (:func:`netkeeper.db.mark_for_write`); every other request's session stays a
    reader and never waits for the SQLite write lock.

    Declared with ``scope="function"`` below so the session closes before the
    response is sent. A streaming response (the SSE stream) would otherwise hold a
    SQLite read transaction open for as long as the client stays connected.
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    with session_scope(factory, write=writes(request)) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session, scope="function")]


def get_reader_session(request: Request) -> Iterator[Session]:
    """A read-only session kept open for the whole response, including while it streams.

    :func:`get_session` closes before the response is sent (``scope="function"``),
    which is wrong for an endpoint that streams its body from the database as it
    goes (an export, P1-11): the generator a ``StreamingResponse`` iterates would
    be reading through an already-closed session. This dependency instead uses
    ``scope="request"``, so it stays open until the response has finished sending.
    It is never marked for write (:func:`netkeeper.db.session_scope`'s default):
    a read has no business taking the SQLite write lock.
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    with session_scope(factory) as session:
        yield session


ReaderSessionDep = Annotated[Session, Depends(get_reader_session, scope="request")]


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


def get_confirmations(request: Request) -> Signer:
    """The process's count confirmation signer (:mod:`netkeeper.crm.confirmation`)."""
    signer: Signer = request.app.state.confirmations
    return signer


Bus = Annotated[EventBus, Depends(get_bus)]
Tasks = Annotated[TaskRunner, Depends(get_tasks)]
Confirmations = Annotated[Signer, Depends(get_confirmations)]
