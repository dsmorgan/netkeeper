"""``/campaigns/{id}/review`` and ``/campaigns/{id}/activate``: the review gate (spec 11.8; P3-09).

The flow, for the campaign builder (P3-11):

1. ``POST /campaigns/{id}/review/start``: ``draft`` to ``reviewing``, once the
   campaign has a step and someone enrolled.
2. ``POST .../review/sample`` draws the sample (the same one while the audience
   is unchanged) and answers its previews; ``POST .../review/previews`` answers
   the previews of enrollments the person looked up. ``POST .../review/approve``
   approves viewed previews, each with the ``fingerprint`` it came with.
3. ``POST .../review/lint``, ``POST .../review/guards/acknowledge`` (with the
   summary and ``audience_fingerprint`` from ``GET .../review``), and
   ``POST .../review/test-send`` for each email step: sent on a mailbox armed to
   send, drafted on one armed for drafts only.
4. ``POST /campaigns/{id}/activate``: ``409`` with ``missing``, a list of every
   requirement not met, unless each is recorded and current.

``GET .../review`` answers the same ``missing`` list at any time. A campaign,
step or enrollment that is not the user's answers ``404``. Every ``POST``
needs the CSRF header. The test send holds no session while it talks to Gmail.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.gmail import Gmail, GmailError
from netkeeper.campaigns.render import me_fields
from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.models import CampaignStatus, MailboxArm, User
from netkeeper.models.base import utcnow
from netkeeper.services import campaign_review as service
from netkeeper.services.mailboxes import MailboxNotFound, MailboxNotReady, open_gmail
from netkeeper.web.deps import CurrentUser, SessionDep, read_only
from netkeeper.web.errors import ApiError
from netkeeper.web.schemas import LintIssueOut

log = logging.getLogger(__name__)

router = APIRouter(tags=["campaigns"])

Responses = dict[int | str, dict[str, Any]]
NOT_FOUND: Responses = {404: {"description": "No such campaign, step or enrollment for this user"}}
CONFLICT: Responses = {409: {"description": "Refused in the campaign's state; the detail says why"}}
GMAIL: Responses = {502: {"description": "Gmail refused or failed the test send; nothing recorded"}}

GmailOpener = Callable[[int, int], Gmail]


class MissingOut(BaseModel):
    requirement: str
    detail: str
    enrollment_ids: list[int] = Field(default_factory=list)
    step_positions: list[int] = Field(default_factory=list)


class ActivationRefusedOut(BaseModel):
    """The ``409`` body of an activation refused for an incomplete review."""

    detail: str
    missing: list[MissingOut]


class TestSendOut(BaseModel):
    """A test Gmail accepted. ``drafted`` when the mailbox was armed for drafts only: the
    test is a draft in the mailbox's Drafts, and ``sent_at`` is when it was drafted."""

    __test__ = False  # not a pytest class, whatever its name

    step_id: int
    to_address: str
    sent_at: datetime
    drafted: bool


class ReviewOut(BaseModel):
    campaign_id: int
    status: CampaignStatus
    content_fingerprint: str
    audience_fingerprint: str
    guard_summary: str
    guards_acknowledged: str | None
    missing: list[MissingOut]


class StepPreviewOut(BaseModel):
    position: int
    channel: str
    to_address: str | None
    subject: str | None
    body: str | None
    issues: list[LintIssueOut]
    error: str | None


class EnrollmentPreviewOut(BaseModel):
    enrollment_id: int
    contact_id: int
    contact_name: str
    sampled: bool
    approved: bool
    fingerprint: str
    steps: list[StepPreviewOut]


class PreviewsOut(BaseModel):
    content_fingerprint: str
    enrollments: list[EnrollmentPreviewOut]


class PreviewsIn(BaseModel):
    enrollment_ids: Annotated[list[int], Field(min_length=1, max_length=service.VIEW_MAX)]


class ApprovalIn(BaseModel):
    enrollment_id: int
    fingerprint: Annotated[str, Field(max_length=64)]


class ApproveIn(BaseModel):
    """Each enrollment with the ``fingerprint`` its preview came with."""

    previews: Annotated[list[ApprovalIn], Field(min_length=1, max_length=service.VIEW_MAX)]


class LintStepOut(BaseModel):
    position: int
    errors: list[LintIssueOut]


class LintOut(BaseModel):
    clean: bool
    steps: list[LintStepOut]


class GuardsIn(BaseModel):
    summary: Annotated[str, Field(max_length=2000)]
    audience_fingerprint: Annotated[str, Field(max_length=64)]


class TestSendIn(BaseModel):
    __test__ = False  # not a pytest class, whatever its name

    step_id: int
    enrollment_id: int | None = None


