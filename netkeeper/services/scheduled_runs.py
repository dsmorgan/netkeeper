"""The scheduler's job handlers for ``netkeeper serve``: a due fire becomes a recorded run (P2-10).

:mod:`netkeeper.services.scheduler` decides *when* a kind fires and hands the
fire to a handler; this module is the handler ``netkeeper serve`` registers
for each kind with a runner (``SERVED_SCHEDULES``: the two connections syncs
and enrichment). A handler:

1. records nothing while the account's schedule is paused (#324; the
   scheduler's gate already skipped the fire, so this only closes the moment
   between that gate and the handler, and answers
   :attr:`~netkeeper.services.scheduler.JobOutcome.PAUSED_AFTER_GATE` so the fire counts as
   skipped, not run), and otherwise records a ``scheduled``
   run through :func:`netkeeper.services.runs.create_run`,
   which refuses on a disarmed account -- the scheduler's arm gate already
   skipped the fire, so reaching this refusal means something bypassed that
   gate, and it is logged as an error, nothing runs, and the answer is ``DISARMED_AFTER_GATE``;
2. submits the run to the task runner, so it is a task like any other (its
   progress on the event stream, cancelled with the process), and waits for it,
   because the scheduler's heartbeat runs one fire at a time on purpose;
3. answers :attr:`~netkeeper.services.scheduler.JobOutcome.RETRY_LATER` when
   the worker could not reach the browser, and the scheduler parks a retry 20
   to 50 minutes out (spec 9.9);
4. for the inbox poll only (P4-08), answers
   :attr:`~netkeeper.services.scheduler.JobOutcome.NOTHING_TO_WATCH` before anything
   else when no live enrollment has a contact with a LinkedIn URN: it records no
   run and attaches to nothing, and the fire counts as skipped;
5. answers :attr:`~netkeeper.services.scheduler.JobOutcome.NOT_DONE` when a full
   sync ran but lost some of the page's answers (#200,
   :func:`netkeeper.services.runs.lost_answers`): it is incomplete and aged
   nobody, so the week's full sync is not done, and the scheduler offers it again
   a day later instead of a week later.

The auto-send kind (ADR 0008, scheduled only while ``[campaigns] linkedin_auto_send``
is on) has a handler of its own, :func:`_auto_send_handler`. It checks the flag again,
waits out the spacing since the last auto-send, and claims one due ``auto_send`` step
(:func:`netkeeper.services.linkedin_steps.claim_auto_send`), which records its scheduled
``message_send`` run; then it runs that run like any other. When it claims nothing it
answers :attr:`~netkeeper.services.scheduler.JobOutcome.NOTHING_TO_SEND`.

Nothing here imports the browser: the worker arrives as a
:class:`~netkeeper.services.runs.RunExecutor` (``tests/test_browser_safety.py``).
"""

from __future__ import annotations

import dataclasses
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Final

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import LinkedInSettings, Settings
from netkeeper.crm.inbox_apply import has_anything_to_watch
from netkeeper.db import off_loop, session_scope
from netkeeper.models import SyncRun, SyncRunKind, SyncRunTrigger, User, UserKind
from netkeeper.models.base import utcnow
from netkeeper.scoping import scoped
from netkeeper.services import runs
from netkeeper.services.events import EventBus
from netkeeper.services.linkedin_accounts import (
    find_account,
    schedule_paused,
    scheduled_runs_armed,
)
from netkeeper.services.linkedin_steps import (
    auto_send_budget_warning,
    auto_send_hold,
    claim_auto_send,
)
from netkeeper.services.scheduler import (
    AUTO_SEND_SPACING_MAX,
    AUTO_SEND_SPACING_MIN,
    SERVED_SCHEDULES,
    JobContext,
    JobHandler,
    JobKind,
    JobOutcome,
    JobRegistry,
    build_scheduler,
    seed_missing_kinds,
    served_schedules,
    stored_due,
)
from netkeeper.services.tasks import TaskRunner

