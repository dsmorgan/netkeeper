"""The FastAPI application factory (spec sections 5 and 14).

:func:`create_app` builds the app. Its lifespan builds the engine (or takes the
one passed in), migrates the database to head, makes sure the local user exists
with the default auto-tag rules seeded, installs the scope guard on the session
factory, marks runs a stopped process left ``running`` as failed, and starts
the event bus and the task runner, all kept on ``app.state``, along with the
signer behind bulk count confirmations. Given an extractor (``netkeeper
serve``), it also starts the scheduler (P2-10), whose scheduled LinkedIn runs
fire only on an account a person armed, and the mailbox poll (P3-01), which
refreshes each Gmail token every ``[campaigns] reply_poll_minutes``, and the
campaign engine's minute tick (P3-06), which fires through the Gmail sender
(P3-07) only on a mailbox a person armed (#277). API
modules under :mod:`netkeeper.web.api` are discovered, so adding an endpoint
never edits this file.
"""

from __future__ import annotations

import importlib
import json
import logging
import pkgutil
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from types import ModuleType

from fastapi import APIRouter, FastAPI
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from netkeeper import __version__, migrations
from netkeeper.campaigns.gmail_oauth import GoogleEndpoints
from netkeeper.config import Settings, load_settings
from netkeeper.crm.confirmation import Signer
from netkeeper.crm.lists import ensure_validated_list
from netkeeper.crm.tags import ensure_default_rules
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models.base import utcnow
from netkeeper.scoping import install_scope_guard
from netkeeper.services.campaign_engine import CampaignEngine, Sender
from netkeeper.services.campaign_sender import GmailSender
from netkeeper.services.events import EventBus
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.mailboxes import MailboxMonitor, PendingAuthorizations, open_gmail
from netkeeper.services.runs import fail_interrupted_runs
from netkeeper.services.scheduled_runs import ServeExtractor, start_serve_scheduler
from netkeeper.services.tasks import TaskRunner
from netkeeper.services.users import ensure_local_user
from netkeeper.web import api as api_package
from netkeeper.web.deps import LocalSingleUser
from netkeeper.web.errors import install_error_handlers
from netkeeper.web.frontend import frontend_dist, mount_frontend
from netkeeper.web.security import LOOPBACK_HOSTNAMES, CSRFMiddleware

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    extractor: ServeExtractor | None = None,
    gmail: GoogleEndpoints | None = None,
    campaign_sender: Sender | None = None,
) -> FastAPI:
    """Build the application without starting it.

    ``settings`` default to :func:`load_settings`. ``engine`` defaults to one built
    from :func:`database_url` at startup and disposed at shutdown; an engine passed
    in (tests) is left to whoever built it.

    ``extractor`` is the LinkedIn extractor half of ``netkeeper serve``
    (``netkeeper.worker.serve_extractor``): with one, the lifespan builds the
    run executor and starts the scheduler, whose scheduled runs fire only on an
    account a person armed. Without one no scheduler starts and the runs API
    answers ``503`` to a start; everything else works the same. The mailbox poll
    starts with the scheduler, for the same reason: only ``serve`` runs
    background work.

    ``gmail`` is where the Gmail OAuth flow sends its requests; None is Google
    itself (``netkeeper.campaigns.gmail_oauth.GOOGLE``). Tests pass a loopback fake.

    ``campaign_sender`` is what the campaign engine hands each firing to; the
    engine's minute tick starts with the scheduler. None is the Gmail sender
    (:class:`~netkeeper.services.campaign_sender.GmailSender`, its requests going
    to ``gmail``), which touches only a mailbox a person armed (#277): with none
    armed, nothing is claimed and Gmail is never called. Tests pass a fake.
    """
    resolved = load_settings() if settings is None else settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active = engine if engine is not None else make_engine(database_url())
        # Each teardown is pushed as its piece starts, so shutdown runs them in
        # reverse: the campaign engine, the mailbox poll, the scheduler, the
        # runs, then the engine. The stack runs every one even when an earlier
        # one raises or is cancelled (the campaign engine can wait 45 s for a
        # send, long enough for a cancel to land there), then re-raises.
        async with AsyncExitStack() as teardown:
            if engine is None:
                teardown.callback(active.dispose)
            tasks = _start(app, active, resolved)
            teardown.push_async_callback(tasks.cancel_all)
            app.state.gmail_endpoints = gmail
            if extractor is not None:
                serving = start_serve_scheduler(
                    extractor,
                    app.state.session_factory,
                    app.state.bus,
                    tasks,
                    resolved.linkedin,
                )
                teardown.callback(serving.stop)
                app.state.executor = serving.executor
                app.state.scheduler = serving.scheduler
                monitor = MailboxMonitor(
                    app.state.session_factory,
                    app.state.bus,
                    interval_s=_poll_minutes(resolved) * 60,
                    endpoints=gmail,
                )
                teardown.push_async_callback(monitor.stop)
                monitor.start()
                app.state.mailbox_monitor = monitor
                sender = (
                    campaign_sender
                    if campaign_sender is not None
                    else _gmail_sender(app.state.session_factory, gmail, resolved)
                )
                campaigns = CampaignEngine(app.state.session_factory, resolved, sender)
                teardown.push_async_callback(campaigns.stop)
                campaigns.start()
                app.state.campaign_engine = campaigns
            yield

    app = FastAPI(
        title="netkeeper",
        version=__version__,
        description="Keep your professional network warm.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CSRFMiddleware,
        allowed_hosts=LOOPBACK_HOSTNAMES | {resolved.web.host.strip("[]").lower()},
    )
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
        fail_interrupted_runs(session, now=utcnow())
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
    # Gmail authorizations waiting for Google's redirect; like the signer, never written down.
    app.state.pending_oauth = PendingAuthorizations()
    app.state.mailbox_monitor = None
    app.state.campaign_engine = None
    return tasks


def _gmail_sender(
    factory: sessionmaker[Session], endpoints: GoogleEndpoints | None, settings: Settings
) -> GmailSender:
    """``serve``'s campaign sender: Gmail, on armed mailboxes only (#277), polling for
    replies every ``[campaigns] reply_poll_minutes`` (P3-08)."""
    return GmailSender(
        factory,
        opener=lambda user_id, mailbox_id: open_gmail(
            factory, user_id, mailbox_id, endpoints=endpoints
        ),
        replies_every=timedelta(minutes=_poll_minutes(settings)),
    )


def _poll_minutes(settings: Settings) -> int:
    """``[campaigns] reply_poll_minutes``, at least one."""
    minutes = settings.campaigns.reply_poll_minutes
    if minutes < 1:
        log.warning("[campaigns] reply_poll_minutes = %d is not a poll; using 1", minutes)
        return 1
    return minutes


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
