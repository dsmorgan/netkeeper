"""Extractor runs: create, watch, cancel, finish, list (spec 8.4, 9.9, 14.1; P2-10).

A ``sync_runs`` row is what a person sees of a run: which kind, who asked for
it, how far it got, and how it ended. This module owns the row. It does not
run anything: the browser worker (``netkeeper.worker``) holds the activity
lock, attaches, and calls the connections sync or enrichment runner, and it
reports back through :func:`record_progress` and :func:`finish_run`.

**Who may start a run.** :func:`create_run` is the one door, and it refuses:

* a kind with no runner (``inbox``, ``message_send``);
* a second run while one of the same account's runs is still ``running`` --
  the browser takes one client per account anyway (spec 9.9), and a second row
  that could only fail ``busy`` tells a person nothing;
* a **scheduled** run while the account's scheduled runs are disarmed
  (:func:`netkeeper.services.linkedin_accounts.scheduled_runs_armed`). Every
  account starts disarmed. The scheduler checks this before it fires (its arm
  gate) and the worker checks it again before it attaches; this is the third
  place, so a scheduled run cannot even be *recorded* on a disarmed account.

A **manual** run is allowed while disarmed. That is how the first supervised
run happens (CP4): a person asks for it, by name, with ``POST /linkedin/runs``
or ``netkeeper linkedin sync``/``enrich``. Nothing that only reads -- a
``GET``, a page load, the SSE stream, starting the server -- ever creates one.

**Cancel** (spec 9.9) is a timestamp on the row, :func:`request_cancel`. The
runners read it (:func:`cancel_requested`) between units of work and inside
their sliced waits, and the run ends ``aborted`` with whatever it completed.
Because the flag is in the database, not in memory, ``netkeeper linkedin
cancel`` in one terminal stops a run another process is doing.

**A process that went away** leaves its runs ``running``. At every start,
:func:`fail_interrupted_runs` marks them ``failed`` ("interrupted"). None is
resumed on its own: an interrupted enrichment can be resumed by a person, and
a scheduled one waits for its next due time.

**Counts only.** ``progress_json`` and ``counts_json`` hold numbers and short
reason words, never a name, URN, slug, cookie, or body, and ``error`` is one
line from the exception that ended the run.

Transactions belong to the caller, and every writer here needs a writer
session (``session_scope(factory, write=True)``).
"""

from __future__ import annotations

import asyncio
import enum
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import LinkedInSettings
from netkeeper.db import is_writer, session_scope
from netkeeper.linkedin import activity_lock
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import (
    JsonValue,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    User,
)
from netkeeper.scoping import get_scoped, scoped, scoped_count
from netkeeper.services import heat as heat_service
from netkeeper.services.linkedin_accounts import (
    ensure_account,
    local_account_id,
    scheduled_runs_armed,
)
from netkeeper.services.linkedin_session import session_flag

log = logging.getLogger(__name__)

#: The kinds a run can be started for today: the two connections syncs (P2-06)
#: and enrichment (P2-07). ``inbox`` and ``message_send`` have no runner yet.
RUNNABLE_KINDS: Final = frozenset(
    {
        SyncRunKind.CONNECTIONS_FULL,
        SyncRunKind.CONNECTIONS_INCREMENTAL,
        SyncRunKind.ENRICH,
    }
)

#: The line an interrupted run's ``error`` carries.
INTERRUPTED: Final = "interrupted: the netkeeper process running it stopped"

#: The longest ``error`` or ``notes`` line kept. A traceback belongs in the log.
MAX_MESSAGE_LENGTH: Final = 500

#: How long a ``running`` run may go without its account's browser lock being
#: held before it counts as left behind by a process that went away. A live run
#: holds the lock for its whole length, except in the moment between the row
#: being committed and the worker taking the lock; this grace covers that
#: moment, so a run just asked for is never failed under the process about to
#: start it (#175 review, F7).
STALE_AFTER: Final = timedelta(minutes=2)

BrowserHeld = Callable[[int], bool]


