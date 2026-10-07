"""``/campaigns``: create, enroll, list, status, pause and resume (P3-13).

The review gate and activation are ``campaign_review``'s
(``/campaigns/{id}/review/...``, ``POST /campaigns/{id}/activate``); nothing here
activates a campaign. ``netkeeper campaigns`` mirrors each route.

- ``POST /campaigns`` makes a ``draft`` from a mailbox and an ordered list of
  templates, with an optional audience source (a list or a filter).
- ``POST /campaigns/{id}/enroll`` enrolls the audience through the guards
  (spec 11.9), as ``pending``, while the campaign is ``draft`` or ``reviewing``.
- ``POST /campaigns/{id}/pause`` and ``.../resume`` are the engine's pause and
  resume: enrollments keep their state and due times.
- ``GET /campaigns/{id}/start-options`` is the scheduled start's default (the next
  Tuesday at 09:00), the suggestion and the reminder, and a warning, never a
  refusal, for a chosen start outside the suggested slots (#338).
- ``PUT /campaigns/{id}/start`` moves an active or paused campaign's start, until
  its first send.
- ``PUT /campaigns/{id}/steps/{step_id}/schedule`` sets a step's day offset and
  time of day, while the campaign is not over.
- ``GET /campaigns/{id}/results`` is what the campaign has done (#350): sends per
  local day, and replies, bounces and opt-outs per step, with the totals
  (:mod:`netkeeper.services.campaign_results` says what counts).
- The lifecycle (#345): ``POST .../end`` ends an active or paused campaign for
  good; ``POST .../archive`` and ``.../unarchive`` hide and show an ended one;
  ``GET .../delete-plan`` and ``DELETE /campaigns/{id}`` delete one that was never
  activated and has no messages, listing the Gmail drafts left behind.
  ``GET /campaigns`` leaves archived campaigns out unless ``archived=true``.

A campaign, template, mailbox or list that is not the user's answers ``404``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy.orm import Session

from netkeeper.config import Settings
from netkeeper.crm.filters import FilterTree
from netkeeper.models import (
    CAMPAIGN_NAME_MAX_LENGTH,
    Campaign,
    CampaignStatus,
    EnrollmentStatus,
    MessageStatus,
    StepCondition,
    StepMode,
    TemplateChannel,
    User,
)
from netkeeper.models.base import utcnow
from netkeeper.services import campaign_results, linkedin_steps
from netkeeper.services import campaigns as service
from netkeeper.services.campaign_engine import hours_for
from netkeeper.web.api.campaign_review import MissingOut
from netkeeper.web.deps import CurrentUser, SessionDep

router = APIRouter(tags=["campaigns"])

Responses = dict[int | str, dict[str, Any]]
NOT_FOUND: Responses = {404: {"description": "No such campaign, template, mailbox or list"}}
CONFLICT: Responses = {409: {"description": "Refused in the campaign's state, or a name taken"}}
INVALID: Responses = {422: {"description": "A value a campaign cannot be made from"}}


@contextmanager
def translate_errors() -> Iterator[None]:
    try:
        yield
    except service.CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.CampaignConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.InvalidCampaign as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


SendTime = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
"""A local time of day, ``HH:MM``, 24-hour."""


class StepIn(BaseModel):
    """One step. Left out, a field takes spec 11.2's default: the first step at once and
    every other seven days on, ``no_reply`` after the first, ``draft`` for email and
    ``prefill`` for LinkedIn, an email follow-up in the first email's thread."""

    template_id: int
    delay_days: Annotated[int, Field(ge=0, le=service.MAX_DELAY_DAYS)] | None = None
    mode: StepMode | None = None
    condition: StepCondition | None = None
    same_thread: bool | None = None
    send_time: SendTime | None = None
    """An explicit local time of day, ``HH:MM`` (#338). Left out, the step aims for the
    next suggested send slot after its delay."""


class CampaignCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=CAMPAIGN_NAME_MAX_LENGTH)]
    mailbox_id: int | None = None
    """Needed when any step is email."""
    steps: Annotated[list[StepIn], Field(min_length=1, max_length=service.MAX_STEPS)]
    list_id: int | None = None
    filter: FilterTree | None = None
    """The audience's source: a list or a filter, not both."""
    daily_cap: Annotated[int, Field(ge=0, le=service.MAX_DAILY_CAP)] | None = None