log = logging.getLogger(__name__)

#: The run kind each scheduled job kind records.
RUN_KIND: Final[dict[JobKind, SyncRunKind]] = {
    JobKind.CONNECTIONS_FULL: SyncRunKind.CONNECTIONS_FULL,
    JobKind.CONNECTIONS_INCREMENTAL: SyncRunKind.CONNECTIONS_INCREMENTAL,
    JobKind.ENRICH: SyncRunKind.ENRICH,
}

#: The inbox poll's run kind. Its handler is registered with the others (P4-01, #380).
INBOX_RUN_KIND: Final = {JobKind.INBOX: SyncRunKind.INBOX}

#: The task name a run is submitted under.
RUN_TASK_NAME: Final = "linkedin.run"


def serve_registry(
    factory: sessionmaker[Session],
    executor: runs.RunExecutor,
    tasks: TaskRunner,
    *,
    clock: Callable[[], datetime],
    settings: Settings | None = None,
) -> JobRegistry:
    """One handler per kind in :data:`RUN_KIND` and :data:`INBOX_RUN_KIND`, each recording
    and running a scheduled run. The inbox handler first checks there is anyone to watch.
    The auto-send kind's handler is registered only when ``settings`` turn
    ``[campaigns] linkedin_auto_send`` on (ADR 0008)."""
    registry: dict[JobKind, JobHandler] = {
        kind: _handler(factory, executor, tasks, run_kind, clock=clock)
        for kind, run_kind in RUN_KIND.items()
    }
    for kind, run_kind in INBOX_RUN_KIND.items():
        registry[kind] = _handler(factory, executor, tasks, run_kind, clock=clock, watch_check=True)
    if settings is not None and settings.campaigns.linkedin_auto_send:
        # ADR 0008: with the flag off there is no auto-send handler at all.
        registry[JobKind.AUTO_SEND] = _auto_send_handler(
            factory, executor, tasks, settings, clock=clock
        )
    return registry


def submit_run(
    tasks: TaskRunner, executor: runs.RunExecutor, run_id: int, user_id: int
) -> tuple[str, Callable[[], runs.RunOutcome]]:
    """Submit run ``run_id`` to the task runner. Returns the task id and a reader
    for the outcome once the task has finished (``DONE`` until then)."""
    outcome: list[runs.RunOutcome] = []

    async def work() -> None:
        outcome.append(await executor.execute(run_id, user_id))

    info = tasks.submit(RUN_TASK_NAME, work, user_id=user_id)
    return info.id, lambda: outcome[0] if outcome else runs.RunOutcome.DONE


class _SchedulePaused(Exception):
    """The schedule was paused between the scheduler's gate and the handler (#324)."""


