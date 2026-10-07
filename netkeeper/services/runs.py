"""Extractor runs: create, watch, cancel, finish, list (spec 8.4, 9.9, 14.1; P2-10).

A ``sync_runs`` row is what a person sees of a run: which kind, who asked for
it, how far it got, and how it ended. This module owns the row. It does not
run anything: the browser worker (``netkeeper.worker``) holds the activity
lock, attaches, and calls the connections sync, enrichment, or inbox poll runner,
and it reports back through :func:`record_progress` and :func:`finish_run`.

**Who may start a run.** :func:`create_run` is the one door, and it refuses:

* a kind with no runner (``message_send``);
* a **scheduled** ``message_send``, except an auto-send's (ADR 0008): a LinkedIn
  prefill runs only when a person asks for it (P4-09), and the scheduler records a
  ``message_send`` run only through an auto-send claim, with :data:`AUTO_SEND_GATE`;
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

**Pause** (#324) is a cancel that keeps its place, for an enrichment run only:
:func:`request_pause` sets the same cancel flag and a marker
(:func:`pause_requested`), and :func:`finish_run` records the run's
``cancelled`` ending as :data:`PAUSED` instead. The run ends ``aborted`` with
its plan stored, as a cancelled one does, and a resume
(``enrich_plan.start_resume``) continues the rest of that plan. A connections
sync has no plan to keep, so it cannot be paused; Cancel stops it. The marker is
one ``settings_kv`` key per run, removed when the run ends, so nothing about
the ``sync_runs`` table changed. A cancel asked for after a pause wins: it
removes the marker.

**A process that went away** leaves its runs ``running``. At every start,
:func:`fail_interrupted_runs` marks them ``failed`` ("interrupted"). None is
resumed on its own: an interrupted enrichment can be resumed by a person, and
a scheduled one waits for its next due time.

**Counts only.** ``progress_json`` and ``counts_json`` hold numbers and short
reason words, never a name, URN, slug, cookie, or body, and ``error`` is one
line from the exception that ended the run. An enrichment's per-visit reasons
(#405) hold netkeeper's own contact ids beside fixed codes, never anything the
page said (:mod:`netkeeper.services.run_diagnostics` reads them back).

Transactions belong to the caller, and every writer here needs a writer
session (``session_scope(factory, write=True)``).
"""

from __future__ import annotations

import asyncio
import enum
import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import LinkedInSettings
from netkeeper.db import CancelledWhileFailing, is_writer, off_loop, session_scope
from netkeeper.linkedin import activity_lock, pacing
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.errors import BrowserUnavailable
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
from netkeeper.services.settings_kv import delete_setting, get_setting, set_setting

log = logging.getLogger(__name__)

#: The kinds a run can be started for today: the two connections syncs (P2-06),
#: enrichment (P2-07), the inbox poll (P4-08), and the LinkedIn prefill (P4-03), which
#: :func:`create_run` still records only manually and only with a claim's gate token.
#: The inbox poll reads its page through ``PageInbox`` (P4-01, #380); a first poll is run by
#: hand, and scheduled polls wait for it.
RUNNABLE_KINDS: Final = frozenset(
    {
        SyncRunKind.CONNECTIONS_FULL,
        SyncRunKind.CONNECTIONS_INCREMENTAL,
        SyncRunKind.ENRICH,
        SyncRunKind.INBOX,
        SyncRunKind.MESSAGE_SEND,
    }
)

MESSAGE_SEND_GATE: Final = object()
"""The token :func:`create_run` needs for a manual ``message_send`` run. Only
:func:`netkeeper.services.linkedin_steps.start_message_send_run` passes it, after the
claim's checks (P4-09), and tests that stand in for it."""

AUTO_SEND_GATE: Final = object()
"""The token :func:`create_run` needs for a **scheduled** ``message_send`` run: an
auto-send (ADR 0008). Only :func:`netkeeper.services.linkedin_steps.start_auto_send_run`
passes it, inside :func:`~netkeeper.services.linkedin_steps.claim_auto_send`, and tests
that stand in for it."""