class EnrollIn(BaseModel):
    """``list_id`` or ``filter`` replaces the audience source first (a ``draft`` only),
    removing the pending enrollments the new source does not hold; its contacts, and
    any ``contact_ids``, are then enrolled through the guards."""

    list_id: int | None = None
    filter: FilterTree | None = None
    contact_ids: Annotated[list[int], Field(max_length=10_000)] = Field(default_factory=list)


class EnrollOut(BaseModel):
    campaign_id: int
    enrolled: int
    already: int
    excluded: int
    removed: int
    """Pending enrollments dropped because a new source no longer holds their contacts."""
    pending: int
    summary: str
    excluded_summary: str
    """The guards' line over the contacts this call considered (#342): an excluded contact
    that never becomes an enrollment, such as yourself, is named here."""


class CampaignSummaryOut(BaseModel):
    id: int
    name: str
    status: CampaignStatus
    mailbox_id: int | None
    steps: int
    enrollments: dict[EnrollmentStatus, int]
    created_at: datetime
    next_action_at: datetime | None = None
    """The soonest due time of an active enrollment, while the campaign is active."""
    starts_at: datetime | None = None
    """The scheduled start (#338); None before activation."""


class StepOut(BaseModel):
    id: int
    """What ``POST .../review/test-send`` takes as ``step_id``."""
    position: int
    channel: TemplateChannel
    mode: StepMode
    condition: StepCondition
    delay_days: int
    send_time: str | None
    """The step's own local time of day, ``HH:MM``; None for the next suggested slot."""
    same_thread: bool
    template_id: int
    template_name: str
    template_version: int
    fired: int
    """Outbound messages of the step, whatever became of them."""
    sent: int
    """Those that went out: ``sent``, or ``bounced`` after they were sent."""
    outbound: dict[MessageStatus, int] = {}
    """The step's outbound messages by status, only the statuses it has: a LinkedIn
    step's ``prefilled``, ``sent`` and ``stale`` counts (#383)."""


class CampaignOut(BaseModel):
    id: int
    name: str
    status: CampaignStatus
    mailbox_id: int | None
    mailbox_email: str | None
    source_list_id: int | None
    filter: dict[str, Any] | None
    daily_cap: int | None
    contacted_within_days_guard: int
    approved_at: datetime | None
    starts_at: datetime | None
    """The scheduled start (#338): nothing is sent before it. None before activation."""
    start_editable: bool
    """Whether ``PUT .../start`` can still move it: active or paused, nothing sent yet."""
    created_at: datetime
    steps: list[StepOut]
    enrollments: dict[EnrollmentStatus, int]
    next_action_at: datetime | None
    missing: list[MissingOut]
    """What the review gate still needs, for a ``draft`` or ``reviewing`` campaign."""
    concluded: bool = False
    """Whether it is over (#345): ended, or every enrollment finished. ``POST .../archive``
    takes only a ``completed`` (ended) campaign."""
    deletable: bool = False
    """Whether ``DELETE /campaigns/{id}`` takes it (#345): never activated, no messages."""


class LeftoverDraftOut(BaseModel):
    """A test-send Gmail draft that deleting the campaign leaves for you to delete."""

    step_position: int
    to_address: str
    drafted_at: datetime
    gmail_draft_id: str


class DeletePlanOut(BaseModel):
    """What deleting the campaign removes and leaves, or why it is refused (#345)."""

    campaign_id: int
    name: str
    deletable: bool
    refusal: str | None
    """Why it cannot be deleted; None when it can."""
    steps: int
    enrollments: int
    leftover_drafts: list[LeftoverDraftOut]
    """Gmail drafts netkeeper never deletes (ADR 0003): delete them by hand."""
    unverifies: str | None = None
    """The mailbox address, when the delete removes the only test drafts that could still
    verify it (#304); a new test draft from another campaign verifies it again."""


class DaySendsOut(BaseModel):
    date: dt.date
    """A local calendar day, in ``CampaignResultsOut.timezone``."""
    sent: int


