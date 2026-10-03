"""The review gate and the test send (spec 11.8; P3-09).

A campaign moves from ``draft`` to ``reviewing`` (:func:`start_review`) once it
has a step and an audience (a ``pending`` enrollment). It is activated only
through :func:`activate`, which refuses with :class:`ReviewIncomplete`, listing
every requirement not met, unless each of these is recorded and still current:

- ``step_approvals``: every step approved once, as a whole (:func:`approve_step`),
  after paging through its rendered messages (:func:`review_step`).
- ``message_approvals``: for a step whose template uses ``{{ personal_line }}``,
  which differs for every contact, each pending enrollment's message approved on
  its own (:func:`approve_messages`) instead (#339).
- ``test_sends``: a test send of each email step (:func:`prepare_test_send`,
  then :func:`record_test_send`).
- ``lint``: every step's template found free of lint errors (:func:`record_lint`).
- ``guards``: the guard summary acknowledged (:func:`acknowledge_guards`).
- ``mailbox``: with an email step, the campaign's mailbox still ``ok``, not
  ``reauth_required`` or ``disabled`` since its test send (#299).

**Invalidated on change.** Every record carries the fingerprint of what it was
made for, and counts only while that fingerprint is still the current one:

- A step's fingerprint (:func:`step_fingerprint`) covers the step's own fields,
  its template's text, the campaign's name and mailbox, and the ``[me]``
  values. A test send counts for its step's fingerprint.
- A step approval counts for its step's fingerprint, so a change to the step or
  its template (an edit, or a new version) undoes it. While it holds, it covers
  every message of the step, including messages rendered later for contacts
  enrolled or edited since, but never a blocked one: a message that fails to
  render, has a lint error, or whose contact a guard excludes stays blocked, and
  the engine checks each of those again when the step fires.
- A single message's approval counts for :func:`message_fingerprint`: the step's
  fingerprint with the contact's merge values and address.
- The content fingerprint (:func:`content_fingerprint`) covers every step's.
  The lint record counts for it.
- The audience fingerprint (:func:`audience_fingerprint`) covers the pending
  enrollments, the audience's source and its contacts. The guard
  acknowledgement counts for it; a step approval does not, since it covers the
  messages of contacts enrolled later. The acknowledged summary must also still be the
  one the guards give now.

**Test sends are never campaign messages.** A test send goes only to the
campaign mailbox's own address, is rendered for an enrollment but addressed to
nobody else, has a ``[Test]`` subject, and is recorded in
``campaign_test_sends``, never in ``messages`` or ``interactions``. The caps,
the recency guard and the engine read only those two, so a test send never
counts toward a cap or recency and never advances an enrollment.

**The test follows the mailbox's arming** (#277, #304), read when the test is
prepared and again just before the Gmail call (:func:`test_send_arming`), which
decides: armed for **send**, it is sent; armed for **drafts** only, it becomes a
Gmail draft (``drafts.create``, never ``messages.send``) in the person's Drafts;
disarmed, it is refused. A test draft counts for the review exactly as a test
send does, and the drafts check searches for its Message-ID
(:func:`test_drafts_to_verify`), so a fresh mailbox can be verified, and armed to
send, before any campaign is active. netkeeper never deletes a test draft.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from sqlalchemy.orm import Session, selectinload

from netkeeper.campaigns.compose import ComposeError, build_message
from netkeeper.campaigns.render import (
    LintIssue,
    MergeValues,
    Rendered,
    Severity,
    TemplateRenderError,
    render,
    uses_personal_line,
)
from netkeeper.campaigns.templates import activation_errors, contact_fields
from netkeeper.config import Settings
from netkeeper.crm import lists as crm_lists
from netkeeper.crm.contacts import sendable_email
from netkeeper.crm.filters import compile_filter, parse_filter
from netkeeper.db import is_writer
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Contact,
    Enrollment,
    EnrollmentStatus,
    Mailbox,
    MailboxArm,
    MailboxStatus,
    StepApproval,
    Template,
    TemplateChannel,
    TestSend,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import campaign_engine, mailboxes
from netkeeper.services.campaign_guards import (
    UNSENDABLE_EMAIL_STATUSES,
    GuardPolicy,
    check_contact,
    check_enrollment,
    excluded_summary,
    load_facts,
    reason_label,
)

log = logging.getLogger(__name__)

STEP_PAGE: Final = 20
"""How many of a step's messages :func:`review_step` answers by default."""

STEP_PAGE_MAX: Final = 50
"""The most of a step's messages one :func:`review_step` call answers."""

APPROVE_MAX: Final = 50
"""The most messages one :func:`approve_messages` call approves."""

TEST_SUBJECT_PREFIX: Final = "[Test] "

LIST_PAGE: Final = 1000


class ReviewError(Exception):
    """Base of the review gate's refusals."""


class ReviewNotFound(ReviewError, LookupError):
    """No such campaign, step or enrollment for this user."""


