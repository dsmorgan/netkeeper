"""A campaign step adopts the newest version of its template (#397).

Editing a template that an active or paused campaign uses makes a new version
(:func:`netkeeper.campaigns.templates.update_template`), and the step keeps the
version it was activated with. This module lets a person have the step use the
newest version instead. It changes what an in-flight campaign sends, so it is
built around one rule and a confirm.

**The rule: a message that exists keeps its text.** Every message a step produced
holds the text it was rendered with (``messages.body_rendered``, spec 11.1): a
message ``scheduled`` (claimed, its send or prefill under way), ``drafted``,
``prefilled``, ``sent``, or any other status. Adoption re-points the step and
nothing else: it never reads, renders again, or writes a message, so an open
prefill, an auto-send in progress, or a Gmail send already claimed goes on with the
text it has. The engine never fires a step twice for an enrollment
(:func:`netkeeper.services.campaign_engine.step_has_message`), so a step that
already fired for an enrollment is never sent again in the new version either.
Only enrollments the step has not fired for yet render the new version, when it
fires for them.

**The confirm.** :func:`preview` reads only. It shows the two versions and their
diff, the new version's lint (errors refuse it, as at activation; warnings are
shown), the step's messages rendered in the new version as the review renders them
(a few samples, and every one that is blocked, which is never sent), which
enrollments will get it, the messages that keep their text, and the enrollments a
template error parked that adoption releases. :func:`adopt` takes the preview's
``fingerprint`` back and is refused (:class:`AdoptionStale`) when the step, either
version, or the campaign changed since. The confirm is the step's approval in the
new version (spec 11.8, #339): :func:`adopt` records a whole-step approval for the
step's new fingerprint, the lint record when every step is now clean, and who
adopted it and when (``campaign_steps.template_adopted_*``).

**Refused** (:attr:`AdoptionPreview.refusal`) unless:

- the campaign is ``active`` or ``paused``. A ``draft`` or ``reviewing`` campaign
  sees a template's edits already (an edit of a template no campaign past ``draft``
  uses changes it in place); a ``completed`` or ``archived`` one sends nothing more;
- a newer version exists;
- the newer version is on the step's channel (an edit can change a template's
  channel) and free of lint errors (:func:`~netkeeper.campaigns.templates.activation_errors`,
  the activation gate: for LinkedIn, a body over the length limit or with a character
  the prefill cannot type, besides the rules every template has). A LinkedIn subject
  (#448) and a long typing time are warnings there, so they are here: the subject is
  never rendered, and a message that renders too long to type is blocked for its
  contact, listed in the preview, and parked at the claim. The step's mode
  does not matter: a ``prefill`` and an ``auto_send`` step take the same checks, and
  every check a claim runs still runs at each fire;
- the newer version does not use ``{{ personal_line }}``: its messages are approved
  one by one in the review (#339), which an active campaign has left.

**Parked enrollments.** An enrollment the step's template blocked waits with
``not_sent_error`` starting ``blocked:`` and no due time (#342). Adoption clears the
reason and makes it due at once, so the next tick, or the next prefill, tries the
step again with every check, in the new version. A ``paused`` campaign still fires
nothing until it is resumed.

Every function that writes needs a writer session (CLAUDE.md).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, exists, func
from sqlalchemy.orm import Session

from netkeeper.campaigns import templates as template_service
from netkeeper.campaigns.render import LintIssue, Severity, lint
from netkeeper.db import is_writer
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Enrollment,
    EnrollmentStatus,
    Message,
    MessageDirection,
    MessageStatus,
    StepApproval,
    Template,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import campaign_review
from netkeeper.services.campaigns import CampaignConflict, CampaignNotFound

log = logging.getLogger(__name__)

ADOPTABLE: Final[frozenset[CampaignStatus]] = frozenset(
    {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}
)
"""A campaign whose steps can adopt a newer template version: activated, not over."""

STILL_TO_SEND: Final[frozenset[EnrollmentStatus]] = frozenset(
    {EnrollmentStatus.PENDING, EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED}
)
"""An enrollment in one of these may still be sent a step it has not had yet."""

OPEN_STATUSES: Final[frozenset[MessageStatus]] = frozenset(
    {MessageStatus.SCHEDULED, MessageStatus.DRAFTED, MessageStatus.PREFILLED}
)
"""A message still in flight: claimed, a Gmail draft, or typed into LinkedIn's composer."""

PARKED_PREFIX: Final = "blocked:"
"""How ``not_sent_error`` starts for an enrollment its step's template blocked (#342)."""

