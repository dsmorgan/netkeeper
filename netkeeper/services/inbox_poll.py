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
  wrote. The next poll reads from the start of the last complete one.
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

from netkeeper.config import LinkedInSettings
from netkeeper.crm import inbox_apply
from netkeeper.db import off_loop, session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.errors import BrowserError
from netkeeper.linkedin.inbox import InboxDelta, InboxJobSpec, InboxReadStopped, InboxSource
from netkeeper.linkedin.observe import ObservationFailed
from netkeeper.models import JsonValue, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.services import budgets, runs
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

#: The ``stop_reason`` of a poll whose page did not prove that: the next poll reads again.
INCOMPLETE: Final = "inbox_incomplete"

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


def job_spec(session: Session, user: User) -> InboxJobSpec:
    """What the next poll may read: since the last complete poll, the watched contacts'
    URNs, and the threads it may open. Read-only."""
    return InboxJobSpec(
        since=last_complete_poll_start(session, user),
        watched_urns=inbox_apply.watched_urns(session, user),
        max_conversations=MAX_CONVERSATIONS_PER_POLL,
        open_threads_for=inbox_apply.threads_to_open(session, user),
    )


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
) -> InboxPollReport:
    """Run inbox poll ``run_id`` through ``source``, apply what it read, and record how it
    ended. See the module docstring.

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

    def prepare() -> InboxJobSpec:
        with session_scope(factory, write=True) as session:
            user = _load_user(session, user_id)
            refuse_if_flagged_or_hot(session, user, account_id, now=clock(), settings=settings)
            return job_spec(session, user)

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

    def apply_and_finish(delta: InboxDelta) -> inbox_apply.InboxCounts:
        try:
            with session_scope(factory, write=True) as session:
                user = _load_user(session, user_id)
                counts = inbox_apply.apply_delta(session, user, delta, polled_at=clock())
                runs.finish_run(
                    session,
                    user,
                    started_run_id,
                    status=SyncRunStatus.COMPLETED if delta.complete else SyncRunStatus.ABORTED,
                    now=clock(),
                    stop_reason=READ if delta.complete else INCOMPLETE,
                    counts=dict(counts.counts()),
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
        spec = await off_loop(prepare)
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
        except (BrowserError, ObservationFailed):
            raise
        except Exception as exc:
            log.error(
                "inbox poll %d: reading the inbox failed (%s)", started_run_id, type(exc).__name__
            )
            raise InboxPollFailed(f"reading the inbox failed: {type(exc).__name__}") from None
        counts = await off_loop(apply_and_finish, delta)
    return InboxPollReport(
        run_id=started_run_id,
        account_id=account_id,
        stop_reason=READ if delta.complete else INCOMPLETE,
        counts=counts,
    )