def _handler(
    factory: sessionmaker[Session],
    executor: runs.RunExecutor,
    tasks: TaskRunner,
    run_kind: SyncRunKind,
    *,
    clock: Callable[[], datetime],
    watch_check: bool = False,
) -> JobHandler:
    def nothing_to_watch(ctx: JobContext) -> bool:
        with session_scope(factory) as session:
            user = session.get(User, ctx.user_id)
            return user is None or not has_anything_to_watch(session, user)

    def first_poll_is_unsupervised(ctx: JobContext) -> bool:
        with session_scope(factory) as session:
            user = session.get(User, ctx.user_id)
            return user is not None and not runs.has_completed_inbox_poll(session, user)

    def record(ctx: JobContext) -> int | None:
        with session_scope(factory, write=True) as session:
            user = session.get(User, ctx.user_id)
            if user is None:
                log.error("scheduled %s: no user %d", run_kind.value, ctx.user_id)
                return None
            if schedule_paused(session, user, ctx.account_id):
                # Paused between the scheduler's gate and here (#324): nothing new starts.
                raise _SchedulePaused
            return runs.create_run(
                session, user, run_kind, trigger=SyncRunTrigger.SCHEDULED, now=clock()
            ).id

    async def handle(ctx: JobContext) -> JobOutcome | None:
        if watch_check and await off_loop(nothing_to_watch, ctx):
            log.info(
                "scheduled %s not started: no live enrollment has a LinkedIn contact",
                run_kind.value,
            )
            return JobOutcome.NOTHING_TO_WATCH
        if watch_check and await off_loop(first_poll_is_unsupervised, ctx):
            log.info(
                "scheduled %s not started: no inbox poll has completed, so the first is"
                " run by hand (`netkeeper linkedin inbox`)",
                run_kind.value,
            )
            return JobOutcome.FIRST_POLL_BY_HAND
        try:
            # Off the event loop, in one writer session as before (#259).
            run_id = await off_loop(record, ctx)
            if run_id is None:
                return None
        except _SchedulePaused:
            log.info("scheduled %s not started: the schedule is paused", run_kind.value)
            return JobOutcome.PAUSED_AFTER_GATE
        except runs.ScheduledRunsDisarmed:
            log.error(
                "scheduled %s reached a disarmed account %d past the scheduler's arm gate;"
                " nothing ran",
                run_kind.value,
                ctx.account_id,
            )
            return JobOutcome.DISARMED_AFTER_GATE
        except runs.RunAlreadyRunning as exc:
            log.info("scheduled %s not started: %s", run_kind.value, exc)
            return JobOutcome.RETRY_LATER
        task_id, result = submit_run(tasks, executor, run_id, ctx.user_id)
        await tasks.wait(task_id)
        if result() is runs.RunOutcome.RETRY_LATER:
            return JobOutcome.RETRY_LATER
        lost = (
            await off_loop(_lost_answers, factory, ctx.user_id, run_id)
            if run_kind is SyncRunKind.CONNECTIONS_FULL
            else 0
        )
        if lost:
            log.info("scheduled full sync run %d lost answers; it is not done", run_id)
            return JobOutcome.NOT_DONE
        return None

    return handle


def auto_send_spacing_left(session: Session, user: User, now: datetime) -> timedelta:
    """How long until the next auto-send may start (ADR 0008): the spacing after the
    newest ``message_send`` run, an auto-send or a prefill a person started, drawn once
    per run between
    :data:`~netkeeper.services.scheduler.AUTO_SEND_SPACING_MIN` and
    :data:`~netkeeper.services.scheduler.AUTO_SEND_SPACING_MAX` minutes, less the time
    since it started. Zero when there is none, or the spacing has passed. Read-only."""
    last = session.scalars(
        scoped(user, SyncRun)
        .where(SyncRun.kind == SyncRunKind.MESSAGE_SEND)
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(1)
    ).first()
    if last is None:
        return timedelta(0)
    # Seeded by the run's id, so one run's spacing is the same at every heartbeat.
    minutes = random.Random(last.id).uniform(AUTO_SEND_SPACING_MIN, AUTO_SEND_SPACING_MAX)  # noqa: S311 -- pacing
    return max(last.started_at + timedelta(minutes=minutes) - now, timedelta(0))