class StepResultsOut(BaseModel):
    step_id: int
    position: int
    sent: int
    """Messages of the step that went out, ``bounced`` ones included."""
    replied: int
    """Enrollments whose first reply came after this step, before the next one sent."""
    bounced: int
    opted_out: int


class ResultTotalsOut(BaseModel):
    sent: int
    contacted: int
    """Enrollments with at least one send: the reply rate's denominator."""
    replied: int
    reply_rate: float | None
    """``replied / contacted``, from 0 to 1; None while nobody has been sent anything."""
    bounced: int
    opted_out: int


class CampaignResultsOut(BaseModel):
    campaign_id: int
    timezone: str
    """The time zone ``sends_per_day`` counts days in: yours."""
    sends_per_day: list[DaySendsOut]
    """From the first send's day to today, a zero for a day with none; empty before
    the first send."""
    steps: list[StepResultsOut]
    totals: ResultTotalsOut


def results_out(results: campaign_results.CampaignResults) -> CampaignResultsOut:
    """The API's answer for ``results``; ``netkeeper campaigns status --json`` prints it too."""
    t = results.totals
    return CampaignResultsOut(
        campaign_id=results.campaign_id,
        timezone=results.timezone,
        sends_per_day=[DaySendsOut(date=d.day, sent=d.sent) for d in results.sends_per_day],
        steps=[
            StepResultsOut(
                step_id=s.step_id,
                position=s.position,
                sent=s.sent,
                replied=s.replied,
                bounced=s.bounced,
                opted_out=s.opted_out,
            )
            for s in results.steps
        ],
        totals=ResultTotalsOut(
            sent=t.sent,
            contacted=t.contacted,
            replied=t.replied,
            reply_rate=t.reply_rate,
            bounced=t.bounced,
            opted_out=t.opted_out,
        ),
    )


class StartOptionsOut(BaseModel):
    timezone: str
    """The time zone the default and the suggestion are in: your ``linkedin.timezone``."""
    default_start: datetime
    """The next Tuesday at 09:00 local time; today when it is Tuesday before 09:00."""
    suggestion: str
    reminder: str
    """``serve`` must be running, and the Mac awake, for anything to send."""
    at: datetime | None
    warning: str | None
    """Why ``at`` is outside the suggested slots. A warning only: nothing is refused."""
    sending_hours: str
    """The sending hours everything after the start keeps, as a sentence (#338)."""


class StartIn(BaseModel):
    starts_at: AwareDatetime
    """The new scheduled start. A time already past starts the campaign now."""


class StepScheduleIn(BaseModel):
    delay_days: Annotated[int, Field(ge=0, le=service.MAX_DELAY_DAYS)]
    """Days after the step before; for step 1, after the start."""
    send_time: SendTime | None = None
    """An explicit local time of day, ``HH:MM``; None for the next suggested slot."""


class EnrollmentOut(BaseModel):
    id: int
    contact_id: int
    contact_name: str
    email: str | None
    status: EnrollmentStatus
    current_step: int | None
    next_action_at: datetime | None
    exit_reason: str | None
    replied_at: datetime | None
    not_sent_error: str | None = None
    """Why the latest try sent nothing, or why the enrollment is blocked: a step template
    that names a removed ``me.*`` field says so (#342). None once something sends."""
    try_again: bool = False
    """The latest LinkedIn prefill typed nothing, and the step waits for you to click
    Try again in the LinkedIn queue (#445). It has no next action until then."""


class EnrollmentPageOut(BaseModel):
    items: list[EnrollmentOut]
    total: int


Limit = Annotated[int, Query(ge=1, le=200, description="Enrollments per page.")]
Offset = Annotated[int, Query(ge=0, description="Enrollments to skip.")]
Search = Annotated[
    str,
    Query(
        max_length=service.ENROLLMENT_SEARCH_MAX,
        description="Part of the contact's name or an address, any case.",
    ),
]


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _summary_out(row: service.CampaignSummary) -> CampaignSummaryOut:
    c = row.campaign
    return CampaignSummaryOut(
        id=c.id,
        name=c.name,
        status=c.status,
        mailbox_id=c.mailbox_id,
        steps=row.steps,
        enrollments=dict(row.enrollments),
        created_at=c.created_at,
        next_action_at=row.next_action_at,
        starts_at=c.starts_at,
    )


