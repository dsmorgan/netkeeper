"""``/campaigns/{id}/review`` and ``/campaigns/{id}/activate``: the review gate (spec 11.8; P3-09).

The flow, for the campaign builder (P3-11):

1. ``POST /campaigns/{id}/review/start``: ``draft`` to ``reviewing``, once the
   campaign has a step and someone enrolled.
2. ``GET .../review/steps/{step_id}`` answers one step's review: one page of its
   rendered messages (``offset``, ``limit``) to page through, every message that
   is blocked (it fails to render, has a lint error, or a guard excludes the
   contact), and the step's ``fingerprint``. ``POST .../review/steps/{step_id}/approve``
   approves the step as a whole for that ``fingerprint``, which covers its
   messages rendered later too, until the step or its template changes. A step
   whose template uses ``{{ personal_line }}`` is not approved as a whole:
   ``POST .../review/steps/{step_id}/messages/approve`` approves its messages
   one by one, each with the ``fingerprint`` it came with (#339).
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

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.gmail import Gmail, GmailError
from netkeeper.campaigns.render import me_fields
from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.models import CampaignStatus, MailboxArm, User
from netkeeper.models.base import utcnow
from netkeeper.services import campaign_review as service
from netkeeper.services import campaigns as campaign_service
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


class RefusedOut(BaseModel):
    """A ``409`` body. ``code`` is ``stale`` when what the person was shown changed since
    (show it again, then retry); ``None`` for any other refusal."""

    detail: str
    code: str | None = None


STALE_CODE = "stale"
"""The ``code`` of a ``409`` refused only because what was shown is no longer current."""
STALE: Responses = {
    409: {
        "model": RefusedOut,
        "description": "Refused in the campaign's state; the detail says why. ``code`` is "
        "``stale`` when only a fingerprint went stale: show it again and retry",
    }
}


class ActivateIn(BaseModel):
    """When the campaign starts (#338)."""

    starts_at: AwareDatetime | None = None
    """The scheduled start: nothing is sent before it. Left out, the next Tuesday at
    09:00 in your time zone. A time already past starts the campaign now."""


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


class MessagePreviewOut(BaseModel):
    """One message of a step, rendered for a pending enrollment. ``blocked`` says why
    it cannot be sent, or is null. ``approved``: an approval covers it."""

    enrollment_id: int
    contact_id: int
    contact_name: str
    to_address: str | None
    subject: str | None
    body: str | None
    issues: list[LintIssueOut]
    blocked: str | None
    approved: bool
    fingerprint: str


class StepReviewOut(BaseModel):
    """One step's review. ``messages`` is the page from ``offset`` of the ``total`` in
    the pager; ``blocked`` lists every message that cannot be sent. ``per_message``:
    the template uses ``{{ personal_line }}``, so each message is approved on its own,
    and ``unapproved`` counts those not approved yet."""

    step_id: int
    position: int
    channel: str
    template_name: str
    fingerprint: str
    per_message: bool
    approved: bool
    total: int
    offset: int
    messages: list[MessagePreviewOut]
    blocked: list[MessagePreviewOut]
    unapproved: int


class StepApproveIn(BaseModel):
    """The step ``fingerprint`` its review came with."""

    fingerprint: Annotated[str, Field(max_length=64)]


class MessageApprovalIn(BaseModel):
    enrollment_id: int
    fingerprint: Annotated[str, Field(max_length=64)]


class MessagesApproveIn(BaseModel):
    """Each message with the ``fingerprint`` it came with."""

    messages: Annotated[
        list[MessageApprovalIn], Field(min_length=1, max_length=service.APPROVE_MAX)
    ]


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
    except service.ReviewStale as exc:
        raise ApiError(409, RefusedOut(detail=str(exc), code=STALE_CODE).model_dump()) from exc
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


def _message_out(m: service.MessagePreview) -> MessagePreviewOut:
    return MessagePreviewOut(
        enrollment_id=m.enrollment_id,
        contact_id=m.contact_id,
        contact_name=m.contact_name,
        to_address=m.to_address,
        subject=m.subject,
        body=m.body,
        issues=[LintIssueOut.model_validate(i.to_json()) for i in m.issues],
        blocked=m.blocked,
        approved=m.approved,
        fingerprint=m.fingerprint,
    )


def _step_review_out(r: service.StepReview) -> StepReviewOut:
    return StepReviewOut(
        step_id=r.step_id,
        position=r.position,
        channel=r.channel.value,
        template_name=r.template_name,
        fingerprint=r.fingerprint,
        per_message=r.per_message,
        approved=r.approved,
        total=r.total,
        offset=r.offset,
        messages=[_message_out(m) for m in r.messages],
        blocked=[_message_out(m) for m in r.blocked],
        unapproved=r.unapproved,
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


@router.get("/campaigns/{campaign_id}/review/steps/{step_id}", responses={**NOT_FOUND, **CONFLICT})
def review_step(
    campaign_id: int,
    step_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=service.STEP_PAGE_MAX)] = service.STEP_PAGE,
) -> StepReviewOut:
    """One step's review: a page of its rendered messages to page through, every
    blocked message, and the ``fingerprint`` an approval of the step is given for."""
    with translate_errors():
        return _step_review_out(
            service.review_step(
                session,
                user,
                campaign_id,
                step_id,
                me=_me(request),
                now=utcnow(),
                offset=offset,
                limit=limit,
            )
        )


