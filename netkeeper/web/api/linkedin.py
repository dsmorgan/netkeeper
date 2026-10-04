"""``/linkedin``: runs, their progress and cancel, budget, heat, pins, and scheduled-run arming.

Spec 14.1's ``linkedin`` resource (P2-10), what the LinkedIn page (P2-12) is
built on:

* ``GET /linkedin/runs`` and ``GET /linkedin/runs/{id}``: every run, newest
  first, with its progress, counts, and how it ended. Live progress arrives on
  ``GET /events`` as ``run.started``, ``run.progress``, and ``run.finished``.
* ``POST /linkedin/runs``: start a run by hand. It records the run, submits it
  to the task runner, and answers ``202`` with the run and task ids at once:
  browser work never runs inside a request (spec 9.9, CLAUDE.md). A manual run
  is allowed while scheduled runs are disarmed -- that is how the first
  supervised run happens -- and ``max_visits`` caps an enrichment below
  today's budget, never above it.
* ``POST /linkedin/runs/{id}/cancel``: the cooperative cancel (spec 9.9). The
  run stops at its next check, ``aborted``, keeping what it completed.
* ``POST /linkedin/runs/{id}/pause``: the same cooperative stop for an
  enrichment, recorded ``paused`` and resumable (#324).
* ``POST /linkedin/runs/{id}/resume``: an aborted enrichment's remaining plan,
  as a new run, never re-planned.
* ``GET /linkedin/runs/{id}/contacts``: the last few contacts the run touched.
* ``GET /linkedin/budget``, ``GET /linkedin/heat``: the counters and the heat
  level, as ``netkeeper posture`` reads them.
* ``POST /linkedin/heat/clear`` (with ``confirm: true`` and the ``last_raised_at``
  the person saw): spec 9.7's manual clear (#181).
* ``GET``/``POST /linkedin/pins``, ``DELETE /linkedin/pins/{contact_id}``: up
  to five contacts at the front of the next enrichment (spec 9.6).
* ``GET /linkedin/schedule``, ``POST /linkedin/schedule/arm`` (with
  ``confirm: true``), ``POST /linkedin/schedule/disarm``: whether scheduled
  runs may fire. Every install starts disarmed. ``POST /linkedin/schedule/pause``
  and ``/unpause`` hold new scheduled runs without disarming (#324).
* ``GET /linkedin/status``: the page's banner in one read.
* ``POST /linkedin/session-flag/clear`` (with ``confirm: true`` and the flag the
  person saw): ``netkeeper linkedin clear-flag``, with its refusals (#181).
* ``GET /linkedin/browser``: ``netkeeper browser launch``'s instructions, as data.
  Read-only and built from config alone; it never attaches (spec 9.9, CLAUDE.md).
* ``GET /linkedin/browser/health``: what the last preflight or run recorded about
  Chrome and the session. Read-only; it never attaches either (#181).

Every ``GET`` here only reads: none starts, resumes, arms, or clears anything. Every
``POST`` and ``DELETE`` goes through the CSRF guard (``X-Netkeeper-Client: 1``
and a same-origin check, spec 14.2).
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, HTTPException, Query, Request

from netkeeper.config import LinkedInSettings
from netkeeper.models import Contact, SyncRun, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.models.base import utcnow
from netkeeper.paths import data_dir
from netkeeper.scoping import get_scoped_contact, scoped_contacts
from netkeeper.services import budgets, enrich_plan, run_contacts, runs
from netkeeper.services import heat as heat_rows
from netkeeper.services import posture as posture_service
from netkeeper.services.browser_launch import (
    CHROME_PROFILE_DIRNAME,
    cdp_port,
    chrome_launch_command,
    remote_host_note,
)
from netkeeper.services.heat import cooldown_multiplier
from netkeeper.services.linkedin_accounts import (
    account_id_for,
    arm_scheduled_runs,
    disarm_scheduled_runs,
    ensure_account,
    find_account,
    pause_schedule,
    schedule_pause_state,
    schedule_paused,
    scheduled_runs_armed,
    unpause_schedule,
)
from netkeeper.services.linkedin_session import (
    FlagClearRefused,
    clear_confirmed_flag,
    last_session_evidence,
    session_flag,
)
from netkeeper.services.scheduled_runs import seed_served_schedule, submit_run
from netkeeper.services.scheduler import SERVED_SCHEDULES, stored_due
from netkeeper.services.visit_budget import todays_visits
from netkeeper.web.deps import CurrentUser, SessionDep, Tasks
from netkeeper.web.schemas import (
    BrowserHealthOut,
    BrowserLaunchOut,
    BudgetOut,
    BudgetStatusOut,
    HeatClearIn,
    HeatOut,
    LinkedInStatusOut,
    PeriodBudgetOut,
    PinIn,
    PinOut,
    RunAccepted,
    RunContactOut,
    RunContactsOut,
    RunOut,
    RunPage,
    RunResumeIn,
    RunStartIn,
    ScheduleArmIn,
    ScheduledJobOut,
    ScheduleOut,
    SessionFlagClearIn,
    TodaysVisitsOut,
)

router = APIRouter(prefix="/linkedin", tags=["linkedin"])

MAX_PAGE: Final = 200

_NO_WORKER = (
    "this netkeeper process has no browser worker (it was not started by `netkeeper serve`),"
    " so it cannot start runs"
)


def _run_out(session: SessionDep, user: User, run: SyncRun) -> RunOut:
    derived = runs.view(run)
    return RunOut(
        id=run.id,
        kind=run.kind,
        status=run.status,
        trigger=run.trigger,
        started_at=run.started_at,
        completed_at=run.completed_at,
        stop_reason=run.stop_reason,
        stop_reason_text=derived.stop_reason_text,
        cancel_requested_at=run.cancel_requested_at,
        max_visits=run.max_visits,
        resume_of_id=run.resume_of_id,
        browser_mode=run.browser_mode,
        progress=run.progress_json,
        counts=run.counts_json,
        notes=run.notes,
        error=run.error,
        planned=derived.planned,
        completed=derived.completed,
        aging_refused=derived.aging_refused,
        resumed_by=derived.resumed_by,
        pause_requested=run.status is SyncRunStatus.RUNNING
        and runs.pause_requested(session, user, run.id),
    )


def _settings(request: Request) -> LinkedInSettings:
    settings: LinkedInSettings = request.app.state.settings.linkedin
    return settings


def _executor(request: Request) -> runs.RunExecutor:
    executor: runs.RunExecutor | None = request.app.state.executor
    if executor is None:
        raise HTTPException(status_code=503, detail=_NO_WORKER)
    return executor


# --- runs ------------------------------------------------------------------------


@router.get("/runs", operation_id="list_linkedin_runs")
def list_runs(
    user: CurrentUser,
    session: SessionDep,
    kind: SyncRunKind | None = None,
    status: SyncRunStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunPage:
    rows, total = runs.list_runs(
        session, user, kind=kind, status=status, limit=limit, offset=offset
    )
    return RunPage(items=[_run_out(session, user, run) for run in rows], total=total)


@router.get(
    "/runs/{run_id}", operation_id="get_linkedin_run", responses={404: {"description": "No run"}}
)
def get_run(run_id: int, user: CurrentUser, session: SessionDep) -> RunOut:
    try:
        return _run_out(session, user, runs.get_run(session, user, run_id))
    except runs.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/runs",
    operation_id="start_linkedin_run",
    status_code=202,
    responses={
        409: {
            "description": "A run is running, the session is flagged, heat is too high, or"
            " it is outside active hours"
        },
        422: {"description": "A kind with no runner, or max_visits on a sync"},
        503: {"description": "This process has no browser worker"},
    },
)
async def start_run(
    body: RunStartIn, request: Request, user: CurrentUser, session: SessionDep, tasks: Tasks
) -> RunAccepted:
    """Record a manual run and submit it; answers at once, before any browser work.

    Outside ``[linkedin] active_hours`` it answers ``409`` with the window, when it
    next opens, and where to change it, and records no run (#213).
    """
    executor = _executor(request)
    try:
        runs.refuse_if_outside_active_hours(_settings(request), now=utcnow())
        # After the hours check, so a refused request writes no row at all (#294).
        account = ensure_account(session, user)
        runs.refuse_if_flagged_or_hot(
            session, user, account.id, now=utcnow(), settings=_settings(request)
        )
        run = runs.create_run(
            session,
            user,
            body.kind,
            trigger=SyncRunTrigger.MANUAL,
            now=utcnow(),
            max_visits=body.max_visits,
        )
    except (
        runs.RunAlreadyRunning,
        runs.HeatSkipped,
        runs.SessionFlagged,
        runs.OutsideActiveHours,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except runs.RunError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _submit(session, tasks, executor, run, user)


@router.post(
    "/runs/{run_id}/cancel",
    operation_id="cancel_linkedin_run",
    responses={404: {"description": "No run"}, 409: {"description": "The run already ended"}},
)
def cancel_run(run_id: int, user: CurrentUser, session: SessionDep) -> RunOut:
    """Ask a running run to stop at its next check (spec 9.9)."""
    try:
        return _run_out(session, user, runs.request_cancel(session, user, run_id, now=utcnow()))
    except runs.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except runs.RunFinished as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/runs/{run_id}/pause",
    operation_id="pause_linkedin_run",
    responses={
        404: {"description": "No run"},
        409: {
            "description": "The run already ended, is being cancelled, or is a sync"
            " (only an enrichment keeps a plan to resume)"
        },
    },
)
def pause_run(run_id: int, user: CurrentUser, session: SessionDep) -> RunOut:
    """Ask a running enrichment to stop at its next check and keep its place (#324).

    It ends ``aborted``, ``paused``, with its plan; ``POST /runs/{id}/resume``
    continues the rest. Nothing here touches the browser: the run reads the flag.
    """
    try:
        return _run_out(session, user, runs.request_pause(session, user, run_id, now=utcnow()))
    except runs.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except runs.RunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/runs/{run_id}/contacts",
    operation_id="list_linkedin_run_contacts",
    responses={404: {"description": "No run"}},
)
def list_run_contacts(run_id: int, user: CurrentUser, session: SessionDep) -> RunContactsOut:
    """The last few contacts the run touched, newest first, and what happened (#324)."""
    try:
        run = runs.get_run(session, user, run_id)
    except runs.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RunContactsOut(
        items=[
            RunContactOut(
                contact_id=item.contact_id,
                first_name=item.first_name,
                last_name=item.last_name,
                outcome=item.outcome,
                outcome_text=item.outcome_text,
            )
            for item in run_contacts.recent(session, user, run)
        ]
    )


@router.post(
    "/runs/{run_id}/resume",
    operation_id="resume_linkedin_run",
    status_code=202,
    responses={
        404: {"description": "No enrichment run with a plan"},
        409: {"description": "Nothing to resume, or the run cannot start now"},
        503: {"description": "This process has no browser worker"},
    },
)
async def resume_run(
    run_id: int,
    body: RunResumeIn,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    tasks: Tasks,
) -> RunAccepted:
    """Record a resume of an enrichment run's remaining plan and submit it."""
    executor = _executor(request)
    account = ensure_account(session, user)
    try:
        enrich_plan.load_plan(session, user, run_id)  # 404 before any 409: is there a plan?
        runs.refuse_if_outside_active_hours(_settings(request), now=utcnow())
        runs.refuse_if_flagged_or_hot(
            session, user, account.id, now=utcnow(), settings=_settings(request)
        )
        run = enrich_plan.start_resume(
            session, user, run_id, now=utcnow(), max_visits=body.max_visits
        )
    except enrich_plan.PlanNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        enrich_plan.PlanFinished,
        runs.RunError,
        runs.HeatSkipped,
        runs.SessionFlagged,
        runs.OutsideActiveHours,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _submit(session, tasks, executor, run, user)


def _submit(
    session: SessionDep, tasks: Tasks, executor: runs.RunExecutor, run: SyncRun, user: User
) -> RunAccepted:
    # Committed before the task is submitted, so the worker's first read finds
    # the run: the request's own commit only happens after this returns.
    run_id = run.id
    session.commit()
    task_id, _ = submit_run(tasks, executor, run_id, user.id)
    return RunAccepted(run_id=run_id, task_id=task_id)


# --- budget and heat -------------------------------------------------------------


@router.get("/budget", operation_id="get_linkedin_budget")
def get_budget(request: Request, user: CurrentUser, session: SessionDep) -> BudgetStatusOut:
    """Every action class's counters against its limits, and today's profile-visit chain."""
    settings = _settings(request)
    account_id = account_id_for(session, user)
    now = utcnow()
    snapshots = [
        budgets.status(session, user, account_id, action, now=now, settings=settings.budget)
        for action in budgets.ActionClass
    ]
    multiplier = cooldown_multiplier(session, user, account_id, now=now, settings=settings.heat)
    visits = todays_visits(
        session, user, account_id, now=now, settings=settings, multiplier=multiplier
    )
    return BudgetStatusOut(
        budgets=[
            BudgetOut(
                action=snapshot.action.value,
                day=PeriodBudgetOut(
                    count=snapshot.day.count,
                    limit=snapshot.day.limit,
                    remaining=snapshot.day.remaining,
                ),
                week=None
                if snapshot.week is None
                else PeriodBudgetOut(
                    count=snapshot.week.count,
                    limit=snapshot.week.limit,
                    remaining=snapshot.week.remaining,
                ),
            )
            for snapshot in snapshots
        ],
        profile_visits_today=TodaysVisitsOut(
            ramp=visits.ramp,
            after_weekend=visits.after_weekend,
            after_heat=visits.after_heat,
            spent_today=visits.spent_today,
            week_left=visits.week_left,
            remaining=visits.remaining,
        ),
        risk_warning=budgets.profile_visit_risk_warning(settings.budget),
        profile_view_notice=budgets.PROFILE_VIEW_NOTICE,
    )


@router.get("/heat", operation_id="get_linkedin_heat")
def get_heat(request: Request, user: CurrentUser, session: SessionDep) -> HeatOut:
    return _heat_out(request, session, user)


@router.post(
    "/heat/clear",
    operation_id="clear_linkedin_heat",
    responses={
        409: {"description": "Nothing to clear, or heat was raised again since the confirm"},
        422: {"description": "confirm was not true"},
    },
)
def clear_heat(
    body: HeatClearIn, request: Request, user: CurrentUser, session: SessionDep
) -> HeatOut:
    """Clear heat by hand: "the block was something else" (spec 9.7, #181).

    Needs ``confirm: true`` and the ``last_raised_at`` the person was shown. It
    clears only that heat: a throttle that raised it again since is refused with
    ``409``, as is heat that was never raised or is already cleared.
    """
    if not body.confirm:
        raise HTTPException(
            status_code=422,
            detail="clearing heat lets runs go at full pace again; send confirm: true",
        )
    try:
        heat_rows.clear_confirmed(
            session,
            user,
            account_id_for(session, user),
            last_raised_at=body.last_raised_at,
            now=utcnow(),
        )
    except heat_rows.HeatClearRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _heat_out(request, session, user)


def _heat_out(request: Request, session: SessionDep, user: User) -> HeatOut:
    heat = posture_service.heat_status(
        session, user, account_id_for(session, user), now=utcnow(), settings=_settings(request)
    )
    return HeatOut(
        score=heat.score,
        threshold=heat.threshold,
        multiplier=heat.multiplier,
        tripped=heat.tripped,
        last_raised_at=heat.last_raised_at,
        cleared_at=heat.cleared_at,
        resumes_at=heat.resumes_at,
    )


# --- pins ---------------------------------------------------------------------------


def _pins_out(session: SessionDep, user: User, pinned: list[int]) -> list[PinOut]:
    if not pinned:
        return []
    rows = {
        contact.id: contact
        for contact in session.scalars(scoped_contacts(user).where(Contact.id.in_(pinned)))
    }
    return [
        PinOut(
            contact_id=contact_id,
            first_name=rows[contact_id].first_name,
            last_name=rows[contact_id].last_name,
        )
        for contact_id in pinned
        if contact_id in rows
    ]


@router.get("/pins", operation_id="list_linkedin_pins")
def list_pins(user: CurrentUser, session: SessionDep) -> list[PinOut]:
    account = find_account(session, user)
    if account is None:
        return []
    return _pins_out(session, user, enrich_plan.pinned(session, user, account.id))


@router.post(
    "/pins",
    operation_id="pin_linkedin_contact",
    responses={404: {"description": "No such contact"}, 409: {"description": "Cannot pin"}},
)
def pin_contact(body: PinIn, user: CurrentUser, session: SessionDep) -> list[PinOut]:
    """Pin a contact to the front of the next enrichment (spec 9.6, at most five)."""
    if get_scoped_contact(session, user, body.contact_id) is None:
        raise HTTPException(status_code=404, detail="no such contact")
    account = ensure_account(session, user)
    try:
        pinned = enrich_plan.pin(session, user, account.id, body.contact_id)
    except enrich_plan.PinError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _pins_out(session, user, pinned)


@router.delete("/pins/{contact_id}", operation_id="unpin_linkedin_contact")
def unpin_contact(contact_id: int, user: CurrentUser, session: SessionDep) -> list[PinOut]:
    account = ensure_account(session, user)
    return _pins_out(session, user, enrich_plan.unpin(session, user, account.id, contact_id))


# --- scheduled runs: armed or not ---------------------------------------------------


def _schedule_out(request: Request, session: SessionDep, user: User) -> ScheduleOut:
    account = find_account(session, user)
    account_id = account_id_for(session, user)
    pause = None if account is None else schedule_pause_state(session, user, account.id)
    return ScheduleOut(
        armed=account is not None and scheduled_runs_armed(session, user, account.id),
        armed_at=None if account is None else account.scheduled_runs_armed_at,
        paused=pause is not None and pause.paused,
        paused_at=None if pause is None else pause.paused_at,
        scheduler_running=request.app.state.scheduler is not None,
        jobs=[
            ScheduledJobOut(
                kind=kind.value,
                interval_hours=schedule.interval.total_seconds() / 3600,
                next_due=stored_due(session, user, account_id, kind),
            )
            for kind, schedule in SERVED_SCHEDULES.items()
        ],
    )


@router.get("/schedule", operation_id="get_linkedin_schedule")
def get_schedule(request: Request, user: CurrentUser, session: SessionDep) -> ScheduleOut:
    return _schedule_out(request, session, user)


@router.post(
    "/schedule/arm",
    operation_id="arm_linkedin_schedule",
    responses={422: {"description": "confirm was not true"}},
)
def arm_schedule(
    body: ScheduleArmIn, request: Request, user: CurrentUser, session: SessionDep
) -> ScheduleOut:
    """Let scheduled LinkedIn runs fire. Needs ``confirm: true``; a person's act only."""
    if not body.confirm:
        raise HTTPException(
            status_code=422,
            detail="arming lets netkeeper visit LinkedIn on its own schedule; send confirm: true",
        )
    now = utcnow()
    arm_scheduled_runs(session, user, now=now)
    seed_served_schedule(session, user, _settings(request), now=now)
    return _schedule_out(request, session, user)


@router.post("/schedule/disarm", operation_id="disarm_linkedin_schedule")
def disarm_schedule(request: Request, user: CurrentUser, session: SessionDep) -> ScheduleOut:
    """Stop scheduled LinkedIn runs from firing. A run already going is not cancelled."""
    disarm_scheduled_runs(session, user)
    return _schedule_out(request, session, user)


@router.post("/schedule/pause", operation_id="pause_linkedin_schedule")
def pause_linkedin_schedule(
    request: Request, user: CurrentUser, session: SessionDep
) -> ScheduleOut:
    """Hold scheduled runs without disarming (#324): no new one starts until unpaused.

    A run already going is not stopped (cancel does that). Due fires met while
    paused are skipped and their cadence moves on, so unpausing starts nothing at
    once. The pause survives a restart of ``netkeeper serve``.
    """
    pause_schedule(session, user, now=utcnow())
    return _schedule_out(request, session, user)


@router.post("/schedule/unpause", operation_id="unpause_linkedin_schedule")
def unpause_linkedin_schedule(
    request: Request, user: CurrentUser, session: SessionDep
) -> ScheduleOut:
    """Let scheduled runs start again; each kind waits for its next due time."""
    unpause_schedule(session, user)
    return _schedule_out(request, session, user)


# --- the banner ------------------------------------------------------------------------


@router.get("/status", operation_id="get_linkedin_status")
def get_status(request: Request, user: CurrentUser, session: SessionDep) -> LinkedInStatusOut:
    return _status_out(request, session, user)


@router.post(
    "/session-flag/clear",
    operation_id="clear_linkedin_session_flag",
    responses={
        409: {"description": "No flag is set, or it changed since the confirm"},
        422: {"description": "confirm was not true"},
    },
)
def clear_session_flag_route(
    body: SessionFlagClearIn, request: Request, user: CurrentUser, session: SessionDep
) -> LinkedInStatusOut:
    """Clear the session flag by hand: ``netkeeper linkedin clear-flag`` (#181).

    Needs ``confirm: true`` and the flag the person was shown (``outcome``,
    ``flagged_at``). The command's refusals apply as they are: no flag set is
    ``409``, and so is a flag that is not the one confirmed, so a flag raised
    again in between is never cleared by an answer about an older one.
    """
    if not body.confirm:
        raise HTTPException(
            status_code=422,
            detail="clearing the session flag lets runs use this session again; send confirm: true",
        )
    try:
        clear_confirmed_flag(session, user, outcome=body.outcome, flagged_at=body.flagged_at)
    except FlagClearRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _status_out(request, session, user)


def _status_out(request: Request, session: SessionDep, user: User) -> LinkedInStatusOut:
    settings = _settings(request)
    account = find_account(session, user)
    account_id = account_id_for(session, user)
    flag = session_flag(session, user)
    running = None if account is None else runs.running_run(session, user, account.id)
    heat = posture_service.heat_status(session, user, account_id, now=utcnow(), settings=settings)
    return LinkedInStatusOut(
        session_flag=None if flag is None else flag.outcome.value,
        session_flagged_at=None if flag is None else flag.flagged_at,
        heat_tripped=heat.tripped,
        armed=account is not None and scheduled_runs_armed(session, user, account.id),
        schedule_paused=account is not None and schedule_paused(session, user, account.id),
        running_run_id=None if running is None else running.id,
        can_start_runs=request.app.state.executor is not None,
    )


# --- what is known about the browser ----------------------------------------------------


@router.get("/browser/health", operation_id="get_linkedin_browser_health")
def get_browser_health(
    request: Request, user: CurrentUser, session: SessionDep
) -> BrowserHealthOut:
    """What netkeeper already knows about Chrome and the session, never a fresh probe (#181).

    The ``linkedin session`` posture row (the last ``netkeeper preflight`` or
    ``posture --probe``, or the newest run that read LinkedIn, under the session
    flag), and the newest run that could not reach Chrome when that is newer.
    Nothing here attaches to the browser (spec 9.9, CLAUDE.md); a live check is
    still ``netkeeper preflight`` in a terminal.
    """
    now = utcnow()
    account = find_account(session, user)
    account_id = account_id_for(session, user)
    row = posture_service.session_row(
        session, user, account_id, now=now, settings=_settings(request)
    )
    evidence = last_session_evidence(session, user, account_id)
    unreachable = (
        None if account is None else runs.last_browser_unavailable(session, user, account.id)
    )
    if (
        unreachable is not None
        and evidence is not None
        and unreachable.completed_at is not None
        and not unreachable.completed_at > evidence.observed_at
    ):
        unreachable = None
    running = None if account is None else runs.running_run(session, user, account.id)
    return BrowserHealthOut(
        checked_at=now,
        can_start_runs=request.app.state.executor is not None,
        session_status=row.status.value,
        session_summary=row.value,
        session_warnings=list(row.warnings),
        chrome_unreachable_at=None if unreachable is None else unreachable.completed_at,
        chrome_unreachable_run_id=None if unreachable is None else unreachable.id,
        running_run_id=None if running is None else running.id,
    )


# --- launch instructions ---------------------------------------------------------------


@router.get("/browser", operation_id="get_linkedin_browser")
def get_browser(request: Request, user: CurrentUser) -> BrowserLaunchOut:
    """``netkeeper browser launch``'s instructions, as data (spec 9.1, ADR 0002).

    netkeeper never starts Chrome; it only ever prints (here, shows) the command
    for a person to run themselves. Everything below comes from config -- no
    attach, so this never awaits browser work (CLAUDE.md). ``user`` is unused --
    the instructions are the same for everyone -- but every route here resolves
    the current user (spec 14.1), local mode's single user included.
    """
    cdp_url = _settings(request).cdp_url
    profile = data_dir() / CHROME_PROFILE_DIRNAME
    return BrowserLaunchOut(
        cdp_url=cdp_url,
        profile_dir=str(profile),
        launch_command=chrome_launch_command(cdp_port(cdp_url), profile),
        remote_host_note=remote_host_note(cdp_url),
        check_command="netkeeper preflight",
    )