def lock_held(account_id: int, *, legacy: bool) -> bool:
    """Whether any process holds ``account_id``'s browser lock (and, for the local
    user's account, ``legacy``, the pre-P2-10 ``browser-local.lock``). Never attaches.

    Reads the lock files with a shared peek that is dropped at once. When a lock
    cannot be inspected at all the answer is "held": a run nobody can prove is
    over is left alone rather than failed under a process that may be running it.
    """
    try:
        if activity_lock.inspect(activity_lock.account_key(account_id)).held:
            return True
        return legacy and activity_lock.inspect(activity_lock.LEGACY_SHARED_KEY).held
    except OSError:
        return True


def browser_held_for(session: Session) -> BrowserHeld:
    """:func:`lock_held` for this database: the legacy lock counts for the local account."""
    local = local_account_id(session)
    return lambda account_id: lock_held(account_id, legacy=account_id == local)


def _stale(run: SyncRun, *, now: datetime, held: BrowserHeld) -> bool:
    return now - run.started_at >= STALE_AFTER and not held(run.linkedin_account_id)


def _fail_stale(run: SyncRun, *, now: datetime) -> None:
    run.status = SyncRunStatus.FAILED
    run.completed_at = now
    run.stop_reason = "interrupted"
    run.error = INTERRUPTED
    log.warning("run %d was left running by a process that went away; marked failed", run.id)


class RunError(ValueError):
    """A run that cannot be started, cancelled, or resumed as asked."""


class RunNotFound(LookupError):
    """No run with that id for this user."""


class RunAlreadyRunning(RunError):
    """One of the account's runs is still running."""


class ScheduledRunsDisarmed(RunError):
    """A scheduled run was asked for while the account's scheduled runs are disarmed."""


class RunFinished(RunError):
    """The run already ended; there is nothing to cancel."""


class HeatSkipped(RuntimeError):
    """Heat is at or above the skip threshold; the run did not start (spec 9.7)."""


class SessionFlagged(RuntimeError):
    """The session flag is set (a checkpoint or a login wall); the run did not start."""


# --- reading ------------------------------------------------------------------


def get_run(session: Session, user: User, run_id: int) -> SyncRun:
    """``user``'s run ``run_id``; :class:`RunNotFound` otherwise. Read-only."""
    run = get_scoped(session, user, SyncRun, run_id)
    if run is None:
        raise RunNotFound(f"no run {run_id}")
    return run


def list_runs(
    session: Session,
    user: User,
    *,
    kind: SyncRunKind | None = None,
    status: SyncRunStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[SyncRun], int]:
    """``user``'s runs, newest first, and how many match in all. Read-only."""
    statement = scoped(user, SyncRun)
    count = scoped_count(user, SyncRun)
    if kind is not None:
        statement = statement.where(SyncRun.kind == kind)
        count = count.where(SyncRun.kind == kind)
    if status is not None:
        statement = statement.where(SyncRun.status == status)
        count = count.where(SyncRun.status == status)
    rows = session.scalars(
        statement.order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(limit).offset(offset)
    ).all()
    return list(rows), int(session.scalar(count) or 0)


def running_run(session: Session, user: User, account_id: int) -> SyncRun | None:
    """The account's run that is still ``running``, if there is one. Read-only."""
    return session.scalars(
        scoped(user, SyncRun)
        .where(
            SyncRun.linkedin_account_id == account_id,
            SyncRun.status == SyncRunStatus.RUNNING,
        )
        .order_by(SyncRun.id.desc())
        .limit(1)
    ).first()


def latest_run(
    session: Session,
    user: User,
    kind: SyncRunKind,
    *,
    status: SyncRunStatus | None = None,
) -> SyncRun | None:
    """``user``'s newest run of ``kind`` (with ``status``, when given). Read-only."""
    statement = scoped(user, SyncRun).where(SyncRun.kind == kind)
    if status is not None:
        statement = statement.where(SyncRun.status == status)
    return session.scalars(
        statement.order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(1)
    ).first()


# --- writing ------------------------------------------------------------------


