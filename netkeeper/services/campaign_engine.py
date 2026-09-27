"""The campaign engine: the enrollment state machine and the minute tick (spec 11.3, 11.4; P3-06).

The engine decides what fires and when. It never sends: each firing goes to a
:class:`Sender`, and the real one (Gmail send and draft) is P3-07's. Without a
sender, the tick does nothing at all, not even reading.

The state machine
-----------------
Spec 11.3's transitions that belong to the engine, each a function here that
needs a writer session:

- :func:`enroll`: a contact the guards pass (spec 11.9) becomes ``pending``.
- :func:`activate`: the campaign goes from ``reviewing`` to ``active``, and each
  ``pending`` enrollment becomes ``active``, its first step due after the
  step's delay, inside the send window.
- A step fires (the tick): ``current_step`` advances, and ``next_action_at`` is
  the next step's delay after the enrollment's **latest sent outbound message**,
  pushed into the send window. Spec 11.3 says the previous step's actual
  ``sent_at``; after a merge the enrollment can hold a newer message from the
  other contact, so the latest one decides (#242 review). After the last step,
  ``completed``.
- :func:`pause_campaign` and :func:`resume_campaign` move the campaign only.
  The tick fires only for an ``active`` enrollment of an ``active`` campaign, so
  a paused campaign fires nothing, and each enrollment keeps its own state and
  ``next_action_at``. Pausing enrollments as well would lose, on resume, which
  of them a person (or a merge) had paused on their own.
- :func:`pause_enrollment`, :func:`resume_enrollment` and :func:`remove_enrollment`
  are the per-enrollment moves.
- ``replied``, ``bounced`` and ``opted_out`` come from detection (P3-08), and
  from the tick's own checks below.

The tick
--------
:func:`run_tick` runs every minute (:class:`CampaignEngine`). For each local user:

1. **Choose**, in a writer session. Candidates are enrollments that are
   ``active``, of a campaign that is ``active``, with ``next_action_at <= now``,
   oldest due first. Status is what selects: a held pause keeps
   ``next_action_at``, so a due time alone means nothing (#242 review). A
   candidate whose next step is on LinkedIn is left unfired (P4) and reported.
   Blocked and LinkedIn rows are left out of the query itself, so however many
   there are, they never crowd out a row that could fire.
   For the rest, in this order, what the campaign or mailbox decides first:

   - Its campaign or mailbox is already blocked this tick: skipped.
   - The send window is closed: deferred to its next opening. A window that
     cannot be read, or never opens, blocks the campaign.
   - The mailbox (spec 11.9, the last bullet): ``ChannelState`` is filled from
     :func:`netkeeper.services.mailboxes.mailbox_health` and today's count. A
     mailbox that is unknown, ``reauth_required`` or disabled, or at its cap,
     blocks every email step on it. Nothing is marked sent.
   - The campaign's own daily cap (``daily_cap``, or ``[campaigns]
     mailbox_daily_cap`` when unset) blocks the campaign.
   - Spacing: the mailbox's next send time (persisted) has not come, or its
     last firing was under the floor ago: the mailbox is blocked.
   - A reply is on the enrollment: it becomes ``replied``.
   - Its next step already has an outbound message on the enrollment, of any
     status: refused and parked (``next_action_at`` cleared). ``current_step``
     alone is not enough: after a merge the enrollment holds the other contact's
     messages (#242 review). A ``discarded`` one counts too: a merge can discard
     a message the sender is holding (:func:`step_has_message`).
   - Another outbound message on it is still waiting (a draft nobody has sent
     yet): parked until it is sent.
   - Cadence: the next step's delay after the latest sent outbound message has
     not passed: deferred to then.
   - The guards (:func:`netkeeper.services.campaign_guards.check_step`). A
     do-not-contact contact becomes ``opted_out``, a bounced address
     ``bounced`` (spec 11.3); any other exclusion is re-checked a day later.
   - The render: a template with lint errors, or one that fails to render, is
     parked with the reason.

   The first candidate that passes is **claimed**: its message is written
   ``scheduled`` with the rendered text, and the enrollment's
   ``next_action_at`` is cleared. At most :data:`BATCH_PER_TICK` are claimed.
2. **Send**, with no session open: the claimed firing goes to the sender.
3. **Record**, in a new writer session: the message's outcome, the enrollment's
   next step, and the mailbox's next send time.

**Restart safety** (spec 11.4). Everything the tick knows between minutes is in
the database: each enrollment's ``next_action_at`` and each mailbox's next send
time (``settings_kv``). A crash before the claim commits changes nothing, and
the next tick chooses again. A crash after it leaves the message ``scheduled``
and the enrollment parked, and the step is never fired a second time: the
refusal above sees the message. Whether it went out is for P3-07 to reconcile.

**Off the event loop** (#259). :class:`CampaignEngine` runs each tick in a
worker thread (``asyncio.to_thread``), as :class:`~netkeeper.services.mailboxes.MailboxMonitor`
does. A blocking SQLite write on the loop thread deadlocks against a request
whose transaction needs the loop to commit.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import random
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Protocol

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from netkeeper.campaigns import schedule
from netkeeper.campaigns.render import MergeValues, TemplateRenderError, me_fields, render
from netkeeper.campaigns.templates import activation_errors, contact_fields
from netkeeper.config import Settings
from netkeeper.crm.contacts import sendable_email
from netkeeper.db import is_writer, session_scope
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Contact,
    Enrollment,
    EnrollmentStatus,
    Message,
    MessageDirection,
    MessageStatus,
    StepMode,
    Template,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.models.base import utcnow
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services.campaign_guards import (
    UNSENDABLE_EMAIL_STATUSES,
    ChannelReason,
    ChannelState,
    Reason,
    Verdict,
    check_channel,
    check_enrollment,
    check_step,
)
from netkeeper.services.mailboxes import MAILBOX_HARD_MAX_PER_DAY, mailbox_health
from netkeeper.services.settings_kv import get_setting, set_setting

log = logging.getLogger(__name__)

BATCH_PER_TICK: Final = 1
"""Spec 11.4: at most this many firings per tick, per user. Sends are never a burst."""

TICK_INTERVAL_S: Final = 60.0
"""Spec 11.4: the tick runs every minute."""

RECHECK_AFTER: Final = timedelta(days=1)
"""A step a guard excluded for a reason that can pass (contacted recently, waiting for
review, in another campaign ...) is checked again this much later, inside the window."""

SCAN_LIMIT: Final = 500
"""At most this many due enrollments are looked at in one tick. The rest wait a minute.
Blocked and LinkedIn rows are left out of the query, so they never use it up."""

PAGE_SIZE: Final = 50
"""Due enrollments are read this many at a time, each page leaving out what is blocked."""

ERROR_MAX_LENGTH: Final = 500
"""``messages.error`` holds one line; a sender's error is cut to fit."""

