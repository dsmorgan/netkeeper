"""Campaigns as a whole: create, enroll, list, and status (P3-13).

The small layer the CLI (``netkeeper campaigns``) and ``/campaigns`` share, so
each command mirrors an endpoint. The state machine stays where it is:

- ``draft`` to ``reviewing`` and ``reviewing`` to ``active`` are the review
  gate's (:mod:`netkeeper.services.campaign_review`). Nothing here activates a
  campaign, and nothing here holds the engine's gate token.
- pause and resume are the engine's (:func:`campaign_engine.pause_campaign`,
  :func:`campaign_engine.resume_campaign`).
- enrollment is the engine's :func:`campaign_engine.enroll`, so the guards
  (spec 11.9) decide who joins, and only a ``draft`` or ``reviewing`` campaign
  takes anyone.

:func:`create_campaign` makes a ``draft`` from a mailbox and an ordered list of
templates. A step's channel is its template's. Its defaults follow spec 11.2's
sequence: the first step at once, each later one seven days on and only if
nobody replied, an email follow-up in the first email's thread. An email step
defaults to ``draft`` mode, and a LinkedIn ``auto_send`` step is refused unless
``[campaigns] linkedin_auto_send`` is on (spec 11.6).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy import func, or_, true
from sqlalchemy.orm import Session

from netkeeper.campaigns import templates as template_service
from netkeeper.config import Settings
from netkeeper.crm import lists as crm_lists
from netkeeper.crm.filters import FilterError, FilterTree, compile_filter
from netkeeper.db import is_writer
from netkeeper.models import (
    CAMPAIGN_NAME_MAX_LENGTH,
    Campaign,
    CampaignStatus,
    CampaignStep,
    Contact,
    ContactEmail,
    Enrollment,
    EnrollmentStatus,
    Mailbox,
    MailboxStatus,
    Message,
    MessageDirection,
    MessageStatus,
    StepCondition,
    StepMode,
    TemplateChannel,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import campaign_engine, campaign_review
from netkeeper.services import mailboxes as mailbox_service

log = logging.getLogger(__name__)

MAX_STEPS: Final = 10
MAX_DELAY_DAYS: Final = 365
MAX_DAILY_CAP: Final = 400
"""Spec 11.4's hard maximum for a mailbox; a campaign's own cap never needs more."""

FOLLOW_UP_DELAY_DAYS: Final = 7
"""Spec 11.2: each step after the first, seven days after the one before."""

EMAIL_MODES: Final = frozenset({StepMode.DRAFT, StepMode.SEND})
LINKEDIN_MODES: Final = frozenset({StepMode.PREFILL, StepMode.AUTO_SEND})


class CampaignError(Exception):
    """Base of this module's refusals."""


class CampaignNotFound(CampaignError, LookupError):
    """No such campaign, template, mailbox or list for this user."""


class CampaignConflict(CampaignError, ValueError):
    """Refused in the campaign's state, or a name already taken."""


class InvalidCampaign(CampaignError, ValueError):
    """A value a campaign cannot be made from; the message says which."""


def _require_writer(session: Session, what: str) -> None:
    if not is_writer(session):
        raise RuntimeError(f"{what} needs a writer session (session_scope(..., write=True))")