@contextmanager
def translate_errors() -> Iterator[None]:
    try:
        yield
    except service.ReviewNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.ReviewIncomplete as exc:
        raise ApiError(409, _refused(exc)) from exc
    except service.ReviewConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _refused(exc: service.ReviewIncomplete) -> dict[str, Any]:
    return ActivationRefusedOut(detail=str(exc), missing=_missing_out(exc.missing)).model_dump()


def _missing_out(missing: Any) -> list[MissingOut]:
    return [
        MissingOut(
            requirement=m.requirement,
            detail=m.detail,
            enrollment_ids=list(m.enrollment_ids),
            step_positions=list(m.step_positions),
        )
        for m in missing
    ]


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _me(request: Request) -> dict[str, str]:
    return me_fields(_settings(request).me)


def _previews_out(previews: service.Previews) -> PreviewsOut:
    return PreviewsOut(
        content_fingerprint=previews.content_fingerprint,
        enrollments=[
            EnrollmentPreviewOut(
                enrollment_id=e.enrollment_id,
                contact_id=e.contact_id,
                contact_name=e.contact_name,
                sampled=e.sampled,
                approved=e.approved,
                fingerprint=e.fingerprint,
                steps=[
                    StepPreviewOut(
                        position=s.position,
                        channel=s.channel.value,
                        to_address=s.to_address,
                        subject=s.subject,
                        body=s.body,
                        issues=[LintIssueOut.model_validate(i.to_json()) for i in s.issues],
                        error=s.error,
                    )
                    for s in e.steps
                ],
            )
            for e in previews.enrollments
        ],
    )


def _review(session: Session, user: User, campaign_id: int, me: dict[str, str]) -> ReviewOut:
    now = utcnow()
    campaign = service.get_campaign(session, user, campaign_id)
    return ReviewOut(
        campaign_id=campaign.id,
        status=campaign.status,
        content_fingerprint=service.content_fingerprint(session, user, campaign, me),
        audience_fingerprint=service.audience_fingerprint(session, user, campaign),
        guard_summary=service.guard_summary(session, user, campaign, now=now),
        guards_acknowledged=campaign.guards_summary,
        missing=_missing_out(service.missing(session, user, campaign, me=me, now=now)),
    )


