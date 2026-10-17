"""When **Restart netkeeper** may stop ``serve``: never in the middle of work (#597).

A restart stops ``serve`` the way Ctrl-C does, and that cancels what is running: a
LinkedIn run (a prefill or an auto-send can be mid-typing), a background task, a
campaign tick that may be mid-send. So :func:`busy_reasons` names anything in flight,
and the endpoint refuses (``409``) while there is any, or, when asked to restart when
idle, :class:`RestartGate` waits for it:

1. every :data:`POLL_S`, it asks :func:`busy_reasons` until nothing is running;
2. then it quiesces: the scheduler pauses, and the campaign engine stops and waits
   for a tick in flight, so no new scheduled work can start;
3. it asks again, waiting on anything started in between (a run a person started by
   hand, which then keeps scheduled runs and sends paused until it ends), and only
   then triggers the restart.

:meth:`RestartGate.cancel` (**Cancel** on the banner) stops the wait and resumes the
scheduler and the engine if it had paused them.

This module never imports :mod:`netkeeper.services.restart`, the one module that
execs: ``serve`` puts its restarter on ``app.state`` as a :class:`RestartHook`, and
``netkeeper/web`` reaches it only through this protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Final, Literal, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.linkedin import activity_lock
from netkeeper.models import SyncRun, SyncRunStatus, User, UserKind
from netkeeper.models.base import utcnow
from netkeeper.scoping import scoped
from netkeeper.services import runs
from netkeeper.services.linkedin_accounts import local_account_id
from netkeeper.services.tasks import TaskRunner

log = logging.getLogger(__name__)

POLL_S: Final = 2.0
"""How often a restart waiting for idle looks again."""


@runtime_checkable
class RestartHook(Protocol):
    """What ``serve`` puts on ``app.state.restarter``."""

    @property
    def mode(self) -> Literal["launchd", "exec"]: ...

    @property
    def requested(self) -> bool: ...

    def trigger(self) -> None: ...


def stored_busy(factory: sessionmaker[Session]) -> list[str]:
    """What the database and the lock files say is running: a run row still
    ``running`` for a local user, or the local account's browser lock held. A row a
    crashed process left ``running`` (``runs.is_stale``) does not count, so it can
    never hold a restart off for good."""
    reasons: list[str] = []
    now = utcnow()
    with session_scope(factory) as session:
        users = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).all()
        for user in users:
            running = session.scalars(
                scoped(user, SyncRun).where(SyncRun.status == SyncRunStatus.RUNNING)
            ).all()
            for run in running:
                if runs.is_stale(session, run, now=now):
                    continue  # left by a process that went away: nothing to cut off
                reasons.append(
                    f"a LinkedIn {run.kind.value.replace('_', ' ')} run (#{run.id}) is going;"
                    " cancel it or wait for it to finish"
                )
        account = local_account_id(session)
    key = (
        activity_lock.SINGLE_ACCOUNT_KEY if account is None else activity_lock.account_key(account)
    )
    state = activity_lock.inspect(key)
    if state.held:
        who = state.holder.describe() if state.holder is not None else "another process"
        reasons.append(f"the browser is in use by {who}")
    return reasons


class Busy(Protocol):
    @property
    def busy(self) -> bool: ...


def live_busy(
    tasks: TaskRunner | None, engine: Busy | None, backups: Busy | None = None
) -> list[str]:
    """What this process has in flight: a background task, a campaign tick, or the
    nightly backup (an exec would kill its ``VACUUM INTO`` part way)."""
    reasons = [
        f"a background task ({info.name}) is running"
        for info in ([] if tasks is None else tasks.active())
    ]
    if engine is not None and engine.busy:
        reasons.append("a campaign send is in progress")
    if backups is not None and backups.busy:
        reasons.append("the nightly backup is being written")
    return reasons


class RestartGate:
    """A restart that waits for idle, at most one per process (see the module)."""

    def __init__(
        self,
        hook: RestartHook,
        *,
        busy: Callable[[], Awaitable[list[str]]],
        quiesce: Callable[[], Awaitable[None]],
        resume: Callable[[], None],
        poll_s: float = POLL_S,
    ) -> None:
        self._hook = hook
        self._busy = busy
        self._quiesce = quiesce
        self._resume = resume
        self._quiesced = False
        self._poll_s = poll_s
        self._task: asyncio.Task[None] | None = None
        self.waiting_on: list[str] = []

    @property
    def waiting(self) -> bool:
        """A restart was asked for and has not stopped serve yet."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Begin waiting for idle, then restart. Idempotent while it waits."""
        if not self.waiting and not self._hook.requested:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="restart-gate")

    async def cancel(self) -> bool:
        """Stop waiting, and undo the quiesce if it happened: scheduled runs fire and
        campaign sends go out again. False when there was nothing to cancel (no wait,
        or the restart already began)."""
        task = self._task
        if task is None or task.done() or self._hook.requested:
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._task = None
        self.waiting_on = []
        if self._quiesced:
            self._resume()
            self._quiesced = False
        log.info("the restart waiting for idle was cancelled")
        return True

    async def _run(self) -> None:
        while True:
            reasons = await self._busy()
            self.waiting_on = reasons
            if reasons:
                log.info("restart waits: %s", "; ".join(reasons))
                await asyncio.sleep(self._poll_s)
                continue
            if self._quiesced:
                break
            await self._quiesce()
            self._quiesced = True  # then look once more: something may have started
        self._hook.trigger()
