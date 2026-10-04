"""``/campaigns/linkedin``: a campaign's LinkedIn steps, prefilled one at a time (spec 11.6; P4-09).

- ``GET /campaigns/linkedin/ready``: due LinkedIn steps, oldest first, each ready to
  prefill once its ``held_until`` (the sending hours) has passed.
- ``GET /campaigns/linkedin/waiting``: ``prefilled`` and ``stale`` messages, waiting
  for you to send or discard them, and ``interrupted`` ones (claimed, their run over
  with no outcome), waiting for you to discard them.
- ``POST /campaigns/linkedin/prefill`` with ``{"enrollment_id": n}`` or
  ``{"next": true}``: claims the step (every check in
  :mod:`netkeeper.services.linkedin_steps`) and submits its ``message_send`` run to
  the task runner. ``202`` with the run id. It never waits for the browser (spec 9.9).
- ``POST /campaigns/linkedin/messages/{id}/check``: "I sent it, check now": a manual
  inbox poll.
- ``POST /campaigns/linkedin/messages/{id}/discard``: you will not send it. The step
  counts as fired, and the enrollment moves on.

Nothing here touches the browser. The minute tick never claims a LinkedIn step: only
these requests, which a person makes, do.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, model_validator

from netkeeper.config import Settings
from netkeeper.models import Contact, Enrollment, EnrollmentStatus, MessageStatus
from netkeeper.models.base import utcnow
from netkeeper.scoping import get_scoped
from netkeeper.services import linkedin_steps as service
from netkeeper.services import runs
from netkeeper.services.scheduled_runs import submit_run
from netkeeper.web.deps import CurrentUser, SessionDep, Tasks
from netkeeper.web.schemas import RunAccepted

router = APIRouter(prefix="/campaigns/linkedin", tags=["campaigns"])

Responses = dict[int | str, dict[str, Any]]
_NO_WORKER: Final = (
    "this process has no browser worker; start `netkeeper serve` to prefill LinkedIn steps"
)

Limit = Annotated[int, Query(ge=1, le=service.READY_PAGE_MAX, description="Items per page.")]
Offset = Annotated[int, Query(ge=0, description="Items to skip.")]


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _executor(request: Request) -> runs.RunExecutor:
    executor: runs.RunExecutor | None = request.app.state.executor
    if executor is None:
        raise HTTPException(status_code=503, detail=_NO_WORKER)
    return executor


def _name(contact: Contact) -> str:
    first = contact.preferred_name or contact.first_name
    return " ".join(part for part in (first, contact.last_name) if part)


class ReadyOut(BaseModel):
    """One due LinkedIn step. The contact is named and nothing more: no message text."""

    enrollment_id: int
    campaign_id: int
    campaign_name: str
    step_position: int
    contact_id: int
    contact_name: str
    due: datetime
    held_until: datetime | None
    """When the sending hours let it go; null for now."""


class ReadyPage(BaseModel):
    items: list[ReadyOut]
    total: int


class WaitingOut(BaseModel):
    """One LinkedIn message waiting for you. No message text."""

    message_id: int
    status: MessageStatus
    """``prefilled``, ``stale`` three days after its prefill, or ``scheduled`` when
    ``interrupted``."""
    interrupted: bool
    """Claimed, and its run ended without recording what it typed (a crash): nobody knows
    what the composer holds. It blocks every later prefill until you discard it."""
    enrollment_id: int
    campaign_id: int
    campaign_name: str
    contact_id: int
    contact_name: str
    prefilled_at: datetime | None


class WaitingPage(BaseModel):
    items: list[WaitingOut]
    total: int


class PrefillIn(BaseModel):
    """One of the two: an enrollment, or ``next`` for the oldest ready one."""

    enrollment_id: int | None = None
    next: bool = False

    @model_validator(mode="after")
    def _one_of(self) -> PrefillIn:
        if (self.enrollment_id is None) == (not self.next):
            raise ValueError('give either "enrollment_id" or "next": true, not both')
        return self


class PrefillAccepted(BaseModel):
    """The ``202`` of a prefill: the claimed message, its run, and the task running it."""

    enrollment_id: int
    message_id: int
    run_id: int
    task_id: str


class PrefillRefused(BaseModel):
    """Why nothing was claimed: the reasons as words, and one line for a person."""

    enrollment_id: int | None
    reasons: list[str]
    detail: str | None


class DiscardedOut(BaseModel):
    message_id: int
    status: MessageStatus
    enrollment_id: int
    enrollment_status: EnrollmentStatus


@router.get("/ready", operation_id="list_linkedin_ready")
def list_ready(
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    limit: Limit = 20,
    offset: Offset = 0,
) -> ReadyPage:
    """Due LinkedIn steps, ready to prefill, oldest due first."""
    rows, total = service.ready_to_prefill(
        session, user, now=utcnow(), settings=_settings(request), limit=limit, offset=offset
    )
    return ReadyPage(
        items=[
            ReadyOut(
                enrollment_id=row.enrollment.id,
                campaign_id=row.campaign.id,
                campaign_name=row.campaign.name,
                step_position=row.step.position,
                contact_id=row.contact.id,
                contact_name=_name(row.contact),
                due=row.due,
                held_until=row.held_until,
            )
            for row in rows
        ],
        total=total,
    )


@router.get("/waiting", operation_id="list_linkedin_waiting")
def list_waiting(
    user: CurrentUser, session: SessionDep, limit: Limit = 20, offset: Offset = 0
) -> WaitingPage:
    """Prefilled and stale LinkedIn messages, waiting for you to send or discard them."""
    rows, total = service.waiting_for_you(session, user, limit=limit, offset=offset)
    return WaitingPage(
        items=[
            WaitingOut(
                message_id=row.message.id,
                status=row.message.status,
                interrupted=row.interrupted,
                enrollment_id=row.enrollment.id,
                campaign_id=row.campaign.id,
                campaign_name=row.campaign.name,
                contact_id=row.contact.id,
                contact_name=_name(row.contact),
                prefilled_at=row.message.prefilled_at,
            )
            for row in rows
        ],
        total=total,
    )


@router.post(
    "/prefill",
    operation_id="prefill_linkedin_step",
    status_code=202,
    responses={
        404: {"description": "No such enrollment, or nothing is ready"},
        409: {"description": "Refused: the reasons are in the body", "model": PrefillRefused},
        503: {"description": "This process has no browser worker"},
    },
)
async def prefill(
    body: PrefillIn, request: Request, user: CurrentUser, session: SessionDep, tasks: Tasks
) -> PrefillAccepted:
    """Claim one LinkedIn step and submit its prefill; answers at once, before any browser
    work. A refusal answers ``409`` with its reasons; what the refusal changed (a reply
    that ended the enrollment, a step parked) is kept."""
    executor = _executor(request)
    settings = _settings(request)
    now = utcnow()
    try:
        if body.next:
            claim = service.claim_next(session, user, now=now, settings=settings)
            if claim is None:
                raise HTTPException(status_code=404, detail="no LinkedIn step is ready")
        else:
            assert body.enrollment_id is not None  # the model's own check
            claim = service.claim_prefill(
                session, user, body.enrollment_id, now=now, settings=settings
            )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not claim.claimed or claim.message_id is None or claim.run_id is None:
        session.commit()  # keep what the refusal changed
        raise HTTPException(
            status_code=409,
            detail=PrefillRefused(
                enrollment_id=claim.enrollment_id, reasons=list(claim.reasons), detail=claim.detail
            ).model_dump(),
        )
    # Committed before the task is submitted, so the worker's first read finds the run.
    session.commit()
    task_id, _ = submit_run(tasks, executor, claim.run_id, user.id)
    return PrefillAccepted(
        enrollment_id=claim.enrollment_id,
        message_id=claim.message_id,
        run_id=claim.run_id,
        task_id=task_id,
    )


@router.post(
    "/messages/{message_id}/check",
    operation_id="check_linkedin_prefill_sent",
    status_code=202,
    responses={
        404: {"description": "No such message"},
        409: {
            "description": "It does not wait for you, a run is running, the session is"
            " flagged, heat is too high, or it is outside active hours"
        },
        422: {"description": "The inbox poll has no runner"},
        503: {"description": "This process has no browser worker"},
    },
)
async def check_sent(
    message_id: int, request: Request, user: CurrentUser, session: SessionDep, tasks: Tasks
) -> RunAccepted:
    """ "I sent it, check now": start a manual inbox poll, which finds the message sent."""
    executor = _executor(request)
    try:
        run = service.check_sent(
            session, user, message_id, now=utcnow(), settings=_settings(request)
        )
    except service.PrefillNotWaiting as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        runs.RunAlreadyRunning,
        runs.HeatSkipped,
        runs.SessionFlagged,
        runs.OutsideActiveHours,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except runs.RunError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run_id = run.id
    session.commit()
    task_id, _ = submit_run(tasks, executor, run_id, user.id)
    return RunAccepted(run_id=run_id, task_id=task_id)


@router.post(
    "/messages/{message_id}/discard",
    operation_id="discard_linkedin_prefill",
    responses={
        404: {"description": "No such message"},
        409: {"description": "It does not wait for you"},
    },
)
def discard(
    message_id: int, request: Request, user: CurrentUser, session: SessionDep
) -> DiscardedOut:
    """You will not send it: ``discarded``. The step counts as fired, and the enrollment
    moves to its next step or completes. Nothing changes in LinkedIn."""
    try:
        message = service.discard(
            session, user, message_id, settings=_settings(request), now=utcnow()
        )
    except service.PrefillNotWaiting as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    enrollment = get_scoped(session, user, Enrollment, message.enrollment_id)
    assert enrollment is not None  # a message's enrollment is never deleted under it
    return DiscardedOut(
        message_id=message.id,
        status=message.status,
        enrollment_id=enrollment.id,
        enrollment_status=enrollment.status,
    )
