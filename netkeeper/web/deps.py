"""Request-scoped dependencies: the session, the current user, the bus, the runner.

The auth provider is the request-side half of P0-09 (spec section 5, ADR 0005);
the scoping helper and the runtime query guard are :mod:`netkeeper.scoping`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Annotated, Any, Protocol

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.crm.confirmation import Signer
from netkeeper.db import session_scope
from netkeeper.models import User, UserKind
from netkeeper.services import ui_settings
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

    "Only reads" is about the database. A handler that changes in-memory state
    outside it, and no row, may carry the mark: Gmail "Check now" (#409) sets a
    flag on the running sender and reads the mailboxes to refuse.
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


def _session(request: Request) -> Iterator[Session]:
    """One session for the request: a writer unless :func:`writes` says otherwise.

    Two dependencies share this body, :data:`SessionDep` and
    :data:`StreamingSessionDep`, because write-ness (:func:`writes`) and how long
    the session lives (``scope=``) are independent: most handlers return their
    response in one step, so their session can end the moment the handler does
    (``scope="function"``) — before the response is sent back to the client.

    A handler that returns a ``StreamingResponse`` (an export, P1-11) cannot use
    that: it reads from the database *while* the response is sending, as the
    generator is pulled. ``Session.close()`` does not invalidate the session —
    it does not raise on the next statement — it silently opens a fresh
    transaction that this dependency never gets a chance to close, one per
    streaming request, forever; the pool's connections fill up with sessions
    nobody is coming back for until the app wedges. ``scope="request"``
    (:data:`StreamingSessionDep`) keeps the one session that started reading
    open until the response has finished sending, so it is also the one that
    finishes reading, and closes exactly once.

    That lifetime has a second benefit: it makes an offset-paginated stream
    snapshot-consistent. SQLite (and PostgreSQL, at the default isolation) reads
    a consistent view for the life of a transaction, so a row inserted or
    deleted by someone else after the export's transaction began is invisible
    to every page the export fetches — no page skips a row that moved past its
    offset or repeats one that moved into it.

    The price is deliberate: that one read transaction stays open for the whole
    download, and on SQLite in WAL mode an open reader stops a checkpoint from
    getting past it, so the WAL file grows for as long as a slow client takes to
    drain a large export. That is the trade for a consistent file. The SSE stream
    (``web/api/events.py``) makes the opposite choice for the same reason: it
    runs for as long as the tab is open, so it holds no session while streaming,
    and its user lookup uses the ordinary ``scope="function"`` session, which
    has closed before the first event is sent (#77).

    Neither dependency is ever a writer for a safe method or a
    :func:`read_only`-marked one; an export in particular is always a read.
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    with session_scope(factory, write=writes(request)) as session:
        yield session


SessionDep = Annotated[Session, Depends(_session, scope="function")]
StreamingSessionDep = Annotated[Session, Depends(_session, scope="request")]


def current_user(request: Request, session: SessionDep) -> User:
    auth: AuthProvider = request.app.state.auth
    return auth.current_user(request, session)


CurrentUser = Annotated[User, Depends(current_user)]


def file_settings(request: Request) -> Settings:
    """What ``serve`` loaded at startup: ``config.toml`` over the defaults, never the
    Settings page's values (those are per user: :func:`effective_settings`)."""
    settings: Settings = request.app.state.settings
    return settings


def effective_settings(request: Request, session: Session, user: User) -> Settings:
    """The settings in force for ``user`` now: ``config.toml``, then the Settings page,
    then the defaults (#343). Resolved per request, so a change applies to the next one."""
    return ui_settings.resolve(session, user, file_settings(request))


def running_settings(request: Request, session: Session, user: User) -> Settings:
    """:func:`effective_settings`, except for what a running ``serve`` read once at
    startup and still uses until it restarts: the reply poll interval. Posture and the
    poll status judge whether a poll is late by the interval actually running (#343)."""
    settings = effective_settings(request, session, user)
    started: Settings | None = getattr(request.app.state, "started_settings", None)
    if started is None:
        return settings
    minutes = started.campaigns.reply_poll_minutes
    return replace(settings, campaigns=replace(settings.campaigns, reply_poll_minutes=minutes))


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