SPACING_KEY_PREFIX: Final = "campaigns.engine.next_send_at.mailbox"
"""``settings_kv`` key prefix of a mailbox's next send time: ``<prefix>.<mailbox id>``."""

ENDING_REASONS: Final[Mapping[Reason, EnrollmentStatus]] = {
    Reason.DO_NOT_CONTACT: EnrollmentStatus.OPTED_OUT,
    Reason.EMAIL_BOUNCED: EnrollmentStatus.BOUNCED,
}
"""Guard reasons that end the enrollment at a step fire (spec 11.3), in this order. Any
other exclusion is re-checked :data:`RECHECK_AFTER` later."""

WAITING_STATUSES: Final[frozenset[MessageStatus]] = frozenset(
    {MessageStatus.SCHEDULED, MessageStatus.DRAFTED, MessageStatus.PREFILLED}
)
"""An outbound message in one of these has not gone out yet, and nobody knows it failed."""


class CampaignEngineError(Exception):
    """A state machine move that is not allowed from where the campaign or enrollment is."""


class Skip(enum.StrEnum):
    """Why the tick did not fire a due enrollment, beyond the guards' own reasons."""

    LINKEDIN_STEP = "linkedin_step"
    CAMPAIGN_BLOCKED = "campaign_blocked"
    REPLIED = "replied"
    STEP_ALREADY_SENT = "step_already_sent"
    WAITING_ON_UNSENT = "waiting_on_unsent"
    NOT_DUE = "not_due"
    OUTSIDE_WINDOW = "outside_window"
    NO_SEND_WINDOW = "no_send_window"
    CAMPAIGN_AT_CAP = "campaign_at_cap"
    SPACING = "spacing"
    TEMPLATE_ERRORS = "template_errors"
    RENDER_FAILED = "render_failed"
    NO_ADDRESS = "no_address"
    GUARD_EXCLUDED = "guard_excluded"
    ENDED = "ended"


# --- the sender ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Firing:
    """One step, rendered, handed to the :class:`Sender`.

    ``thread_id`` is the Gmail thread of the enrollment's first sent email, for a
    step with ``same_thread`` (spec 11.5); ``None`` otherwise.
    """

    message_id: int
    user_id: int
    campaign_id: int
    campaign_name: str
    enrollment_id: int
    contact_id: int
    mailbox_id: int | None
    step_position: int
    channel: TemplateChannel
    mode: StepMode
    same_thread: bool
    thread_id: str | None
    to_address: str | None
    subject: str | None
    body: str


class SendOutcome(enum.StrEnum):
    SENT = "sent"
    DRAFTED = "drafted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SendResult:
    """What the sender did. ``at`` is when the message went out (``sent_at``); it is what
    the next step's timing derives from. ``error`` is one line, never a body or header."""

    outcome: SendOutcome
    at: datetime | None = None
    gmail_message_id: str | None = None
    gmail_thread_id: str | None = None
    gmail_draft_id: str | None = None
    error: str | None = None


class Sender(Protocol):
    """Sends or drafts one firing (P3-07). Blocking: the engine calls it in a worker thread,
    with no session open."""

    def send(self, firing: Firing) -> SendResult: ...


# --- what a tick did ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Decision:
    """What the tick did with one due enrollment: fired, or not and why."""

    enrollment_id: int
    fired: bool
    reasons: tuple[str, ...] = ()


@dataclass(slots=True)
class TickResult:
    """One user's tick. ``next_wake`` is the earliest time a tick could do something new
    (a due time, the spacing, tomorrow's cap); the minute loop ignores it, a simulation
    jumps to it."""

    user_id: int
    decisions: list[Decision] = field(default_factory=list)
    fired: list[tuple[Firing, SendResult]] = field(default_factory=list)
    next_wake: datetime | None = None

    def skipped(self) -> dict[int, tuple[str, ...]]:
        return {d.enrollment_id: d.reasons for d in self.decisions if not d.fired}


# --- the state machine --------------------------------------------------------------


def _require_writer(session: Session, where: str) -> None:
    if not is_writer(session):
        raise RuntimeError(
            f"{where} needs a writer session; use session_scope(factory, write=True)"
        )


def _campaign(session: Session, user: User, campaign_id: int) -> Campaign:
    campaign = session.scalars(
        scoped(user, Campaign)
        .where(Campaign.id == campaign_id)
        .execution_options(populate_existing=True)
    ).first()
    if campaign is None:
        raise LookupError(f"no campaign {campaign_id}")
    return campaign


def _enrollment(session: Session, user: User, enrollment_id: int) -> Enrollment:
    enrollment = session.scalars(
        scoped(user, Enrollment)
        .where(Enrollment.id == enrollment_id)
        .execution_options(populate_existing=True)
    ).first()
    if enrollment is None:
        raise LookupError(f"no enrollment {enrollment_id}")
    return enrollment


def _steps(session: Session, user: User, campaign_id: int) -> list[CampaignStep]:
    return list(
        session.scalars(
            scoped(user, CampaignStep)
            .where(CampaignStep.campaign_id == campaign_id)
            .order_by(CampaignStep.position)
            .execution_options(populate_existing=True)
        )
    )


@dataclass(frozen=True, slots=True)
class EnrollResult:
    enrolled: tuple[int, ...]
    already: tuple[int, ...]
    verdicts: tuple[Verdict, ...]


ENROLLING_STATUSES: Final[frozenset[CampaignStatus]] = frozenset(
    {CampaignStatus.DRAFT, CampaignStatus.REVIEWING}
)
"""A campaign takes new enrollments only before it is activated: after that, every
contact it sends to went through the review (spec 11.8)."""


