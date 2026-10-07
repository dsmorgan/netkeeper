"""Run a LinkedIn inbox poll: run row, budget, heat, session flag, and apply (P4-08, #378).

The seam between :mod:`netkeeper.linkedin.inbox` (the contract; P4-01's page
source reads the page, no database) and :mod:`netkeeper.crm.inbox_apply` (rows,
no browser), modeled on :mod:`netkeeper.services.connections_sync`:

* **Refusals first.** A poll does not start while the session flag is set or
  heat is at or above its skip threshold (spec 9.7). The run is recorded
  ``failed`` with that reason, and nothing is read or spent.
* **One unit, one budget unit.** A poll is one unit of work: one
  ``inbox_polls`` unit (:func:`netkeeper.services.budgets.consume`) is spent
  before the page loads, in its own short writer session. A refusal ends the
  run ``aborted``, ``budget``, before anything is read. A cancel asked for
  before then ends it ``cancelled`` the same way.
* **The stopping page.** A source that lands on a wall raises
  :class:`~netkeeper.linkedin.inbox.InboxReadStopped`. ``Throttled`` or
  ``Checkpoint`` raises heat; ``Checkpoint`` or ``LoggedOut`` sets the session
  flag. The run ends ``aborted`` with the outcome as its reason. Nothing retries.
* **Apply.** What the page read is applied in one writer session, after the
  read, with the run's ending in the same transaction. A poll whose page proved
  it read back to the last complete poll ends ``completed``, ``inbox_read``;
  one that did not ends ``aborted``, ``inbox_incomplete``, keeping what it
  wrote. The next poll reads from the start of the last complete one; before
  the first, from the earliest sent outbound message of any live enrollment.
* **Counts only.** ``counts_json`` holds numbers. No message text reaches a log
  line, the run row, an error, or the event stream: an exception from the
  source or the apply is recorded by its type alone.

Every database session here runs whole off the event loop (``db.off_loop``,
#259). The caller holds the account's browser lock for the length of
:func:`poll_inbox` and passes the source; nothing here attaches to a browser.
``netkeeper.worker`` does.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import LinkedInSettings, Settings
from netkeeper.crm import inbox_apply
from netkeeper.db import off_loop, session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.errors import BrowserError
from netkeeper.linkedin.inbox import (
    BOOTSTRAP_MAX_CONVERSATIONS,
    InboxDelta,
    InboxJobSpec,
    InboxReadStopped,
    InboxSource,
)
from netkeeper.linkedin.observe import ObservationFailed
from netkeeper.models import JsonValue, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.services import budgets, route_breaker, runs
from netkeeper.services import heat as heat_service
from netkeeper.services.budgets import ActionClass, BudgetExceeded
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.runs import recording as runs_recording
from netkeeper.services.runs import refuse_if_flagged_or_hot

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]

#: The most conversations one poll reads. A poll every three hours rarely finds more
#: than a handful active; the bound keeps a first poll, or one after a long gap, short.
MAX_CONVERSATIONS_PER_POLL: Final = 40

#: The ``stop_reason`` of a poll whose page proved it read back to the last complete one.
READ: Final = "inbox_read"

#: The ``stop_reason`` of a poll whose page showed somebody else's mailbox: nothing written.
OWNER_MISMATCH: Final = "owner_mismatch"

#: The ``stop_reason`` of a poll whose page did not prove that: the next poll reads again.
INCOMPLETE: Final = "inbox_incomplete"

#: The ``stop_reason`` of a first poll that read :data:`BOOTSTRAP_MAX_CONVERSATIONS`
#: without reaching its ``since``: counted complete, so later polls can move on, with a
#: posture warning to check older replies by hand (#388 review, S3).
FIRST_SHORT: Final = "inbox_first_short"

#: The walls that count toward the inbox breaker (#437): an unknown shape, or a page
#: that moved or a retired query (``not_found``). Throttles, checkpoints and log-outs
#: have their own handling (heat, the session flag).
_ROUTE_OUTCOMES: Final = frozenset({Outcome.ROUTE_CHANGED, Outcome.NOT_FOUND})

_HEAT_OUTCOMES: Final = frozenset({Outcome.THROTTLED, Outcome.CHECKPOINT})
_FLAG_OUTCOMES: Final = frozenset({Outcome.CHECKPOINT, Outcome.LOGGED_OUT})


class InboxPollFailed(RuntimeError):
    """Reading or applying the inbox failed. The message names the exception's type only."""


