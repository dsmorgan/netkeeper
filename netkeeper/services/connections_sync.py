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

* **The run.** Every sync is a ``sync_runs`` row (P2-10): progress is written
  to it as pages arrive, a cancel on it (spec 9.9) stops the sync before the
  next page or inside the wait between two, and how the sync ended -- with its
  counts, and an aging refusal spelled out (#169 E) -- is recorded on it.

The caller holds the account's browser activity lock (spec 9.9) for the
length of :func:`sync_connections` and passes the source that reads through
that browser run. Nothing here attaches to a browser: ``netkeeper.worker``
does.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

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
from netkeeper.models import JsonValue, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.services import budgets, runs
from netkeeper.services import heat as heat_service
from netkeeper.services.budgets import ActionClass, BudgetExceeded
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.pacing import profiles
from netkeeper.services.runs import HeatSkipped as HeatSkipped
from netkeeper.services.runs import SessionFlagged as SessionFlagged
from netkeeper.services.runs import recording as runs_recording
from netkeeper.services.runs import refuse_if_flagged_or_hot, stop_reason_of

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]

#: The longest single sleep between two pages before the cancel flag is read
#: again (spec 9.9), as enrichment's own ``CANCEL_SLICE_S``.
CANCEL_SLICE_S: Final = 5.0

_HEAT_OUTCOMES = frozenset({Outcome.THROTTLED, Outcome.CHECKPOINT})
_FLAG_OUTCOMES = frozenset({Outcome.CHECKPOINT, Outcome.LOGGED_OUT})


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
    run_id: int = 0
    cancelled: bool = False

    def counts(self) -> dict[str, JsonValue]:
        """The run's ``counts_json``: numbers and reason words only (#169 E: ``aging.refused``)."""
        pages = self.pages
        return {
            "mode": self.result.mode.value,
            "pages": self.result.pages,
            "connections": self.result.connections,
            "total": self.result.max_total,
            "complete": self.result.complete,
            "seen": pages.seen,
            "created": pages.created,
            "updated": pages.updated,
            "needs_review": pages.needs_review,
            "conflicts": pages.conflicts,
            "reconnected": pages.reconnected,
            "aging": None
            if self.aging is None
            else {
                "missed": self.aging.missed,
                "disconnected": self.aging.disconnected,
                "refused": self.aging.refused,
            },
            "outcome": None if self.result.outcome is None else self.result.outcome.value,
            "heat_raised": self.heat_raised,
            "session_flagged": self.session_flagged,
        }


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _load_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"no user {user_id}")
    return user


@dataclass(slots=True)
class _BudgetGate:
    """Cancel, then one ``connection_pages`` unit, before each page; a paced wait between pages.

    A cancel (spec 9.9) refuses the next page like the budget does, and
    :attr:`cancelled` says which it was. The wait between pages is sliced
    (:data:`CANCEL_SLICE_S`) and the flag is read between slices, so a cancel
    lands within seconds even inside a long distraction pause.
    """

    factory: sessionmaker[Session]
    user_id: int
    account_id: int
    run_id: int
    settings: LinkedInSettings
    clock: Clock
    sleep: Sleep
    rng: random.Random
    multiplier: float
    cancelled: bool = False

    def _cancel_requested(self) -> bool:
        with session_scope(self.factory) as session:
            return runs.cancel_requested(session, _load_user(session, self.user_id), self.run_id)

    async def before_page(self, number: int) -> bool:
        if self.cancelled or self._cancel_requested():
            self.cancelled = True
            log.info("connections sync: cancelled before page %d", number)
            return False
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
        remaining = human_delay(
            self.rng,
            median=delay.median * self.multiplier,
            sigma=delay.sigma,
            tail_p=delay.tail_p,
            tail_range=delay.tail_range,
        )
        while remaining > 0:
            step = min(CANCEL_SLICE_S, remaining)
            await self.sleep(step)
            remaining -= step
            if self._cancel_requested():
                self.cancelled = True
                log.info("connections sync: cancelled during the wait between pages")
                return