def enroll(
    session: Session, user: User, campaign_id: int, contact_ids: Collection[int], *, now: datetime
) -> EnrollResult:
    """Enroll each of ``contact_ids`` the guards pass (spec 11.9) as ``pending``.

    A contact already enrolled in the campaign is left as it is and reported in
    ``already``. Refused for a campaign past review (:data:`ENROLLING_STATUSES`).
    """
    _require_writer(session, "enroll")
    campaign = _campaign(session, user, campaign_id)
    if campaign.status not in ENROLLING_STATUSES:
        raise CampaignEngineError(f"campaign {campaign_id} is {campaign.status}; nobody can join")
    ids = sorted(set(contact_ids))
    present = set(
        session.scalars(
            scoped(user, Enrollment)
            .with_only_columns(Enrollment.contact_id)
            .where(Enrollment.campaign_id == campaign_id, Enrollment.contact_id.in_(ids))
        )
    )
    fresh = [i for i in ids if i not in present]
    verdicts = check_enrollment(session, user, campaign, fresh, now=now)
    enrolled = [v.contact_id for v in verdicts if v.eligible]
    for contact_id in enrolled:
        session.add(
            Enrollment(
                user_id=user.id,
                campaign_id=campaign_id,
                contact_id=contact_id,
                status=EnrollmentStatus.PENDING,
            )
        )
    session.flush()
    log.info(
        "campaign %d: %d enrolled, %d excluded, %d already in",
        campaign_id,
        len(enrolled),
        len(verdicts) - len(enrolled),
        len(present),
    )
    return EnrollResult(tuple(enrolled), tuple(sorted(present)), tuple(verdicts))


def activate(
    session: Session, user: User, campaign_id: int, *, settings: Settings, now: datetime
) -> Campaign:
    """``reviewing`` to ``active``: every ``pending`` enrollment becomes ``active``.

    Refused unless the campaign is ``reviewing`` with ``approved_at`` recorded (the
    review gate, spec 11.8; P3-09 records it), has steps, has a mailbox when a step
    is email, and every step's template is free of lint errors. Each first step is
    due after its delay, inside the send window.
    """
    _require_writer(session, "activate")
    campaign = _campaign(session, user, campaign_id)
    if campaign.status is not CampaignStatus.REVIEWING:
        raise CampaignEngineError(f"campaign {campaign_id} is {campaign.status}, not reviewing")
    if campaign.approved_at is None:
        raise CampaignEngineError(f"campaign {campaign_id} has not passed review")
    steps = _steps(session, user, campaign_id)
    if not steps:
        raise CampaignEngineError(f"campaign {campaign_id} has no steps")
    if campaign.mailbox_id is None and any(s.channel is TemplateChannel.EMAIL for s in steps):
        raise CampaignEngineError(f"campaign {campaign_id} has email steps and no mailbox")
    me_keys = me_fields(settings.me).keys()
    for step in steps:
        template = get_scoped(session, user, Template, step.template_id)
        if template is None or activation_errors(template, me_keys):
            raise CampaignEngineError(f"step {step.position}'s template has lint errors")
    first_due = _in_window(
        settings, user, campaign, now + timedelta(days=steps[0].delay_days)
    ) or now + timedelta(days=steps[0].delay_days)
    campaign.status = CampaignStatus.ACTIVE
    pending = session.scalars(
        scoped(user, Enrollment).where(
            Enrollment.campaign_id == campaign_id, Enrollment.status == EnrollmentStatus.PENDING
        )
    ).all()
    for enrollment in pending:
        enrollment.status = EnrollmentStatus.ACTIVE
        enrollment.next_action_at = first_due
    session.flush()
    log.info("campaign %d active with %d enrollments", campaign_id, len(pending))
    return campaign


def pause_campaign(session: Session, user: User, campaign_id: int) -> Campaign:
    """``active`` to ``paused``. Enrollments keep their state and due times (see the module)."""
    _require_writer(session, "pause_campaign")
    campaign = _campaign(session, user, campaign_id)
    if campaign.status is not CampaignStatus.ACTIVE:
        raise CampaignEngineError(f"campaign {campaign_id} is {campaign.status}, not active")
    campaign.status = CampaignStatus.PAUSED
    session.flush()
    log.info("campaign %d paused", campaign_id)
    return campaign


def resume_campaign(session: Session, user: User, campaign_id: int) -> Campaign:
    """``paused`` to ``active``. A step that came due meanwhile fires at the next chance,
    one at a time and spaced like any other."""
    _require_writer(session, "resume_campaign")
    campaign = _campaign(session, user, campaign_id)
    if campaign.status is not CampaignStatus.PAUSED:
        raise CampaignEngineError(f"campaign {campaign_id} is {campaign.status}, not paused")
    campaign.status = CampaignStatus.ACTIVE
    session.flush()
    log.info("campaign %d resumed", campaign_id)
    return campaign


def pause_enrollment(session: Session, user: User, enrollment_id: int) -> Enrollment:
    """``active`` to ``paused``, keeping ``next_action_at`` for the resume."""
    _require_writer(session, "pause_enrollment")
    enrollment = _enrollment(session, user, enrollment_id)
    if enrollment.status is not EnrollmentStatus.ACTIVE:
        raise CampaignEngineError(f"enrollment {enrollment_id} is {enrollment.status}, not active")
    enrollment.status = EnrollmentStatus.PAUSED
    session.flush()
    return enrollment


def resume_enrollment(session: Session, user: User, enrollment_id: int) -> Enrollment:
    """``paused`` to ``active``."""
    _require_writer(session, "resume_enrollment")
    enrollment = _enrollment(session, user, enrollment_id)
    if enrollment.status is not EnrollmentStatus.PAUSED:
        raise CampaignEngineError(f"enrollment {enrollment_id} is {enrollment.status}, not paused")
    enrollment.status = EnrollmentStatus.ACTIVE
    session.flush()
    return enrollment


REMOVABLE_STATUSES: Final[frozenset[EnrollmentStatus]] = frozenset(
    {EnrollmentStatus.PENDING, EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED}
)


def remove_enrollment(session: Session, user: User, enrollment_id: int) -> Enrollment:
    """Manual removal (spec 11.3): the enrollment is over, and nothing waiting on it goes out."""
    _require_writer(session, "remove_enrollment")
    enrollment = _enrollment(session, user, enrollment_id)
    if enrollment.status not in REMOVABLE_STATUSES:
        raise CampaignEngineError(f"enrollment {enrollment_id} is already {enrollment.status}")
    _end(session, user, enrollment, EnrollmentStatus.REMOVED, "removed")
    return enrollment


