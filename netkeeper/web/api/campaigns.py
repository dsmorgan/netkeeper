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

A campaign, template, mailbox or list that is not the user's answers ``404``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from netkeeper.campaigns.render import me_fields
from netkeeper.config import Settings
from netkeeper.crm.filters import FilterTree
from netkeeper.models import (
    CAMPAIGN_NAME_MAX_LENGTH,
    Campaign,
    CampaignStatus,
    EnrollmentStatus,
    StepCondition,
    StepMode,
    TemplateChannel,
    User,
)
from netkeeper.models.base import utcnow
from netkeeper.services import campaigns as service
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


class StepIn(BaseModel):
    """One step. Left out, a field takes spec 11.2's default: the first step at once and
    every other seven days on, ``no_reply`` after the first, ``draft`` for email and
    ``prefill`` for LinkedIn, an email follow-up in the first email's thread."""

    template_id: int
    delay_days: Annotated[int, Field(ge=0, le=service.MAX_DELAY_DAYS)] | None = None
    mode: StepMode | None = None
    condition: StepCondition | None = None
    same_thread: bool | None = None


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


class StepOut(BaseModel):
    id: int
    """What ``POST .../review/test-send`` takes as ``step_id``."""
    position: int
    channel: TemplateChannel
    mode: StepMode
    condition: StepCondition
    delay_days: int
    same_thread: bool
    template_id: int
    template_name: str
    template_version: int
    fired: int
    sent: int


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
    created_at: datetime
    steps: list[StepOut]
    enrollments: dict[EnrollmentStatus, int]
    next_action_at: datetime | None
    missing: list[MissingOut]
    """What the review gate still needs, for a ``draft`` or ``reviewing`` campaign."""


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
        created_at=c.created_at,
        steps=[
            StepOut(
                id=s.step.id,
                position=s.step.position,
                channel=s.step.channel,
                mode=s.step.mode,
                condition=s.step.condition,
                delay_days=s.step.delay_days,
                same_thread=s.step.same_thread,
                template_id=s.step.template_id,
                template_name=s.template_name,
                template_version=s.template_version,
                fired=s.fired,
                sent=s.sent,
            )
            for s in detail.steps
        ],
        enrollments=dict(detail.enrollments),
        next_action_at=detail.next_action_at,
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
    detail = service.campaign_status(
        session, user, campaign_id, me=me_fields(_settings(request).me), now=utcnow()
    )
    return _campaign_out(detail)


@router.get("/campaigns", operation_id="list_campaigns")
def list_campaigns(session: SessionDep, user: CurrentUser) -> list[CampaignSummaryOut]:
    """Every campaign, newest first, with its enrollment counts by status."""
    return [_summary_out(row) for row in service.list_campaigns(session, user)]


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