LIST_MAX: Final = 100
"""The most enrollments, or open messages, a preview names; the totals count them all."""

SAMPLE_MAX: Final = 3
"""How many of the step's messages, rendered in the new version, a preview shows."""

RENDER_MAX: Final = 2000
"""The most affected enrollments a preview renders, to find the blocked ones."""


class AdoptionStale(CampaignConflict):
    """The step, a version, or the campaign changed since the preview; preview it again."""


@dataclass(frozen=True, slots=True)
class VersionText:
    template_id: int
    name: str
    version: int
    subject: str | None
    body: str


@dataclass(frozen=True, slots=True)
class EnrollmentRef:
    enrollment_id: int
    contact_id: int
    contact_name: str
    status: EnrollmentStatus


@dataclass(frozen=True, slots=True)
class OpenMessage:
    """A message of the step still in flight. It keeps the text it was rendered with."""

    message_id: int
    enrollment_id: int
    contact_name: str
    status: MessageStatus


@dataclass(frozen=True, slots=True)
class AdoptionPreview:
    campaign_id: int
    campaign_status: CampaignStatus
    step_id: int
    position: int
    current: VersionText
    newest: VersionText
    """The same as ``current`` when nothing newer exists."""
    diff: str
    """A unified diff from ``current`` to ``newest``, subject first."""
    errors: tuple[LintIssue, ...]
    """The newest version's lint errors: any one refuses the adoption."""
    warnings: tuple[LintIssue, ...]
    refusal: str | None
    """Why the step cannot adopt ``newest``; None when it can."""
    affected_total: int
    """Enrollments the step has not fired for that may still get it: they get ``newest``."""
    affected: tuple[EnrollmentRef, ...]
    """The first :data:`LIST_MAX` of them."""
    released: int
    """Of those, enrollments the template parked (``blocked:``), due again on adopting."""
    kept: dict[MessageStatus, int]
    """The step's outbound messages by status. Each keeps the text it has."""
    open_messages: tuple[OpenMessage, ...]
    """The first :data:`LIST_MAX` of the step's messages still in flight."""
    samples: tuple[campaign_review.MessagePreview, ...]
    """A few affected enrollments' messages, rendered in ``newest`` as the review would."""
    blocked_total: int
    """Affected enrollments whose message in ``newest`` is blocked, as the review finds it
    (it does not render, a lint error once rendered, such as a LinkedIn message too long
    to type, or a guard): never sent, whatever the adoption."""
    blocked: tuple[campaign_review.MessagePreview, ...]
    """The first :data:`LIST_MAX` of them, each with why."""
    fingerprint: str
    """What :func:`adopt` checks the confirm against."""


@dataclass(frozen=True, slots=True)
class AdoptionResult:
    campaign_id: int
    step_id: int
    position: int
    from_version: int
    to_version: int
    template_id: int
    released: int
    affected_total: int
    adopted_at: datetime


def _require_writer(session: Session, what: str) -> None:
    if not is_writer(session):
        raise RuntimeError(f"{what} needs a writer session (session_scope(..., write=True))")


def _campaign(session: Session, user: User, campaign_id: int) -> Campaign:
    campaign = session.scalars(
        scoped(user, Campaign)
        .where(Campaign.id == campaign_id)
        .execution_options(populate_existing=True)
    ).first()
    if campaign is None:
        raise CampaignNotFound(f"no campaign {campaign_id}")
    return campaign


def _step(session: Session, user: User, campaign_id: int, step_id: int) -> CampaignStep:
    step = session.scalars(
        scoped(user, CampaignStep)
        .where(CampaignStep.id == step_id, CampaignStep.campaign_id == campaign_id)
        .execution_options(populate_existing=True)
    ).first()
    if step is None:
        raise CampaignNotFound(f"no step {step_id} in campaign {campaign_id}")
    return step


def _text(row: Template) -> VersionText:
    return VersionText(row.id, row.name, row.version, row.subject, row.body)


def _diff(current: Template, newest: Template) -> str:
    def lines(row: Template) -> list[str]:
        subject = [] if row.subject is None else [f"Subject: {row.subject}", ""]
        return [*subject, *row.body.splitlines()]

    return "\n".join(
        difflib.unified_diff(
            lines(current),
            lines(newest),
            fromfile=f"v{current.version}",
            tofile=f"v{newest.version}",
            lineterm="",
        )
    )