def _end(
    session: Session,
    user: User,
    enrollment: Enrollment,
    status: EnrollmentStatus,
    reason: str | None,
) -> None:
    """Leave the sequence: ``reason`` is ``exit_reason``, None for a sequence that ran out."""
    enrollment.status = status
    enrollment.exit_reason = reason
    enrollment.next_action_at = None
    waiting = session.scalars(
        scoped(user, Message).where(
            Message.enrollment_id == enrollment.id,
            Message.direction == MessageDirection.OUT,
            Message.status == MessageStatus.SCHEDULED,
        )
    ).all()
    # Only ``scheduled``: a drafted or prefilled message is the person's to send or delete,
    # and P3-07 and P4 track what becomes of it.
    for message in waiting:
        message.status = MessageStatus.DISCARDED
    session.flush()
    log.info("enrollment %d is %s (%s)", enrollment.id, status, reason)


def schedule_next(
    session: Session, user: User, enrollment_id: int, *, settings: Settings, now: datetime
) -> Enrollment:
    """Set the enrollment's next step due time from its latest sent outbound message.

    For whatever learns that a message went out after the tick recorded it: P3-07's
    draft-to-sent detection calls this once a draft is seen sent. A live
    enrollment with no step left is ``completed``.
    """
    _require_writer(session, "schedule_next")
    enrollment = _enrollment(session, user, enrollment_id)
    campaign = _campaign(session, user, enrollment.campaign_id)
    _advance(session, user, settings, campaign, enrollment)
    return enrollment


# --- reading what an enrollment holds -----------------------------------------------


def _latest_sent(session: Session, user: User, enrollment_id: int) -> datetime | None:
    """The enrollment's latest sent outbound message, on any step and from either contact
    of a merge (#242 review)."""
    return session.scalar(
        scoped(user, Message)
        .with_only_columns(func.max(Message.sent_at))
        .where(
            Message.enrollment_id == enrollment_id,
            Message.direction == MessageDirection.OUT,
            Message.sent_at.is_not(None),
        )
    )


def step_has_message(session: Session, user: User, enrollment_id: int, step_id: int) -> bool:
    """Whether the enrollment already holds an outbound message of this step, whatever its
    status: the step fired, whether or not anyone knows it went out (#242 review).

    ``discarded`` counts too. A merge discards the outranked side's ``scheduled``
    message, and that one may be in the sender's hands right now: if the process
    stops before the send is recorded, only this refusal keeps the step from going
    out a second time (review of #264). A parked enrollment is the safe outcome."""
    found = session.scalar(
        scoped(user, Message)
        .with_only_columns(Message.id)
        .where(
            Message.enrollment_id == enrollment_id,
            Message.step_id == step_id,
            Message.direction == MessageDirection.OUT,
        )
        .limit(1)
    )
    return found is not None


def _waiting(session: Session, user: User, enrollment_id: int) -> bool:
    found = session.scalar(
        scoped(user, Message)
        .with_only_columns(Message.id)
        .where(
            Message.enrollment_id == enrollment_id,
            Message.direction == MessageDirection.OUT,
            Message.status.in_(WAITING_STATUSES),
        )
        .limit(1)
    )
    return found is not None


def _reply_at(session: Session, user: User, enrollment_id: int) -> datetime | None:
    """When the first reply on the enrollment came, if one did (newest time known for it)."""
    row = session.execute(
        scoped(user, Message)
        .with_only_columns(Message.sent_at, Message.created_at)
        .where(Message.enrollment_id == enrollment_id, Message.direction == MessageDirection.IN)
        .order_by(Message.id)
        .limit(1)
    ).first()
    if row is None:
        return None
    sent_at: datetime | None = row[0]
    created_at: datetime = row[1]
    return sent_at or created_at


def _thread_id(session: Session, user: User, enrollment_id: int) -> str | None:
    return session.scalar(
        scoped(user, Message)
        .with_only_columns(Message.gmail_thread_id)
        .where(
            Message.enrollment_id == enrollment_id,
            Message.channel == TemplateChannel.EMAIL,
            Message.direction == MessageDirection.OUT,
            Message.gmail_thread_id.is_not(None),
        )
        .order_by(Message.id)
        .limit(1)
    )


# --- windows, caps and spacing ------------------------------------------------------


def window_for(settings: Settings, user: User, campaign: Campaign) -> schedule.SendWindow:
    """The campaign's send window. Raises :class:`~netkeeper.campaigns.schedule.WindowError`."""
    return schedule.send_window(settings.campaigns, user.timezone, campaign.send_window_json)


def _in_window(settings: Settings, user: User, campaign: Campaign, at: datetime) -> datetime | None:
    """``at`` pushed into the campaign's window; None when the window is unreadable or shut."""
    try:
        return window_for(settings, user, campaign).next_open(at)
    except schedule.WindowError:
        return None


def campaign_cap(settings: Settings, campaign: Campaign) -> int:
    """The campaign's own daily cap: its ``daily_cap``, else ``[campaigns] mailbox_daily_cap``
    (``NULL`` is "the config's", ``models.campaigns``). Never over the mailbox hard max."""
    cap = (
        campaign.daily_cap
        if campaign.daily_cap is not None
        else settings.campaigns.mailbox_daily_cap
    )
    return max(0, min(cap, MAILBOX_HARD_MAX_PER_DAY))


def mailbox_count(
    session: Session, user: User, mailbox_id: int, since: datetime, until: datetime
) -> int:
    """Outbound email of every campaign on the mailbox fired in ``[since, until)``, any status:
    a failed send may still have gone out, so it counts against the cap."""
    fired = func.coalesce(Message.scheduled_at, Message.sent_at)
    return int(
        session.scalar(
            scoped(user, Message)
            .with_only_columns(func.count(Message.id))
            .join(Enrollment, Enrollment.id == Message.enrollment_id)
            .join(Campaign, Campaign.id == Enrollment.campaign_id)
            .where(
                Enrollment.user_id == user.id,
                Campaign.user_id == user.id,
                Campaign.mailbox_id == mailbox_id,
                Message.channel == TemplateChannel.EMAIL,
                Message.direction == MessageDirection.OUT,
                fired >= since,
                fired < until,
            )
        )
        or 0
    )


def campaign_count(
    session: Session, user: User, campaign_id: int, since: datetime, until: datetime
) -> int:
    """Outbound messages of the campaign fired in ``[since, until)``, any status."""
    fired = func.coalesce(Message.scheduled_at, Message.sent_at)
    return int(
        session.scalar(
            scoped(user, Message)
            .with_only_columns(func.count(Message.id))
            .join(Enrollment, Enrollment.id == Message.enrollment_id)
            .where(
                Enrollment.user_id == user.id,
                Enrollment.campaign_id == campaign_id,
                Message.direction == MessageDirection.OUT,
                fired >= since,
                fired < until,
            )
        )
        or 0
    )


