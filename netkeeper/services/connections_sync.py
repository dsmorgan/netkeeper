"""Run a connections sync: job, budget, heat, session flag, and mapping (spec 9.4-9.8).

The seam between :mod:`netkeeper.linkedin.connections` (pure, no database) and
:mod:`netkeeper.crm.apply` (rows, no browser). This module owns neither half's
logic; it decides what the job may do and what happens after it stops:

* **Heat skip.** A run does not start while heat is at or above the skip
  threshold (spec 9.7). The scheduler checks too; a run started any other way
  must not be the hole in that. Heat is read once, at the start of the run, and
  not again between pages: a run stops on the first non-``Ok`` response anyway,
  so heat can only rise at the end of a run, never in the middle of one.
* **Session flag.** A run does not start while the session flag is set (spec
  9.7): after a checkpoint or a login wall, the next run fetching anyway would
  be the retry ADR 0002 forbids, one run later. Clearing the flag is a person's
  act (``services.linkedin_session.clear_session_flag``).
* **Budget, between pages.** Before every page the gate spends one
  ``connection_pages`` unit (:func:`netkeeper.services.budgets.consume`, spec
  9.6) in its own short writer session, and a refusal stops the run before the
  fetch, never during it. The run's page budget is what is left of today's,
  shrunk by heat's cooldown multiplier while warm (spec 9.7).
* **Pacing.** Between pages the gate waits a human-like delay from
  ``[linkedin.pacing]``, stretched by the same multiplier.
* **The stopping response.** ``Throttled`` or ``Checkpoint`` raises heat;
  ``Checkpoint`` or ``LoggedOut`` sets the session flag (spec 9.7). Nothing is
  retried here or in the job.
* **Mapping.** Each page is written in its own writer session as it arrives,
  so the write lock is never held across a fetch or a pause. After a
  *complete* full sync, and only then, contacts it did not see are aged
  (:func:`netkeeper.crm.apply.age_unseen`, spec 9.8).

The caller holds the account's browser activity lock (spec 9.9) for the
length of :func:`sync_connections` and passes the source that reads through
that browser run. Nothing here attaches to a browser.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import LinkedInSettings
from netkeeper.crm import apply as mapping
from netkeeper.db import session_scope
from netkeeper.linkedin import heat as heat_math
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.connections import (
    ConnectionsPage,
    ConnectionsSource,
    ProgressEvent,
    ProgressSink,
    StopReason,
    SyncJobSpec,
    SyncMode,
    SyncResult,
    run_connections_sync,
)
from netkeeper.linkedin.pacing import human_delay
from netkeeper.models import User
from netkeeper.services import budgets
from netkeeper.services import heat as heat_service
from netkeeper.services.budgets import ActionClass, BudgetExceeded
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import flag_session, session_flag
from netkeeper.services.pacing import profiles

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]

_HEAT_OUTCOMES = frozenset({Outcome.THROTTLED, Outcome.CHECKPOINT})
_FLAG_OUTCOMES = frozenset({Outcome.CHECKPOINT, Outcome.LOGGED_OUT})


class HeatSkipped(RuntimeError):
    """Heat is at or above the skip threshold; the run did not start (spec 9.7)."""


class SessionFlagged(RuntimeError):
    """The session flag is set (a checkpoint or a login wall); the run did not start."""


@dataclass(frozen=True, slots=True)
class SyncRunReport:
    """What one run did: the job's result, the mapping's counts, and what followed.

    A run with nothing left of today's budget still runs, fetches nothing, and
    reports :attr:`StopReason.PAGE_BUDGET`. ``aging`` is ``None`` unless the run
    was a complete full sync.
    """

    account_id: int
    result: SyncResult
    pages: mapping.PageCounts
    aging: mapping.AgingCounts | None = None
    heat_raised: bool = False
    session_flagged: bool = False


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _load_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"no user {user_id}")
    return user


@dataclass(slots=True)
class _BudgetGate:
    """Spends one ``connection_pages`` unit before each page, and paces between pages."""

    factory: sessionmaker[Session]
    user_id: int
    account_id: int
    settings: LinkedInSettings
    clock: Clock
    sleep: Sleep
    rng: random.Random
    multiplier: float

    async def before_page(self, number: int) -> bool:
        with session_scope(self.factory, write=True) as session:
            user = _load_user(session, self.user_id)
            try:
                budgets.consume(
                    session,
                    user,
                    self.account_id,
                    ActionClass.CONNECTION_PAGES,
                    now=self.clock(),
                    settings=self.settings.budget,
                )
            except BudgetExceeded as exc:
                log.info("connections sync: page %d refused by the budget: %s", number, exc)
                return False
        return True

    async def between_pages(self) -> None:
        delay = profiles(self.settings.pacing).delay
        await self.sleep(
            human_delay(
                self.rng,
                median=delay.median * self.multiplier,
                sigma=delay.sigma,
                tail_p=delay.tail_p,
                tail_range=delay.tail_range,
            )
        )


async def sync_connections(
    factory: sessionmaker[Session],
    user_id: int,
    mode: SyncMode,
    source: ConnectionsSource,
    *,
    settings: LinkedInSettings,
    on_progress: ProgressSink | None = None,
    clock: Clock = _utcnow,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
) -> SyncRunReport:
    """Run one ``mode`` sync for ``user_id`` through ``source`` and apply what it read.

    ``SessionFlagged`` while the session flag is set, and ``HeatSkipped`` when
    heat is at or above its skip threshold; either way nothing is fetched and
    nothing is spent. Every other stop is a :class:`SyncRunReport`.
    An exception from the mapping propagates after the pages before it were
    committed; it never ages anyone.
    """
    with session_scope(factory, write=True) as session:
        user = _load_user(session, user_id)
        account_id = ensure_account(session, user).id
        now = clock()
        flagged_by = session_flag(session, user)
        if flagged_by is not None:
            raise SessionFlagged(
                f"the LinkedIn session is flagged ({flagged_by.outcome.value}); not syncing"
                " until it is cleared"
            )
        if heat_service.should_skip(session, user, account_id, now=now, settings=settings.heat):
            raise HeatSkipped(f"heat is at or above {settings.heat.skip_threshold}; not syncing")
        multiplier = heat_service.cooldown_multiplier(
            session, user, account_id, now=now, settings=settings.heat
        )
        remaining = budgets.status(
            session,
            user,
            account_id,
            ActionClass.CONNECTION_PAGES,
            now=now,
            settings=settings.budget,
        ).day.remaining
        known = mapping.known_urns(session, user) if mode is SyncMode.INCREMENTAL else frozenset()

    # Spec 9.7: while warm, the per-run budget shrinks -- never to zero while
    # anything is left of the day's.
    page_budget = 0 if remaining == 0 else heat_math.shrink(remaining, multiplier)
    spec = SyncJobSpec(mode=mode, page_budget=page_budget, known_urns=known)
    gate = _BudgetGate(
        factory=factory,
        user_id=user_id,
        account_id=account_id,
        settings=settings,
        clock=clock,
        sleep=sleep,
        rng=rng if rng is not None else random.Random(),  # noqa: S311 -- pacing, not crypto
        multiplier=multiplier,
    )
    counts = mapping.PageCounts()

    async def on_page(page: ConnectionsPage) -> None:
        with session_scope(factory, write=True) as session:
            mapping.apply_page(session, _load_user(session, user_id), page, counts)

    async def progress(event: ProgressEvent) -> None:
        return None

    result = await run_connections_sync(
        spec,
        source,
        gate,
        on_page=on_page,
        on_progress=on_progress if on_progress is not None else progress,
        clock=clock,
    )

    aging: mapping.AgingCounts | None = None
    heat_raised = flagged = False
    with session_scope(factory, write=True) as session:
        user = _load_user(session, user_id)
        if result.reason is StopReason.RESPONSE and result.outcome is not None:
            if result.outcome in _HEAT_OUTCOMES:
                heat_service.raise_heat(
                    session, user, account_id, now=clock(), settings=settings.heat
                )
                heat_raised = True
            if result.outcome in _FLAG_OUTCOMES:
                flag_session(session, user, result.outcome, url=result.final_url or "")
                flagged = True
        if result.complete:
            aging = mapping.age_unseen(
                session,
                user,
                result.seen_urns,
                observed_at=clock(),
                disconnect_after_misses=settings.disconnect_after_misses,
            )
    return SyncRunReport(
        account_id=account_id,
        result=result,
        pages=counts,
        aging=aging,
        heat_raised=heat_raised,
        session_flagged=flagged,
    )