def _fingerprint(
    campaign: Campaign, step: CampaignStep, current: Template, newest: Template
) -> str:
    """The step as it is, and the version it would adopt."""
    value = [
        campaign.status,
        campaign_review.step_fingerprint(step, current, campaign),
        [newest.id, newest.version, newest.channel, newest.subject, newest.body],
    ]
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _no_message_for(step: CampaignStep) -> ColumnElement[bool]:
    """SQL: the enrollment has no outbound message of ``step``, of any status."""
    return ~exists().where(
        Message.user_id == step.user_id,
        Message.enrollment_id == Enrollment.id,
        Message.step_id == step.id,
        Message.direction == MessageDirection.OUT,
    )


def _affected(session: Session, user: User, step: CampaignStep) -> list[Enrollment]:
    """Enrollments that may still get ``step`` and have no message of it: the ones a new
    version reaches. A merge can leave an enrollment holding the step's message from the
    other contact, so the message, not ``current_step`` alone, decides (#242 review)."""
    return list(
        session.scalars(
            scoped(user, Enrollment)
            .where(
                Enrollment.campaign_id == step.campaign_id,
                Enrollment.status.in_(sorted(STILL_TO_SEND)),
                func.coalesce(Enrollment.current_step, 0) < step.position,
                _no_message_for(step),
            )
            .order_by(Enrollment.id)
            .execution_options(populate_existing=True)
        )
    )


def _parked(enrollment: Enrollment, step: CampaignStep) -> bool:
    """Waiting on ``step``'s template: parked with a ``blocked:`` reason (#342)."""
    return (
        enrollment.status is EnrollmentStatus.ACTIVE
        and (enrollment.current_step or 0) + 1 == step.position
        and enrollment.next_action_at is None
        and (enrollment.not_sent_error or "").startswith(PARKED_PREFIX)
    )


def _refusal(
    campaign: Campaign,
    step: CampaignStep,
    current: Template,
    newest: Template,
    errors: Sequence[LintIssue],
) -> str | None:
    if campaign.status not in ADOPTABLE:
        return (
            f"campaign {campaign.id} is {campaign.status}; only an active or paused campaign's"
            " step adopts a new template version"
        )
    if newest.id == current.id:
        return f"step {step.position} already uses the newest version (v{current.version})"
    if newest.channel is not step.channel:
        return (
            f"v{newest.version} is a {newest.channel} template and step {step.position} is"
            f" {step.channel}; a step keeps its channel"
        )
    if errors:
        rules = ", ".join(sorted({issue.rule.value for issue in errors}))
        return f"v{newest.version} has lint errors ({rules}); fix the template first"
    if campaign_review.is_per_message(newest):
        return (
            f"v{newest.version} uses {{{{ personal_line }}}}, whose messages are approved one"
            " by one in a review; an active or paused campaign cannot adopt it"
        )
    return None


def preview(
    session: Session, user: User, campaign_id: int, step_id: int, *, now: datetime
) -> AdoptionPreview:
    """What adopting the newest version would change, and whether it is allowed. Reads only."""
    campaign = _campaign(session, user, campaign_id)
    step = _step(session, user, campaign_id, step_id)
    current = get_scoped(session, user, Template, step.template_id)
    if current is None:  # the foreign key keeps it; a guard, not a case
        raise CampaignConflict(f"step {step.position}'s template is gone")
    newest = template_service.newest_version(session, user, current)
    issues = lint(newest.channel, newest.subject, newest.body)
    errors = tuple(i for i in issues if i.severity is Severity.ERROR)
    warnings = tuple(i for i in issues if i.severity is not Severity.ERROR)
    affected = _affected(session, user, step)
    contacts = campaign_review._contacts(session, user, [e.contact_id for e in affected[:LIST_MAX]])
    kept: Counter[MessageStatus] = Counter()
    for status, n in session.execute(
        scoped(user, Message)
        .with_only_columns(Message.status, func.count(Message.id))
        .where(Message.step_id == step.id, Message.direction == MessageDirection.OUT)
        .group_by(Message.status)
    ):
        kept[status] += n
    open_rows = list(
        session.scalars(
            scoped(user, Message)
            .where(
                Message.step_id == step.id,
                Message.direction == MessageDirection.OUT,
                Message.status.in_(sorted(OPEN_STATUSES)),
            )
            .order_by(Message.id)
            .limit(LIST_MAX)
        )
    )
    open_contacts = campaign_review._contacts(session, user, [m.contact_id for m in open_rows])
    refusal = _refusal(campaign, step, current, newest, errors)
    rendered: list[campaign_review.MessagePreview] = []
    if newest.id != current.id and newest.channel is step.channel and not errors:
        # As the review renders them (spec 11.8): the engine's render, and the guards that
        # decide at a fire. A blocked one is never sent; the claim checks it again.
        rendered = campaign_review._render_messages(
            session,
            user,
            campaign,
            step,
            newest,
            affected[:RENDER_MAX],
            now,
            step_approved=False,
            approved_messages={},
        )
    blocked = [m for m in rendered if m.blocked is not None]
    return AdoptionPreview(
        campaign_id=campaign.id,
        campaign_status=campaign.status,
        step_id=step.id,
        position=step.position,
        current=_text(current),
        newest=_text(newest),
        diff=_diff(current, newest),
        errors=errors,
        warnings=warnings,
        refusal=refusal,
        affected_total=len(affected),
        affected=tuple(
            EnrollmentRef(
                e.id,
                e.contact_id,
                campaign_review._contact_name(contacts.get(e.contact_id)),
                e.status,
            )
            for e in affected[:LIST_MAX]
        ),
        released=sum(1 for e in affected if _parked(e, step)),
        kept=dict(kept),
        open_messages=tuple(
            OpenMessage(
                m.id,
                m.enrollment_id,
                campaign_review._contact_name(open_contacts.get(m.contact_id)),
                m.status,
            )
            for m in open_rows
        ),
        samples=tuple(m for m in rendered if m.blocked is None)[:SAMPLE_MAX],
        blocked_total=len(blocked),
        blocked=tuple(blocked[:LIST_MAX]),
        fingerprint=_fingerprint(campaign, step, current, newest),
    )