def _last_fired(session: Session, user: User, mailbox_id: int) -> datetime | None:
    fired = func.coalesce(Message.scheduled_at, Message.sent_at)
    return session.scalar(
        scoped(user, Message)
        .with_only_columns(func.max(fired))
        .join(Enrollment, Enrollment.id == Message.enrollment_id)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .where(
            Enrollment.user_id == user.id,
            Campaign.user_id == user.id,
            Campaign.mailbox_id == mailbox_id,
            Message.channel == TemplateChannel.EMAIL,
            Message.direction == MessageDirection.OUT,
        )
    )


def _spacing_key(mailbox_id: int) -> str:
    return f"{SPACING_KEY_PREFIX}.{mailbox_id}"


def next_send_at(session: Session, user: User, mailbox_id: int) -> datetime | None:
    """When the mailbox may fire again, as the last firing left it. Read-only."""
    raw = get_setting(session, user, _spacing_key(mailbox_id))
    return datetime.fromisoformat(raw) if isinstance(raw, str) else None


def _set_next_send_at(session: Session, user: User, mailbox_id: int, at: datetime) -> None:
    set_setting(session, user, _spacing_key(mailbox_id), at.isoformat())


def _spacing_release(
    session: Session, user: User, settings: Settings, mailbox_id: int
) -> datetime | None:
    """The later of the persisted next send time and the floor after the last firing."""
    candidates: list[datetime] = []
    stored = next_send_at(session, user, mailbox_id)
    if stored is not None:
        candidates.append(stored)
    last = _last_fired(session, user, mailbox_id)
    if last is not None:
        candidates.append(last + timedelta(seconds=settings.campaigns.send_spacing_floor_s))
    return max(candidates) if candidates else None


# --- the tick: choosing -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Claim:
    firing: Firing
    step_id: int
    # Drawn before the claim, so nothing about spacing can fail once the sender has sent.
    gap: timedelta


@dataclass(slots=True)
class _Blocks:
    """What is blocked for the rest of this tick, and until when, if that is known."""

    campaigns: dict[int, tuple[tuple[str, ...], datetime | None]] = field(default_factory=dict)
    mailboxes: dict[int, tuple[tuple[str, ...], datetime | None]] = field(default_factory=dict)
    windows: dict[int, schedule.SendWindow] = field(default_factory=dict)


def _next_step_join(user: User) -> ColumnElement[bool]:
    return and_(
        CampaignStep.user_id == user.id,
        CampaignStep.campaign_id == Enrollment.campaign_id,
        CampaignStep.position == func.coalesce(Enrollment.current_step, 0) + 1,
    )


def _selected(user: User, now: datetime) -> Select[tuple[Enrollment]]:
    """Due enrollments, selected on status (#242 review), never on the due time alone."""
    return (
        scoped(user, Enrollment)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .outerjoin(CampaignStep, _next_step_join(user))
        .where(
            Campaign.user_id == user.id,
            Campaign.status == CampaignStatus.ACTIVE,
            Enrollment.status == EnrollmentStatus.ACTIVE,
            Enrollment.next_action_at.is_not(None),
            Enrollment.next_action_at <= now,
        )
    )


def _due(
    session: Session,
    user: User,
    now: datetime,
    *,
    seen: Collection[int],
    campaigns: Collection[int],
    mailboxes: Collection[int],
) -> list[tuple[Enrollment, CampaignStep | None]]:
    """The next page of due enrollments with their next step, oldest due first.

    Left out in the query, so they can never fill the scan and starve the rest: the
    rows this tick has already looked at, those of a campaign or mailbox blocked this
    tick (a cap, a mailbox needing re-authorization ...), and those whose next step
    is on LinkedIn (P4), which keep their due time (review of #264).
    """
    statement = (
        _selected(user, now)
        .add_columns(CampaignStep)
        .where(or_(CampaignStep.id.is_(None), CampaignStep.channel != TemplateChannel.LINKEDIN))
    )
    if seen:
        statement = statement.where(Enrollment.id.not_in(sorted(seen)))
    if campaigns:
        statement = statement.where(Enrollment.campaign_id.not_in(sorted(campaigns)))
    if mailboxes:
        statement = statement.where(
            or_(Campaign.mailbox_id.is_(None), Campaign.mailbox_id.not_in(sorted(mailboxes)))
        )
    rows = session.execute(
        statement.order_by(Enrollment.next_action_at, Enrollment.id)
        .limit(PAGE_SIZE)
        .execution_options(populate_existing=True)
    ).tuples()
    return list(rows)


def _linkedin_due(session: Session, user: User, now: datetime) -> list[int]:
    """Some of the due enrollments whose next step is on LinkedIn, to report them (P4)."""
    return list(
        session.scalars(
            _selected(user, now)
            .with_only_columns(Enrollment.id)
            .where(CampaignStep.channel == TemplateChannel.LINKEDIN)
            .order_by(Enrollment.next_action_at, Enrollment.id)
            .limit(PAGE_SIZE)
        )
    )


def _next_due(session: Session, user: User, now: datetime) -> datetime | None:
    return session.scalar(
        scoped(user, Enrollment)
        .with_only_columns(func.min(Enrollment.next_action_at))
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .outerjoin(
            CampaignStep,
            and_(
                CampaignStep.user_id == user.id,
                CampaignStep.campaign_id == Enrollment.campaign_id,
                CampaignStep.position == func.coalesce(Enrollment.current_step, 0) + 1,
            ),
        )
        .where(
            Campaign.user_id == user.id,
            Campaign.status == CampaignStatus.ACTIVE,
            Enrollment.status == EnrollmentStatus.ACTIVE,
            Enrollment.next_action_at > now,
            or_(CampaignStep.id.is_(None), CampaignStep.channel != TemplateChannel.LINKEDIN),
        )
    )


