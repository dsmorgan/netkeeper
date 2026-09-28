"""The review gate and the test send (spec 11.8; P3-09).

A campaign moves from ``draft`` to ``reviewing`` (:func:`start_review`) once it
has a step and an audience (a ``pending`` enrollment). It is activated only
through :func:`activate`, which refuses with :class:`ReviewIncomplete`, listing
every requirement not met, unless each of these is recorded and still current:

- ``sample_previews``: the rendered previews of a sample of up to
  :data:`SAMPLE_SIZE` pending enrollments, drawn by the server
  (:func:`draw_sample`), each approved (:func:`approve`).
- ``searched_previews``: every other enrollment whose previews the person
  looked up (:func:`view`), approved too.
- ``test_sends``: a test send of each email step (:func:`prepare_test_send`,
  then :func:`record_test_send`).
- ``lint``: every step's template found free of lint errors (:func:`record_lint`).
- ``guards``: the guard summary acknowledged (:func:`acknowledge_guards`).

**Invalidated on change.** Every record carries the fingerprint of what it was
made for, and counts only while that fingerprint is still the current one:

- A step's fingerprint (:func:`step_fingerprint`) covers the step's own fields,
  its template's text, the campaign's name and mailbox, and the ``[me]``
  values. A test send counts for its step's fingerprint.
- The content fingerprint (:func:`content_fingerprint`) covers every step's.
  Preview approvals and the lint record count for it, so any change to a step or
  a template, or a step added or removed, undoes them.
- The audience fingerprint (:func:`audience_fingerprint`) covers the pending
  enrollments, the audience's source and its contacts. The sample and the guard
  acknowledgement count for it. The acknowledged summary must also still be the
  one the guards give now.

**Test sends are never campaign messages.** A test send goes only to the
campaign mailbox's own address, is rendered for an enrollment but addressed to
nobody else, has a ``[Test]`` subject, and is recorded in
``campaign_test_sends``, never in ``messages`` or ``interactions``. The caps,
the recency guard and the engine read only those two, so a test send never
counts toward a cap or recency and never advances an enrollment. It needs the
mailbox armed for **send** (#277), checked before any Gmail call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import secrets
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

from sqlalchemy.orm import Session, selectinload

from netkeeper.campaigns.compose import ComposeError, build_message
from netkeeper.campaigns.render import (
    LintIssue,
    MergeValues,
    Rendered,
    TemplateRenderError,
    render,
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
    ReviewPreview,
    Template,
    TemplateChannel,
    TestSend,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import campaign_engine, mailboxes
from netkeeper.services.campaign_guards import (
    UNSENDABLE_EMAIL_STATUSES,
    check_enrollment,
    excluded_summary,
)

log = logging.getLogger(__name__)

SAMPLE_SIZE: Final = 10
"""Spec 11.8: rendered previews of 10 sampled enrollments."""

VIEW_MAX: Final = 50
"""The most enrollments one :func:`view` call renders."""

TEST_SUBJECT_PREFIX: Final = "[Test] "

LIST_PAGE: Final = 1000


class ReviewError(Exception):
    """Base of the review gate's refusals."""


class ReviewNotFound(ReviewError, LookupError):
    """No such campaign, step or enrollment for this user."""


class ReviewConflict(ReviewError, ValueError):
    """Refused in the campaign's current state; the message says why."""


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


