"""The browser worker: a recorded run in, browser work done, the run's ending recorded (P2-10).

This is the one place a LinkedIn run meets the browser. For a ``sync_runs``
row (:mod:`netkeeper.services.runs`) it takes the account's activity lock,
attaches to the Chrome the user started (ADR 0002: attach only, never
launch), builds the source that reads through that tab, and calls the runner:
:func:`netkeeper.services.connections_sync.sync_connections` or
:func:`netkeeper.services.enrichment.enrich_contacts`. The runners own budgets,
heat, the session flag, pacing, cancel, and how the run ended; this module
owns only what needs the browser.

**Who calls it.** ``netkeeper serve``'s scheduler (through
:mod:`netkeeper.services.scheduled_runs`), the runs API (which submits it to
the task runner and answers ``202``, never awaiting it inside the request,
spec 9.9), and ``netkeeper linkedin sync``/``enrich`` on their own loops. The
app and the API reach it only as a :class:`~netkeeper.services.runs.RunExecutor`,
so no request handler imports the browser (``tests/test_browser_safety.py``
lists this module among the browser's few callers on purpose).

**Nothing is attached before it has to be.** Before the lock and the attach
the worker re-checks, from the database, the things that would make the run
refuse anyway: a scheduled run on a disarmed account (the third check, after
the scheduler's arm gate and ``runs.create_run``), a scheduled connections run
whose route-changed breaker is tripped (the second independent check for that
gate, matching the arming design; #191 review F6), the session flag, and heat
over its skip threshold. A refused run is recorded ``failed`` and the browser
is never touched. And the connections page a sync reads is loaded by its
source's first ``fetch_page``, after the runner's own checks, not before them.

**When the browser is not there.** ``BrowserUnavailable`` (Chrome is not
running, or went away twice in one run) and ``BrowserBusy`` (another run, or
another netkeeper process, holds the account) end the run ``failed`` and
answer :attr:`~netkeeper.services.runs.RunOutcome.RETRY_LATER`; the scheduler
parks a retry 20 to 50 minutes out (spec 9.9). Nothing here retries, and
nothing answers a missing browser by starting one.

**Events.** ``run.started``, ``run.progress`` (counts only), and
``run.finished`` go out on the event bus for the SSE stream, each for the run's
own user.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import LinkedInSettings, Settings, load_settings
from netkeeper.db import session_scope
from netkeeper.linkedin import activity_lock
from netkeeper.linkedin.activity_lock import account_key
from netkeeper.linkedin.browser import (
    ActivityLocks,
    AttachBrowserProvider,
    BrowserBusy,
    BrowserProvider,
    BrowserRun,
    BrowserUnavailable,
)
from netkeeper.linkedin.connections import ConnectionsSource, SyncMode
from netkeeper.linkedin.enrich import BrowserProfiles
from netkeeper.linkedin.fetch import PageVoyagerFetch
from netkeeper.linkedin.pacing import ScrollPlan
from netkeeper.linkedin.page_connections import PageConnections
from netkeeper.logging_setup import setup_logging
from netkeeper.models import SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.services import route_breaker, runs
from netkeeper.services.connections_sync import sync_connections
from netkeeper.services.enrichment import enrich_contacts
from netkeeper.services.events import Event, EventBus
from netkeeper.services.linkedin_accounts import local_account_id, scheduled_runs_armed
from netkeeper.services.scheduled_runs import ServeExtractor
from netkeeper.web.app import create_app

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def connections_source(
    run: BrowserRun, *, mode: SyncMode, sleep: Sleep = asyncio.sleep
) -> ConnectionsSource:
    """The source a live connections sync reads through (spec 9.3, ADR 0006).

    :class:`~netkeeper.linkedin.page_connections.PageConnections`: the connections
    page, loaded and scrolled like a person, read from the answers the page itself
    fetches. It loads the page inside its first ``fetch_page``, which the job calls
    only after the runner's refusals and the gate's budget, so a refused run never
    loads a page. An incremental sync requires the list sorted newest first, since it
    stops at the first page of connections it knows.

    There is no second source behind it (#187). The in-page Voyager fetch has no
    connections endpoint to call any more (the #149 capture), and P2-08's DOM scroll
    reads selectors nobody has seen on the live page, which found nothing on the first
    supervised run; a run whose page answers are unreadable stops as ``RouteChanged``
    and ages nobody. One of these per run. ``sleep`` waits out the scroll's pauses and
    dwell.
    """
    return PageConnections(run, require_newest_first=mode is SyncMode.INCREMENTAL, sleep=sleep)


def profile_source(run: BrowserRun, *, sleep: Sleep = asyncio.sleep) -> BrowserProfiles:
    """The source a live enrichment reads through: the tab's navigation, its scroll,
    and the in-page API, all from this one run (P2-07). ``sleep`` waits out the
    scroll's pauses and dwell, the same sleeper the run's other waits use."""

    async def scroll(plan: ScrollPlan) -> object:
        return await run.scroll(plan, sleep=sleep)

    return BrowserProfiles(navigate=run.goto, scroll_page=scroll, fetch=PageVoyagerFetch(run))


_MODE: Final[dict[SyncRunKind, SyncMode]] = {
    SyncRunKind.CONNECTIONS_FULL: SyncMode.FULL,
    SyncRunKind.CONNECTIONS_INCREMENTAL: SyncMode.INCREMENTAL,
}

#: The kinds the route-changed breaker governs (#189 item 1, #191 review F6): not
#: ``enrich``, which has its own separate unreadable-profile cap. Matches
#: ``services.scheduler``'s own ``_ROUTE_BREAKER_KINDS``.
_ROUTE_BREAKER_KINDS: Final = frozenset(
    {SyncRunKind.CONNECTIONS_FULL, SyncRunKind.CONNECTIONS_INCREMENTAL}
)


@dataclass(frozen=True, slots=True)
class _RunFacts:
    kind: SyncRunKind
    status: SyncRunStatus
    trigger: SyncRunTrigger
    account_id: int


class BrowserWorker:
    """Executes recorded runs against the attached browser. A ``RunExecutor``."""

    def __init__(
        self,
        provider: BrowserProvider,
        factory: sessionmaker[Session],
        settings: LinkedInSettings,
        *,
        bus: EventBus | None = None,
        clock: Clock = _utcnow,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.provider = provider
        self._factory = factory
        self._settings = settings
        self._bus = bus
        self._clock = clock
        self._sleep = sleep
        self._rng = rng

    async def execute(self, run_id: int, user_id: int) -> runs.RunOutcome:
        """Run ``run_id`` to its end and record how it ended. See the module docstring."""
        facts = self._facts(run_id, user_id)
        if facts is None:
            return runs.RunOutcome.DONE
        refusal = self._refusal(run_id, user_id, facts)
        if refusal is not None:
            self._finish(run_id, user_id, SyncRunStatus.FAILED, refusal[0], refusal[1])
            self._publish("run.finished", run_id, user_id, {"status": "failed"})
            return runs.RunOutcome.DONE
        self._publish("run.started", run_id, user_id, {"kind": facts.kind.value})
        outcome = runs.RunOutcome.DONE
        try:
            async with self.provider.run(activity_lock.account_key(facts.account_id)) as browser:
                await self._run_job(run_id, user_id, facts, browser)
        except (BrowserBusy, BrowserUnavailable) as exc:
            reason = "browser_busy" if isinstance(exc, BrowserBusy) else "browser_unavailable"
            log.warning("run %d could not use the browser: %s", run_id, exc)
            self._finish(run_id, user_id, SyncRunStatus.FAILED, reason, runs.describe(exc))
            outcome = runs.RunOutcome.RETRY_LATER
        except asyncio.CancelledError:
            # The process is shutting down. A cancel that landed inside the runner
            # was recorded there; one that landed while attaching was not.
            self._finish(run_id, user_id, SyncRunStatus.ABORTED, "interrupted", runs.INTERRUPTED)
            raise
        except (runs.HeatSkipped, runs.SessionFlagged) as exc:
            log.info("run %d refused: %s", run_id, exc)  # recorded by the runner
        except Exception as exc:
            # The runner recorded it; this only stops it from taking the caller down.
            log.exception("run %d failed", run_id)
            self._finish(run_id, user_id, SyncRunStatus.FAILED, "error", runs.describe(exc))
        self._publish("run.finished", run_id, user_id, {"status": self._status(run_id, user_id)})
        return outcome

    async def _run_job(
        self, run_id: int, user_id: int, facts: _RunFacts, browser: BrowserRun
    ) -> None:
        async def progress(event: Any) -> None:
            self._publish("run.progress", run_id, user_id, _plain(event))

        if facts.kind is SyncRunKind.ENRICH:
            await enrich_contacts(
                self._factory,
                user_id,
                profile_source(browser, sleep=self._sleep),
                settings=self._settings,
                run_id=run_id,
                on_progress=progress,
                clock=self._clock,
                sleep=self._sleep,
                rng=self._rng,
            )
            return
        await sync_connections(
            self._factory,
            user_id,
            _MODE[facts.kind],
            connections_source(browser, mode=_MODE[facts.kind], sleep=self._sleep),
            settings=self._settings,
            run_id=run_id,
            on_progress=progress,
            clock=self._clock,
            sleep=self._sleep,
            rng=self._rng,
        )

    def _facts(self, run_id: int, user_id: int) -> _RunFacts | None:
        with session_scope(self._factory) as session:
            user = session.get(User, user_id)
            if user is None:
                log.error("run %d: no user %d", run_id, user_id)
                return None
            try:
                run = runs.get_run(session, user, run_id)
            except runs.RunNotFound:
                log.error("run %d does not exist for user %d", run_id, user_id)
                return None
            facts = _RunFacts(
                kind=run.kind,
                status=run.status,
                trigger=run.trigger,
                account_id=run.linkedin_account_id,
            )
        if facts.status is not SyncRunStatus.RUNNING:
            log.warning("run %d already ended %s; not running it", run_id, facts.status.value)
            return None
        if facts.kind not in runs.RUNNABLE_KINDS:
            self._finish(
                run_id, user_id, SyncRunStatus.FAILED, "no_runner", f"no runner for {facts.kind}"
            )
            return None
        return facts

    def _refusal(self, run_id: int, user_id: int, facts: _RunFacts) -> tuple[str, str] | None:
        """``(stop_reason, error)`` when the run must not touch the browser at all."""
        with session_scope(self._factory) as session:
            user = session.get(User, user_id)
            assert user is not None  # _facts found it
            if facts.trigger is SyncRunTrigger.SCHEDULED and not scheduled_runs_armed(
                session, user, facts.account_id
            ):
                log.error("scheduled run %d reached the worker on a disarmed account", run_id)
                return "disarmed", "scheduled runs are disarmed for this account"
            if (
                facts.trigger is SyncRunTrigger.SCHEDULED
                and facts.kind in _ROUTE_BREAKER_KINDS
                and route_breaker.tripped(session, user, facts.account_id)
            ):
                # #191 review F6: a second, independent check, matching the arming
                # design above -- the scheduler's own gate (services.scheduler's
                # poll_and_fire) already skips a tripped account's due fire, so
                # reaching this refusal means something bypassed that gate.
                log.error(
                    "scheduled run %d reached the worker with the route-changed"
                    " breaker tripped for account %d",
                    run_id,
                    facts.account_id,
                )
                return (
                    "route_changed_breaker",
                    "the route-changed breaker is tripped for this account",
                )
            try:
                runs.refuse_if_flagged_or_hot(
                    session, user, facts.account_id, now=self._clock(), settings=self._settings
                )
            except runs.SessionFlagged as exc:
                return "session_flagged", str(exc)
            except runs.HeatSkipped as exc:
                return "heat_skip", str(exc)
        return None

    def _finish(
        self, run_id: int, user_id: int, status: SyncRunStatus, reason: str, error: str
    ) -> None:
        with session_scope(self._factory, write=True) as session:
            user = session.get(User, user_id)
            if user is None:
                return
            runs.finish_run(
                session,
                user,
                run_id,
                status=status,
                now=self._clock(),
                stop_reason=reason,
                error=error,
            )

    def _status(self, run_id: int, user_id: int) -> str:
        with session_scope(self._factory) as session:
            user = session.get(User, user_id)
            if user is None:
                return "unknown"
            return runs.get_run(session, user, run_id).status.value

    def _publish(self, kind: str, run_id: int, user_id: int, data: dict[str, Any]) -> None:
        if self._bus is None:
            return
        self._bus.publish(Event(type=kind, data={"run_id": run_id, **data}, user_id=user_id))


def _plain(event: object) -> dict[str, Any]:
    """A progress event as JSON-ready counts: the dataclass's fields, enums as values."""
    if not dataclasses.is_dataclass(event) or isinstance(event, type):
        return {}
    return {
        key: value.value if isinstance(value, enum.Enum) else value
        for key, value in dataclasses.asdict(event).items()
    }


# --- what `netkeeper serve` runs --------------------------------------------------


def serve_extractor(
    settings: Settings, *, provider: BrowserProvider | None = None
) -> ServeExtractor:
    """The extractor ``netkeeper serve`` hands the app: a worker on the one attach provider.

    ``provider`` defaults to :class:`~netkeeper.linkedin.browser.AttachBrowserProvider`
    on ``linkedin.cdp_url``, built once the app has its database, so the
    legacy-lock co-claim (#169 F) goes with the local user's account whatever its
    id (#175 review, F10). Building it attaches to nothing; the provider only
    connects inside a run, and a run on a disarmed account is never scheduled.
    """

    def executor(factory: sessionmaker[Session], bus: EventBus) -> runs.RunExecutor:
        chosen = provider
        if chosen is None:
            with session_scope(factory) as session:
                local = local_account_id(session)
            partner = activity_lock.SINGLE_ACCOUNT_KEY if local is None else account_key(local)
            chosen = AttachBrowserProvider(
                settings.linkedin.cdp_url, locks=ActivityLocks(legacy_partner=partner)
            )
        return BrowserWorker(chosen, factory, settings.linkedin, bus=bus)

    return ServeExtractor(executor=executor)


def serve_app(settings: Settings) -> FastAPI:
    """The app ``netkeeper serve`` runs: ``create_app`` with the extractor."""
    return create_app(settings, extractor=serve_extractor(settings))


def dev_app() -> FastAPI:
    """The factory ``netkeeper serve --reload`` hands uvicorn as an import string.

    The reloader imports it in a worker process that never runs the CLI callback,
    so logging is set up here first; then it is :func:`serve_app` with the settings
    resolved from the environment and the search path.
    """
    setup_logging()
    return serve_app(load_settings())