class _Chooser:
    """One user's choose phase: see the module docstring, "The tick"."""

    def __init__(
        self,
        session: Session,
        user: User,
        settings: Settings,
        now: datetime,
        result: TickResult,
        rng: random.Random,
    ) -> None:
        self.rng = rng
        self.session = session
        self.user = user
        self.settings = settings
        self.now = now
        self.result = result
        self.blocks = _Blocks()
        self.wakes: list[datetime] = []
        self.campaigns: dict[int, Campaign] = {}

    def skip(self, enrollment: Enrollment, *reasons: str) -> None:
        self.result.decisions.append(Decision(enrollment.id, False, tuple(reasons)))

    def defer(
        self, enrollment: Enrollment, campaign: Campaign, until: datetime, *reasons: str
    ) -> None:
        """Move the due time to ``until``, pushed into the window when the window is known."""
        window = self.blocks.windows.get(campaign.id)
        opens = window.next_open(until) if window is not None else None
        enrollment.next_action_at = opens or until
        self.skip(enrollment, *reasons)

    def park(self, enrollment: Enrollment, *reasons: str) -> None:
        """Out of the tick until something else sets a due time again."""
        enrollment.next_action_at = None
        log.warning("enrollment %d parked: %s", enrollment.id, ", ".join(reasons))
        self.skip(enrollment, *reasons)

    def block_campaign(
        self, campaign_id: int, reasons: tuple[str, ...], until: datetime | None = None
    ) -> None:
        self.blocks.campaigns[campaign_id] = (reasons, until)
        if until is not None:
            self.wakes.append(until)

    def block_mailbox(
        self, mailbox_id: int, reasons: tuple[str, ...], until: datetime | None = None
    ) -> None:
        self.blocks.mailboxes[mailbox_id] = (reasons, until)
        if until is not None:
            self.wakes.append(until)

    def choose(self) -> _Claim | None:
        claimed: _Claim | None = None
        for enrollment_id in _linkedin_due(self.session, self.user, self.now):
            self.result.decisions.append(Decision(enrollment_id, False, (Skip.LINKEDIN_STEP,)))
        seen: list[int] = []
        while claimed is None and len(seen) < SCAN_LIMIT:
            page = _due(
                self.session,
                self.user,
                self.now,
                seen=seen,
                campaigns=self.blocks.campaigns.keys(),
                mailboxes=self.blocks.mailboxes.keys(),
            )
            if not page:
                break
            for enrollment, step in page:
                seen.append(enrollment.id)
                claimed = self._consider(enrollment, step)
                if claimed is not None:
                    break
        upcoming = _next_due(self.session, self.user, self.now)
        if upcoming is not None:
            self.wakes.append(upcoming)
        future = [w for w in self.wakes if w > self.now]
        self.result.next_wake = min(future) if future else None
        return claimed

    def _consider(self, enrollment: Enrollment, step: CampaignStep | None) -> _Claim | None:
        session, user, now = self.session, self.user, self.now
        campaign = self.campaigns.get(enrollment.campaign_id)
        if campaign is None:  # read fresh once per tick
            campaign = self.campaigns[enrollment.campaign_id] = _campaign(
                session, user, enrollment.campaign_id
            )
        if step is None:
            # Positions are unique but may skip a number (a step deleted from a draft):
            # the next step is the next position up, as _advance has it.
            step = next(
                (
                    s
                    for s in _steps(session, user, campaign.id)
                    if s.position > (enrollment.current_step or 0)
                ),
                None,
            )
            if step is not None and step.channel is not TemplateChannel.EMAIL:
                self.skip(enrollment, Skip.LINKEDIN_STEP)
                return None
        if step is None:  # nothing left: the last step's record should have said so
            if enrollment.current_step is None:
                self.park(enrollment, Skip.CAMPAIGN_BLOCKED, "no_steps")
            else:
                _end(session, user, enrollment, EnrollmentStatus.COMPLETED, None)
                self.skip(enrollment, Skip.ENDED, EnrollmentStatus.COMPLETED.value)
            return None
        if step.channel is not TemplateChannel.EMAIL:
            # LinkedIn steps are P4: left unfired, due as they are.
            self.skip(enrollment, Skip.LINKEDIN_STEP)
            return None
        if campaign.id in self.blocks.campaigns:
            self.skip(enrollment, Skip.CAMPAIGN_BLOCKED, *self.blocks.campaigns[campaign.id][0])
            return None
        if campaign.mailbox_id is not None and campaign.mailbox_id in self.blocks.mailboxes:
            self.skip(
                enrollment, Skip.CAMPAIGN_BLOCKED, *self.blocks.mailboxes[campaign.mailbox_id][0]
            )
            return None

        try:
            window = self.blocks.windows.get(campaign.id) or window_for(
                self.settings, user, campaign
            )
        except schedule.WindowError as exc:
            log.warning("campaign %d sends nothing: %s", campaign.id, exc)
            self.block_campaign(campaign.id, (Skip.NO_SEND_WINDOW,))
            self.skip(enrollment, Skip.NO_SEND_WINDOW)
            return None
        self.blocks.windows[campaign.id] = window

        opens = window.next_open(now)
        if opens is None:
            self.block_campaign(campaign.id, (Skip.NO_SEND_WINDOW,))
            self.skip(enrollment, Skip.NO_SEND_WINDOW)
            return None
        if opens > now:
            self.defer(enrollment, campaign, opens, Skip.OUTSIDE_WINDOW)
            return None

        day_start, day_end = window.day_bounds(now)
        tomorrow = window.next_open(day_end) or day_end
        mailbox_reasons = self._mailbox(campaign, day_start, day_end)
        if mailbox_reasons:
            until = tomorrow if ChannelReason.MAILBOX_AT_CAP.value in mailbox_reasons else None
            if campaign.mailbox_id is None:
                self.block_campaign(campaign.id, mailbox_reasons, until)
            else:
                self.block_mailbox(campaign.mailbox_id, mailbox_reasons, until)
            self.skip(enrollment, *mailbox_reasons)
            return None
        assert campaign.mailbox_id is not None  # _mailbox reports a missing one
        if campaign_count(session, user, campaign.id, day_start, day_end) >= campaign_cap(
            self.settings, campaign
        ):
            self.block_campaign(campaign.id, (Skip.CAMPAIGN_AT_CAP,), tomorrow)
            self.skip(enrollment, Skip.CAMPAIGN_AT_CAP)
            return None
        release = _spacing_release(session, user, self.settings, campaign.mailbox_id)
        if release is not None and release > now:
            self.block_mailbox(campaign.mailbox_id, (Skip.SPACING,), release)
            self.skip(enrollment, Skip.SPACING)
            return None

        replied = _reply_at(session, user, enrollment.id)
        if replied is not None:
            enrollment.replied_at = enrollment.replied_at or replied
            _end(session, user, enrollment, EnrollmentStatus.REPLIED, "replied")
            self.skip(enrollment, Skip.ENDED, Skip.REPLIED)
            return None
        if step_has_message(session, user, enrollment.id, step.id):
            self.park(enrollment, Skip.STEP_ALREADY_SENT)
            return None
        if _waiting(session, user, enrollment.id):
            self.park(enrollment, Skip.WAITING_ON_UNSENT)
            return None

        latest = _latest_sent(session, user, enrollment.id)
        if latest is None and step.position > 1:
            # A later step with nothing sent before it: the earlier one failed or was
            # never seen going out. Nothing to count its delay from.
            self.park(enrollment, Skip.WAITING_ON_UNSENT)
            return None
        if latest is not None:
            due = latest + timedelta(days=step.delay_days)
            if due > now:
                self.defer(enrollment, campaign, due, Skip.NOT_DUE)
                return None

        verdict = check_step(session, user, enrollment, step, now=now)
        if not verdict.eligible:
            self._excluded(enrollment, campaign, verdict)
            return None
        return self._claim(campaign, enrollment, step, latest)

    def _mailbox(
        self, campaign: Campaign, day_start: datetime, day_end: datetime
    ) -> tuple[str, ...]:
        """Spec 11.9's last bullet for email, from ``mailbox_health`` (#251). Unknown excludes."""
        state = ChannelState()
        if campaign.mailbox_id is not None:
            health = mailbox_health(self.session, self.user, campaign.mailbox_id)
            if health is not None:
                state = ChannelState(
                    mailbox_ok=health.healthy,
                    mailbox_sent_today=mailbox_count(
                        self.session, self.user, health.mailbox_id, day_start, day_end
                    ),
                    mailbox_daily_cap=max(0, min(health.daily_cap, MAILBOX_HARD_MAX_PER_DAY)),
                )
        return tuple(r.value for r in check_channel(TemplateChannel.EMAIL, state))

    def _excluded(self, enrollment: Enrollment, campaign: Campaign, verdict: Verdict) -> None:
        reasons = tuple(r.value for r in verdict.reasons)
        if (
            Reason.CAMPAIGN_NOT_ACTIVE in verdict.reasons
            or Reason.ENROLLMENT_NOT_ACTIVE in verdict.reasons
        ):
            self.skip(enrollment, Skip.GUARD_EXCLUDED, *reasons)  # changed under us: leave it
            return
        for reason, status in ENDING_REASONS.items():
            if reason in verdict.reasons:
                _end(self.session, self.user, enrollment, status, reason.value)
                self.skip(enrollment, Skip.ENDED, *reasons)
                return
        self.defer(enrollment, campaign, self.now + RECHECK_AFTER, Skip.GUARD_EXCLUDED, *reasons)

    def _claim(
        self,
        campaign: Campaign,
        enrollment: Enrollment,
        step: CampaignStep,
        latest: datetime | None,
    ) -> _Claim | None:
        session, user = self.session, self.user
        template = get_scoped(session, user, Template, step.template_id)
        me = me_fields(self.settings.me)
        if template is None or activation_errors(template, me.keys()):
            self.park(enrollment, Skip.TEMPLATE_ERRORS)
            return None
        contact = session.scalars(
            scoped(user, Contact)
            .where(Contact.id == enrollment.contact_id)
            .options(selectinload(Contact.emails), selectinload(Contact.positions))
            .execution_options(populate_existing=True)
        ).first()
        address = (
            None if contact is None else sendable_email(contact, refuse=UNSENDABLE_EMAIL_STATUSES)
        )
        if contact is None or address is None:  # the guards passed it a moment ago
            self.park(enrollment, Skip.NO_ADDRESS)
            return None
        window = self.blocks.windows[campaign.id]
        today = window.local_date(self.now)
        values = MergeValues(
            contact=contact_fields(contact, today),
            me=me,
            campaign_name=campaign.name,
            step_number=step.position,
            previous_send_date=latest,
        )
        try:
            rendered = render(
                template.channel, template.subject, template.body, values, today=today
            )
        except TemplateRenderError as exc:
            log.warning(
                "enrollment %d step %d did not render: %s", enrollment.id, step.position, exc
            )
            self.park(enrollment, Skip.RENDER_FAILED)
            return None
        if not rendered.subject:
            self.park(enrollment, Skip.RENDER_FAILED)
            return None
        message = Message(
            user_id=user.id,
            enrollment_id=enrollment.id,
            step_id=step.id,
            contact_id=enrollment.contact_id,
            channel=step.channel,
            direction=MessageDirection.OUT,
            status=MessageStatus.SCHEDULED,
            subject=rendered.subject,
            body_rendered=rendered.body,
            scheduled_at=self.now,
        )
        session.add(message)
        enrollment.next_action_at = None
        session.flush()
        self.result.decisions.append(Decision(enrollment.id, True))
        firing = Firing(
            message_id=message.id,
            user_id=user.id,
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            enrollment_id=enrollment.id,
            contact_id=enrollment.contact_id,
            mailbox_id=campaign.mailbox_id,
            step_position=step.position,
            channel=step.channel,
            mode=step.mode,
            same_thread=step.same_thread,
            thread_id=_thread_id(session, user, enrollment.id) if step.same_thread else None,
            to_address=address.email,
            subject=rendered.subject,
            body=rendered.body,
        )
        log.info(
            "campaign %d: step %d for enrollment %d claimed as message %d",
            campaign.id,
            step.position,
            enrollment.id,
            message.id,
        )
        return _Claim(firing, step.id, _spacing_gap(self.settings, self.rng))