def get_campaign(session: Session, user: User, campaign_id: int) -> Campaign:
    campaign = get_scoped(session, user, Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotFound(f"no campaign {campaign_id}")
    return campaign


# --- create -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepSpec:
    """One step to create. ``None`` takes the default (see the module docstring)."""

    template_id: int
    delay_days: int | None = None
    mode: StepMode | None = None
    condition: StepCondition | None = None
    same_thread: bool | None = None


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise InvalidCampaign("a campaign needs a name")
    if len(cleaned) > CAMPAIGN_NAME_MAX_LENGTH:
        raise InvalidCampaign(f"a campaign name is at most {CAMPAIGN_NAME_MAX_LENGTH} characters")
    return cleaned


def _step(
    session: Session,
    user: User,
    position: int,
    spec: StepSpec,
    *,
    earlier_email: bool,
    settings: Settings,
) -> CampaignStep:
    try:
        template = template_service.get_template(session, user, spec.template_id)
    except template_service.TemplateNotFound as exc:
        raise CampaignNotFound(f"step {position}: {exc}") from exc
    if template_service.is_superseded(session, user, template):
        raise InvalidCampaign(
            f"step {position}: template {template.id} is an older version; use its newest"
        )
    channel = template.channel
    email = channel is TemplateChannel.EMAIL
    mode = spec.mode or (StepMode.DRAFT if email else StepMode.PREFILL)
    if mode not in (EMAIL_MODES if email else LINKEDIN_MODES):
        raise InvalidCampaign(f"step {position}: a {channel.value} step cannot be {mode.value}")
    if mode is StepMode.AUTO_SEND and not settings.campaigns.linkedin_auto_send:
        raise InvalidCampaign(
            f"step {position}: auto_send needs [campaigns] linkedin_auto_send = true"
        )
    delay = spec.delay_days
    if delay is None:
        delay = 0 if position == 1 else FOLLOW_UP_DELAY_DAYS
    if not 0 <= delay <= MAX_DELAY_DAYS:
        raise InvalidCampaign(f"step {position}: the delay is 0 to {MAX_DELAY_DAYS} days")
    same_thread = earlier_email and email if spec.same_thread is None else spec.same_thread
    if same_thread and not (email and earlier_email):
        raise InvalidCampaign(
            f"step {position}: only an email step after an earlier email step can share its thread"
        )
    condition = spec.condition or (
        StepCondition.ALWAYS if position == 1 else StepCondition.NO_REPLY
    )
    return CampaignStep(
        user_id=user.id,
        position=position,
        channel=channel,
        template_id=template.id,
        delay_days=delay,
        mode=mode,
        condition=condition,
        same_thread=same_thread,
    )


def _check_filter(session: Session, user: User, tree: FilterTree) -> dict[str, object]:
    """A filter an audience can be read from, stored as ``lists.filter_json`` stores one."""
    try:
        crm_lists.check_list_references(session, user, tree)
        compile_filter(user, tree, session=session)
    except FilterError as exc:
        raise InvalidCampaign(f"the audience filter: {exc}") from exc
    return tree.model_dump(mode="json")


def _check_list(session: Session, user: User, list_id: int) -> int:
    try:
        return crm_lists.get_list(session, user, list_id).id
    except crm_lists.ListNotFound as exc:
        raise CampaignNotFound(str(exc)) from exc


def _mailbox(session: Session, user: User, mailbox_id: int) -> Mailbox:
    try:
        return mailbox_service.get_mailbox(session, user, mailbox_id)
    except mailbox_service.MailboxNotFound as exc:
        raise CampaignNotFound(str(exc)) from exc


def create_campaign(
    session: Session,
    user: User,
    *,
    name: str,
    steps: Sequence[StepSpec],
    settings: Settings,
    mailbox_id: int | None = None,
    list_id: int | None = None,
    filter: FilterTree | None = None,
    daily_cap: int | None = None,
) -> Campaign:
    """A new ``draft`` campaign with ``steps``, in order. Nobody is enrolled yet: see
    :func:`enroll`. ``list_id`` or ``filter`` (not both) is the audience's source."""
    _require_writer(session, "create_campaign")
    cleaned = _clean_name(name)
    if not 1 <= len(steps) <= MAX_STEPS:
        raise InvalidCampaign(f"a campaign has 1 to {MAX_STEPS} steps")
    if list_id is not None and filter is not None:
        raise InvalidCampaign("the audience is a list or a filter, not both")
    if daily_cap is not None and not 0 <= daily_cap <= MAX_DAILY_CAP:
        raise InvalidCampaign(f"the daily cap is 0 to {MAX_DAILY_CAP}")
    taken = session.scalar(
        scoped(user, Campaign).with_only_columns(Campaign.id).where(Campaign.name == cleaned)
    )
    if taken is not None:
        raise CampaignConflict(f"a campaign named {cleaned!r} already exists")
    mailbox = None if mailbox_id is None else _mailbox(session, user, mailbox_id)
    if mailbox is not None and mailbox.status is not MailboxStatus.OK:
        # A draft on a mailbox that cannot send would only fail at review or at the tick.
        raise CampaignConflict(f"{mailbox.email} is {mailbox.status}; reconnect it first")
    built: list[CampaignStep] = []
    for position, spec in enumerate(steps, start=1):
        earlier_email = any(s.channel is TemplateChannel.EMAIL for s in built)
        built.append(
            _step(session, user, position, spec, earlier_email=earlier_email, settings=settings)
        )
    if mailbox is None and any(s.channel is TemplateChannel.EMAIL for s in built):
        raise InvalidCampaign("a campaign with an email step needs a mailbox")
    campaign = Campaign(
        user_id=user.id,
        name=cleaned,
        status=CampaignStatus.DRAFT,
        mailbox_id=None if mailbox is None else mailbox.id,
        source_list_id=None if list_id is None else _check_list(session, user, list_id),
        filter_json=None if filter is None else _check_filter(session, user, filter),
        daily_cap=daily_cap,
        contacted_within_days_guard=settings.campaigns.contacted_within_days_guard,
    )
    campaign.steps.extend(built)
    session.add(campaign)
    session.flush()
    log.info("campaign %d created as a draft with %d steps", campaign.id, len(built))
    return campaign


# --- enroll -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnrollOutcome:
    campaign_id: int
    enrolled: int
    already: int
    excluded: int
    """Contacts this call considered and the guards kept out."""
    removed: int
    """Pending enrollments dropped because a new source no longer holds their contacts."""
    pending: int
    """Pending enrollments after the call: who the review will see."""
    summary: str
    """Spec 11.8's line over the audience, as the review shows it (its source's contacts
    and the pending ones)."""


def _drop_outside(session: Session, user: User, campaign_id: int, keep: set[int]) -> int:
    """Delete the campaign's pending enrollments whose contact is not in ``keep``.

    Only ever on a ``draft``: a pending enrollment has fired nothing, so no message
    names it, and a draft has no review records to lose. One that has a message
    anyway is left alone rather than deleted."""
    with_messages = scoped(user, Message).with_only_columns(Message.enrollment_id).scalar_subquery()
    stale = list(
        session.scalars(
            scoped(user, Enrollment).where(
                Enrollment.campaign_id == campaign_id,
                Enrollment.status == EnrollmentStatus.PENDING,
                Enrollment.contact_id.not_in(keep) if keep else true(),
                Enrollment.id.not_in(with_messages),
            )
        )
    )
    for enrollment in stale:
        session.delete(enrollment)
    session.flush()
    return len(stale)


def enroll(
    session: Session,
    user: User,
    campaign_id: int,
    *,
    now: datetime,
    list_id: int | None = None,
    filter: FilterTree | None = None,
    contact_ids: Sequence[int] = (),
) -> EnrollOutcome:
    """Enroll the campaign's audience as ``pending``, through the guards.

    ``list_id`` or ``filter`` replaces the campaign's audience source, and only on a
    ``draft``: the pending enrollments whose contacts the new source does not hold
    (and that are not among ``contact_ids``) are removed first, so the audience is
    the new source's. The source's contacts, and any ``contact_ids``, are then
    enrolled by :func:`campaign_engine.enroll`, which applies the guards and refuses
    a campaign past review.
    """
    _require_writer(session, "enroll")
    campaign = get_campaign(session, user, campaign_id)
    if list_id is not None and filter is not None:
        raise InvalidCampaign("the audience is a list or a filter, not both")
    removed = 0
    if list_id is not None or filter is not None:
        if campaign.status is not CampaignStatus.DRAFT:
            raise CampaignConflict(
                f"campaign {campaign_id} is {campaign.status}; its audience source is fixed"
            )
        campaign.source_list_id = None if list_id is None else _check_list(session, user, list_id)
        campaign.filter_json = None if filter is None else _check_filter(session, user, filter)
        session.flush()
        keep = campaign_review.source_contact_ids(session, user, campaign) | set(contact_ids)
        removed = _drop_outside(session, user, campaign_id, keep)
    ids = campaign_review.source_contact_ids(session, user, campaign) | set(contact_ids)
    if not ids:
        raise InvalidCampaign(
            f"campaign {campaign_id} has no audience: give a list, a filter or contacts"
        )
    try:
        result = campaign_engine.enroll(session, user, campaign_id, ids, now=now)
    except campaign_engine.CampaignEngineError as exc:
        raise CampaignConflict(str(exc)) from exc
    pending = session.scalar(
        scoped(user, Enrollment)
        .with_only_columns(func.count(Enrollment.id))
        .where(
            Enrollment.campaign_id == campaign_id,
            Enrollment.status == EnrollmentStatus.PENDING,
        )
    )
    return EnrollOutcome(
        campaign_id=campaign_id,
        enrolled=len(result.enrolled),
        already=len(result.already),
        excluded=len(result.verdicts) - len(result.enrolled),
        removed=removed,
        pending=pending or 0,
        summary=campaign_review.guard_summary(session, user, campaign, now=now),
    )


# --- read ---------------------------------------------------------------------------


def _enrollment_counts(
    session: Session, user: User, campaign_ids: Sequence[int]
) -> dict[int, Counter[EnrollmentStatus]]:
    counts: dict[int, Counter[EnrollmentStatus]] = {i: Counter() for i in campaign_ids}
    if not campaign_ids:
        return counts
    rows = session.execute(
        scoped(user, Enrollment)
        .with_only_columns(Enrollment.campaign_id, Enrollment.status, func.count(Enrollment.id))
        .where(Enrollment.campaign_id.in_(campaign_ids))
        .group_by(Enrollment.campaign_id, Enrollment.status)
    ).tuples()
    for campaign_id, status, n in rows:
        counts[campaign_id][status] = n
    return counts


@dataclass(frozen=True, slots=True)
class CampaignSummary:
    campaign: Campaign
    steps: int
    enrollments: Mapping[EnrollmentStatus, int]
    next_action_at: datetime | None = None
    """The soonest due time of an active enrollment, while the campaign is active."""


def list_campaigns(session: Session, user: User) -> list[CampaignSummary]:
    """Every campaign of ``user``, newest first, with its enrollment counts by status."""
    rows = list(session.scalars(scoped(user, Campaign).order_by(Campaign.id.desc())))
    ids = [c.id for c in rows]
    counts = _enrollment_counts(session, user, ids)
    steps: dict[int, int] = dict.fromkeys(ids, 0)
    if ids:
        for campaign_id, n in session.execute(
            scoped(user, CampaignStep)
            .with_only_columns(CampaignStep.campaign_id, func.count(CampaignStep.id))
            .where(CampaignStep.campaign_id.in_(ids))
            .group_by(CampaignStep.campaign_id)
        ).tuples():
            steps[campaign_id] = n
    active = [c.id for c in rows if c.status is CampaignStatus.ACTIVE]
    next_at: dict[int, datetime | None] = {}
    if active:
        for campaign_id, due in session.execute(
            scoped(user, Enrollment)
            .with_only_columns(Enrollment.campaign_id, func.min(Enrollment.next_action_at))
            .where(
                Enrollment.campaign_id.in_(active),
                Enrollment.status == EnrollmentStatus.ACTIVE,
            )
            .group_by(Enrollment.campaign_id)
        ).tuples():
            next_at[campaign_id] = due
    return [CampaignSummary(c, steps[c.id], dict(counts[c.id]), next_at.get(c.id)) for c in rows]


@dataclass(frozen=True, slots=True)
class StepStatus:
    step: CampaignStep
    template_name: str
    template_version: int
    fired: int
    """Outbound messages of the step, whatever became of them."""
    sent: int


@dataclass(frozen=True, slots=True)
class CampaignDetail:
    campaign: Campaign
    mailbox_email: str | None
    steps: tuple[StepStatus, ...]
    enrollments: Mapping[EnrollmentStatus, int]
    next_action_at: datetime | None
    """The soonest due time of an active enrollment, while the campaign is active."""
    missing: tuple[campaign_review.Missing, ...] = field(default=())
    """What the review gate still needs, for a ``draft`` or ``reviewing`` campaign."""


REVIEWABLE: Final = frozenset({CampaignStatus.DRAFT, CampaignStatus.REVIEWING})


def campaign_status(
    session: Session, user: User, campaign_id: int, *, me: Mapping[str, str], now: datetime
) -> CampaignDetail:
    """One campaign: its steps and their progress, its enrollments, its next fire, and
    what its review still misses."""
    campaign = get_campaign(session, user, campaign_id)
    steps = list(
        session.scalars(
            scoped(user, CampaignStep)
            .where(CampaignStep.campaign_id == campaign_id)
            .order_by(CampaignStep.position)
        )
    )
    outbound: Counter[tuple[int, bool]] = Counter()
    for step_id, status, n in session.execute(
        scoped(user, Message)
        .with_only_columns(Message.step_id, Message.status, func.count(Message.id))
        .where(
            Message.step_id.in_([s.id for s in steps]),
            Message.direction == MessageDirection.OUT,
        )
        .group_by(Message.step_id, Message.status)
    ).tuples():
        if step_id is not None:
            outbound[step_id, False] += n
            if status is MessageStatus.SENT:
                outbound[step_id, True] += n
    step_rows = []
    for step in steps:
        template = template_service.get_template(session, user, step.template_id)
        step_rows.append(
            StepStatus(
                step,
                template.name,
                template.version,
                outbound[step.id, False],
                outbound[step.id, True],
            )
        )
    next_at = None
    if campaign.status is CampaignStatus.ACTIVE:
        next_at = session.scalar(
            scoped(user, Enrollment)
            .with_only_columns(func.min(Enrollment.next_action_at))
            .where(
                Enrollment.campaign_id == campaign_id,
                Enrollment.status == EnrollmentStatus.ACTIVE,
            )
        )
    mailbox = (
        None
        if campaign.mailbox_id is None
        else get_scoped(session, user, Mailbox, campaign.mailbox_id)
    )
    gaps: tuple[campaign_review.Missing, ...] = ()
    if campaign.status in REVIEWABLE:
        gaps = tuple(campaign_review.missing(session, user, campaign, me=me, now=now))
    return CampaignDetail(
        campaign=campaign,
        mailbox_email=None if mailbox is None else mailbox.email,
        steps=tuple(step_rows),
        enrollments=dict(_enrollment_counts(session, user, [campaign_id])[campaign_id]),
        next_action_at=next_at,
        missing=gaps,
    )


ENROLLMENT_SEARCH_MAX: Final = 200
"""The longest search text :func:`list_enrollments` takes."""


@dataclass(frozen=True, slots=True)
class EnrollmentRow:
    enrollment: Enrollment
    contact_name: str
    email: str | None
    """The contact's primary address, for finding someone by it."""


@dataclass(frozen=True, slots=True)
class EnrollmentPage:
    items: tuple[EnrollmentRow, ...]
    total: int


def list_enrollments(
    session: Session,
    user: User,
    campaign_id: int,
    *,
    q: str = "",
    status: EnrollmentStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> EnrollmentPage:
    """One campaign's enrollments, oldest first, with each contact's name.

    ``q`` matches the contact's first, preferred or last name, or an address, as a
    case-insensitive substring; ``status`` keeps one status. The review screen finds
    an enrollment to preview with it, and the campaign page lists them (P3-11)."""
    get_campaign(session, user, campaign_id)
    stmt = (
        scoped(user, Enrollment)
        .join(Contact, Contact.id == Enrollment.contact_id)
        .where(Enrollment.campaign_id == campaign_id, Contact.user_id == user.id)
    )
    if status is not None:
        stmt = stmt.where(Enrollment.status == status)
    text = q.strip().lower()[:ENROLLMENT_SEARCH_MAX]
    if text:
        # Bound, so never injection; escaped so `%` or `_` in a search means itself.
        literal = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{literal}%"
        by_email = (
            scoped(user, ContactEmail)
            .with_only_columns(ContactEmail.contact_id)
            .where(func.lower(ContactEmail.email).like(pattern, escape="\\"))
        )
        stmt = stmt.where(
            or_(
                func.lower(Contact.first_name).like(pattern, escape="\\"),
                func.lower(Contact.preferred_name).like(pattern, escape="\\"),
                func.lower(Contact.last_name).like(pattern, escape="\\"),
                Contact.id.in_(by_email),
            )
        )
    total = session.scalar(stmt.with_only_columns(func.count(Enrollment.id))) or 0
    rows = session.execute(
        stmt.with_only_columns(Enrollment, Contact)
        .order_by(Enrollment.id)
        .limit(limit)
        .offset(offset)
    ).tuples()
    pairs = list(rows)
    emails: dict[int, str] = {}
    if pairs:
        for contact_id, email in session.execute(
            scoped(user, ContactEmail)
            .with_only_columns(ContactEmail.contact_id, ContactEmail.email)
            .where(ContactEmail.contact_id.in_([c.id for _, c in pairs]))
            .order_by(ContactEmail.is_primary.desc(), ContactEmail.id)
        ).tuples():
            emails.setdefault(contact_id, email)
    return EnrollmentPage(
        items=tuple(
            EnrollmentRow(
                enrollment,
                f"{contact.preferred_name or contact.first_name} {contact.last_name}".strip(),
                emails.get(contact.id),
            )
            for enrollment, contact in pairs
        ),
        total=total,
    )


# --- pause and resume: the engine's, with this module's errors ----------------------


def pause(session: Session, user: User, campaign_id: int) -> Campaign:
    """``active`` to ``paused`` (:func:`campaign_engine.pause_campaign`)."""
    get_campaign(session, user, campaign_id)
    try:
        return campaign_engine.pause_campaign(session, user, campaign_id)
    except campaign_engine.CampaignEngineError as exc:
        raise CampaignConflict(str(exc)) from exc


def resume(session: Session, user: User, campaign_id: int) -> Campaign:
    """``paused`` to ``active`` (:func:`campaign_engine.resume_campaign`)."""
    get_campaign(session, user, campaign_id)
    try:
        return campaign_engine.resume_campaign(session, user, campaign_id)
    except campaign_engine.CampaignEngineError as exc:
        raise CampaignConflict(str(exc)) from exc