class ReviewConflict(ReviewError, ValueError):
    """Refused in the campaign's current state; the message says why."""


class ReviewStale(ReviewConflict):
    """Refused because what the person was shown is no longer current: a preview's or the
    guard summary's fingerprint changed since. Showing it again and retrying can succeed,
    unlike any other :class:`ReviewConflict` (#299)."""


@dataclass(frozen=True, slots=True)
class Missing:
    """One activation requirement not met, and why."""

    requirement: str
    detail: str
    enrollment_ids: tuple[int, ...] = ()
    step_positions: tuple[int, ...] = ()


class ReviewIncomplete(ReviewError):
    """Activation refused: :attr:`missing` lists every requirement not met."""

    def __init__(self, missing: Sequence[Missing]) -> None:
        self.missing = tuple(missing)
        super().__init__(
            "the review is not complete: " + ", ".join(m.requirement for m in self.missing)
        )


# --- fingerprints -------------------------------------------------------------------


def _digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def step_fingerprint(
    step: CampaignStep, template: Template | None, campaign: Campaign, me: Mapping[str, str]
) -> str:
    """What a test send of ``step`` shows: see the module docstring."""
    return _digest(
        {
            "step": [
                step.id,
                step.position,
                step.channel,
                step.mode,
                step.delay_days,
                step.condition,
                step.same_thread,
                step.template_id,
            ],
            "template": None
            if template is None
            else [template.id, template.channel, template.subject, template.body],
            "campaign": [campaign.name, campaign.mailbox_id],
            "me": dict(me),
        }
    )


def _steps(session: Session, user: User, campaign_id: int) -> list[CampaignStep]:
    return list(
        session.scalars(
            scoped(user, CampaignStep)
            .where(CampaignStep.campaign_id == campaign_id)
            .order_by(CampaignStep.position)
            .execution_options(populate_existing=True)
        )
    )


def _template(session: Session, user: User, step: CampaignStep) -> Template | None:
    return get_scoped(session, user, Template, step.template_id)


def content_fingerprint(
    session: Session, user: User, campaign: Campaign, me: Mapping[str, str]
) -> str:
    """Every step's fingerprint, in order: what a preview shows."""
    return _digest(
        [
            step_fingerprint(step, _template(session, user, step), campaign, me)
            for step in _steps(session, user, campaign.id)
        ]
    )


def _pending(session: Session, user: User, campaign_id: int) -> list[Enrollment]:
    return list(
        session.scalars(
            scoped(user, Enrollment)
            .where(
                Enrollment.campaign_id == campaign_id,
                Enrollment.status == EnrollmentStatus.PENDING,
            )
            .order_by(Enrollment.id)
            .execution_options(populate_existing=True)
        )
    )


def source_contact_ids(session: Session, user: User, campaign: Campaign) -> set[int]:
    """The contacts of the campaign's audience source: its list or its filter."""
    if campaign.filter_json is not None:
        tree = parse_filter(campaign.filter_json)
        return set(
            session.scalars(
                compile_filter(user, tree, session=session).with_only_columns(Contact.id)
            )
        )
    if campaign.source_list_id is None:
        return set()
    found: set[int] = set()
    offset = 0
    while True:
        page, total = crm_lists.list_members(
            session, user, campaign.source_list_id, limit=LIST_PAGE, offset=offset
        )
        found.update(contact.id for contact in page)
        offset += LIST_PAGE
        if offset >= total or not page:
            return found


def _audience(session: Session, user: User, campaign: Campaign) -> tuple[list[Enrollment], str]:
    """The pending enrollments and the audience fingerprint."""
    pending = _pending(session, user, campaign.id)
    source = source_contact_ids(session, user, campaign)
    fingerprint = _digest(
        {
            "source": [campaign.source_list_id, campaign.filter_json],
            "contacts": sorted(source),
            "pending": [[e.id, e.contact_id] for e in pending],
        }
    )
    return pending, fingerprint


def audience_fingerprint(session: Session, user: User, campaign: Campaign) -> str:
    return _audience(session, user, campaign)[1]


def guard_summary(session: Session, user: User, campaign: Campaign, *, now: datetime) -> str:
    """Spec 11.8's line over the audience: its source's contacts and the pending ones.

    From :func:`~netkeeper.services.campaign_guards.check_enrollment` and
    :func:`~netkeeper.services.campaign_guards.excluded_summary`, never restated.
    """
    ids = source_contact_ids(session, user, campaign) | {
        e.contact_id for e in _pending(session, user, campaign.id)
    }
    verdicts = check_enrollment(session, user, campaign, ids, now=now)
    return excluded_summary(verdicts, contacted_within_days=campaign.contacted_within_days_guard)


# --- reading ------------------------------------------------------------------------