#: The line an interrupted run's ``error`` carries.
INTERRUPTED: Final = "interrupted: the netkeeper process running it stopped"

#: The ``stop_reason`` of a run a person paused (#324): stopped like a cancel, at the
#: next check, and resumable, since its plan is kept.
PAUSED: Final = "paused"

#: The ``stop_reason`` a cancel records. A paused run's is :data:`PAUSED` instead.
CANCELLED: Final = "cancelled"

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


def _fail_stale(session: Session, user: User, run: SyncRun, *, now: datetime) -> None:
    _clear_pause(session, user, run.id)
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


class RunNotPausable(RunError):
    """Only an enrichment run keeps a plan to resume, so only one can be paused."""


class HeatSkipped(RuntimeError):
    """Heat is at or above the skip threshold; the run did not start (spec 9.7)."""


class SessionFlagged(RuntimeError):
    """The session flag is set (a checkpoint or a login wall); the run did not start."""


class OutsideActiveHours(RuntimeError):
    """A manual run was asked for outside ``[linkedin] active_hours``; it was not recorded.

    The message is :func:`netkeeper.linkedin.pacing.outside_window_message`: the
    window, when it next opens, and where to change it (#213).
    """


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


def last_browser_unavailable(session: Session, user: User, account_id: int) -> SyncRun | None:
    """The account's newest ended run that could not reach Chrome (#181). Read-only.

    A run ends ``browser_unavailable`` when the worker could not attach, or Chrome
    went away mid-run. This only reads what the worker already recorded; it never
    looks at the browser itself.
    """
    return session.scalars(
        scoped(user, SyncRun)
        .where(
            SyncRun.linkedin_account_id == account_id,
            SyncRun.stop_reason == "browser_unavailable",
            SyncRun.completed_at.is_not(None),
        )
        .order_by(SyncRun.completed_at.desc(), SyncRun.id.desc())
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
    gate: object = None,
) -> SyncRun:
    """Record a new ``running`` run of ``kind`` for ``user``'s account, and return it.

    A manual ``message_send`` run is recorded only with ``gate``
    :data:`MESSAGE_SEND_GATE`, which only
    :func:`netkeeper.services.linkedin_steps.start_message_send_run` passes, inside a
    prefill claim (P4-09); a scheduled one only with :data:`AUTO_SEND_GATE`, inside an
    auto-send claim (ADR 0008). Never through ``POST /linkedin/runs`` or the CLI's run
    commands.

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
    if (
        kind is SyncRunKind.MESSAGE_SEND
        and trigger is not SyncRunTrigger.MANUAL
        and gate is not AUTO_SEND_GATE
    ):
        # Person-triggered (P4-09), unless an auto-send claim records it (ADR 0008):
        # checked before anything else.
        raise RunError("a message_send run is only ever started by a person, never scheduled")
    if (
        kind is SyncRunKind.MESSAGE_SEND
        and trigger is SyncRunTrigger.MANUAL
        and gate is not MESSAGE_SEND_GATE
    ):
        raise RunError("a message_send run is started only by a LinkedIn prefill claim")
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
        _fail_stale(session, user, busy, now=now)
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
    """Replace the run's ``progress_json`` with ``progress``: counts, reason words, and an
    enrichment's per-visit reason record (#405), never anything from the page."""
    _require_writer(session)
    run = get_run(session, user, run_id)
    run.progress_json = dict(progress)
    return run


