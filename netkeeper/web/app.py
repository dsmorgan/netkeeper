"""The FastAPI application factory (spec sections 5 and 14).

:func:`create_app` builds the app. Its lifespan builds the engine (or takes the
one passed in), migrates the database to head, makes sure the local user exists
with the default auto-tag rules seeded, installs the scope guard on the session
factory, marks runs a stopped process left ``running`` as failed, and starts
the event bus and the task runner, all kept on ``app.state``, along with the
signer behind bulk count confirmations. Given an extractor (``netkeeper
serve``), it also starts the scheduler (P2-10); scheduled LinkedIn runs fire
only on an account a person armed. API
modules under :mod:`netkeeper.web.api` are discovered, so adding an endpoint
never edits this file.
"""

from __future__ import annotations

import importlib
import json
import logging
import pkgutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import ModuleType

from fastapi import APIRouter, FastAPI
from sqlalchemy import Engine

from netkeeper import __version__, migrations
from netkeeper.config import Settings, load_settings
from netkeeper.crm.confirmation import Signer
from netkeeper.crm.lists import ensure_validated_list
from netkeeper.crm.tags import ensure_default_rules
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin import activity_lock
from netkeeper.models.base import utcnow
from netkeeper.scoping import install_scope_guard
from netkeeper.services.events import EventBus
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.runs import fail_interrupted_runs
from netkeeper.services.scheduled_runs import ServeExtractor, ServeScheduler, start_serve_scheduler
from netkeeper.services.tasks import TaskRunner
from netkeeper.services.users import ensure_local_user
from netkeeper.web import api as api_package
from netkeeper.web.deps import LocalSingleUser
from netkeeper.web.errors import install_error_handlers
from netkeeper.web.frontend import frontend_dist, mount_frontend
from netkeeper.web.security import CSRFMiddleware

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    extractor: ServeExtractor | None = None,
) -> FastAPI:
    """Build the application without starting it.

    ``settings`` default to :func:`load_settings`. ``engine`` defaults to one built
    from :func:`database_url` at startup and disposed at shutdown; an engine passed
    in (tests) is left to whoever built it.

    ``extractor`` is the LinkedIn extractor half of ``netkeeper serve``
    (``netkeeper.worker.serve_extractor``): with one, the lifespan builds the
    run executor and starts the scheduler, whose scheduled runs fire only on an
    account a person armed. Without one no scheduler starts and the runs API
    answers ``503`` to a start; everything else works the same.
    """
    resolved = load_settings() if settings is None else settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active = engine if engine is not None else make_engine(database_url())
        tasks: TaskRunner | None = None
        serving: ServeScheduler | None = None
        try:
            tasks = _start(app, active, resolved)
            if extractor is not None:
                serving = start_serve_scheduler(
                    extractor,
                    app.state.session_factory,
                    app.state.bus,
                    tasks,
                    resolved.linkedin,
                )
                app.state.executor = serving.executor
                app.state.scheduler = serving.scheduler
            yield
        finally:
            if serving is not None:
                serving.stop()
            if tasks is not None:
                await tasks.cancel_all()
            if engine is None:
                active.dispose()

    app = FastAPI(
        title="netkeeper",
        version=__version__,
        description="Keep your professional network warm.",
        lifespan=lifespan,
    )
    app.add_middleware(CSRFMiddleware)
    install_error_handlers(app)
    for name, router in discover_routers():
        app.include_router(router, prefix=API_PREFIX)
        log.debug("mounted api module %s", name)
    mount_frontend(app, frontend_dist())
    return app


def _start(app: FastAPI, engine: Engine, settings: Settings) -> TaskRunner:
    """Migrate, ensure the local user, guard the session factory, and fill ``app.state``."""
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:  # reads, then may insert the user
        user = ensure_local_user(session, settings=settings)
        ensure_default_rules(session, user)  # once per user; a deleted default stays deleted
        ensure_validated_list(session, user)  # once per user; a deleted "Validated" stays deleted
        ensure_account(session, user)  # the account budgets, heat, and runs belong to
        # Runs a stopped process left "running" are over; none is resumed on its own.
        # One whose account's browser lock is held right now belongs to a live
        # process (a `netkeeper linkedin sync` in a terminal) and is left alone.
        fail_interrupted_runs(session, now=utcnow(), browser_held=_browser_held)
        log.info(
            "database at revision %s, local user %d", migrations.current_revision(engine), user.id
        )
    bus = EventBus()
    tasks = TaskRunner(bus)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.bus = bus
    app.state.tasks = tasks
    app.state.executor = None
    app.state.scheduler = None
    app.state.auth = LocalSingleUser()
    # One signing key per process, never written down: an outstanding bulk
    # confirmation does not survive a restart, and nothing has to be cleaned up.
    app.state.confirmations = Signer.generated()
    return tasks


def _browser_held(account_id: int) -> bool:
    """Whether any process holds ``account_id``'s browser lock (or the legacy one).

    Reads the lock files only (a shared peek, dropped at once); never attaches.
    """
    try:
        key = activity_lock.account_key(account_id)
        if activity_lock.inspect(key).held:
            return True
        return (
            key == activity_lock.SINGLE_ACCOUNT_KEY
            and activity_lock.inspect(activity_lock.LEGACY_SHARED_KEY).held
        )
    except OSError:
        return True  # cannot tell: leave the run alone rather than fail a live one


def discover_routers(package: ModuleType = api_package) -> list[tuple[str, APIRouter]]:
    """Import every module in ``package``; return ``(module name, router)`` for those with one.

    Sorted by module name so the route order, and with it the exported schema, is
    the same on every machine.
    """
    found: list[tuple[str, APIRouter]] = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            found.append((info.name, router))
    return found


def openapi_json(app: FastAPI) -> str:
    """The OpenAPI schema as stable JSON: sorted keys, two-space indent, trailing newline."""
    return json.dumps(app.openapi(), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