def get_campaign(session: Session, user: User, campaign_id: int) -> Campaign:
    campaign = session.scalars(
        scoped(user, Campaign)
        .where(Campaign.id == campaign_id)
        .execution_options(populate_existing=True)
    ).first()
    if campaign is None:
        raise ReviewNotFound(f"no campaign {campaign_id}")
    return campaign


def _reviewing(session: Session, user: User, campaign_id: int) -> Campaign:
    campaign = get_campaign(session, user, campaign_id)
    if campaign.status is not CampaignStatus.REVIEWING:
        raise ReviewConflict(f"campaign {campaign_id} is {campaign.status}, not reviewing")
    return campaign


def _require_writer(session: Session, what: str) -> None:
    if not is_writer(session):
        raise RuntimeError(f"{what} needs a writer session (session_scope(..., write=True))")


# --- the transition -----------------------------------------------------------------


def start_review(session: Session, user: User, campaign_id: int) -> Campaign:
    """``draft`` to ``reviewing``: needs at least one step and one pending enrollment."""
    _require_writer(session, "start_review")
    campaign = get_campaign(session, user, campaign_id)
    if campaign.status is not CampaignStatus.DRAFT:
        raise ReviewConflict(f"campaign {campaign_id} is {campaign.status}, not draft")
    if not _steps(session, user, campaign_id):
        raise ReviewConflict(f"campaign {campaign_id} has no steps")
    if not _pending(session, user, campaign_id):
        raise ReviewConflict(f"campaign {campaign_id} has no audience: nobody is enrolled")
    campaign.status = CampaignStatus.REVIEWING
    session.flush()
    log.info("campaign %d is reviewing", campaign_id)
    return campaign


# --- step review: the pager, the blocked list, the approvals ------------------------


def _contact(session: Session, user: User, contact_id: int) -> Contact | None:
    return session.scalars(
        scoped(user, Contact)
        .where(Contact.id == contact_id)
        .options(selectinload(Contact.emails), selectinload(Contact.positions))
        .execution_options(populate_existing=True)
    ).first()


def _render_step(
    template: Template | None,
    contact: Contact,
    campaign: Campaign,
    step: CampaignStep,
    me: Mapping[str, str],
    today: date,
) -> Rendered:
    """The step as the engine renders it at a fire, less the previous send's date."""
    if template is None:
        raise TemplateRenderError("the step's template is gone")
    values = MergeValues(
        contact=contact_fields(contact, today),
        me=me,
        campaign_name=campaign.name,
        step_number=step.position,
    )
    return render(template.channel, template.subject, template.body, values, today=today)


@dataclass(frozen=True, slots=True)
class MessagePreview:
    """One pending enrollment's message for one step, rendered as the engine would.

    ``blocked`` says why it cannot be sent (it fails to render, has a lint
    error, or a guard excludes the contact), or is None. ``approved`` is whether
    an approval covers it: the step's, or for a ``personal_line`` step its own,
    never for a blocked message of a step approved as a whole. ``fingerprint`` is
    what approving this one message is checked against (:func:`approve_messages`).
    """

    enrollment_id: int
    contact_id: int
    contact_name: str
    to_address: str | None
    subject: str | None
    body: str | None
    issues: tuple[LintIssue, ...]
    blocked: str | None
    approved: bool
    fingerprint: str


@dataclass(frozen=True, slots=True)
class StepReview:
    """One step's review: its approval, one page of its messages, and its blocked ones.

    ``total`` is how many messages the pager holds, and ``messages`` the page from
    ``offset``. A step approved as a whole pages through the messages that can be
    sent; ``blocked`` lists the rest. A ``per_message`` step (its template uses
    ``{{ personal_line }}``) pages through every pending enrollment, blocked ones
    too, since each must be approved on its own.
    """

    step_id: int
    position: int
    channel: TemplateChannel
    template_name: str
    fingerprint: str
    per_message: bool
    approved: bool
    total: int
    offset: int
    messages: tuple[MessagePreview, ...]
    blocked: tuple[MessagePreview, ...]
    unapproved: int


def _step(session: Session, user: User, campaign_id: int, step_id: int) -> CampaignStep:
    step = session.scalars(
        scoped(user, CampaignStep)
        .where(CampaignStep.id == step_id, CampaignStep.campaign_id == campaign_id)
        .execution_options(populate_existing=True)
    ).first()
    if step is None:
        raise ReviewNotFound(f"no step {step_id} in campaign {campaign_id}")
    return step


def step_at(session: Session, user: User, campaign_id: int, position: int) -> CampaignStep:
    """The campaign's step at ``position`` (1 is the first)."""
    get_campaign(session, user, campaign_id)
    step = next((s for s in _steps(session, user, campaign_id) if s.position == position), None)
    if step is None:
        raise ReviewNotFound(f"no step {position} in campaign {campaign_id}")
    return step


def is_per_message(template: Template | None) -> bool:
    """Whether the step's messages must each be approved: its template names
    ``{{ personal_line }}``, which differs for every contact (#339)."""
    return template is not None and uses_personal_line(
        template.channel, template.subject, template.body
    )


