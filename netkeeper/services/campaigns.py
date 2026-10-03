"""Campaigns as a whole: create, enroll, list, and status (P3-13).

The small layer the CLI (``netkeeper campaigns``) and ``/campaigns`` share, so
each command mirrors an endpoint. The state machine stays where it is:

- ``draft`` to ``reviewing`` and ``reviewing`` to ``active`` are the review
  gate's (:mod:`netkeeper.services.campaign_review`). Nothing here activates a
  campaign, and nothing here holds the engine's gate token.
- pause and resume are the engine's (:func:`campaign_engine.pause_campaign`,
  :func:`campaign_engine.resume_campaign`).
- the scheduled start (#338): :func:`start_options` says what the default start is
  and whether a chosen one is in a suggested send slot; :func:`set_start` moves an
  active or paused campaign's start until its first send
  (:func:`campaign_engine.set_start`); :func:`set_step_schedule` changes a step's
  delay and time of day at any time before the campaign is over.
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

from netkeeper.campaigns import schedule
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
    send_time: str | None = None
    """An explicit local time of day, ``HH:MM``; None aims for the next suggested slot."""


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise InvalidCampaign("a campaign needs a name")
    if len(cleaned) > CAMPAIGN_NAME_MAX_LENGTH:
        raise InvalidCampaign(f"a campaign name is at most {CAMPAIGN_NAME_MAX_LENGTH} characters")
    return cleaned


def _send_time(position: int, value: str | None) -> str | None:
    """``HH:MM``, or None for the suggested slot. Refuses anything else."""
    if value is None:
        return None
    try:
        clock = schedule.parse_clock(value)
    except schedule.ScheduleError as exc:
        raise InvalidCampaign(f"step {position}: {exc}") from exc
    return f"{clock:%H:%M}"


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
        send_time=_send_time(position, spec.send_time),
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
    start_editable: bool = False
    """Whether the scheduled start can still move: active or paused, nothing fired yet."""
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
    start_editable = campaign.status in campaign_engine.START_EDITABLE and not (
        campaign_engine.has_fired(session, user, campaign_id)
    )
    return CampaignDetail(
        campaign=campaign,
        mailbox_email=None if mailbox is None else mailbox.email,
        steps=tuple(step_rows),
        enrollments=dict(_enrollment_counts(session, user, [campaign_id])[campaign_id]),
        next_action_at=next_at,
        missing=gaps,
        start_editable=start_editable,
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
    """``paused`` to ``active`` (:func:`campaign_engine.resume_campaign`). Refused, as
    activation is, while an email campaign's mailbox is not ``ok`` (#299)."""
    campaign = get_campaign(session, user, campaign_id)
    # Only a paused one: any other state keeps the engine's own refusal.
    if campaign.status is CampaignStatus.PAUSED and (
        gap := campaign_review.mailbox_gap(session, user, campaign)
    ):
        raise CampaignConflict(gap)
    try:
        return campaign_engine.resume_campaign(session, user, campaign_id)
    except campaign_engine.CampaignEngineError as exc:
        raise CampaignConflict(str(exc)) from exc


# --- the scheduled start and step timing (#338) -------------------------------------


@dataclass(frozen=True, slots=True)
class StartOptions:
    """What the activate dialog and ``campaigns activate`` show about the start."""

    timezone: str
    default_start: datetime
    """The next Tuesday at 09:00 local time (:func:`schedule.default_start`)."""
    suggestion: str
    reminder: str
    at: datetime | None = None
    """The start asked about, if any."""
    warning: str | None = None
    """Why ``at`` is outside the suggested slots; None inside them, or with no ``at``."""


def _slots(settings: Settings, user: User) -> schedule.Suggested:
    try:
        return schedule.suggested(settings.campaigns, user.timezone)
    except schedule.ScheduleError as exc:
        raise InvalidCampaign(f"the send schedule cannot be read: {exc}") from exc