def _campaign_out(detail: service.CampaignDetail) -> CampaignOut:
    c: Campaign = detail.campaign
    return CampaignOut(
        id=c.id,
        name=c.name,
        status=c.status,
        mailbox_id=c.mailbox_id,
        mailbox_email=detail.mailbox_email,
        source_list_id=c.source_list_id,
        filter=c.filter_json,
        daily_cap=c.daily_cap,
        contacted_within_days_guard=c.contacted_within_days_guard,
        approved_at=c.approved_at,
        starts_at=c.starts_at,
        start_editable=detail.start_editable,
        created_at=c.created_at,
        steps=[
            StepOut(
                id=s.step.id,
                position=s.step.position,
                channel=s.step.channel,
                mode=s.step.mode,
                condition=s.step.condition,
                delay_days=s.step.delay_days,
                send_time=s.step.send_time,
                same_thread=s.step.same_thread,
                template_id=s.step.template_id,
                template_name=s.template_name,
                template_version=s.template_version,
                fired=s.fired,
                sent=s.sent,
                outbound=dict(s.by_status),
            )
            for s in detail.steps
        ],
        enrollments=dict(detail.enrollments),
        next_action_at=detail.next_action_at,
        concluded=detail.concluded,
        deletable=detail.deletable,
        missing=[
            MissingOut(
                requirement=m.requirement,
                detail=m.detail,
                enrollment_ids=list(m.enrollment_ids),
                step_positions=list(m.step_positions),
            )
            for m in detail.missing
        ],
    )


def _detail(session: Session, user: User, campaign_id: int, request: Request) -> CampaignOut:
    detail = service.campaign_status(session, user, campaign_id, now=utcnow())
    return _campaign_out(detail)


@router.get("/campaigns", operation_id="list_campaigns")
def list_campaigns(
    session: SessionDep,
    user: CurrentUser,
    archived: Annotated[
        bool, Query(description="Only the archived campaigns, in place of the others.")
    ] = False,
) -> list[CampaignSummaryOut]:
    """Every campaign that is not archived, newest first, with its enrollment counts by
    status. ``archived=true`` lists only the archived ones (#345)."""
    return [_summary_out(row) for row in service.list_campaigns(session, user, archived=archived)]


@router.post(
    "/campaigns",
    operation_id="create_campaign",
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT, **INVALID},
)
def create_campaign(
    body: CampaignCreate, request: Request, session: SessionDep, user: CurrentUser
) -> CampaignOut:
    """A new ``draft``. Nobody is enrolled until ``POST .../enroll``."""
    with translate_errors():
        campaign = service.create_campaign(
            session,
            user,
            name=body.name,
            steps=[
                service.StepSpec(
                    template_id=s.template_id,
                    delay_days=s.delay_days,
                    mode=s.mode,
                    condition=s.condition,
                    same_thread=s.same_thread,
                    send_time=s.send_time,
                )
                for s in body.steps
            ],
            settings=_settings(request),
            mailbox_id=body.mailbox_id,
            list_id=body.list_id,
            filter=body.filter,
            daily_cap=body.daily_cap,
        )
        return _detail(session, user, campaign.id, request)