@dataclass(frozen=True, slots=True)
class InboxPollReport:
    """How one poll ended. ``counts`` is ``None`` when nothing was read."""

    run_id: int
    account_id: int
    stop_reason: str
    counts: inbox_apply.InboxCounts | None = None
    outcome: Outcome | None = None
    heat_raised: bool = False
    session_flagged: bool = False


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _load_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"no user {user_id}")
    return user


def last_complete_poll_start(session: Session, user: User) -> datetime | None:
    """When the newest complete inbox poll started, or ``None`` before the first. Read-only."""
    run = runs.latest_run(session, user, SyncRunKind.INBOX, status=SyncRunStatus.COMPLETED)
    return None if run is None else run.started_at


@dataclass(frozen=True, slots=True)
class PollPlan:
    """The spec a poll reads with, and whether it is the first (no poll completed yet)."""

    spec: InboxJobSpec
    first: bool


def job_spec(session: Session, user: User, *, now: datetime) -> PollPlan:
    """What the next poll may read: since the last complete poll, the watched contacts'
    URNs, and the threads it may open. Read-only.

    A first poll, before any has completed, reads back to the earliest sent outbound
    message the poll watches for replies to
    (:func:`~netkeeper.crm.inbox_apply.first_live_outreach`), and may read up to
    :data:`~netkeeper.linkedin.inbox.BOOTSTRAP_MAX_CONVERSATIONS` to get there. With
    no such message ``since`` is ``None``, and a poll that reads ``max_conversations``
    counts as complete.
    """
    since = last_complete_poll_start(session, user)
    first = since is None
    if first:
        since = inbox_apply.first_live_outreach(session, user, now=now)
    spec = InboxJobSpec(
        since=since,
        watched_urns=inbox_apply.watched_urns(session, user),
        max_conversations=BOOTSTRAP_MAX_CONVERSATIONS if first else MAX_CONVERSATIONS_PER_POLL,
        open_threads_for=inbox_apply.threads_to_open(session, user),
    )
    return PollPlan(spec=spec, first=first)


def _zero_counts() -> dict[str, JsonValue]:
    return dict(inbox_apply.InboxCounts().counts())