def _auto_send_handler(
    factory: sessionmaker[Session],
    executor: runs.RunExecutor,
    tasks: TaskRunner,
    settings: Settings,
    *,
    clock: Callable[[], datetime],
) -> JobHandler:
    """The auto-send fire (ADR 0008): claim one due ``auto_send`` step and run it."""

    def claim(ctx: JobContext) -> int | str:
        """The claimed run's id, or why nothing was claimed, in fixed words."""
        if not settings.campaigns.linkedin_auto_send:
            return "auto-send is off"
        with session_scope(factory, write=True) as session:
            user = session.get(User, ctx.user_id)
            if user is None:
                return "no such user"
            if schedule_paused(session, user, ctx.account_id):
                raise _SchedulePaused
            if not scheduled_runs_armed(session, user, ctx.account_id):
                raise runs.ScheduledRunsDisarmed("scheduled runs are disarmed")
            if auto_send_hold(session, user, ctx.account_id) is not None:
                return "auto-send is held until the open message bubbles are closed"
            now = clock()
            if auto_send_spacing_left(session, user, now) > timedelta(0):
                return "the spacing since the last auto-send has not passed"
            claimed = claim_auto_send(session, user, now=now, settings=settings)
            if claimed is None:
                return "no auto-send step is ready"
            if not claimed.claimed or claimed.run_id is None:
                return ", ".join(claimed.reasons)
            return claimed.run_id

    async def handle(ctx: JobContext) -> JobOutcome | None:
        try:
            claimed = await off_loop(claim, ctx)
        except _SchedulePaused:
            log.info("scheduled auto-send not started: the schedule is paused")
            return JobOutcome.PAUSED_AFTER_GATE
        except runs.ScheduledRunsDisarmed:
            log.error("scheduled auto-send reached a disarmed account past the arm gate")
            return JobOutcome.DISARMED_AFTER_GATE
        if isinstance(claimed, str):
            log.info("scheduled auto-send claimed nothing: %s", claimed)
            return JobOutcome.NOTHING_TO_SEND
        task_id, result = submit_run(tasks, executor, claimed, ctx.user_id)
        await tasks.wait(task_id)
        if result() is runs.RunOutcome.RETRY_LATER:
            return JobOutcome.RETRY_LATER
        return None

    return handle


def _lost_answers(factory: sessionmaker[Session], user_id: int, run_id: int) -> int:
    """How many answers run ``run_id`` lost (#200); 0 for a run or user that is gone."""
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        if user is None:
            return 0
        try:
            return runs.lost_answers(runs.get_run(session, user, run_id))
        except runs.RunNotFound:
            return 0


# --- what `netkeeper serve` hands the app --------------------------------------


ExecutorFactory = Callable[[sessionmaker[Session], EventBus], runs.RunExecutor]


@dataclass(frozen=True, slots=True)
class ServeExtractor:
    """The extractor half of ``netkeeper serve``: how to build the run executor
    once the app has its database and event bus, and the clock the scheduler reads.

    ``netkeeper.worker.serve_extractor`` builds the real one (the attach
    provider); a test passes one built on a fake browser and a fake clock and
    drives the same lifespan ``serve`` does. An app built without one
    (``create_app()`` in most tests) starts no scheduler and cannot start runs.
    """

    executor: ExecutorFactory
    clock: Callable[[], datetime] = utcnow
    rng: random.Random | None = None


@dataclass(slots=True)
class ServeScheduler:
    """What the app keeps while it runs: the executor, and the started scheduler."""

    executor: runs.RunExecutor
    scheduler: AsyncIOScheduler

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)


def start_serve_scheduler(
    extractor: ServeExtractor,
    factory: sessionmaker[Session],
    bus: EventBus,
    tasks: TaskRunner,
    settings: LinkedInSettings,
    *,
    campaign_settings: Settings | None = None,
) -> ServeScheduler:
    """Build the executor and the scheduler for every local user's account, and start it.

    Establishing the schedule writes due times and fires nothing; a fire needs
    a heartbeat, and a fire on a disarmed account is skipped by the arm gate
    (``build_scheduler``'s default, never turned off here). The scheduler runs
    :data:`~netkeeper.services.scheduler.SERVED_SCHEDULES` only (the inbox poll
    included since P4-01) with the configured heat gate and active hours, and auto-send
    (ADR 0008) only while ``campaign_settings`` turn ``[campaigns] linkedin_auto_send`` on.
    """
    executor = extractor.executor(factory, bus)
    _quiet_the_heartbeat()
    start, end = (time.fromisoformat(value) for value in settings.active_hours)
    whole = campaign_settings if campaign_settings is not None else Settings()
    whole = dataclasses.replace(whole, linkedin=settings)
    schedules = served_schedules(whole.campaigns.linkedin_auto_send)
    built = build_scheduler(
        factory,
        _local_accounts(factory),
        registry=serve_registry(factory, executor, tasks, clock=extractor.clock, settings=whole),
        schedules=schedules,
        active_start=start,
        active_end=end,
        heat_settings=settings.heat,
        rng=extractor.rng,
        clock=extractor.clock,
    )
    built.start()
    log.info("scheduler started: %s", ", ".join(kind.value for kind in schedules))
    if JobKind.AUTO_SEND in schedules:
        log.warning(
            "LinkedIn auto-send is on: netkeeper clicks Send itself for auto_send steps (ADR 0008)"
        )
        warning = auto_send_budget_warning(whole)
        if warning is not None:
            log.warning("%s", warning)
    return ServeScheduler(executor=executor, scheduler=built)