def create_run(
    session: Session,
    user: User,
    kind: SyncRunKind,
    *,
    trigger: SyncRunTrigger,
    now: datetime,
    max_visits: int | None = None,
    resume_of_id: int | None = None,
    browser_held: BrowserHeld | None = None,
) -> SyncRun:
    """Record a new ``running`` run of ``kind`` for ``user``'s account, and return it.

    Refuses (see the module docstring) a kind with no runner
    (:class:`RunError`), a second run while one is running
    (:class:`RunAlreadyRunning`), and a scheduled run on a disarmed account
    (:class:`ScheduledRunsDisarmed`). ``max_visits`` is for enrichment only
    and must be at least 1; it can only lower the day's budget.

    A ``running`` run whose account's browser lock nobody holds, older than
    :data:`STALE_AFTER`, was left behind by a process that went away (a CLI run
    killed with ``SIGKILL``): it is marked ``failed`` here rather than blocking
    every new run until the next start. ``browser_held`` defaults to reading the
    lock files (:func:`browser_held_for`).
    """
    _require_writer(session)
    _require_aware(now)
    if kind not in RUNNABLE_KINDS:
        raise RunError(f"no runner exists for {kind.value} runs yet")
    if max_visits is not None:
        if kind is not SyncRunKind.ENRICH:
            raise RunError("max_visits is for enrichment runs only")
        if max_visits < 1:
            raise RunError("max_visits must be at least 1")
    if resume_of_id is not None and kind is not SyncRunKind.ENRICH:
        raise RunError("only an enrichment run can be resumed")
    account = ensure_account(session, user)
    if trigger is SyncRunTrigger.SCHEDULED and not scheduled_runs_armed(session, user, account.id):
        raise ScheduledRunsDisarmed(
            "scheduled LinkedIn runs are disarmed for this account; arm them with"
            " `netkeeper linkedin schedule arm` once a supervised run has gone well"
        )
    busy = running_run(session, user, account.id)
    if busy is not None and _stale(busy, now=now, held=browser_held or browser_held_for(session)):
        _fail_stale(busy, now=now)
        busy = None
    if busy is not None:
        raise RunAlreadyRunning(
            f"run {busy.id} ({busy.kind.value}) is still running for this account;"
            " cancel it or wait for it to finish"
        )
    run = SyncRun(
        user_id=user.id,
        linkedin_account_id=account.id,
        kind=kind,
        status=SyncRunStatus.RUNNING,
        trigger=trigger,
        started_at=now,
        max_visits=max_visits,
        resume_of_id=resume_of_id,
    )
    session.add(run)
    session.flush()
    log.info(
        "run %d (%s, %s) created for user %d account %d",
        run.id,
        kind.value,
        trigger.value,
        user.id,
        account.id,
    )
    return run


def record_progress(
    session: Session, user: User, run_id: int, progress: Mapping[str, JsonValue]
) -> SyncRun:
    """Replace the run's ``progress_json`` with ``progress`` (counts only)."""
    _require_writer(session)
    run = get_run(session, user, run_id)
    run.progress_json = dict(progress)
    return run


def finish_run(
    session: Session,
    user: User,
    run_id: int,
    *,
    status: SyncRunStatus,
    now: datetime,
    stop_reason: str | None = None,
    counts: Mapping[str, JsonValue] | None = None,
    notes: Sequence[str] = (),
    error: str | None = None,
) -> SyncRun:
    """Record how the run ended. A run that already ended is left as it is."""
    _require_writer(session)
    _require_aware(now)
    if status is SyncRunStatus.RUNNING:
        raise ValueError("a finished run is completed, aborted, or failed")
    run = get_run(session, user, run_id)
    if run.status is not SyncRunStatus.RUNNING:
        log.warning("run %d already ended %s; not recording %s", run_id, run.status, status)
        return run
    run.status = status
    run.completed_at = now
    run.stop_reason = None if stop_reason is None else stop_reason[:32]
    if counts is not None:
        run.counts_json = dict(counts)
    if notes:
        run.notes = _line(" ".join(notes))
    if error is not None:
        run.error = _line(error)
    log.info("run %d ended %s%s", run_id, status.value, f" ({stop_reason})" if stop_reason else "")
    return run