def _contacts(session: Session, user: User, contact_ids: Collection[int]) -> dict[int, Contact]:
    if not contact_ids:
        return {}
    found = session.scalars(
        scoped(user, Contact)
        .where(Contact.id.in_(sorted(set(contact_ids))))
        .options(selectinload(Contact.emails), selectinload(Contact.positions))
        .execution_options(populate_existing=True)
    )
    return {contact.id: contact for contact in found}


def _contact_name(contact: Contact | None) -> str:
    if contact is None:
        return ""
    return f"{contact.preferred_name or contact.first_name} {contact.last_name}".strip()


def _address(contact: Contact | None) -> str | None:
    if contact is None:
        return None
    email = sendable_email(contact, refuse=UNSENDABLE_EMAIL_STATUSES)
    return None if email is None else email.email


def message_fingerprint(step_print: str, contact: Contact | None, today: date) -> str:
    """What one message of a step shows: the step's fingerprint, the contact's merge
    values and its sendable address. An approval of the message counts only while
    this is unchanged, so editing the step, its template or the contact undoes it."""
    if contact is None:
        return _digest([step_print, None])
    return _digest([step_print, contact_fields(contact, today), _address(contact)])


def _approvals(
    session: Session, user: User, campaign_id: int, step_id: int | None = None
) -> list[StepApproval]:
    query = scoped(user, StepApproval).where(StepApproval.campaign_id == campaign_id)
    if step_id is not None:
        query = query.where(StepApproval.step_id == step_id)
    return list(
        session.scalars(query.order_by(StepApproval.id).execution_options(populate_existing=True))
    )


def _step_approved(rows: Sequence[StepApproval], step_id: int, fingerprint: str) -> bool:
    return any(
        r.step_id == step_id and r.enrollment_id is None and r.fingerprint == fingerprint
        for r in rows
    )


def _messages_approved(rows: Sequence[StepApproval], step_id: int) -> dict[int, str]:
    """Each enrollment's approved message fingerprint for the step."""
    return {
        r.enrollment_id: r.fingerprint
        for r in rows
        if r.step_id == step_id and r.enrollment_id is not None
    }


def _render_messages(
    session: Session,
    user: User,
    campaign: Campaign,
    step: CampaignStep,
    template: Template | None,
    pending: Sequence[Enrollment],
    me: Mapping[str, str],
    now: datetime,
    *,
    step_approved: bool,
    approved_messages: Mapping[int, str],
) -> list[MessagePreview]:
    """The step's message for each pending enrollment, in enrollment order, each with
    the reason it is blocked, if it is. The guards are the ones that decide at a step
    fire, for this step's channel; the render is the engine's."""
    today = now.date()
    contacts = _contacts(session, user, [e.contact_id for e in pending])
    facts = load_facts(session, user, list(contacts), campaign_id=campaign.id)
    policy = GuardPolicy(contacted_within_days=campaign.contacted_within_days_guard)
    step_print = step_fingerprint(step, template, campaign, me)
    per_message = is_per_message(template)
    out: list[MessagePreview] = []
    for enrollment in pending:
        contact = contacts.get(enrollment.contact_id)
        address = _address(contact) if step.channel is TemplateChannel.EMAIL else None
        subject: str | None = None
        body: str | None = None
        issues: tuple[LintIssue, ...] = ()
        blocked: str | None = None
        verdict = check_contact(
            facts.get(enrollment.contact_id), enrollment.contact_id, step.channel, policy, now=now
        )
        if contact is None:
            blocked = "the contact is gone"
        else:
            try:
                rendered = _render_step(template, contact, campaign, step, me, today)
            except TemplateRenderError as exc:
                blocked = f"does not render: {exc}"
            else:
                subject, body, issues = rendered.subject, rendered.body, rendered.issues
                errors = [i for i in issues if i.severity is Severity.ERROR]
                if errors:
                    blocked = f"lint error: {errors[0].message}"
                elif step.channel is TemplateChannel.EMAIL and not subject:
                    blocked = "renders with no subject"
        if blocked is None and verdict.reason is not None:
            label = reason_label(
                verdict.reason, contacted_within_days=campaign.contacted_within_days_guard
            )
            blocked = f"excluded by a guard: {label}"
        fingerprint = message_fingerprint(step_print, contact, today)
        if per_message:
            approved = approved_messages.get(enrollment.id) == fingerprint
        else:
            approved = step_approved and blocked is None
        out.append(
            MessagePreview(
                enrollment.id,
                enrollment.contact_id,
                _contact_name(contact),
                address,
                subject,
                body,
                issues,
                blocked,
                approved,
                fingerprint,
            )
        )
    return out