def _source_contact_ids(session: Session, user: User, campaign: Campaign) -> set[int]:
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
    source = _source_contact_ids(session, user, campaign)
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
    ids = _source_contact_ids(session, user, campaign) | {
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


def _rows(session: Session, user: User, campaign_id: int) -> list[ReviewPreview]:
    return list(
        session.scalars(
            scoped(user, ReviewPreview)
            .where(ReviewPreview.campaign_id == campaign_id)
            .order_by(ReviewPreview.enrollment_id)
            .execution_options(populate_existing=True)
        )
    )


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


# --- previews -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepPreview:
    position: int
    channel: TemplateChannel
    to_address: str | None
    subject: str | None
    body: str | None
    issues: tuple[LintIssue, ...]
    error: str | None


@dataclass(frozen=True, slots=True)
class EnrollmentPreview:
    enrollment_id: int
    contact_id: int
    contact_name: str
    sampled: bool
    approved: bool
    fingerprint: str
    steps: tuple[StepPreview, ...]


@dataclass(frozen=True, slots=True)
class Previews:
    content_fingerprint: str
    enrollments: tuple[EnrollmentPreview, ...] = field(default=())


def _contact(session: Session, user: User, contact_id: int) -> Contact | None:
    return session.scalars(
        scoped(user, Contact)
        .where(Contact.id == contact_id)
        .options(selectinload(Contact.emails), selectinload(Contact.positions))
        .execution_options(populate_existing=True)
    ).first()


def preview_fingerprint(
    session: Session, user: User, content: str, contact_id: int, today: date
) -> str:
    """What one enrollment's previews show: the content fingerprint, the contact's merge
    values and its sendable address. An approval counts only while this is unchanged, so
    editing the contact (a name, an email) undoes it."""
    contact = _contact(session, user, contact_id)
    if contact is None:
        return _digest([content, None])
    email = sendable_email(contact, refuse=UNSENDABLE_EMAIL_STATUSES)
    return _digest(
        [content, contact_fields(contact, today), None if email is None else email.email]
    )


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


def _preview(
    session: Session,
    user: User,
    campaign: Campaign,
    steps: Sequence[CampaignStep],
    row: ReviewPreview,
    enrollment: Enrollment,
    content: str,
    me: Mapping[str, str],
    today: date,
) -> EnrollmentPreview:
    contact = _contact(session, user, enrollment.contact_id)
    shown: list[StepPreview] = []
    for step in steps:
        address = None
        if contact is not None and step.channel is TemplateChannel.EMAIL:
            email = sendable_email(contact, refuse=UNSENDABLE_EMAIL_STATUSES)
            address = None if email is None else email.email
        try:
            if contact is None:
                raise TemplateRenderError("the contact is gone")
            rendered = _render_step(
                _template(session, user, step), contact, campaign, step, me, today
            )
        except TemplateRenderError as exc:
            shown.append(
                StepPreview(step.position, step.channel, address, None, None, (), str(exc))
            )
            continue
        shown.append(
            StepPreview(
                step.position,
                step.channel,
                address,
                rendered.subject,
                rendered.body,
                rendered.issues,
                None,
            )
        )
    name = (
        ""
        if contact is None
        else f"{contact.preferred_name or contact.first_name} {contact.last_name}".strip()
    )
    fingerprint = preview_fingerprint(session, user, content, enrollment.contact_id, today)
    return EnrollmentPreview(
        enrollment.id,
        enrollment.contact_id,
        name,
        row.sampled,
        row.approved_at is not None and row.approved_fingerprint == fingerprint,
        fingerprint,
        tuple(shown),
    )


def _previews(
    session: Session,
    user: User,
    campaign: Campaign,
    rows: Sequence[ReviewPreview],
    me: Mapping[str, str],
    today: date,
) -> Previews:
    content = content_fingerprint(session, user, campaign, me)
    steps = _steps(session, user, campaign.id)
    out = []
    for row in rows:
        enrollment = get_scoped(session, user, Enrollment, row.enrollment_id)
        if enrollment is not None:
            out.append(
                _preview(session, user, campaign, steps, row, enrollment, content, me, today)
            )
    return Previews(content, tuple(out))


def _sample_is_current(rows: Sequence[ReviewPreview], audience: str) -> bool:
    sample = [r for r in rows if r.sampled]
    return bool(sample) and all(r.sample_fingerprint == audience for r in sample)


def draw_sample(
    session: Session,
    user: User,
    campaign_id: int,
    *,
    me: Mapping[str, str],
    now: datetime,
    rng: random.Random | None = None,
) -> Previews:
    """The sample's previews. The sample is drawn once per audience: while the audience
    fingerprint is unchanged, the same sample comes back; after a change, a new one."""
    _require_writer(session, "draw_sample")
    campaign = _reviewing(session, user, campaign_id)
    pending, audience = _audience(session, user, campaign)
    rows = _rows(session, user, campaign_id)
    if not _sample_is_current(rows, audience):
        # The old draw stays as viewed previews: each still has to be approved, as any
        # preview the person looked at does, unless it is drawn again.
        for old in rows:
            if old.sampled:
                old.sampled = False
                old.sample_fingerprint = None
        chooser = rng if rng is not None else random.SystemRandom()
        chosen = sorted(chooser.sample([e.id for e in pending], min(SAMPLE_SIZE, len(pending))))
        existing = {r.enrollment_id: r for r in _rows(session, user, campaign_id)}
        for enrollment_id in chosen:
            row = existing.get(enrollment_id)
            if row is None:
                row = ReviewPreview(
                    user_id=user.id,
                    campaign_id=campaign_id,
                    enrollment_id=enrollment_id,
                    viewed_at=now,
                )
                session.add(row)
            row.sampled = True
            row.sample_fingerprint = audience
        session.flush()
        log.info("campaign %d: sample of %d drawn", campaign_id, len(chosen))
    sample = [r for r in _rows(session, user, campaign_id) if r.sampled]
    for row in sample:
        row.viewed_at = now
    session.flush()
    return _previews(session, user, campaign, sample, me, now.date())


def view(
    session: Session,
    user: User,
    campaign_id: int,
    enrollment_ids: Collection[int],
    *,
    me: Mapping[str, str],
    now: datetime,
) -> Previews:
    """The previews of enrollments the person looked up. Each one viewed must be
    approved before activation, as the sample must."""
    _require_writer(session, "view")
    campaign = _reviewing(session, user, campaign_id)
    ids = sorted(set(enrollment_ids))
    if not ids or len(ids) > VIEW_MAX:
        raise ReviewConflict(f"view between 1 and {VIEW_MAX} enrollments at a time")
    pending = {e.id for e in _pending(session, user, campaign_id)}
    unknown = [i for i in ids if i not in pending]
    if unknown:
        raise ReviewNotFound(f"no pending enrollment {unknown[0]} in campaign {campaign_id}")
    existing = {r.enrollment_id: r for r in _rows(session, user, campaign_id)}
    rows = []
    for enrollment_id in ids:
        row = existing.get(enrollment_id)
        if row is None:
            row = ReviewPreview(
                user_id=user.id,
                campaign_id=campaign_id,
                enrollment_id=enrollment_id,
                sampled=False,
                viewed_at=now,
            )
            session.add(row)
        row.viewed_at = now
        rows.append(row)
    session.flush()
    return _previews(session, user, campaign, rows, me, now.date())


def approve(
    session: Session,
    user: User,
    campaign_id: int,
    seen: Mapping[int, str],
    *,
    me: Mapping[str, str],
    now: datetime,
) -> list[ReviewPreview]:
    """Approve previews the person viewed. ``seen`` maps each enrollment to the
    ``fingerprint`` its preview came with: refused when a step, a template or the
    contact changed since."""
    _require_writer(session, "approve")
    campaign = _reviewing(session, user, campaign_id)
    ids = sorted(seen)
    if not ids:
        raise ReviewConflict("approve at least one enrollment")
    content = content_fingerprint(session, user, campaign, me)
    rows = {r.enrollment_id: r for r in _rows(session, user, campaign_id)}
    pending = {e.id: e for e in _pending(session, user, campaign_id)}
    current: dict[int, str] = {}
    for enrollment_id in ids:
        if enrollment_id not in pending:
            raise ReviewConflict(f"enrollment {enrollment_id} is not pending in this campaign")
        if enrollment_id not in rows:
            raise ReviewConflict(f"enrollment {enrollment_id}'s previews were not viewed")
        current[enrollment_id] = preview_fingerprint(
            session, user, content, pending[enrollment_id].contact_id, now.date()
        )
        if seen[enrollment_id] != current[enrollment_id]:
            raise ReviewConflict(
                f"enrollment {enrollment_id}'s previews changed since they were viewed;"
                " view them again"
            )
    for enrollment_id in ids:
        rows[enrollment_id].approved_at = now
        rows[enrollment_id].approved_fingerprint = current[enrollment_id]
    session.flush()
    return [rows[i] for i in sorted(ids)]


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
        raise ReviewConflict("the audience changed since the summary was shown; look again")
    if summary_seen != summary:
        raise ReviewConflict(f"the guard results changed; they are now: {summary}")
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
    """A test send of an email step, rendered for one of the campaign's enrollments
    (the given one, else the first pending) and addressed to the campaign mailbox's
    own address, never the contact's.

    Refused (:class:`ReviewConflict`) unless the mailbox is ``ok`` and armed for send.
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
    if mailbox.arm is not MailboxArm.SEND:
        raise ReviewConflict(
            f"{mailbox.email} is not armed for send; a test send needs `gmail arm --send`"
        )
    if mailbox.status is not MailboxStatus.OK:
        raise ReviewConflict(f"{mailbox.email} is {mailbox.status}")
    pending = _pending(session, user, campaign_id)
    if enrollment_id is None:  # the first sampled one, else the first pending one
        sampled = {r.enrollment_id for r in _rows(session, user, campaign_id) if r.sampled}
        enrollment = next((e for e in pending if e.id in sampled), pending[0] if pending else None)
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
    try:
        message = build_message(
            to=to,
            subject=TEST_SUBJECT_PREFIX + rendered.subject,
            body=rendered.body,
            message_id=f"<{secrets.token_hex(16)}@{to.rpartition('@')[2]}>",
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
        message,
    )


def still_armed_for_send(session: Session, user: User, mailbox_id: int) -> bool:
    """Read again just before the Gmail call, as the sender does before each write."""
    return mailboxes.armed(session, user, mailbox_id) is MailboxArm.SEND


def record_test_send(
    session: Session,
    user: User,
    plan: TestSendPlan,
    *,
    gmail_message_id: str | None,
    now: datetime,
) -> TestSend:
    """Record a test send Gmail accepted. Writes nothing but this row and the campaign's
    ``test_sent_at``: no message, no interaction, no enrollment change."""
    _require_writer(session, "record_test_send")
    campaign = get_campaign(session, user, plan.campaign_id)
    row = TestSend(
        user_id=user.id,
        campaign_id=plan.campaign_id,
        step_id=plan.step_id,
        fingerprint=plan.fingerprint,
        to_address=plan.to_address,
        gmail_message_id=gmail_message_id,
        sent_at=now,
    )
    session.add(row)
    campaign.test_sent_at = now
    session.flush()
    log.info("campaign %d: step %d test sent", plan.campaign_id, plan.step_position)
    return row


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
    live = {e.id: e for e in pending}
    rows = _rows(session, user, campaign.id)

    def unapproved(candidates: Sequence[ReviewPreview]) -> tuple[int, ...]:
        return tuple(
            r.enrollment_id
            for r in candidates
            if r.approved_at is None
            or r.enrollment_id not in live
            or r.approved_fingerprint
            != preview_fingerprint(
                session, user, content, live[r.enrollment_id].contact_id, now.date()
            )
        )

    if not live:
        out.append(Missing("audience", "nobody is enrolled"))
    if not _sample_is_current(rows, audience):
        out.append(Missing("sample_previews", "no sample was drawn for the current audience"))
    elif ids := unapproved([r for r in rows if r.sampled]):
        out.append(Missing("sample_previews", "sampled previews not approved", enrollment_ids=ids))
    if ids := unapproved([r for r in rows if not r.sampled and r.enrollment_id in live]):
        out.append(Missing("searched_previews", "viewed previews not approved", enrollment_ids=ids))
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