def record_progress_field(
    session: Session, user: User, run_id: int, key: str, value: JsonValue
) -> SyncRun:
    """Set one key of the run's ``progress_json``, keeping the rest as it was (#405)."""
    _require_writer(session)
    run = get_run(session, user, run_id)
    run.progress_json = {**(run.progress_json or {}), key: value}
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
    if pause_requested(session, user, run_id):
        if stop_reason == CANCELLED:
            stop_reason = PAUSED
        _clear_pause(session, user, run_id)
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

    The flag is set first, stale or not (#177 G1). "Stale" is judged from this
    process's lock files, so a live run in another data directory that shares
    this database looks stale from here. Its runner still reads the flag, and
    the ``failed`` status, and stops.
    """
    _require_writer(session)
    _require_aware(now)
    run = get_run(session, user, run_id)
    if run.status is not SyncRunStatus.RUNNING:
        raise RunFinished(f"run {run_id} already ended {run.status.value}")
    if run.cancel_requested_at is None:
        run.cancel_requested_at = now
        log.info("cancel requested for run %d", run_id)
    if pause_requested(session, user, run_id):
        # A cancel after a pause wins: the run ends cancelled, not paused.
        _clear_pause(session, user, run_id)
        log.info("cancel replaces the pause asked for run %d", run_id)
    if _stale(run, now=now, held=browser_held or browser_held_for(session)):
        _fail_stale(session, user, run, now=now)
    return run


def request_pause(
    session: Session,
    user: User,
    run_id: int,
    *,
    now: datetime,
    browser_held: BrowserHeld | None = None,
) -> SyncRun:
    """Ask an enrichment run to stop at its next check and keep its place (#324).

    The same cooperative stop as :func:`request_cancel` -- the runner reads the
    same flag between profiles and inside its sliced waits -- plus a marker that
    makes :func:`finish_run` record the ending as :data:`PAUSED`, not
    ``cancelled``. The run ends ``aborted`` with its plan, and a resume continues
    the rest. Idempotent while it runs.

    :class:`RunFinished` for a run that already ended, :class:`RunNotPausable`
    for a connections sync (it keeps no plan), and :class:`RunError` for a run
    already being cancelled. A run left behind by a process that went away is
    marked ``failed`` at once, as :func:`request_cancel` does; its plan is just
    as resumable.
    """
    _require_writer(session)
    _require_aware(now)
    run = get_run(session, user, run_id)
    if run.status is not SyncRunStatus.RUNNING:
        raise RunFinished(f"run {run_id} already ended {run.status.value}")
    if run.kind is not SyncRunKind.ENRICH:
        raise RunNotPausable(
            f"run {run_id} is a {run.kind.value} run; only an enrichment run keeps a plan"
            " to resume, so cancel a sync instead"
        )
    already = pause_requested(session, user, run_id)
    if run.cancel_requested_at is not None and not already:
        raise RunError(f"run {run_id} is already being cancelled")
    if not already:
        set_setting(session, user, _pause_key(run_id), now.isoformat())
        run.cancel_requested_at = now
        log.info("pause requested for run %d", run_id)
    if _stale(run, now=now, held=browser_held or browser_held_for(session)):
        _fail_stale(session, user, run, now=now)
    return run


def pause_requested(session: Session, user: User, run_id: int) -> bool:
    """Whether a person asked run ``run_id`` to pause and it has not ended yet. Read-only."""
    return get_setting(session, user, _pause_key(run_id)) is not None


def _pause_key(run_id: int) -> str:
    return f"linkedin.run.{run_id}.pause_requested"


def _clear_pause(session: Session, user: User, run_id: int) -> None:
    delete_setting(session, user, _pause_key(run_id))


def cancel_requested(session: Session, user: User, run_id: int) -> bool:
    """Whether the run must stop: a person asked it to, or it is no longer ``running``.

    A run that something else already ended (marked ``failed`` as left behind by
    another process that shares this database, #177 G1) has nobody waiting for
    its work, so its runner stops at the next check as it would for a cancel.
    A run that does not exist reads as not cancelled. Read-only.
    """
    statement = scoped(user, SyncRun).with_only_columns(SyncRun.cancel_requested_at, SyncRun.status)
    row = session.execute(statement.where(SyncRun.id == run_id)).one_or_none()
    if row is None:
        return False
    requested_at, status = row
    return requested_at is not None or status is not SyncRunStatus.RUNNING


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
            _fail_stale(session, user, run, now=now)
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


def refuse_if_outside_active_hours(settings: LinkedInSettings, *, now: datetime) -> None:
    """:class:`OutsideActiveHours` when ``now`` is outside ``[linkedin] active_hours`` (#213).

    For a run started by hand, checked before its row is created, so a run that
    could not visit anything is never recorded. Every manual kind is checked,
    connections syncs included: the scheduler never starts any kind outside the
    window (spec 9.5), and a person starting one by hand gets the same rule and
    the same sentence. Inside a run, only enrichment checks the window again
    between units, since a sync is minutes long and one stopped part-way through
    the list would age nobody for the week. A window that does not parse is a
    :class:`RunError`: nothing may run unguarded by it.
    """
    _require_aware(now)
    try:
        start, end = (time.fromisoformat(value) for value in settings.active_hours)
    except ValueError as exc:
        raise RunError(
            f"[linkedin] active_hours is not two HH:MM times ({settings.active_hours!r}),"
            " so no run can start until config.toml is fixed"
        ) from exc
    if not pacing.is_active_at(now, settings.timezone, start=start, end=end):
        raise OutsideActiveHours(
            pacing.outside_window_message(now, settings.timezone, start=start, end=end)
        )


#: Every ``stop_reason`` a run can carry, in the words a person reads (#213). The
#: short reason stays the stored value (a breaker counts on it); these are what the
#: CLI, the API's ``stop_reason_text``, and the LinkedIn page show beside it.
STOP_REASON_TEXT: Final[Mapping[str, str]] = {
    # how a run ends on its own
    "end_of_list": "read to the end of the connections list",
    "caught_up": "caught up with connections it already knew",
    "page_budget": "read the pages it was allowed",
    "end_of_plan": "visited every profile in its plan",
    "visit_budget": "made the visits it was allowed",
    "budget": "today's or this week's budget is spent",
    "inactive": "outside active hours",
    "cancelled": "cancelled",
    "paused": "paused; resume it to continue its plan",
    "answer_lost": "lost some of the page's answers",
    "inbox_read": "read the inbox back to the last complete poll",
    "inbox_incomplete": "read part of the inbox; the next poll reads it again",
    "inbox_first_short": "the first poll could not read back to the earliest outreach",
    # what LinkedIn answered
    "throttled": "LinkedIn throttled it",
    "checkpoint": "LinkedIn showed a checkpoint",
    "logged_out": "LinkedIn asked to log in",
    "route_changed": "the page's answers changed shape",
    "not_found": "a profile was not found",
    # refused before it started, or could not run
    "session_flagged": "refused: the session is flagged",
    "heat_skip": "refused: heat is over its skip threshold",
    "disarmed": "refused: scheduled runs are disarmed",
    "route_changed_breaker": "refused: the route-changed breaker is tripped",
    "answer_lost_breaker": "refused: the answer-lost limit is tripped",
    "contact_info_breaker": "refused: the Contact info breaker is tripped",
    "inbox_route_changed_breaker": "refused: the inbox breaker is tripped",
    "inbox_owner_breaker": "refused: the inbox owner breaker is tripped",
    "no_runner": "refused: no runner for this kind",
    "no_source": "refused: the LinkedIn inbox poll has no page source yet",
    "owner_mismatch": (
        "the page showed another LinkedIn mailbox than this account's; if Chrome is signed"
        " in to another LinkedIn account, sign back in to yours; otherwise run"
        " `netkeeper linkedin inbox-forget-owner` (or, if your own contact has a LinkedIn"
        " ID, it does not match this mailbox and netkeeper cannot edit it yet)"
    ),
    "first_inbox_poll": "refused: the first LinkedIn inbox poll is run by hand",
    "browser_busy": "the browser was busy with another run",
    "browser_unavailable": "Chrome was not reachable or went away mid-run",
    "interrupted": "the netkeeper process running it stopped",
    "error": "an error stopped it",
}


def has_completed_inbox_poll(session: Session, user: User) -> bool:
    """Whether any inbox poll has completed, by hand or on schedule. Read-only."""
    return latest_run(session, user, SyncRunKind.INBOX, status=SyncRunStatus.COMPLETED) is not None


def describe_stop_reason(reason: str | None) -> str | None:
    """``reason`` in plain words, or the reason itself when it has none yet; ``None`` stays."""
    if reason is None:
        return None
    return STOP_REASON_TEXT.get(reason, reason)


def stop_reason_of(reason: str, outcome: Outcome | None) -> str:
    """A run's ``stop_reason``: the job's reason, or the outcome that stopped it."""
    return outcome.value if reason == "response" and outcome is not None else reason


@asynccontextmanager
async def recording(
    factory: sessionmaker[Session], user_id: int, run_id: int, *, clock: Callable[[], datetime]
) -> AsyncIterator[None]:
    """Record on run ``run_id`` how the block failed, if it did, and re-raise.

    A refusal (:class:`SessionFlagged`, :class:`HeatSkipped`) is ``failed``
    with that reason; a cancellation from outside the run (the process
    shutting down) is ``aborted``, "interrupted", keeping what completed,
    unless the database write in flight when it landed failed too
    (:class:`~netkeeper.db.CancelledWhileFailing`, #266), which is ``failed``,
    "error"; any other exception is ``failed``, with the first line of its
    message, except a lost browser (:class:`~netkeeper.linkedin.errors.BrowserUnavailable`),
    which is ``failed``, "browser_unavailable". A run the block already finished is
    left as it is.

    The ending is written off the event loop (:func:`netkeeper.db.off_loop`,
    #259), after any database work the block still had in flight.
    """
    try:
        yield
    except SessionFlagged as exc:
        await off_loop(
            _finish_quietly,
            factory,
            user_id,
            run_id,
            clock,
            SyncRunStatus.FAILED,
            "session_flagged",
            exc,
        )
        raise
    except HeatSkipped as exc:
        await off_loop(
            _finish_quietly, factory, user_id, run_id, clock, SyncRunStatus.FAILED, "heat_skip", exc
        )
        raise
    except CancelledWhileFailing as exc:
        # The process is shutting down, but the write in flight failed on its own
        # (#266): that is a failure, not a clean interruption.
        await off_loop(
            _finish_quietly,
            factory,
            user_id,
            run_id,
            clock,
            SyncRunStatus.FAILED,
            "error",
            exc.error,
        )
        raise
    except asyncio.CancelledError:
        await off_loop(
            _finish_quietly,
            factory,
            user_id,
            run_id,
            clock,
            SyncRunStatus.ABORTED,
            "interrupted",
            None,
        )
        raise
    except BrowserUnavailable as exc:
        # Chrome went away mid-run (#177): the same reason as a run that could not
        # attach at all, so the run reads "Chrome was not reachable or went away
        # mid-run", not "error".
        await off_loop(
            _finish_quietly,
            factory,
            user_id,
            run_id,
            clock,
            SyncRunStatus.FAILED,
            "browser_unavailable",
            exc,
        )
        raise
    except Exception as exc:
        await off_loop(
            _finish_quietly, factory, user_id, run_id, clock, SyncRunStatus.FAILED, "error", exc
        )
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


def lost_answers(run: SyncRun) -> int:
    """How many of the page's answers a connections run lost (#200), from its counts.

    A connections run's ``counts_json.lost`` lists every lost start; a run with any
    is incomplete, whatever its ``stop_reason``. Any other shape reads as none.
    """
    counts = run.counts_json if isinstance(run.counts_json, dict) else {}
    lost = counts.get("lost")
    return len(lost) if isinstance(lost, list) else 0


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
    #: ``stop_reason`` in plain words (:data:`STOP_REASON_TEXT`), or ``None`` while running.
    stop_reason_text: str | None = None


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
        stop_reason_text=describe_stop_reason(run.stop_reason),
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