@router.post(
    "/campaigns/{campaign_id}/review/steps/{step_id}/approve", responses={**NOT_FOUND, **STALE}
)
def approve_step(
    campaign_id: int,
    step_id: int,
    body: StepApproveIn,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
) -> ReviewOut:
    """Approve every message of the step at once, for the ``fingerprint`` its review came
    with. It covers messages rendered later too, until the step or its template changes,
    and never a blocked one. Refused for a step that uses ``{{ personal_line }}``."""
    with translate_errors():
        service.approve_step(
            session,
            user,
            campaign_id,
            step_id,
            fingerprint_seen=body.fingerprint,
            me=_me(request),
            now=utcnow(),
        )
        return _review(session, user, campaign_id, _me(request))


@router.post(
    "/campaigns/{campaign_id}/review/steps/{step_id}/messages/approve",
    responses={**NOT_FOUND, **STALE},
)
def approve_messages(
    campaign_id: int,
    step_id: int,
    body: MessagesApproveIn,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
) -> ReviewOut:
    """Approve messages of a step that uses ``{{ personal_line }}`` one by one, each for
    the ``fingerprint`` it was shown with. Refused for any other step."""
    with translate_errors():
        service.approve_messages(
            session,
            user,
            campaign_id,
            step_id,
            {m.enrollment_id: m.fingerprint for m in body.messages},
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


@router.post("/campaigns/{campaign_id}/review/guards/acknowledge", responses={**NOT_FOUND, **STALE})
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
        # The client first (it reads the Keychain), then the arming, read last before the
        # Gmail call. The access token is fetched lazily, inside that call.
        gmail = _opener(request)(user.id, plan.mailbox_id)
        with session_scope(factory) as session:
            arming = service.test_send_arming(session, user, plan.mailbox_id)
        if arming is None:
            raise HTTPException(status_code=409, detail="the mailbox is no longer armed")
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


@router.post(
    "/campaigns/{campaign_id}/activate",
    responses={
        **NOT_FOUND,
        **REFUSED,
        422: {"description": "The send schedule (time zone or holidays) cannot be read"},
    },
)
def activate_campaign(
    campaign_id: int,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    body: Annotated[ActivateIn | None, Body()] = None,
) -> ReviewOut:
    """``reviewing`` to ``active``, starting at ``starts_at`` (#338): ``409`` with
    ``missing`` unless every review requirement is recorded and current, checked in
    this one writer transaction. Nothing of the campaign is sent before its start."""
    now = utcnow()
    try:
        starts_at = campaign_service.resolve_start(
            user,
            settings=_settings(request),
            now=now,
            starts_at=None if body is None else body.starts_at,
        )
    except campaign_service.InvalidCampaign as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with translate_errors():
        service.activate(
            session,
            user,
            campaign_id,
            settings=_settings(request),
            me=_me(request),
            now=now,
            starts_at=starts_at,
        )
        return _review(session, user, campaign_id, _me(request))