def review_step(
    session: Session,
    user: User,
    campaign_id: int,
    step_id: int,
    *,
    me: Mapping[str, str],
    now: datetime,
    offset: int = 0,
    limit: int = STEP_PAGE,
) -> StepReview:
    """One step's review, for any campaign of the user's: whether it is approved, one
    page of its messages, and every blocked one. Reads only."""
    if offset < 0 or not 1 <= limit <= STEP_PAGE_MAX:
        raise ReviewConflict(f"page from an offset of 0 or more, 1 to {STEP_PAGE_MAX} at a time")
    campaign = get_campaign(session, user, campaign_id)
    step = _step(session, user, campaign_id, step_id)
    template = _template(session, user, step)
    fingerprint = step_fingerprint(step, template, campaign, me)
    per_message = is_per_message(template)
    rows = _approvals(session, user, campaign_id, step.id)
    approved = not per_message and _step_approved(rows, step.id, fingerprint)
    messages = _render_messages(
        session,
        user,
        campaign,
        step,
        template,
        _pending(session, user, campaign_id),
        me,
        now,
        step_approved=approved,
        approved_messages=_messages_approved(rows, step.id),
    )
    blocked = tuple(m for m in messages if m.blocked is not None)
    pager = messages if per_message else [m for m in messages if m.blocked is None]
    return StepReview(
        step_id=step.id,
        position=step.position,
        channel=step.channel,
        template_name="" if template is None else f"{template.name} v{template.version}",
        fingerprint=fingerprint,
        per_message=per_message,
        approved=approved,
        total=len(pager),
        offset=offset,
        messages=tuple(pager[offset : offset + limit]),
        blocked=blocked,
        unapproved=sum(1 for m in messages if not m.approved) if per_message else 0,
    )


def approve_step(
    session: Session,
    user: User,
    campaign_id: int,
    step_id: int,
    *,
    fingerprint_seen: str,
    me: Mapping[str, str],
    now: datetime,
) -> StepApproval:
    """Approve every message of a step at once, for the step ``fingerprint`` its review
    came with: refused (:class:`ReviewStale`) when the step or its template changed
    since. The approval covers the step's messages rendered later too, while that
    fingerprint holds, but never a blocked one. Refused for a step whose template uses
    ``{{ personal_line }}``: its messages are approved one by one."""
    _require_writer(session, "approve_step")
    campaign = _reviewing(session, user, campaign_id)
    step = _step(session, user, campaign_id, step_id)
    template = _template(session, user, step)
    if is_per_message(template):
        raise ReviewConflict(
            f"step {step.position} uses {{{{ personal_line }}}}, so each of its messages"
            " is approved on its own"
        )
    fingerprint = step_fingerprint(step, template, campaign, me)
    if fingerprint_seen != fingerprint:
        raise ReviewStale(
            f"step {step.position} changed since it was shown (its template, its settings or"
            " the campaign's); review it again"
        )
    row = next(
        (r for r in _approvals(session, user, campaign_id, step.id) if r.enrollment_id is None),
        None,
    )
    if row is None:
        row = StepApproval(
            user_id=user.id, campaign_id=campaign_id, step_id=step.id, enrollment_id=None
        )
        session.add(row)
    row.fingerprint = fingerprint
    row.approved_at = now
    session.flush()
    log.info("campaign %d: step %d approved", campaign_id, step.position)
    return row


def approve_messages(
    session: Session,
    user: User,
    campaign_id: int,
    step_id: int,
    seen: Mapping[int, str],
    *,
    me: Mapping[str, str],
    now: datetime,
) -> list[StepApproval]:
    """Approve single messages of a ``personal_line`` step. ``seen`` maps each pending
    enrollment to the ``fingerprint`` its message came with: refused
    (:class:`ReviewStale`) when the step, its template or the contact changed since.
    Refused for any other step, which is approved as a whole (:func:`approve_step`)."""
    _require_writer(session, "approve_messages")
    campaign = _reviewing(session, user, campaign_id)
    step = _step(session, user, campaign_id, step_id)
    template = _template(session, user, step)
    if not is_per_message(template):
        raise ReviewConflict(
            f"step {step.position} is approved as a whole; only a step that uses"
            " {{ personal_line }} has its messages approved one by one"
        )
    ids = sorted(seen)
    if not ids or len(ids) > APPROVE_MAX:
        raise ReviewConflict(f"approve between 1 and {APPROVE_MAX} messages at a time")
    pending = {e.id: e for e in _pending(session, user, campaign_id)}
    unknown = [i for i in ids if i not in pending]
    if unknown:
        raise ReviewConflict(f"enrollment {unknown[0]} is not pending in this campaign")
    step_print = step_fingerprint(step, template, campaign, me)
    contacts = _contacts(session, user, [pending[i].contact_id for i in ids])
    current = {
        i: message_fingerprint(step_print, contacts.get(pending[i].contact_id), now.date())
        for i in ids
    }
    for enrollment_id in ids:
        if seen[enrollment_id] != current[enrollment_id]:
            raise ReviewStale(
                f"enrollment {enrollment_id}'s message for step {step.position} changed since"
                " it was shown; review it again"
            )
    rows = {r.enrollment_id: r for r in _approvals(session, user, campaign_id, step.id)}
    out = []
    for enrollment_id in ids:
        row = rows.get(enrollment_id)
        if row is None:
            row = StepApproval(
                user_id=user.id,
                campaign_id=campaign_id,
                step_id=step.id,
                enrollment_id=enrollment_id,
            )
            session.add(row)
        row.fingerprint = current[enrollment_id]
        row.approved_at = now
        out.append(row)
    session.flush()
    log.info("campaign %d: %d messages of step %d approved", campaign_id, len(ids), step.position)
    return out