def adopt(
    session: Session,
    user: User,
    campaign_id: int,
    step_id: int,
    *,
    fingerprint_seen: str,
    now: datetime,
) -> AdoptionResult:
    """Have the step use the newest version of its template, as :func:`preview` showed it.

    Refused (:class:`~netkeeper.services.campaigns.CampaignConflict`) for any
    :attr:`AdoptionPreview.refusal`, and (:class:`AdoptionStale`) when the preview's
    fingerprint is no longer the current one. Writes the step, its approval, the lint
    record, and the parked enrollments it releases. Never a message: see the module.
    """
    _require_writer(session, "adopt")
    shown = preview(session, user, campaign_id, step_id, now=now)
    if shown.refusal is not None:
        raise CampaignConflict(shown.refusal)
    if fingerprint_seen != shown.fingerprint:
        raise AdoptionStale(
            f"step {shown.position} or its template changed since it was shown; look at the"
            " change again"
        )
    campaign = _campaign(session, user, campaign_id)
    step = _step(session, user, campaign_id, step_id)
    newest = get_scoped(session, user, Template, shown.newest.template_id)
    assert newest is not None  # the preview just read it, in this transaction
    step.template = newest
    step.template_id = newest.id
    step.template_adopted_at = now
    step.template_adopted_by = user.id
    step.template_adopted_from_version = shown.current.version
    session.flush()
    _approve(session, user, campaign, step, now=now)
    released = 0
    for enrollment in _affected(session, user, step):
        if _parked(enrollment, step):
            enrollment.not_sent_error = None
            enrollment.not_sent_count = 0
            enrollment.not_sent_since = None
            enrollment.next_action_at = now
            released += 1
    session.flush()
    log.info(
        "campaign %d: step %d adopted template %d v%d (was v%d); %d parked enrollments due again",
        campaign_id,
        step.position,
        newest.id,
        newest.version,
        shown.current.version,
        released,
    )
    return AdoptionResult(
        campaign_id=campaign_id,
        step_id=step.id,
        position=step.position,
        from_version=shown.current.version,
        to_version=newest.version,
        template_id=newest.id,
        released=released,
        affected_total=shown.affected_total,
        adopted_at=now,
    )


def _approve(
    session: Session, user: User, campaign: Campaign, step: CampaignStep, *, now: datetime
) -> None:
    """The confirm is the step's approval for its new fingerprint (spec 11.8, #339), and the
    lint record is made again when every step is now clean, as :func:`record_lint` would."""
    fingerprint = campaign_review.step_fingerprint(step, step.template, campaign)
    row = session.scalars(
        scoped(user, StepApproval).where(
            StepApproval.step_id == step.id, StepApproval.enrollment_id.is_(None)
        )
    ).first()
    if row is None:
        row = StepApproval(
            user_id=user.id, campaign_id=campaign.id, step_id=step.id, enrollment_id=None
        )
        session.add(row)
    row.fingerprint = fingerprint
    row.approved_at = now
    if not any(issues for _, issues in campaign_review._lint_errors(session, user, campaign.id)):
        campaign.lint_checked_at = now
        campaign.lint_fingerprint = campaign_review.content_fingerprint(session, user, campaign)
    session.flush()