def start_warning(slots: schedule.Suggested, at: datetime) -> str | None:
    """A warning, never a refusal, for a start outside the suggested slots (#338)."""
    if slots.is_holiday(at):
        return "That day is on your holiday list. netkeeper will still send then."
    if not slots.is_suggested(at):
        return (
            f"That is outside the suggested slots (Tue to Thu, {schedule.SUGGESTED_HOURS[0]}"
            f" to {schedule.SUGGESTED_HOURS[1]}). netkeeper will still send then."
        )
    return None


def start_options(
    user: User, *, settings: Settings, now: datetime, at: datetime | None = None
) -> StartOptions:
    """The default start, the suggestion and the reminder, and a warning for ``at``."""
    slots = _slots(settings, user)
    if at is not None and (at.tzinfo is None or at.utcoffset() is None):
        raise InvalidCampaign("a start time must say its time zone")
    return StartOptions(
        timezone=user.timezone,
        default_start=schedule.default_start(now, slots),
        suggestion=schedule.SUGGESTION,
        reminder=schedule.SERVE_REMINDER,
        at=at,
        warning=None if at is None else start_warning(slots, max(at, now)),
    )


def resolve_start(
    user: User, *, settings: Settings, now: datetime, starts_at: datetime | None
) -> datetime:
    """``starts_at``, or the default start when it is None. Refuses a naive time."""
    if starts_at is None:
        return schedule.default_start(now, _slots(settings, user))
    if starts_at.tzinfo is None or starts_at.utcoffset() is None:
        raise InvalidCampaign("a start time must say its time zone")
    return starts_at


def set_start(
    session: Session,
    user: User,
    campaign_id: int,
    *,
    settings: Settings,
    now: datetime,
    starts_at: datetime,
) -> Campaign:
    """Move the scheduled start (:func:`campaign_engine.set_start`): refused once the
    campaign has sent anything, or unless it is active or paused."""
    get_campaign(session, user, campaign_id)
    if starts_at.tzinfo is None or starts_at.utcoffset() is None:
        raise InvalidCampaign("a start time must say its time zone")
    try:
        return campaign_engine.set_start(
            session, user, campaign_id, settings=settings, now=now, starts_at=starts_at
        )
    except campaign_engine.CampaignEngineError as exc:
        raise CampaignConflict(str(exc)) from exc


SCHEDULE_EDITABLE: Final = frozenset(
    {CampaignStatus.DRAFT, CampaignStatus.REVIEWING, CampaignStatus.ACTIVE, CampaignStatus.PAUSED}
)
"""A campaign whose steps' timing can change: any that is not over."""


def set_step_schedule(
    session: Session,
    user: User,
    campaign_id: int,
    step_id: int,
    *,
    settings: Settings,
    delay_days: int,
    send_time: str | None,
) -> CampaignStep:
    """Set a step's day offset and time of day (#338): ``send_time`` ``HH:MM``, or None
    for the next suggested slot after the delay.

    Only the timing changes, never what is sent. On an active or paused campaign,
    each live enrollment already waiting for this step is due again by the new
    timing (:func:`campaign_engine.reschedule_step`). On a draft or reviewing one,
    the review's records that name the step's timing are no longer current, as for
    any other change to a step.
    """
    _require_writer(session, "set_step_schedule")
    campaign = get_campaign(session, user, campaign_id)
    if campaign.status not in SCHEDULE_EDITABLE:
        raise CampaignConflict(
            f"campaign {campaign_id} is {campaign.status}; its steps no longer change"
        )
    step = get_scoped(session, user, CampaignStep, step_id)
    if step is None or step.campaign_id != campaign_id:
        raise CampaignNotFound(f"no step {step_id} in campaign {campaign_id}")
    if not 0 <= delay_days <= MAX_DELAY_DAYS:
        raise InvalidCampaign(f"step {step.position}: the delay is 0 to {MAX_DELAY_DAYS} days")
    step.delay_days = delay_days
    step.send_time = _send_time(step.position, send_time)
    session.flush()
    if campaign.status in campaign_engine.START_EDITABLE:
        moved = campaign_engine.reschedule_step(session, user, step, settings=settings)
        log.info(
            "campaign %d step %d timing changed; %d enrollments due again",
            campaign_id,
            step.position,
            moved,
        )
    return step