# --- lint and guards ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LintResult:
    clean: bool
    steps: tuple[tuple[int, tuple[LintIssue, ...]], ...]


def record_lint(
    session: Session, user: User, campaign_id: int, *, me: Mapping[str, str], now: datetime
) -> LintResult:
    """Lint every step's template; recorded only when none has an error."""
    _require_writer(session, "record_lint")
    campaign = _reviewing(session, user, campaign_id)
    found = _lint_errors(session, user, campaign_id, me)
    clean = not any(issues for _, issues in found)
    if clean:
        campaign.lint_checked_at = now
        campaign.lint_fingerprint = content_fingerprint(session, user, campaign, me)
        session.flush()
    return LintResult(clean, found)


def _lint_errors(
    session: Session, user: User, campaign_id: int, me: Mapping[str, str]
) -> tuple[tuple[int, tuple[LintIssue, ...]], ...]:
    out = []
    for step in _steps(session, user, campaign_id):
        template = _template(session, user, step)
        if template is None:
            raise ReviewConflict(f"step {step.position}'s template is gone")
        out.append((step.position, tuple(activation_errors(template, me.keys()))))
    return tuple(out)


def acknowledge_guards(
    session: Session,
    user: User,
    campaign_id: int,
    *,
    summary_seen: str,
    audience_fingerprint_seen: str,
    now: datetime,
) -> Campaign:
    """Acknowledge the guard summary the person was shown. Refused unless it is still the
    current summary for the current audience."""
    _require_writer(session, "acknowledge_guards")
    campaign = _reviewing(session, user, campaign_id)
    audience = audience_fingerprint(session, user, campaign)
    summary = guard_summary(session, user, campaign, now=now)
    if audience_fingerprint_seen != audience:
        raise ReviewStale("the audience changed since the summary was shown; look again")
    if summary_seen != summary:
        raise ReviewStale(f"the guard results changed; they are now: {summary}")
    campaign.guards_acknowledged_at = now
    campaign.guards_fingerprint = audience
    campaign.guards_summary = summary
    session.flush()
    return campaign


# --- the test send ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TestSendPlan:
    """Everything a test send needs, read in one session and sent with none open."""

    __test__ = False  # not a pytest class, whatever its name

    campaign_id: int
    step_id: int
    step_position: int
    mailbox_id: int
    to_address: str
    fingerprint: str
    rfc822_message_id: str
    message: Any  # email.message.EmailMessage


def prepare_test_send(
    session: Session,
    user: User,
    campaign_id: int,
    step_id: int,
    *,
    enrollment_id: int | None,
    me: Mapping[str, str],
    today: date,
) -> TestSendPlan:
    """A test send of an email step, rendered for one of the campaign's pending
    enrollments (the given one, else the first) and addressed to the campaign mailbox's
    own address, never the contact's.

    Refused (:class:`ReviewConflict`) unless the mailbox is ``ok`` and armed, for
    drafts or for send. Whether it is drafted or sent is decided just before the
    Gmail call, by :func:`test_send_arming`.
    """
    campaign = _reviewing(session, user, campaign_id)
    step = get_scoped(session, user, CampaignStep, step_id)
    if step is None or step.campaign_id != campaign_id:
        raise ReviewNotFound(f"no step {step_id} in campaign {campaign_id}")
    if step.channel is not TemplateChannel.EMAIL:
        raise ReviewConflict(f"step {step.position} is not an email step")
    mailbox = (
        None
        if campaign.mailbox_id is None
        else get_scoped(session, user, Mailbox, campaign.mailbox_id)
    )
    if mailbox is None:
        raise ReviewConflict(f"campaign {campaign_id} has no mailbox")
    if mailbox.arm is None:
        raise ReviewConflict(
            f"{mailbox.email} is not armed; arm it for drafts (`gmail arm`) to make the test"
            " a draft in your Drafts, or to send (`gmail arm --send`) to send it to you"
        )
    if mailbox.status is not MailboxStatus.OK:
        raise ReviewConflict(f"{mailbox.email} is {mailbox.status}")
    pending = _pending(session, user, campaign_id)
    if enrollment_id is None:  # the first pending one
        enrollment = pending[0] if pending else None
    else:
        enrollment = next((e for e in pending if e.id == enrollment_id), None)
    if enrollment is None:
        raise ReviewNotFound(f"no pending enrollment to render step {step.position} for")
    contact = _contact(session, user, enrollment.contact_id)
    template = _template(session, user, step)
    try:
        if contact is None:
            raise TemplateRenderError("the contact is gone")
        rendered = _render_step(template, contact, campaign, step, me, today)
    except TemplateRenderError as exc:
        raise ReviewConflict(f"step {step.position} does not render: {exc}") from exc
    if not rendered.subject:
        raise ReviewConflict(f"step {step.position} renders with no subject")
    to = mailbox.email
    rfc822_message_id = f"<{secrets.token_hex(16)}@{to.rpartition('@')[2]}>"
    try:
        message = build_message(
            to=to,
            subject=TEST_SUBJECT_PREFIX + rendered.subject,
            body=rendered.body,
            message_id=rfc822_message_id,
        )
    except ComposeError as exc:
        raise ReviewConflict(f"the test message cannot be built: {exc}") from exc
    return TestSendPlan(
        campaign_id,
        step.id,
        step.position,
        mailbox.id,
        to,
        step_fingerprint(step, template, campaign, me),
        rfc822_message_id,
        message,
    )