async def poll_inbox(
    factory: sessionmaker[Session],
    user_id: int,
    source: InboxSource,
    *,
    settings: LinkedInSettings,
    run_id: int | None = None,
    clock: Clock = _utcnow,
    campaign_settings: Settings | None = None,
) -> InboxPollReport:
    """Run inbox poll ``run_id`` through ``source``, apply what it read, and record how it
    ended. See the module docstring.

    ``campaign_settings`` go to the reply hook (P4-02): a prefilled message seen sent
    schedules its enrollment's next step with them. ``None``: the built-in defaults.

    ``run_id`` is a ``running`` inbox run (``services.runs.create_run``); ``None``
    records a new manual one first. ``SessionFlagged`` and ``HeatSkipped`` propagate
    after the run is recorded ``failed``.
    """

    def start(run_id: int | None) -> tuple[int, int]:
        with session_scope(factory, write=True) as session:
            user = _load_user(session, user_id)
            if run_id is None:
                run_id = runs.create_run(
                    session, user, SyncRunKind.INBOX, trigger=SyncRunTrigger.MANUAL, now=clock()
                ).id
            run = runs.get_run(session, user, run_id)
            if run.kind is not SyncRunKind.INBOX:
                raise ValueError(f"run {run_id} is a {run.kind.value} run, not inbox")
            return run_id, run.linkedin_account_id

    started_run_id, account_id = await off_loop(start, run_id)

    def prepare() -> PollPlan:
        with session_scope(factory, write=True) as session:
            user = _load_user(session, user_id)
            refuse_if_flagged_or_hot(session, user, account_id, now=clock(), settings=settings)
            return job_spec(session, user, now=clock())

    def spend() -> str | None:
        """``None`` when the poll may read; otherwise the reason it stops first."""
        with session_scope(factory, write=True) as session:
            user = _load_user(session, user_id)
            if runs.cancel_requested(session, user, started_run_id):
                return runs.CANCELLED
            try:
                budgets.consume(
                    session,
                    user,
                    account_id,
                    ActionClass.INBOX_POLLS,
                    now=clock(),
                    settings=settings.budget,
                )
            except BudgetExceeded as exc:
                log.info("inbox poll: refused by the budget: %s", exc)
                return "budget"
        return None

    def finish(
        status: SyncRunStatus, reason: str, counts: dict[str, JsonValue], *, error: str | None
    ) -> None:
        with session_scope(factory, write=True) as session:
            runs.finish_run(
                session,
                _load_user(session, user_id),
                started_run_id,
                status=status,
                now=clock(),
                stop_reason=reason,
                counts=counts,
                error=error,
            )

    def record_response(outcome: Outcome, url: str) -> tuple[bool, bool]:
        heat_raised = flagged = False
        with session_scope(factory, write=True) as session:
            user = _load_user(session, user_id)
            if outcome in _HEAT_OUTCOMES:
                heat_service.raise_heat(
                    session, user, account_id, now=clock(), settings=settings.heat
                )
                heat_raised = True
            if outcome in _FLAG_OUTCOMES:
                flag_session(session, user, outcome, url=url)
                flagged = True
        return heat_raised, flagged

    def record_breaker(*, route_changed: bool, completed: bool) -> None:
        """#437: the inbox's own streak, in its own writer session. A ``route_changed``
        stop is recorded before the run's ending is written (as connections_sync does);
        a completed poll clears it after the apply has committed. Never touches the
        connections streaks."""
        with session_scope(factory, write=True) as session:
            route_breaker.record_inbox(
                session,
                _load_user(session, user_id),
                account_id,
                route_changed=route_changed,
                completed=completed,
                now=clock(),
            )

    def record_owner_cleared() -> None:
        """#443: a completed poll clears the owner-mismatch streak, in its own writer
        session. (A mismatch is counted in ``apply_and_finish``'s transaction.)"""
        with session_scope(factory, write=True) as session:
            route_breaker.record_inbox_owner(
                session,
                _load_user(session, user_id),
                account_id,
                owner_mismatch=False,
                completed=True,
                now=clock(),
            )

    def apply_and_finish(
        delta: InboxDelta, reason: str, since: datetime | None
    ) -> inbox_apply.InboxCounts | None:
        try:
            with session_scope(factory, write=True) as session:
                user = _load_user(session, user_id)
                if not inbox_apply.owner_matches(session, user, delta.owner_urn):
                    # The page is not this account's inbox: write nothing, end aborted.
                    runs.finish_run(
                        session,
                        user,
                        started_run_id,
                        status=SyncRunStatus.ABORTED,
                        now=clock(),
                        stop_reason=OWNER_MISMATCH,
                        counts=_zero_counts(),
                    )
                    # #443: counted in the same transaction as the ending.
                    route_breaker.record_inbox_owner(
                        session,
                        user,
                        account_id,
                        owner_mismatch=True,
                        completed=False,
                        now=clock(),
                    )
                    return None
                counts = inbox_apply.apply_delta(
                    session, user, delta, polled_at=clock(), settings=campaign_settings
                )
                notes: tuple[str, ...] = ()
                if reason == FIRST_SHORT and since is not None:
                    inbox_apply.record_short_first_poll(session, user, since)
                    notes = (
                        f"the first LinkedIn inbox poll could not read back to {since:%Y-%m-%d};"
                        " check older LinkedIn replies by hand.",
                    )
                runs.finish_run(
                    session,
                    user,
                    started_run_id,
                    status=SyncRunStatus.ABORTED
                    if reason == INCOMPLETE
                    else SyncRunStatus.COMPLETED,
                    now=clock(),
                    stop_reason=reason,
                    counts=dict(counts.counts()),
                    notes=notes,
                )
                return counts
        except Exception as exc:
            # The rows carry message text, and a database error quotes its parameters:
            # record the type alone, and drop the chain so no traceback carries them.
            log.error(
                "inbox poll %d: applying what was read failed (%s)",
                started_run_id,
                type(exc).__name__,
            )
            raise InboxPollFailed(f"applying the inbox failed: {type(exc).__name__}") from None

    async with runs_recording(factory, user_id, started_run_id, clock=clock):
        plan = await off_loop(prepare)
        spec = plan.spec
        refused = await off_loop(spend)
        if refused is not None:
            await off_loop(finish, SyncRunStatus.ABORTED, refused, _zero_counts(), error=None)
            return InboxPollReport(
                run_id=started_run_id, account_id=account_id, stop_reason=refused
            )
        try:
            delta = await source.read(spec)
        except InboxReadStopped as stop:
            heat_raised, flagged = await off_loop(record_response, stop.outcome, stop.final_url)
            if stop.outcome in _ROUTE_OUTCOMES:
                await off_loop(record_breaker, route_changed=True, completed=False)
            await off_loop(
                finish, SyncRunStatus.ABORTED, stop.outcome.value, _zero_counts(), error=None
            )
            log.warning("inbox poll %d: the page answered %s", started_run_id, stop.outcome.value)
            return InboxPollReport(
                run_id=started_run_id,
                account_id=account_id,
                stop_reason=stop.outcome.value,
                outcome=stop.outcome,
                heat_raised=heat_raised,
                session_flagged=flagged,
            )
        except ObservationFailed:
            # #437 review: the observation mechanism failing to read the page says the
            # route could not be read, as in connections_sync (#191 F1). Count it, then
            # let ``runs_recording`` finish the run failed. BrowserError stays out: a
            # local browser or tab problem says nothing about the messaging page.
            await off_loop(record_breaker, route_changed=True, completed=False)
            raise
        except BrowserError:
            raise
        except Exception as exc:
            log.error(
                "inbox poll %d: reading the inbox failed (%s)", started_run_id, type(exc).__name__
            )
            raise InboxPollFailed(f"reading the inbox failed: {type(exc).__name__}") from None
        reason = _ending(delta, plan)
        counts = await off_loop(apply_and_finish, delta, reason, spec.since)
        if counts is not None and reason != INCOMPLETE:
            # The poll completed: a poll that reads again clears the inbox breaker.
            await off_loop(record_breaker, route_changed=False, completed=True)
            await off_loop(record_owner_cleared)
        if counts is None:
            log.error(
                "inbox poll %d: the page showed another mailbox; nothing written", started_run_id
            )
            return InboxPollReport(
                run_id=started_run_id, account_id=account_id, stop_reason=OWNER_MISMATCH
            )
    return InboxPollReport(
        run_id=started_run_id, account_id=account_id, stop_reason=reason, counts=counts
    )


def _ending(delta: InboxDelta, plan: PollPlan) -> str:
    """``inbox_read`` for a complete read; for a first poll that could not reach its
    ``since``, ``inbox_first_short`` (counted complete); otherwise ``inbox_incomplete``."""
    if delta.complete or (plan.first and plan.spec.since is None):
        return READ
    return FIRST_SHORT if plan.first else INCOMPLETE