# --- the tick: recording ------------------------------------------------------------


def _advance(
    session: Session, user: User, settings: Settings, campaign: Campaign, enrollment: Enrollment
) -> None:
    """Set the next step's due time from the latest sent message, or complete the enrollment."""
    if enrollment.status not in (EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED):
        return
    steps = _steps(session, user, campaign.id)
    position = enrollment.current_step or 0
    upcoming = next((s for s in steps if s.position > position), None)
    if upcoming is None:
        _end(session, user, enrollment, EnrollmentStatus.COMPLETED, None)
        return
    latest = _latest_sent(session, user, enrollment.id)
    if latest is None or _waiting(session, user, enrollment.id):
        # A step not yet seen going out (a draft waiting for the person): the next
        # step's delay counts from when it does (schedule_next).
        enrollment.next_action_at = None
        return
    due = latest + timedelta(days=upcoming.delay_days)
    enrollment.next_action_at = _in_window(settings, user, campaign, due) or due
    session.flush()


def _record(
    session: Session,
    user: User,
    settings: Settings,
    claim: _Claim,
    result: SendResult,
    *,
    now: datetime,
) -> None:
    """The outcome on the message, the enrollment's next step, and the mailbox's spacing."""
    firing = claim.firing
    message = get_scoped(session, user, Message, firing.message_id)
    if message is None:
        log.error("message %d vanished while it was being sent", firing.message_id)
        return
    session.refresh(message)
    at = result.at or now
    if result.outcome is SendOutcome.SENT:
        message.status = MessageStatus.SENT
        message.sent_at = at
    elif result.outcome is SendOutcome.DRAFTED:
        message.status = MessageStatus.DRAFTED
    else:
        message.status = MessageStatus.FAILED
        message.error = (result.error or "the sender gave no reason")[:ERROR_MAX_LENGTH]
    message.gmail_message_id = result.gmail_message_id or message.gmail_message_id
    message.gmail_thread_id = result.gmail_thread_id or message.gmail_thread_id
    message.gmail_draft_id = result.gmail_draft_id or message.gmail_draft_id
    session.flush()
    # A merge may have moved the message to another enrollment meanwhile: follow it.
    enrollment = _enrollment(session, user, message.enrollment_id)
    campaign = _campaign(session, user, enrollment.campaign_id)
    if result.outcome is SendOutcome.FAILED:
        log.warning(
            "enrollment %d step %d failed; it waits for a person",
            enrollment.id,
            firing.step_position,
        )
    else:
        enrollment.current_step = max(enrollment.current_step or 0, firing.step_position)
        _advance(session, user, settings, campaign, enrollment)
    if firing.mailbox_id is not None:
        _set_next_send_at(session, user, firing.mailbox_id, max(at, now) + claim.gap)
    session.flush()