def test_send_arming(session: Session, user: User, mailbox_id: int) -> MailboxArm | None:
    """The mailbox's arming, read again just before the Gmail call, as the sender reads it
    before each write. It decides the test: ``send`` sends it, ``draft`` drafts it, None
    (disarmed since it was prepared) refuses it.

    A mailbox armed to send after the test was prepared sends it: the message is the
    same either way, addressed only to the mailbox itself, and sending is what the
    person armed it for. One taken back to drafts drafts it, so a test is never sent
    on a mailbox not armed for send at the moment of the call.
    """
    return mailboxes.armed(session, user, mailbox_id)


def record_test_send(
    session: Session,
    user: User,
    plan: TestSendPlan,
    *,
    gmail_message_id: str | None,
    gmail_draft_id: str | None = None,
    now: datetime,
) -> TestSend:
    """Record a test Gmail accepted: sent, or drafted when ``gmail_draft_id`` is given.
    Either counts for the step's fingerprint. Writes nothing but this row and the
    campaign's ``test_sent_at``: no message, no interaction, no enrollment change."""
    _require_writer(session, "record_test_send")
    campaign = get_campaign(session, user, plan.campaign_id)
    row = TestSend(
        user_id=user.id,
        campaign_id=plan.campaign_id,
        step_id=plan.step_id,
        fingerprint=plan.fingerprint,
        to_address=plan.to_address,
        gmail_message_id=gmail_message_id,
        gmail_draft_id=gmail_draft_id,
        rfc822_message_id=plan.rfc822_message_id,
        sent_at=now,
    )
    session.add(row)
    campaign.test_sent_at = now
    session.flush()
    log.info(
        "campaign %d: step %d test %s",
        plan.campaign_id,
        plan.step_position,
        "sent" if gmail_draft_id is None else "drafted",
    )
    return row


@dataclass(frozen=True, slots=True)
class TestDraftCheck:
    """A test draft whose Message-ID the drafts check searches for (#304)."""

    __test__ = False  # not a pytest class, whatever its name

    mailbox_id: int
    test_send_id: int
    rfc822_message_id: str


def test_drafts_to_verify(session: Session, user: User) -> list[TestDraftCheck]:
    """The user's test drafts to search for by Message-ID: on each armed mailbox not yet
    verified, its :func:`~netkeeper.services.mailboxes.recent_test_drafts`, newest first.
    Nothing for a disarmed or verified mailbox."""
    out: list[TestDraftCheck] = []
    unverified = session.scalars(
        scoped(user, Mailbox)
        .where(Mailbox.armed_at.is_not(None), Mailbox.message_id_verified_at.is_(None))
        .order_by(Mailbox.id)
        .execution_options(populate_existing=True)
    )
    for mailbox in list(unverified):
        out.extend(
            TestDraftCheck(mailbox.id, row.id, row.rfc822_message_id)
            for row in mailboxes.recent_test_drafts(session, user, mailbox)
            if row.rfc822_message_id is not None
        )
    return out


def record_test_drafts_not_found(
    session: Session, user: User, test_send_ids: Collection[int], *, now: datetime
) -> None:
    """The drafts check searched for these test drafts and found none of them."""
    _require_writer(session, "record_test_drafts_not_found")
    for row in session.scalars(scoped(user, TestSend).where(TestSend.id.in_(test_send_ids))):
        row.not_found_at = now
    session.flush()


# --- the gate -----------------------------------------------------------------------