@router.get("/campaigns/{campaign_id}", operation_id="get_campaign", responses=NOT_FOUND)
def get_campaign(
    campaign_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> CampaignOut:
    """One campaign: steps and their progress, enrollments, next fire, what review misses."""
    with translate_errors():
        return _detail(session, user, campaign_id, request)


@router.get(
    "/campaigns/{campaign_id}/results",
    operation_id="get_campaign_results",
    responses=NOT_FOUND,
)
def get_campaign_results(
    campaign_id: int, session: SessionDep, user: CurrentUser
) -> CampaignResultsOut:
    """Sends per local day, and replies, bounces and opt-outs per step, with the totals."""
    with translate_errors():
        results = campaign_results.campaign_results(session, user, campaign_id, now=utcnow())
    return results_out(results)


@router.get(
    "/campaigns/{campaign_id}/enrollments",
    operation_id="list_campaign_enrollments",
    responses=NOT_FOUND,
)
def list_enrollments(
    campaign_id: int,
    session: SessionDep,
    user: CurrentUser,
    q: Search = "",
    status: EnrollmentStatus | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> EnrollmentPageOut:
    """One campaign's enrollments, oldest first: who, their status, their next fire.
    ``q`` finds one by name or address, as the review screen's search does."""
    with translate_errors():
        page = service.list_enrollments(
            session, user, campaign_id, q=q, status=status, limit=limit, offset=offset
        )
    return EnrollmentPageOut(
        items=[
            EnrollmentOut(
                id=row.enrollment.id,
                contact_id=row.enrollment.contact_id,
                contact_name=row.contact_name,
                email=row.email,
                status=row.enrollment.status,
                current_step=row.enrollment.current_step,
                next_action_at=row.enrollment.next_action_at,
                exit_reason=row.enrollment.exit_reason,
                replied_at=row.enrollment.replied_at,
                not_sent_error=row.enrollment.not_sent_error,
                try_again=row.enrollment.status is EnrollmentStatus.ACTIVE
                and linkedin_steps.needs_try_again(row.enrollment),
            )
            for row in page.items
        ],
        total=page.total,
    )


@router.post(
    "/campaigns/{campaign_id}/enroll",
    operation_id="enroll_campaign",
    responses={**NOT_FOUND, **CONFLICT, **INVALID},
)
def enroll(campaign_id: int, body: EnrollIn, session: SessionDep, user: CurrentUser) -> EnrollOut:
    """Enroll the audience as ``pending``, through the guards (spec 11.9)."""
    with translate_errors():
        outcome = service.enroll(
            session,
            user,
            campaign_id,
            now=utcnow(),
            list_id=body.list_id,
            filter=body.filter,
            contact_ids=body.contact_ids,
        )
    return EnrollOut(
        campaign_id=outcome.campaign_id,
        enrolled=outcome.enrolled,
        already=outcome.already,
        excluded=outcome.excluded,
        removed=outcome.removed,
        pending=outcome.pending,
        summary=outcome.summary,
        excluded_summary=outcome.excluded_summary,
    )


@router.post(
    "/campaigns/{campaign_id}/pause",
    operation_id="pause_campaign",
    responses={**NOT_FOUND, **CONFLICT},
)
def pause(
    campaign_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> CampaignOut:
    """``active`` to ``paused``: nothing fires until resumed; enrollments keep their state."""
    with translate_errors():
        service.pause(session, user, campaign_id)
        return _detail(session, user, campaign_id, request)


@router.post(
    "/campaigns/{campaign_id}/resume",
    operation_id="resume_campaign",
    responses={**NOT_FOUND, **CONFLICT},
)
def resume(
    campaign_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> CampaignOut:
    """``paused`` to ``active``: a step that came due meanwhile fires at the next chance."""
    with translate_errors():
        service.resume(session, user, campaign_id)
        return _detail(session, user, campaign_id, request)


@router.get(
    "/campaigns/{campaign_id}/start-options",
    operation_id="campaign_start_options",
    responses={**NOT_FOUND, **INVALID},
)
def start_options(
    campaign_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    at: Annotated[
        AwareDatetime | None,
        Query(description="A start to check against the suggested slots, with its zone."),
    ] = None,
) -> StartOptionsOut:
    """The default scheduled start, the suggestion and the reminder (#338), and a
    warning when ``at`` is outside the suggested slots. Changes nothing."""
    with translate_errors():
        service.get_campaign(session, user, campaign_id)
        options = service.start_options(
            user,
            settings=_settings(request),
            now=utcnow(),
            at=at,
            hours=hours_for(session, user),
        )
    return StartOptionsOut(
        timezone=options.timezone,
        default_start=options.default_start,
        suggestion=options.suggestion,
        reminder=options.reminder,
        at=options.at,
        warning=options.warning,
        sending_hours=options.sending_hours,
    )


@router.put(
    "/campaigns/{campaign_id}/start",
    operation_id="set_campaign_start",
    responses={**NOT_FOUND, **CONFLICT, **INVALID},
)
def set_start(
    campaign_id: int, body: StartIn, request: Request, session: SessionDep, user: CurrentUser
) -> CampaignOut:
    """Move an active or paused campaign's scheduled start. ``409`` once it has sent."""
    with translate_errors():
        service.set_start(
            session,
            user,
            campaign_id,
            settings=_settings(request),
            now=utcnow(),
            starts_at=body.starts_at,
        )
        return _detail(session, user, campaign_id, request)


@router.put(
    "/campaigns/{campaign_id}/steps/{step_id}/schedule",
    operation_id="set_step_schedule",
    responses={**NOT_FOUND, **CONFLICT, **INVALID},
)
def set_step_schedule(
    campaign_id: int,
    step_id: int,
    body: StepScheduleIn,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
) -> CampaignOut:
    """Set a step's day offset and time of day (#338). Only timing changes. ``409`` once
    the campaign is completed or archived."""
    with translate_errors():
        service.set_step_schedule(
            session,
            user,
            campaign_id,
            step_id,
            settings=_settings(request),
            delay_days=body.delay_days,
            send_time=body.send_time,
        )
        return _detail(session, user, campaign_id, request)


def _plan_out(plan: service.DeletePlan) -> DeletePlanOut:
    return DeletePlanOut(
        campaign_id=plan.campaign_id,
        name=plan.name,
        deletable=plan.deletable,
        refusal=plan.refusal,
        steps=plan.steps,
        enrollments=plan.enrollments,
        leftover_drafts=[
            LeftoverDraftOut(
                step_position=d.step_position,
                to_address=d.to_address,
                drafted_at=d.drafted_at,
                gmail_draft_id=d.gmail_draft_id,
            )
            for d in plan.leftover_drafts
        ],
        unverifies=plan.unverifies,
    )


@router.post(
    "/campaigns/{campaign_id}/end",
    operation_id="end_campaign",
    responses={**NOT_FOUND, **CONFLICT},
)
def end(campaign_id: int, request: Request, session: SessionDep, user: CurrentUser) -> CampaignOut:
    """``active`` or ``paused`` to ``completed``, for good (#345): nothing fires again.
    Enrollments keep their state, so replies to a step already sent still count."""
    with translate_errors():
        service.end(session, user, campaign_id)
        return _detail(session, user, campaign_id, request)


@router.post(
    "/campaigns/{campaign_id}/archive",
    operation_id="archive_campaign",
    responses={**NOT_FOUND, **CONFLICT},
)
def archive(
    campaign_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> CampaignOut:
    """Hide an ended (``completed``) campaign from the list and the dashboard (#345),
    keeping its messages and results. ``409`` for an active or paused campaign, even one
    whose every enrollment finished (end it first), and for one never activated (delete
    it)."""
    with translate_errors():
        service.archive(session, user, campaign_id)
        return _detail(session, user, campaign_id, request)


@router.post(
    "/campaigns/{campaign_id}/unarchive",
    operation_id="unarchive_campaign",
    responses={**NOT_FOUND, **CONFLICT},
)
def unarchive(
    campaign_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> CampaignOut:
    """``archived`` back to ``completed`` (#345). It never sends again."""
    with translate_errors():
        service.unarchive(session, user, campaign_id)
        return _detail(session, user, campaign_id, request)


@router.get(
    "/campaigns/{campaign_id}/delete-plan",
    operation_id="get_campaign_delete_plan",
    responses=NOT_FOUND,
)
def delete_plan(campaign_id: int, session: SessionDep, user: CurrentUser) -> DeletePlanOut:
    """What ``DELETE /campaigns/{id}`` would remove, the Gmail drafts it would leave,
    or why it is refused (#345). Changes nothing."""
    with translate_errors():
        return _plan_out(service.delete_plan(session, user, campaign_id))


@router.delete(
    "/campaigns/{campaign_id}",
    operation_id="delete_campaign",
    responses={**NOT_FOUND, **CONFLICT},
)
def delete_campaign(campaign_id: int, session: SessionDep, user: CurrentUser) -> DeletePlanOut:
    """Delete a campaign never activated and with no messages (#345), with its steps and
    enrollments. ``409``, with the reason, for any other: archive one that sent. The
    answer lists the Gmail drafts left behind: netkeeper never deletes one (ADR 0003)."""
    with translate_errors():
        return _plan_out(service.delete_campaign(session, user, campaign_id))