def seed_served_schedule(
    session: Session,
    user: User,
    settings: LinkedInSettings,
    *,
    now: datetime,
    rng: random.Random | None = None,
) -> list[JobKind]:
    """Seed a due time for each served kind that has none, as arming does (#327).

    ``serve`` startup already gives a missing kind its first due time
    (:func:`~netkeeper.services.scheduler.build_scheduler`); arming does it too, so a
    kind added after the schedule was established never waits for a restart. A kind
    that has a due time keeps it. Needs a writer session. Returns the kinds seeded.

    Only an established schedule is filled in. When no served kind has a due time yet
    (``serve`` has never run), this seeds nothing and leaves the whole schedule to
    ``serve``'s first start: seeded here, every due time could have lapsed by then,
    and that first start would read them as downtime and catch them all up at once.
    """
    account = find_account(session, user)
    if account is None:
        return []
    if all(stored_due(session, user, account.id, kind) is None for kind in SERVED_SCHEDULES):
        return []
    start, end = (time.fromisoformat(value) for value in settings.active_hours)
    seeded = seed_missing_kinds(
        session,
        user,
        account.id,
        now=now,
        schedules=SERVED_SCHEDULES,
        rng=rng if rng is not None else random.Random(),  # noqa: S311 -- jitter, not crypto
        tz=user.timezone,
        active_start=start,
        active_end=end,
    )
    for kind, result in seeded.items():
        log.info("scheduler: seeded %s for account %d, due %s", kind.value, account.id, result.due)
    return list(seeded)


def _local_accounts(factory: sessionmaker[Session]) -> Callable[[], list[tuple[User, int]]]:
    """Every local user with their account id, read fresh at each call."""

    def accounts() -> list[tuple[User, int]]:
        with session_scope(factory) as session:
            users = session.scalars(
                select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)
            ).all()
            found: list[tuple[User, int]] = []
            for user in users:
                account = find_account(session, user)
                if account is not None:
                    found.append((user, account.id))
            return found

    return accounts


class _BusyHeartbeat(logging.Filter):
    """Drops APScheduler's "skipped: maximum number of running instances" warning.

    The heartbeat awaits a run to its end on purpose (one fire at a time, see
    ``services.scheduler``), so during an hour of enrichment every minute's tick
    finds it still running. That is the design working, not a warning.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "maximum number of running instances reached" not in record.getMessage()


def _quiet_the_heartbeat() -> None:
    """Once per process: the filter above, and APScheduler's per-tick INFO lines
    ("Running job", "executed successfully") down to WARNING."""
    scheduler_log = logging.getLogger("apscheduler.scheduler")
    if not any(isinstance(item, _BusyHeartbeat) for item in scheduler_log.filters):
        scheduler_log.addFilter(_BusyHeartbeat())
    executors_log = logging.getLogger("apscheduler.executors")
    if executors_log.level < logging.WARNING:
        executors_log.setLevel(logging.WARNING)