def missing(
    session: Session, user: User, campaign: Campaign, *, me: Mapping[str, str], now: datetime
) -> list[Missing]:
    """Every activation requirement not recorded, or recorded for something since changed."""
    out: list[Missing] = []
    if campaign.status is not CampaignStatus.REVIEWING:
        out.append(Missing("reviewing", f"the campaign is {campaign.status}, not reviewing"))
    steps = _steps(session, user, campaign.id)
    content = content_fingerprint(session, user, campaign, me)
    pending, audience = _audience(session, user, campaign)
    if not pending:
        out.append(Missing("audience", "nobody is enrolled"))
    approvals = _approvals(session, user, campaign.id)
    whole: list[int] = []
    per_message: list[int] = []
    unapproved: set[int] = set()
    contacts: dict[int, Contact] | None = None
    for step in steps:
        template = _template(session, user, step)
        fingerprint = step_fingerprint(step, template, campaign, me)
        if not is_per_message(template):
            if not _step_approved(approvals, step.id, fingerprint):
                whole.append(step.position)
            continue
        if contacts is None:
            contacts = _contacts(session, user, [e.contact_id for e in pending])
        approved = _messages_approved(approvals, step.id)
        late = {
            e.id
            for e in pending
            if approved.get(e.id)
            != message_fingerprint(fingerprint, contacts.get(e.contact_id), now.date())
        }
        if late:
            per_message.append(step.position)
            unapproved |= late
    if whole:
        out.append(Missing("step_approvals", "steps not approved", step_positions=tuple(whole)))
    if per_message:
        out.append(
            Missing(
                "message_approvals",
                "messages of steps that use {{ personal_line }} not approved one by one",
                enrollment_ids=tuple(sorted(unapproved)),
                step_positions=tuple(per_message),
            )
        )
    sent = set(
        session.scalars(
            scoped(user, TestSend)
            .with_only_columns(TestSend.fingerprint)
            .where(TestSend.campaign_id == campaign.id)
        )
    )
    untested = tuple(
        step.position
        for step in steps
        if step.channel is TemplateChannel.EMAIL
        and step_fingerprint(step, _template(session, user, step), campaign, me) not in sent
    )
    if untested:
        out.append(
            Missing("test_sends", "email steps with no current test send", step_positions=untested)
        )
    if gap := mailbox_gap(session, user, campaign):
        out.append(Missing("mailbox", gap))
    if not steps:
        out.append(Missing("lint", "the campaign has no steps"))
    elif campaign.lint_fingerprint != content:
        out.append(Missing("lint", "no lint result for the current steps and templates"))
    elif errors := tuple(p for p, issues in _lint_errors(session, user, campaign.id, me) if issues):
        out.append(Missing("lint", "templates with lint errors", step_positions=errors))
    if campaign.guards_fingerprint != audience:
        out.append(
            Missing("guards", "the guard summary for the current audience is not acknowledged")
        )
    elif campaign.guards_summary != (summary := guard_summary(session, user, campaign, now=now)):
        out.append(
            Missing(
                "guards",
                f"the guard results changed since they were acknowledged: {summary}"
                f" (acknowledged: {campaign.guards_summary})",
            )
        )
    return out


def mailbox_gap(session: Session, user: User, campaign: Campaign) -> str | None:
    """Why the campaign's mailbox cannot send now, or None when it is ``ok`` or the
    campaign has no email step (#299): one that went ``reauth_required`` or ``disabled``
    after its test send blocks activation, and resuming
    (:func:`netkeeper.services.campaigns.resume`)."""
    steps = _steps(session, user, campaign.id)
    if not any(step.channel is TemplateChannel.EMAIL for step in steps):
        return None
    mailbox = (
        None
        if campaign.mailbox_id is None
        else session.scalars(
            scoped(user, Mailbox)
            .where(Mailbox.id == campaign.mailbox_id)
            .execution_options(populate_existing=True)
        ).first()
    )
    if mailbox is None:
        return "the campaign has email steps and no mailbox"
    if mailbox.status is not MailboxStatus.OK:
        return f"{mailbox.email} is {mailbox.status}, not ok"
    return None


def activate(
    session: Session,
    user: User,
    campaign_id: int,
    *,
    settings: Settings,
    me: Mapping[str, str],
    now: datetime,
) -> Campaign:
    """The only way a campaign should go ``active``: :class:`ReviewIncomplete` unless every
    requirement is met, then ``approved_at`` and
    :func:`netkeeper.services.campaign_engine.activate`, in the caller's writer transaction."""
    _require_writer(session, "activate")
    campaign = get_campaign(session, user, campaign_id)
    gaps = missing(session, user, campaign, me=me, now=now)
    if gaps:
        raise ReviewIncomplete(gaps)
    campaign.approved_at = now
    session.flush()
    try:
        return campaign_engine.activate(
            session,
            user,
            campaign_id,
            settings=settings,
            now=now,
            gate=campaign_engine.REVIEW_GATE,
        )
    except campaign_engine.CampaignEngineError as exc:
        raise ReviewConflict(str(exc)) from exc