def request_cancel(
    session: Session,
    user: User,
    run_id: int,
    *,
    now: datetime,
    browser_held: BrowserHeld | None = None,
) -> SyncRun:
    """Ask the run to stop at its next check (spec 9.9). Idempotent while it runs.

    :class:`RunFinished` for a run that already ended. A run left behind by a
    process that went away (see :func:`create_run`) has nobody to read the flag,
    so it is marked ``failed`` at once instead, and returned.
    """
    _require_writer(session)
    _require_aware(now)
    run = get_run(session, user, run_id)
    if run.status is not SyncRunStatus.RUNNING:
        raise RunFinished(f"run {run_id} already ended {run.status.value}")
    if _stale(run, now=now, held=browser_held or browser_held_for(session)):
        _fail_stale(run, now=now)
        return run
    if run.cancel_requested_at is None:
        run.cancel_requested_at = now
        log.info("cancel requested for run %d", run_id)
    return run


def cancel_requested(session: Session, user: User, run_id: int) -> bool:
    """Whether a person asked the run to stop. Read-only."""
    statement = scoped(user, SyncRun).with_only_columns(SyncRun.cancel_requested_at)
    value = session.scalar(statement.where(SyncRun.id == run_id))
    return value is not None


def fail_interrupted_runs(
    session: Session,
    *,
    now: datetime,
    browser_held: BrowserHeld | None = None,
) -> int:
    """Mark every user's ``running`` run ``failed``: its process is gone. Returns how many.

    Called once at process start, before anything could start a run. Walks every
    user, one scoped query each. ``browser_held(account_id)`` says whether some
    process holds that account's browser lock right now (default: the lock
    files, :func:`browser_held_for`); a run on such an account is left alone,
    because it may be a ``netkeeper linkedin sync`` in a terminal that is still
    going. So is one younger than :data:`STALE_AFTER`: a terminal's run in the
    moment between committing its row and taking the lock (#175 review, F7).
    ``create_run`` and ``request_cancel`` catch it later if it really was left.
    """
    _require_writer(session)
    _require_aware(now)
    held = browser_held or browser_held_for(session)
    users = session.scalars(select(User).order_by(User.id)).all()
    total = 0
    for user in users:
        stale = session.scalars(
            scoped(user, SyncRun).where(SyncRun.status == SyncRunStatus.RUNNING)
        ).all()
        for run in stale:
            if not _stale(run, now=now, held=held):
                continue
            _fail_stale(run, now=now)
            total += 1
    if total:
        log.warning("marked %d run(s) left running by a stopped process as failed", total)
    return total


# --- what every runner does around its job ------------------------------------


def refuse_if_flagged_or_hot(
    session: Session, user: User, account_id: int, *, now: datetime, settings: LinkedInSettings
) -> None:
    """:class:`SessionFlagged` or :class:`HeatSkipped` when no run may start (spec 9.7). Read-only.

    After a checkpoint or a login wall, a run that fetched anyway would be the
    retry ADR 0002 forbids, one run later; clearing the flag is a person's act.
    Above the heat skip threshold no browser job runs, whoever started it: the
    scheduler checks too, but a manual run must not be the hole in that.
    """
    flagged_by = session_flag(session, user)
    if flagged_by is not None:
        raise SessionFlagged(
            f"the LinkedIn session is flagged ({flagged_by.outcome.value}); no run starts"
            " until it is cleared"
        )
    if heat_service.should_skip(session, user, account_id, now=now, settings=settings.heat):
        raise HeatSkipped(
            f"heat is at or above {settings.heat.skip_threshold}; no run starts until it cools"
        )


def stop_reason_of(reason: str, outcome: Outcome | None) -> str:
    """A run's ``stop_reason``: the job's reason, or the outcome that stopped it."""
    return outcome.value if reason == "response" and outcome is not None else reason