@router.get("/campaigns/{campaign_id}/review", responses=NOT_FOUND)
def get_review(
    campaign_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> ReviewOut:
    """Where the review stands: the fingerprints, the guard summary, what is missing."""
    with translate_errors():
        return _review(session, user, campaign_id, _me(request))


@router.post("/campaigns/{campaign_id}/review/start", responses={**NOT_FOUND, **CONFLICT})
def start_review(
    campaign_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> ReviewOut:
    """``draft`` to ``reviewing``: needs a step and someone enrolled."""
    with translate_errors():
        service.start_review(session, user, campaign_id)
        return _review(session, user, campaign_id, _me(request))


@router.post("/campaigns/{campaign_id}/review/sample", responses={**NOT_FOUND, **CONFLICT})
def sample_previews(
    campaign_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> PreviewsOut:
    """The sample's rendered previews, drawn by the server once per audience."""
    with translate_errors():
        return _previews_out(
            service.draw_sample(session, user, campaign_id, me=_me(request), now=utcnow())
        )


@router.post("/campaigns/{campaign_id}/review/previews", responses={**NOT_FOUND, **CONFLICT})
def view_previews(
    campaign_id: int, body: PreviewsIn, request: Request, session: SessionDep, user: CurrentUser
) -> PreviewsOut:
    """The rendered previews of enrollments the person looked up. Each must be approved."""
    with translate_errors():
        return _previews_out(
            service.view(
                session, user, campaign_id, body.enrollment_ids, me=_me(request), now=utcnow()
            )
        )


@router.post("/campaigns/{campaign_id}/review/approve", responses={**NOT_FOUND, **CONFLICT})
def approve_previews(
    campaign_id: int, body: ApproveIn, request: Request, session: SessionDep, user: CurrentUser
) -> ReviewOut:
    """Approve viewed previews, each for the ``fingerprint`` it was shown with."""
    with translate_errors():
        service.approve(
            session,
            user,
            campaign_id,
            {p.enrollment_id: p.fingerprint for p in body.previews},
            me=_me(request),
            now=utcnow(),
        )
        return _review(session, user, campaign_id, _me(request))


@router.post("/campaigns/{campaign_id}/review/lint", responses={**NOT_FOUND, **CONFLICT})
def lint_campaign(
    campaign_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> LintOut:
    """Lint every step's template; the result is recorded only when there is no error."""
    with translate_errors():
        result = service.record_lint(session, user, campaign_id, me=_me(request), now=utcnow())
    return LintOut(
        clean=result.clean,
        steps=[
            LintStepOut(
                position=position,
                errors=[LintIssueOut.model_validate(i.to_json()) for i in issues],
            )
            for position, issues in result.steps
        ],
    )


@router.post(
    "/campaigns/{campaign_id}/review/guards/acknowledge", responses={**NOT_FOUND, **CONFLICT}
)
def acknowledge_guards(
    campaign_id: int, body: GuardsIn, request: Request, session: SessionDep, user: CurrentUser
) -> ReviewOut:
    """Acknowledge the guard summary; refused unless it is still the current one."""
    with translate_errors():
        service.acknowledge_guards(
            session,
            user,
            campaign_id,
            summary_seen=body.summary,
            audience_fingerprint_seen=body.audience_fingerprint,
            now=utcnow(),
        )
        return _review(session, user, campaign_id, _me(request))


def _opener(request: Request) -> GmailOpener:
    """The Gmail client for a mailbox. Tests set ``app.state.gmail_opener`` to a fake."""
    opener: GmailOpener | None = getattr(request.app.state, "gmail_opener", None)
    if opener is not None:
        return opener
    factory: sessionmaker[Session] = request.app.state.session_factory
    endpoints = getattr(request.app.state, "gmail_endpoints", None)
    return lambda user_id, mailbox_id: open_gmail(factory, user_id, mailbox_id, endpoints=endpoints)


@router.post(
    "/campaigns/{campaign_id}/review/test-send",
    responses={**NOT_FOUND, **CONFLICT, **GMAIL},
)
@read_only  # reads, sends with no session open, then opens its own writer
def test_send(
    campaign_id: int, body: TestSendIn, request: Request, user: CurrentUser
) -> TestSendOut:
    """Test one email step, rendered for an enrollment, addressed to the campaign
    mailbox's own address. Follows the mailbox's arming, read again just before the
    Gmail call: armed to send, it is sent; armed for drafts only, it is a draft in the
    mailbox's Drafts (never ``messages.send``); disarmed, ``409``. Never a campaign
    message: it counts toward no cap or recency and advances no enrollment."""
    factory: sessionmaker[Session] = request.app.state.session_factory
    me = _me(request)
    with session_scope(factory) as session, translate_errors():
        plan = service.prepare_test_send(
            session,
            user,
            campaign_id,
            body.step_id,
            enrollment_id=body.enrollment_id,
            me=me,
            today=utcnow().date(),
        )
    what = f"of step {plan.step_position} of campaign {plan.campaign_id}"
    purpose = f"test {what}"
    draft_id: str | None = None
    try:
        with session_scope(factory) as session:
            arming = service.test_send_arming(session, user, plan.mailbox_id)
        if arming is None:
            raise HTTPException(status_code=409, detail="the mailbox is no longer armed")
        gmail = _opener(request)(user.id, plan.mailbox_id)
        if arming is MailboxArm.SEND:
            purpose = f"test send {what}"
            ref = gmail.send(plan.message, purpose=purpose)
        else:  # armed for drafts only: never messages.send (#277, #304)
            purpose = f"test draft {what}"
            draft = gmail.create_draft(plan.message, purpose=purpose)
            ref, draft_id = draft.message, draft.id
    except (MailboxNotFound, MailboxNotReady) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GmailError as exc:
        log.info("%s failed: %s", purpose, type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"Gmail: {exc}") from exc
    now = utcnow()
    with session_scope(factory, write=True) as session, translate_errors():
        row = service.record_test_send(
            session, user, plan, gmail_message_id=ref.id, gmail_draft_id=draft_id, now=now
        )
        return TestSendOut(
            step_id=row.step_id,
            to_address=row.to_address,
            sent_at=row.sent_at,
            drafted=row.gmail_draft_id is not None,
        )


REFUSED: Responses = {
    409: {
        "model": ActivationRefusedOut,
        "description": "The review is not complete (``missing`` lists what is not), or the "
        "campaign cannot be activated in its state",
    }
}


@router.post("/campaigns/{campaign_id}/activate", responses={**NOT_FOUND, **REFUSED})
def activate_campaign(
    campaign_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> ReviewOut:
    """``reviewing`` to ``active``: ``409`` with ``missing`` unless every review
    requirement is recorded and current, checked in this one writer transaction."""
    with translate_errors():
        service.activate(
            session,
            user,
            campaign_id,
            settings=_settings(request),
            me=_me(request),
            now=utcnow(),
        )
        return _review(session, user, campaign_id, _me(request))