def _spacing_gap(settings: Settings, rng: random.Random) -> timedelta:
    """Raises ValueError for a spacing that is not one (see :func:`run_tick`)."""
    return schedule.spacing_delay(
        rng,
        median_s=settings.campaigns.send_spacing_median_s,
        floor_s=settings.campaigns.send_spacing_floor_s,
    )


def _send(sender: Sender, firing: Firing) -> SendResult:
    try:
        return sender.send(firing)
    except Exception as exc:  # the sender's failure is the message's, never the tick's
        log.exception("sending message %d failed", firing.message_id)
        return SendResult(SendOutcome.FAILED, error=f"sender raised {type(exc).__name__}")


def tick_user(
    factory: sessionmaker[Session],
    user_id: int,
    *,
    settings: Settings,
    sender: Sender,
    now: datetime,
    clock: Callable[[], datetime],
    rng: random.Random,
) -> TickResult:
    """One user's tick: choose and claim, send with no session open, record. Blocking."""
    result = TickResult(user_id)
    claims: list[_Claim] = []
    for _ in range(BATCH_PER_TICK):
        with session_scope(factory, write=True) as session:
            user = session.get(User, user_id)
            if user is None:
                return result
            claim = _Chooser(session, user, settings, now, result, rng).choose()
        if claim is None:
            break
        claims.append(claim)
        outcome = _send(sender, claim.firing)
        with session_scope(factory, write=True) as session:
            user = session.get(User, user_id)
            if user is None:
                return result
            _record(session, user, settings, claim, outcome, now=clock())
        result.fired.append((claim.firing, outcome))
    return result


def run_tick(
    factory: sessionmaker[Session],
    *,
    settings: Settings,
    sender: Sender | None,
    clock: Callable[[], datetime] = utcnow,
    rng: random.Random | None = None,
) -> list[TickResult]:
    """One tick for every local user. Blocking: run it off the event loop.

    Without a sender nothing is read or written: sending arrives with P3-07.
    """
    if sender is None:
        return []
    now = clock()
    if now.tzinfo is None:
        raise ValueError("the tick's clock must be timezone-aware")
    draw = rng if rng is not None else random.Random()  # noqa: S311 -- spacing, not crypto
    try:
        _spacing_gap(settings, random.Random(0))  # noqa: S311 -- a check, not crypto
    except ValueError as exc:
        # Refused before anything is claimed: sends are never a burst (review of #264).
        log.error("campaign sends are held: %s", exc)
        return []
    with session_scope(factory) as session:
        user_ids = list(
            session.scalars(select(User.id).where(User.kind == UserKind.LOCAL).order_by(User.id))
        )
    results: list[TickResult] = []
    for user_id in user_ids:
        try:
            results.append(
                tick_user(
                    factory,
                    user_id,
                    settings=settings,
                    sender=sender,
                    now=now,
                    clock=clock,
                    rng=draw,
                )
            )
        except Exception:  # one user's failure is not the next user's
            log.exception("campaign tick failed for user %d", user_id)
    return results


# --- the minute loop ----------------------------------------------------------------


class CampaignEngine:
    """The minute tick ``netkeeper serve`` runs (spec 11.4), each tick in a worker thread.

    Like :class:`~netkeeper.services.mailboxes.MailboxMonitor`: no SQLite write
    happens on the event loop's thread, so a request holding the write lock can
    always reach its commit (#259).
    """

    def __init__(
        self,
        factory: sessionmaker[Session],
        settings: Settings,
        sender: Sender | None,
        *,
        interval_s: float = TICK_INTERVAL_S,
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("the campaign tick interval must be positive")
        self._factory = factory
        self._settings = settings
        self._sender = sender
        self._interval_s = interval_s
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 -- spacing, not crypto
        self._task: asyncio.Task[None] | None = None

    @property
    def sender(self) -> Sender | None:
        return self._sender

    def start(self) -> None:
        if self._sender is None:
            log.info("the campaign engine has no sender yet (P3-07); nothing will fire")
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="campaign-tick")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def tick_once(self) -> list[TickResult]:
        """One tick, off the loop."""
        return await asyncio.to_thread(
            run_tick,
            self._factory,
            settings=self._settings,
            sender=self._sender,
            clock=self._clock,
            rng=self._rng,
        )

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                await self.tick_once()
            except Exception:
                log.exception("campaign tick failed; trying again next minute")


def firings(results: Iterable[TickResult]) -> list[tuple[Firing, SendResult]]:
    """Every firing in ``results``, in order."""
    return [fired for result in results for fired in result.fired]
