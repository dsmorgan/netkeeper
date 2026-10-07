"""A campaign step adopts the newest version of its template (#397).

- ``GET /campaigns/{id}/steps/{step_id}/adoption`` shows what adopting would change:
  both versions and their diff, the new version's lint, a few messages rendered in
  it, the enrollments that get it, the messages that keep their text, and why it is
  refused, if it is. Changes nothing.
- ``POST /campaigns/{id}/steps/{step_id}/adopt`` adopts it, with the ``fingerprint``
  the preview came with and ``confirm: true``. ``409`` when it is refused, or when
  the step or a version changed since the preview.

:mod:`netkeeper.services.template_adoption` holds the rules: a message that exists,
in flight or sent, always keeps the text it was rendered with. ``netkeeper campaigns
adopt-template`` mirrors both routes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from netkeeper.models import CampaignStatus, EnrollmentStatus, MessageStatus
from netkeeper.models.base import utcnow
from netkeeper.services import template_adoption as service
from netkeeper.web.api.campaign_review import MessagePreviewOut, _message_out
from netkeeper.web.api.campaigns import CampaignOut, _detail, translate_errors
from netkeeper.web.deps import CurrentUser, SessionDep
from netkeeper.web.schemas import LintIssueOut

router = APIRouter(tags=["campaigns"])

Responses = dict[int | str, dict[str, Any]]
NOT_FOUND: Responses = {404: {"description": "No such campaign or step"}}
CONFLICT: Responses = {
    409: {"description": "Refused: see the preview's refusal, or it changed since the preview"}
}


class VersionOut(BaseModel):
    template_id: int
    name: str
    version: int
    subject: str | None
    body: str


class EnrollmentRefOut(BaseModel):
    enrollment_id: int
    contact_id: int
    contact_name: str
    status: EnrollmentStatus


class OpenMessageOut(BaseModel):
    """A message of the step still in flight: it keeps the text it was rendered with."""

    message_id: int
    enrollment_id: int
    contact_name: str
    status: MessageStatus


class AdoptionOut(BaseModel):
    campaign_id: int
    campaign_status: CampaignStatus
    step_id: int
    position: int
    current: VersionOut
    newest: VersionOut
    """The same as ``current`` when no newer version exists."""
    diff: str
    """A unified diff from ``current`` to ``newest``."""
    errors: list[LintIssueOut]
    """The newest version's lint errors: any one refuses the adoption."""
    warnings: list[LintIssueOut]
    refusal: str | None
    """Why the step cannot adopt ``newest``; null when it can."""
    affected_total: int
    """Enrollments the step has not fired for yet: they get the newest version."""
    affected: list[EnrollmentRefOut]
    """The first hundred of them."""
    released: int
    """Of those, enrollments the template parked (a ``blocked:`` or ``too_long:`` reason),
    due again once it is adopted."""
    kept: dict[MessageStatus, int]
    """The step's messages by status. Each keeps the text it was rendered with."""
    open_messages: list[OpenMessageOut]
    """The step's messages still in flight (claimed, drafted or prefilled)."""
    samples: list[MessagePreviewOut]
    """A few of the affected enrollments' messages, rendered in the newest version."""
    blocked_total: int
    """Affected enrollments whose message in the newest version is blocked (it does not
    render, has a lint error once rendered, or a guard excludes the contact): never sent."""
    blocked: list[MessagePreviewOut]
    """The first hundred of them, each with why."""
    blocked_capped: bool
    """True when more enrollments are affected than the preview renders (2,000), so
    ``blocked_total`` counts only the first 2,000."""
    fingerprint: str
    """What ``POST .../adopt`` takes back."""


class AdoptIn(BaseModel):
    fingerprint: Annotated[str, Field(max_length=64)]
    """The ``fingerprint`` of the preview you confirmed."""
    confirm: bool = False
    """Required: you looked at the change and the enrollments it reaches."""


class AdoptedOut(BaseModel):
    from_version: int
    to_version: int
    released: int
    """Parked enrollments due again."""
    affected_total: int
    adopted_at: datetime
    campaign: CampaignOut


def _version(v: service.VersionText) -> VersionOut:
    return VersionOut(
        template_id=v.template_id, name=v.name, version=v.version, subject=v.subject, body=v.body
    )


def _preview_out(p: service.AdoptionPreview) -> AdoptionOut:
    return AdoptionOut(
        campaign_id=p.campaign_id,
        campaign_status=p.campaign_status,
        step_id=p.step_id,
        position=p.position,
        current=_version(p.current),
        newest=_version(p.newest),
        diff=p.diff,
        errors=[LintIssueOut.model_validate(i.to_json()) for i in p.errors],
        warnings=[LintIssueOut.model_validate(i.to_json()) for i in p.warnings],
        refusal=p.refusal,
        affected_total=p.affected_total,
        affected=[
            EnrollmentRefOut(
                enrollment_id=e.enrollment_id,
                contact_id=e.contact_id,
                contact_name=e.contact_name,
                status=e.status,
            )
            for e in p.affected
        ],
        released=p.released,
        kept=dict(p.kept),
        open_messages=[
            OpenMessageOut(
                message_id=m.message_id,
                enrollment_id=m.enrollment_id,
                contact_name=m.contact_name,
                status=m.status,
            )
            for m in p.open_messages
        ],
        samples=[_message_out(m) for m in p.samples],
        blocked_total=p.blocked_total,
        blocked=[_message_out(m) for m in p.blocked],
        blocked_capped=p.blocked_capped,
        fingerprint=p.fingerprint,
    )


@router.get(
    "/campaigns/{campaign_id}/steps/{step_id}/adoption",
    operation_id="step_template_adoption",
    responses=NOT_FOUND,
)
def get_adoption(
    campaign_id: int, step_id: int, session: SessionDep, user: CurrentUser
) -> AdoptionOut:
    """What adopting the newest version of the step's template would change. Changes
    nothing."""
    with translate_errors():
        return _preview_out(service.preview(session, user, campaign_id, step_id, now=utcnow()))


@router.post(
    "/campaigns/{campaign_id}/steps/{step_id}/adopt",
    operation_id="adopt_step_template",
    responses={**NOT_FOUND, **CONFLICT},
)
def adopt(
    campaign_id: int,
    step_id: int,
    body: AdoptIn,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
) -> AdoptedOut:
    """Have the step use the newest version of its template, as the preview showed it.
    Every message that exists keeps its text; enrollments the step has not fired for get
    the new version. The confirm approves the step in it."""
    if not body.confirm:
        raise HTTPException(
            status_code=422, detail="confirm the change: set confirm to true after the preview"
        )
    with translate_errors():
        result = service.adopt(
            session,
            user,
            campaign_id,
            step_id,
            fingerprint_seen=body.fingerprint,
            now=utcnow(),
        )
        return AdoptedOut(
            from_version=result.from_version,
            to_version=result.to_version,
            released=result.released,
            affected_total=result.affected_total,
            adopted_at=result.adopted_at,
            campaign=_detail(session, user, campaign_id, request),
        )