@contextmanager
def recording(
    factory: sessionmaker[Session], user_id: int, run_id: int, *, clock: Callable[[], datetime]
) -> Iterator[None]:
    """Record on run ``run_id`` how the block failed, if it did, and re-raise.

    A refusal (:class:`SessionFlagged`, :class:`HeatSkipped`) is ``failed``
    with that reason; a cancellation from outside the run (the process
    shutting down) is ``aborted``, "interrupted", keeping what completed; any
    other exception is ``failed``, with the first line of its message. A run
    the block already finished is left as it is.
    """
    try:
        yield
    except SessionFlagged as exc:
        _finish_quietly(
            factory, user_id, run_id, clock, SyncRunStatus.FAILED, "session_flagged", exc
        )
        raise
    except HeatSkipped as exc:
        _finish_quietly(factory, user_id, run_id, clock, SyncRunStatus.FAILED, "heat_skip", exc)
        raise
    except asyncio.CancelledError:
        _finish_quietly(factory, user_id, run_id, clock, SyncRunStatus.ABORTED, "interrupted", None)
        raise
    except Exception as exc:
        _finish_quietly(factory, user_id, run_id, clock, SyncRunStatus.FAILED, "error", exc)
        raise


def describe(exc: BaseException) -> str:
    """``Type: first line of the message``, the one line a failed run keeps."""
    text = str(exc).strip()
    first = text.splitlines()[0] if text else ""
    return f"{type(exc).__name__}: {first}" if first else type(exc).__name__


def _finish_quietly(
    factory: sessionmaker[Session],
    user_id: int,
    run_id: int,
    clock: Callable[[], datetime],
    status: SyncRunStatus,
    stop_reason: str,
    exc: BaseException | None,
) -> None:
    """Finish the run on the way out of a failure, never masking the failure itself."""
    try:
        with session_scope(factory, write=True) as session:
            user = session.get(User, user_id)
            if user is None:
                return
            finish_run(
                session,
                user,
                run_id,
                status=status,
                now=clock(),
                stop_reason=stop_reason,
                error=INTERRUPTED if exc is None else describe(exc),
            )
    except Exception:
        log.exception("could not record how run %d ended", run_id)


# --- the worker seam ----------------------------------------------------------


class RunOutcome(enum.StrEnum):
    """What executing a run came to, as far as the scheduler cares."""

    DONE = "done"
    """The run ran (whatever its status): the cadence moves on."""

    RETRY_LATER = "retry_later"
    """The browser could not be reached or was busy (spec 9.9): retry 20-50 min out."""


class RunExecutor(Protocol):
    """What executes a recorded run. ``netkeeper.worker.BrowserWorker`` is the real one.

    Defined here, on the core side, so the app and the scheduler can hold one
    without importing the browser (``tests/test_browser_safety.py``).
    """

    async def execute(self, run_id: int, user_id: int) -> RunOutcome: ...


@dataclass(frozen=True, slots=True)
class RunView:
    """A run as the API and the CLI show it: the row's fields, plus the ones derived here."""

    run: SyncRun
    planned: int | None
    completed: int | None
    aging_refused: str | None
    #: The run that took over this one's remaining plan, once one has (spec 9.9):
    #: a plan is resumed at most once, so a client can tell "already resumed" apart
    #: from "still resumable" without a second call to load the plan itself.
    resumed_by: int | None


def view(run: SyncRun) -> RunView:
    """The derived fields: an enrichment plan's size and progress, a refused aging's reason."""
    plan = run.plan_json or {}
    contact_ids = plan.get("contact_ids")
    done = plan.get("completed")
    resumed_by = plan.get("resumed_by")
    counts: dict[str, Any] = run.counts_json or {}
    aging = counts.get("aging")
    refused = aging.get("refused") if isinstance(aging, dict) else None
    return RunView(
        run=run,
        planned=len(contact_ids) if isinstance(contact_ids, list) else None,
        completed=len(done) if isinstance(done, list) else None,
        aging_refused=refused if isinstance(refused, str) else None,
        resumed_by=resumed_by if isinstance(resumed_by, int) else None,
    )


def _line(text: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:MAX_MESSAGE_LENGTH]


def _require_writer(session: Session) -> None:
    if not is_writer(session):
        raise RuntimeError("runs need a writer session; use session_scope(factory, write=True)")


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