async def sync_connections(
    factory: sessionmaker[Session],
    user_id: int,
    mode: SyncMode,
    source: ConnectionsSource,
    *,
    settings: LinkedInSettings,
    run_id: int | None = None,
    on_progress: ProgressSink | None = None,
    clock: Clock = _utcnow,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
) -> SyncRunReport:
    """Run connections sync run ``run_id`` in ``mode`` through ``source``, apply what it read,
    and record how it ended on the run.

    ``run_id`` is a ``running`` run of the matching kind
    (``services.runs.create_run``); ``None`` records a new manual one first.

    ``SessionFlagged`` while the session flag is set, and ``HeatSkipped`` when
    heat is at or above its skip threshold; either way nothing is fetched,
    nothing is spent, and the run is recorded ``failed`` with that reason.
    Every other stop is a :class:`SyncRunReport`, recorded ``completed`` (the
    end of the list, or an incremental sync caught up) or ``aborted`` (the
    budget, a cancel, a stopping response). An exception from the mapping
    propagates after the pages before it were committed, is recorded
    ``failed``, and never ages anyone.
    """
    kind = _KIND_OF[mode]
    with session_scope(factory, write=True) as session:
        user = _load_user(session, user_id)
        if run_id is None:
            run_id = runs.create_run(
                session, user, kind, trigger=SyncRunTrigger.MANUAL, now=clock()
            ).id
        run = runs.get_run(session, user, run_id)
        if run.kind is not kind:
            raise ValueError(f"run {run_id} is a {run.kind.value} run, not {kind.value}")
        account_id = run.linkedin_account_id

    with runs_recording(factory, user_id, run_id, clock=clock):
        with session_scope(factory, write=True) as session:
            user = _load_user(session, user_id)
            now = clock()
            refuse_if_flagged_or_hot(session, user, account_id, now=now, settings=settings)
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
            known = (
                mapping.known_urns(session, user) if mode is SyncMode.INCREMENTAL else frozenset()
            )

        # Spec 9.7: while warm, the per-run budget shrinks -- never to zero while
        # anything is left of the day's.
        page_budget = 0 if remaining == 0 else heat_math.shrink(remaining, multiplier)
        spec = SyncJobSpec(mode=mode, page_budget=page_budget, known_urns=known)
        gate = _BudgetGate(
            factory=factory,
            user_id=user_id,
            account_id=account_id,
            run_id=run_id,
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
            with session_scope(factory, write=True) as session:
                runs.record_progress(
                    session,
                    _load_user(session, user_id),
                    run_id,
                    {
                        "mode": event.mode.value,
                        "pages": event.pages,
                        "connections": event.connections,
                        "total": event.total,
                        "stopped": None if event.stopped is None else event.stopped.value,
                    },
                )
            if on_progress is not None:
                await on_progress(event)

        result = await run_connections_sync(
            spec,
            source,
            gate,
            on_page=on_page,
            on_progress=progress,
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
                    created_by_sync=frozenset(counts.created_contact_ids),
                    seen_public_ids=result.seen_public_ids,
                    held_for_review=frozenset(counts.review_contact_ids),
                )
            report = SyncRunReport(
                account_id=account_id,
                result=result,
                pages=counts,
                aging=aging,
                heat_raised=heat_raised,
                session_flagged=flagged,
                run_id=run_id,
                cancelled=gate.cancelled,
            )
            runs.finish_run(
                session,
                user,
                run_id,
                status=(
                    SyncRunStatus.COMPLETED
                    if result.reason in _NATURAL_ENDS and not gate.cancelled
                    else SyncRunStatus.ABORTED
                ),
                now=clock(),
                stop_reason=(
                    "cancelled"
                    if gate.cancelled
                    else stop_reason_of(result.reason.value, result.outcome)
                ),
                counts=report.counts(),
                notes=_aging_notes(aging),
            )
    return report


#: How a sync may end ``completed``: it read to the end of the list, or an
#: incremental sync reached connections it already knew. Every other stop
#: (the budget, a cancel, a response) is ``aborted``, keeping what it wrote.
_NATURAL_ENDS = frozenset({StopReason.END_OF_LIST, StopReason.CAUGHT_UP})

_KIND_OF: dict[SyncMode, SyncRunKind] = {
    SyncMode.FULL: SyncRunKind.CONNECTIONS_FULL,
    SyncMode.INCREMENTAL: SyncRunKind.CONNECTIONS_INCREMENTAL,
}


def _aging_notes(aging: mapping.AgingCounts | None) -> tuple[str, ...]:
    """#169 E: a complete full sync that refused to age anyone says so on the run."""
    if aging is None or aging.refused is None:
        return ()
    return (f"aged nobody: {aging.refused}",)
