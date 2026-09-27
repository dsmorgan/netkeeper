"""The campaign engine: the enrollment state machine and the minute tick (spec 11.3, 11.4; P3-06).

The engine decides what fires and when. It never sends: each firing goes to a
:class:`Sender`, and the Gmail one (send and draft modes) is
:class:`netkeeper.services.campaign_sender.GmailSender` (P3-07). Without a
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
     messages (#242 review). A ``discarded`` one counts too
     (:func:`step_has_message`).
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
refusal above sees the message.

**Did it go out?** (P3-07, #269). A message's RFC 822 Message-ID is derived from
its row (:func:`netkeeper.campaigns.compose.message_id_for`), so it is known
before the send. A message still ``scheduled`` :data:`RECONCILE_AFTER` after its
claim is a leftover: a crash, a stop that did not wait, or a send whose answer
never came (:attr:`SendOutcome.UNKNOWN`). Nothing else changes it: an end
(reply, removal, bounce, do-not-contact) and a merge leave it ``scheduled``.
Each tick, a sender that is a :class:`Reconciler` searches Gmail for each
leftover's Message-ID first, and the ``settle_*`` functions here record what it
found: ``sent``, ``drafted``, or, when Gmail does not have it after
:data:`RECONCILE_GIVE_UP_MISSES` searches over :data:`RECONCILE_GIVE_UP_AFTER`
(its search can lag a send), ``failed`` or ``discarded``. A leftover is never
sent again.

**Nothing sent, for now** (#273 review). A send that certainly sent nothing, for
a reason that may pass (:attr:`SendOutcome.NOT_SENT`: a rate limit, an outage
before the write, a mailbox not ready), gives its claim back: the message row is
deleted, the enrollment is due :func:`retry_after` later, and its mailbox waits
:data:`RETRY_AFTER`. An outage is waited out; it never fails a step. The tries are
counted on the enrollment (``not_sent_count``, with the latest reason), and each
waits twice as long as the one before, up to :data:`RETRY_AFTER_MAX`. Only
:data:`NOT_SENT_GIVE_UP_TRIES` tries in a row over :data:`NOT_SENT_GIVE_UP_AFTER`
fail the step, with the reason, for a person (#280). Any other outcome ends the run.

**Off the event loop** (#259). :class:`CampaignEngine` runs each tick in a
worker thread (``asyncio.to_thread``), as :class:`~netkeeper.services.mailboxes.MailboxMonitor`
does. A blocking SQLite write on the loop thread deadlocks against a request
whose transaction needs the loop to commit.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import enum
import logging
import random
import threading
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Protocol, runtime_checkable

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from netkeeper.campaigns import schedule
from netkeeper.campaigns.compose import ComposeError, campaign_label, message_id_for
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
    Mailbox,
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
    MAILBOX_ADDRESS = "mailbox_address"
    GUARD_EXCLUDED = "guard_excluded"
    ENDED = "ended"


# --- the sender ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Firing:
    """One step, rendered, handed to the :class:`Sender`.

    ``thread_id`` is the Gmail thread of the enrollment's first sent email, for a
    step with ``same_thread`` (spec 11.5); ``None`` otherwise.

    ``rfc822_message_id`` is the Message-ID the message goes out with, derived
    from the message row before anything is sent
    (:func:`netkeeper.campaigns.compose.message_id_for`): after a crash or an
    unknown outcome, a search for it says whether Gmail has the message (#269).
    ``label`` is the campaign's Gmail label (spec 11.5). Both are None only for a
    firing with no mailbox.
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
    rfc822_message_id: str | None = None
    label: str | None = None


class SendOutcome(enum.StrEnum):
    """What became of a firing.

    ``unknown`` is a send whose answer never came (Gmail's ``outcome_unknown``,
    or a sender that raised): the message may be in the recipient's inbox. It
    is never recorded as ``failed``. The message stays ``scheduled``, its
    enrollment parked, until :meth:`Reconciler.reconcile` finds it by Message-ID (#269).

    ``not_sent`` is a send that certainly sent nothing and may work later: a rate
    limit, Gmail unavailable before the write, a mailbox not ready. The claim is
    given back: the message row is deleted, the enrollment is due again
    :data:`RETRY_AFTER` later, and the mailbox sends nothing until then (#273
    review). ``failed`` is what a retry would not change (a refusal, a thread
    that is gone, a message that cannot be built): parked for a person.
    """

    SENT = "sent"
    DRAFTED = "drafted"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NOT_SENT = "not_sent"


@dataclass(frozen=True, slots=True)
class SendResult:
    """What the sender did. ``at`` is when the message went out (``sent_at``); it is what
    the next step's timing derives from. ``error`` is one line, never a body or header.
    ``thread_known`` is, for a draft, the Gmail message ids its thread already held when
    the draft was made: none of them is ever the draft, sent (#273 review)."""

    outcome: SendOutcome
    at: datetime | None = None
    gmail_message_id: str | None = None
    gmail_thread_id: str | None = None
    gmail_draft_id: str | None = None
    error: str | None = None
    thread_known: tuple[str, ...] = ()


class Sender(Protocol):
    """Sends or drafts one firing (P3-07). Blocking: the engine calls it in a worker thread,
    with no session open."""

    def send(self, firing: Firing) -> SendResult: ...


@runtime_checkable
class Reconciler(Protocol):
    """A sender that can also look at what it sent before (P3-07): each tick, before
    anything is chosen, the engine calls :meth:`reconcile` for the user. Blocking, and
    called with no session open; it opens its own. It must not raise for a Gmail
    failure: whatever it could not settle waits for the next tick."""

    def reconcile(
        self,
        factory: sessionmaker[Session],
        user_id: int,
        *,
        settings: Settings,
        now: datetime,
    ) -> None: ...


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
    """Manual removal (spec 11.3): the enrollment is over, and no step of it fires again.

    A message it holds that is still ``scheduled`` is left for :meth:`Reconciler.reconcile`:
    it may be in the sender's hands, or out already (#269)."""
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
    """Leave the sequence: ``reason`` is ``exit_reason``, None for a sequence that ran out.

    No message changes here. A ``scheduled`` one is the "did it go out?" signal: it
    may be in the sender's hands right now, or have gone out before a crash, so
    only :meth:`Reconciler.reconcile`, after a search by Message-ID, says whether it was sent
    or discarded (#269). A drafted or prefilled message is the person's to send or
    delete, and the drafts poll (P3-07) and P4 track what becomes of it.
    """
    enrollment.status = status
    enrollment.exit_reason = reason
    enrollment.next_action_at = None
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

    ``discarded`` counts too: a merge discards the outranked side's waiting
    draft, and a reconcile discards a leftover that never reached Gmail (#269).
    Either way the step fired once, and a parked enrollment is the safe outcome
    (review of #264)."""
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
    """The Gmail thread of the enrollment's first **sent** email: a follow-up joins it
    (spec 11.5). A draft never sent, or a discarded one, is no thread to reply in."""
    return session.scalar(
        scoped(user, Message)
        .with_only_columns(Message.gmail_thread_id)
        .where(
            Message.enrollment_id == enrollment_id,
            Message.channel == TemplateChannel.EMAIL,
            Message.direction == MessageDirection.OUT,
            Message.status == MessageStatus.SENT,
            Message.gmail_thread_id.is_not(None),
        )
        .order_by(Message.sent_at, Message.id)
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


@dataclass(frozen=True, slots=True)
class UpcomingFire:
    """One enrollment the tick will consider when its ``next_action_at`` comes (P3-12)."""

    enrollment: Enrollment
    due: datetime
    campaign: Campaign
    step: CampaignStep | None
    contact: Contact


def upcoming(session: Session, user: User, *, limit: int) -> tuple[list[UpcomingFire], int]:
    """The next ``limit`` fires, soonest first, and how many there are in all.

    A read for the dashboard: nothing here changes a row. It selects what
    :func:`_selected` selects, with no bound on the due time, so a row already
    due is the next tick's and is listed first. As in :func:`_next_due`, a row
    whose next step is on LinkedIn is left out: the tick never fires it (P4).
    Selection is on status, never on the due time alone (#242 review): a held
    pause keeps its ``next_action_at`` and is not listed.
    """
    statement = (
        scoped(user, Enrollment)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .outerjoin(CampaignStep, _next_step_join(user))
        .join(Contact, Contact.id == Enrollment.contact_id)
        .where(
            Campaign.user_id == user.id,
            Contact.user_id == user.id,
            Campaign.status == CampaignStatus.ACTIVE,
            Enrollment.status == EnrollmentStatus.ACTIVE,
            Enrollment.next_action_at.is_not(None),
            or_(CampaignStep.id.is_(None), CampaignStep.channel != TemplateChannel.LINKEDIN),
        )
    )
    total = session.scalar(statement.with_only_columns(func.count(Enrollment.id)).order_by(None))
    rows = session.execute(
        statement.add_columns(Campaign, CampaignStep, Contact)
        .order_by(Enrollment.next_action_at, Enrollment.id)
        .limit(limit)
    ).tuples()
    fires = [
        UpcomingFire(
            enrollment=enrollment,
            due=enrollment.next_action_at,
            campaign=campaign,
            step=step,
            contact=contact,
        )
        for enrollment, campaign, step, contact in rows
        if enrollment.next_action_at is not None  # the query's own condition
    ]
    return fires, total or 0


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
        mailbox = (
            None
            if campaign.mailbox_id is None
            else get_scoped(session, user, Mailbox, campaign.mailbox_id)
        )
        created = utcnow()
        try:
            if mailbox is None:
                raise ComposeError("no mailbox")
            # Checked before the claim: the Message-ID must be known before the send.
            message_id_for(user_id=user.id, message_id=0, created_at=created, address=mailbox.email)
        except ComposeError:
            self.park(enrollment, Skip.MAILBOX_ADDRESS)
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
            created_at=created,
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
            rfc822_message_id=message_id_for(
                user_id=user.id, message_id=message.id, created_at=created, address=mailbox.email
            ),
            label=campaign_label(mailbox.label_prefix, campaign.name),
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
    if result.outcome is SendOutcome.NOT_SENT:
        if _give_back(session, user, settings, message, result, now=now):
            if firing.mailbox_id is not None:
                wait = max(claim.gap, RETRY_AFTER)
                _set_next_send_at(session, user, firing.mailbox_id, now + wait)
            session.flush()
            return
        # Tried too often for too long: failed below, for a person (#280).
        result = SendResult(SendOutcome.FAILED, error=message.error)
    _clear_not_sent(session, user, message.enrollment_id)
    at = result.at or now
    if result.outcome is SendOutcome.SENT:
        message.status = MessageStatus.SENT
        message.sent_at = at
        message.error = None
    elif result.outcome is SendOutcome.DRAFTED:
        message.status = MessageStatus.DRAFTED
        message.error = None
        message.thread_known_json = list(result.thread_known)
    elif result.outcome is SendOutcome.UNKNOWN:
        # It may have gone out. It stays ``scheduled``, the "did it go out?" signal,
        # and its enrollment parked, until reconcile finds it or rules it out (#269).
        message.status = MessageStatus.SCHEDULED
        message.error = (
            f"outcome unknown ({result.error or 'no answer'}); reconciling by Message-ID"
        )[:ERROR_MAX_LENGTH]
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
    elif result.outcome is SendOutcome.UNKNOWN:
        log.warning(
            "enrollment %d step %d: the send's outcome is unknown; it waits for reconcile",
            enrollment.id,
            firing.step_position,
        )
    else:
        enrollment.current_step = max(enrollment.current_step or 0, firing.step_position)
        _advance(session, user, settings, campaign, enrollment)
    if firing.mailbox_id is not None:
        _set_next_send_at(session, user, firing.mailbox_id, max(at, now) + claim.gap)
    session.flush()


RETRY_AFTER: Final = timedelta(minutes=15)
"""A step whose send certainly sent nothing (:attr:`SendOutcome.NOT_SENT`) is due again
this much later the first time, and its mailbox sends nothing until then: an outage is
waited out, never turned into failed steps (#273 review). Each further try in a row
waits twice as long as the one before, up to :data:`RETRY_AFTER_MAX` (#280)."""

RETRY_AFTER_MAX: Final = timedelta(hours=2)
"""The longest an enrollment waits between tries that sent nothing (#280). Its mailbox
waits only :data:`RETRY_AFTER`: one enrollment's thread that cannot be read must not
hold every other enrollment on the mailbox for hours."""

NOT_SENT_GIVE_UP_TRIES: Final = 8
"""A step is failed for a person only after this many tries in a row sent nothing ...

A try that sent nothing leaves no row behind, so without a limit a step whose thread
could never be read was tried every :data:`RETRY_AFTER` for good (#280)."""

NOT_SENT_GIVE_UP_AFTER: Final = timedelta(hours=24)
"""... and only once this long has passed since the first of them. Both: an outage
shorter than this never fails a step, however many tries it costs, and a step whose
campaign was paused for days between two tries still gets all of its tries."""


def retry_after(tries: int) -> timedelta:
    """How long an enrollment waits after ``tries`` sends in a row that sent nothing."""
    if tries < 1:
        raise ValueError("tries counts from 1")
    return min(RETRY_AFTER * (1 << min(tries - 1, 16)), RETRY_AFTER_MAX)


def _clear_not_sent(session: Session, user: User, enrollment_id: int) -> None:
    """Any outcome but ``not_sent`` ends a run of tries that sent nothing."""
    enrollment = _enrollment(session, user, enrollment_id)
    enrollment.not_sent_count = 0
    enrollment.not_sent_since = None
    enrollment.not_sent_error = None


def _give_back(
    session: Session,
    user: User,
    settings: Settings,
    message: Message,
    result: SendResult,
    *,
    now: datetime,
) -> bool:
    """Undo a claim whose send sent nothing: the message row goes, so the step is free to
    fire again, and the enrollment is due :func:`retry_after` later, inside the window.

    Deleting the row is safe only because nothing reached Gmail: a send whose outcome
    is unknown is never given back. The next claim writes a new row, and so a new
    Message-ID.

    The try is counted on the enrollment. Once :data:`NOT_SENT_GIVE_UP_TRIES` in a
    row have sent nothing over at least :data:`NOT_SENT_GIVE_UP_AFTER`, nothing is
    given back: the message keeps its row, with the reason as its ``error``, and
    False is returned for the caller to record it ``failed`` (#280). True when the
    claim was given back.
    """
    reason = (result.error or "no reason given")[:ERROR_MAX_LENGTH]
    # A merge may have moved the message to another enrollment meanwhile: follow it.
    enrollment = _enrollment(session, user, message.enrollment_id)
    enrollment.not_sent_count += 1
    enrollment.not_sent_since = enrollment.not_sent_since or now
    enrollment.not_sent_error = reason
    tries, since = enrollment.not_sent_count, enrollment.not_sent_since
    if tries >= NOT_SENT_GIVE_UP_TRIES and now - since >= NOT_SENT_GIVE_UP_AFTER:
        hours = int((now - since).total_seconds() // 3600)
        message.error = (f"nothing sent after {tries} tries over {hours} h; the last: {reason}")[
            :ERROR_MAX_LENGTH
        ]
        log.warning(
            "message %d: %d tries in a row sent nothing; enrollment %d waits for a person",
            message.id,
            tries,
            enrollment.id,
        )
        return False
    wait = retry_after(tries)
    session.delete(message)
    session.flush()
    log.warning(
        "message %d sent nothing (%s); enrollment %d is due again in %s (try %d)",
        message.id,
        reason,
        enrollment.id,
        wait,
        tries,
    )
    # A paused enrollment keeps its due time for the resume; an ended one has none.
    if enrollment.status in (EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED):
        campaign = _campaign(session, user, enrollment.campaign_id)
        due = now + wait
        enrollment.next_action_at = _in_window(settings, user, campaign, due) or due
    return True


def _spacing_gap(settings: Settings, rng: random.Random) -> timedelta:
    """Raises ValueError for a spacing that is not one (see :func:`run_tick`)."""
    return schedule.spacing_delay(
        rng,
        median_s=settings.campaigns.send_spacing_median_s,
        floor_s=settings.campaigns.send_spacing_floor_s,
    )


def _send(sender: Sender, firing: Firing) -> SendResult:
    """The sender's result, fit to record.

    A sender that raises may have sent before it did, so its outcome is
    ``unknown``, never ``failed`` (#269). A naive ``at`` would make the record
    raise after the send: it is refused, and the record uses its own time (#269).
    """
    try:
        result = sender.send(firing)
    except Exception as exc:  # the sender's failure is the message's, never the tick's
        log.exception("sending message %d failed", firing.message_id)
        return SendResult(SendOutcome.UNKNOWN, error=f"sender raised {type(exc).__name__}")
    if result.at is not None and (result.at.tzinfo is None or result.at.utcoffset() is None):
        log.error(
            "the sender gave message %d a time with no zone; the record's own time is used",
            firing.message_id,
        )
        result = dataclasses.replace(result, at=None)
    return result


# --- reconciling what the sender may have done (P3-07) -----------------------------


RECONCILE_AFTER: Final = timedelta(minutes=10)
"""A ``scheduled`` message this long after its claim is a leftover: a crash, a stop
that did not wait, or a send whose answer never came. Before that it may still be in
the sender's hands, and a message just sent may not be in Gmail's search yet."""

RECONCILE_BATCH: Final = 10
"""At most this many leftovers per mailbox per tick. Per mailbox, so a mailbox that
cannot be searched (disconnected, waiting for re-authorization) never holds another's
slots (#273 review)."""

RECONCILE_SEARCH_EVERY: Final = timedelta(minutes=20)
"""A leftover whose search found nothing is searched again no sooner than this."""

RECONCILE_GIVE_UP_MISSES: Final = 4
"""A leftover is ruled "not in Gmail" only after this many searches found nothing ...

Gmail's search can lag a send by minutes, and a message ruled not sent that went out
anyway is lost to the sequence for good (#273 review)."""

RECONCILE_GIVE_UP_AFTER: Final = timedelta(hours=2)
"""... and only once this long has passed since the first of them."""

DRAFT_MISSING: Final = "draft not found in Gmail, and nothing sent in its thread; checking again"
"""``messages.error`` of a draft seen gone once. Seen gone again, it is ``discarded``: a
draft the person just sent (undo send, scheduled send) may not show as sent at once."""

DRAFT_DISCARDED_REASON: Final = "draft_discarded"
"""``exit_reason`` of an enrollment whose draft the person deleted (spec 11.5)."""

LIVE_STATUSES: Final[frozenset[EnrollmentStatus]] = frozenset(
    {EnrollmentStatus.PENDING, EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED}
)
"""An enrollment in one of these may still send a step."""


@dataclass(frozen=True, slots=True)
class Tracked:
    """One outbound email the sender has to look up in Gmail, read with no Gmail call.

    ``thread_known`` is every Gmail message id the user's other messages in its
    thread already hold, and, for a draft, every one its thread held when the
    draft was made, so a sent draft is told apart from an earlier step or a note
    the person sent before (#273 review).
    """

    message_id: int
    user_id: int
    enrollment_id: int
    mailbox_id: int
    status: MessageStatus
    mode: StepMode | None
    rfc822_message_id: str
    scheduled_at: datetime | None
    gmail_message_id: str | None
    gmail_thread_id: str | None
    gmail_draft_id: str | None
    marked_missing: bool
    label: str
    thread_known: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ReconcileWork:
    """What :func:`reconcile_work` found for one user."""

    leftovers: tuple[Tracked, ...] = ()
    drafts: tuple[Tracked, ...] = ()

    def mailboxes(self) -> list[int]:
        return sorted({t.mailbox_id for t in (*self.leftovers, *self.drafts)})


def _thread_known(session: Session, user: User, message: Message) -> frozenset[str]:
    if message.gmail_thread_id is None:
        return frozenset()
    held = session.scalars(
        scoped(user, Message)
        .with_only_columns(Message.gmail_message_id)
        .where(
            Message.gmail_thread_id == message.gmail_thread_id,
            Message.id != message.id,
            Message.gmail_message_id.is_not(None),
        )
    )
    return frozenset(value for value in held if value is not None)


def _tracked(
    session: Session, user: User, statement: Select[tuple[Message]], *, with_thread: bool
) -> tuple[Tracked, ...]:
    rows = session.execute(
        statement.add_columns(Mailbox, Campaign.name, CampaignStep.mode)
        .join(Enrollment, Enrollment.id == Message.enrollment_id)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .join(Mailbox, Mailbox.id == Campaign.mailbox_id)
        .outerjoin(
            CampaignStep,
            and_(CampaignStep.id == Message.step_id, CampaignStep.user_id == user.id),
        )
        .where(
            Enrollment.user_id == user.id,
            Campaign.user_id == user.id,
            Mailbox.user_id == user.id,
            Message.channel == TemplateChannel.EMAIL,
            Message.direction == MessageDirection.OUT,
        )
        .order_by(Message.id)
        .execution_options(populate_existing=True)
    ).tuples()
    found: list[Tracked] = []
    for message, mailbox, campaign_name, mode in rows:
        try:
            rfc822 = message_id_for(
                user_id=user.id,
                message_id=message.id,
                created_at=message.created_at,
                address=mailbox.email,
            )
        except ComposeError:
            log.error("message %d has no Message-ID to search for; left as it is", message.id)
            continue
        found.append(
            Tracked(
                message_id=message.id,
                user_id=user.id,
                enrollment_id=message.enrollment_id,
                mailbox_id=mailbox.id,
                status=message.status,
                mode=mode,
                rfc822_message_id=rfc822,
                scheduled_at=message.scheduled_at,
                gmail_message_id=message.gmail_message_id,
                gmail_thread_id=message.gmail_thread_id,
                gmail_draft_id=message.gmail_draft_id,
                marked_missing=message.error == DRAFT_MISSING,
                label=campaign_label(mailbox.label_prefix, campaign_name),
                thread_known=(
                    _thread_known(session, user, message)
                    | frozenset(message.thread_known_json or ())
                    if with_thread
                    else frozenset()
                ),
            )
        )
    return tuple(found)


def reconcile_work(session: Session, user: User, *, now: datetime) -> ReconcileWork:
    """What the sender has to look up for ``user``. Read-only.

    - **Leftovers:** ``scheduled`` for :data:`RECONCILE_AFTER` or more (#269), and
      not searched in the last :data:`RECONCILE_SEARCH_EVERY`. At most
      :data:`RECONCILE_BATCH` per mailbox, so one that cannot be searched never
      holds another mailbox's slots (#273 review).
    - **Drafts:** ``drafted`` with a Gmail draft id, for the drafts poll (spec 11.5).

    A ``discarded`` message's Gmail draft, if it has one, is left where it is:
    netkeeper deletes nothing in Gmail (ADR 0003; #273, question 2).
    """
    due = scoped(user, Message).where(
        Message.status == MessageStatus.SCHEDULED,
        Message.scheduled_at <= now - RECONCILE_AFTER,
        or_(
            Message.reconcile_last_miss_at.is_(None),
            Message.reconcile_last_miss_at <= now - RECONCILE_SEARCH_EVERY,
        ),
    )
    mailbox_ids = session.scalars(
        scoped(user, Mailbox).with_only_columns(Mailbox.id).order_by(Mailbox.id)
    ).all()
    leftovers = tuple(
        tracked
        for mailbox_id in mailbox_ids
        for tracked in _tracked(
            session,
            user,
            due.where(Mailbox.id == mailbox_id).limit(RECONCILE_BATCH),
            with_thread=False,
        )
    )
    drafts = _tracked(
        session,
        user,
        scoped(user, Message).where(
            Message.status == MessageStatus.DRAFTED, Message.gmail_draft_id.is_not(None)
        ),
        with_thread=True,
    )
    return ReconcileWork(leftovers, drafts)


def _tracked_message(
    session: Session, user: User, message_id: int, expect: Collection[MessageStatus]
) -> Message | None:
    """The message, read fresh, if it is still in one of ``expect``; else None (logged)."""
    message = session.scalars(
        scoped(user, Message)
        .where(Message.id == message_id)
        .execution_options(populate_existing=True)
    ).first()
    if message is None or message.status not in expect:
        log.info("message %d changed while it was looked up; left as it is", message_id)
        return None
    return message


def _step_position(session: Session, user: User, step_id: int | None) -> int | None:
    if step_id is None:
        return None
    return session.scalar(
        scoped(user, CampaignStep)
        .with_only_columns(CampaignStep.position)
        .where(CampaignStep.id == step_id)
    )


def _after_settling(
    session: Session, user: User, settings: Settings, message: Message, *, fired: bool
) -> None:
    """The enrollment's next step, now that the message's outcome is known."""
    enrollment = _enrollment(session, user, message.enrollment_id)
    if fired:
        position = _step_position(session, user, message.step_id)
        if position is not None:
            enrollment.current_step = max(enrollment.current_step or 0, position)
    campaign = _campaign(session, user, enrollment.campaign_id)
    _advance(session, user, settings, campaign, enrollment)
    session.flush()


def settle_sent(
    session: Session,
    user: User,
    settings: Settings,
    message_id: int,
    *,
    expect: Collection[MessageStatus],
    at: datetime,
    gmail_message_id: str,
    gmail_thread_id: str,
) -> bool:
    """Gmail has the message as sent: ``sent`` at ``at``, and the next step scheduled
    from it (spec 11.3). For a leftover found by its Message-ID, a draft seen sent,
    and a discarded draft the person sent anyway. False when the message moved on."""
    _require_writer(session, "settle_sent")
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    message = _tracked_message(session, user, message_id, expect)
    if message is None:
        return False
    message.status = MessageStatus.SENT
    message.sent_at = at
    message.error = None
    message.gmail_message_id = gmail_message_id
    message.gmail_thread_id = gmail_thread_id
    session.flush()
    _after_settling(session, user, settings, message, fired=True)
    log.info("message %d is sent (found in Gmail)", message_id)
    return True


def settle_drafted(
    session: Session,
    user: User,
    settings: Settings,
    message_id: int,
    *,
    gmail_message_id: str,
    gmail_thread_id: str,
    gmail_draft_id: str | None,
    thread_known: Collection[str] = (),
) -> bool:
    """A leftover Gmail has as a draft: ``drafted``, waiting for the person.
    ``thread_known`` is the other Gmail message ids in its thread (:class:`SendResult`)."""
    _require_writer(session, "settle_drafted")
    message = _tracked_message(session, user, message_id, (MessageStatus.SCHEDULED,))
    if message is None:
        return False
    message.status = MessageStatus.DRAFTED
    message.error = None
    message.gmail_message_id = gmail_message_id
    message.gmail_thread_id = gmail_thread_id
    message.gmail_draft_id = gmail_draft_id
    message.thread_known_json = sorted(thread_known)
    session.flush()
    _after_settling(session, user, settings, message, fired=True)
    log.info("message %d is drafted (found in Gmail)", message_id)
    return True


def settle_not_sent(
    session: Session, user: User, settings: Settings, message_id: int, *, now: datetime
) -> bool:
    """A search for a leftover's Message-ID found nothing. It is never sent again (#269).

    One empty search rules nothing out: Gmail's search can lag a send. The miss is
    counted, and the message stays ``scheduled`` until :data:`RECONCILE_GIVE_UP_MISSES`
    searches have found nothing over at least :data:`RECONCILE_GIVE_UP_AFTER` (#273
    review). Then:

    - ``discarded`` when nothing would send it anyway: its enrollment is over, or
      holds another message of the same step (a merge combined two).
    - Otherwise ``failed``, and the enrollment stays parked for a person.

    True once it is settled.
    """
    _require_writer(session, "settle_not_sent")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    message = _tracked_message(session, user, message_id, (MessageStatus.SCHEDULED,))
    if message is None:
        return False
    message.reconcile_misses += 1
    message.reconcile_first_miss_at = message.reconcile_first_miss_at or now
    message.reconcile_last_miss_at = now
    if (
        message.reconcile_misses < RECONCILE_GIVE_UP_MISSES
        or now - message.reconcile_first_miss_at < RECONCILE_GIVE_UP_AFTER
    ):
        message.error = (
            f"not found in Gmail yet ({message.reconcile_misses} searches);"
            " searching again, never sending again"
        )
        session.flush()
        log.info("message %d is not in Gmail's search yet; it waits", message_id)
        return False
    enrollment = _enrollment(session, user, message.enrollment_id)
    twin = session.scalar(
        scoped(user, Message)
        .with_only_columns(Message.id)
        .where(
            Message.enrollment_id == enrollment.id,
            Message.step_id == message.step_id,
            Message.direction == MessageDirection.OUT,
            Message.id != message.id,
            Message.status.not_in((MessageStatus.DISCARDED, MessageStatus.FAILED)),
        )
        .limit(1)
    )
    if enrollment.status not in LIVE_STATUSES or (message.step_id is not None and twin):
        message.status = MessageStatus.DISCARDED
        message.error = None
        session.flush()
        _after_settling(session, user, settings, message, fired=False)
        log.info("message %d never reached Gmail; discarded", message_id)
    else:
        message.status = MessageStatus.FAILED
        message.error = "not in Gmail after a crash or an unknown outcome; not sent again"
        session.flush()
        log.warning("message %d never reached Gmail; it waits for a person", message_id)
    return True


def settle_draft_missing(session: Session, user: User, message_id: int) -> bool:
    """The draft is gone and nothing in its thread was sent. The first time, it is
    marked (:data:`DRAFT_MISSING`); the second, the message is ``discarded`` and the
    enrollment ``removed`` (spec 11.5). True once it is discarded."""
    _require_writer(session, "settle_draft_missing")
    message = _tracked_message(session, user, message_id, (MessageStatus.DRAFTED,))
    if message is None:
        return False
    if message.error != DRAFT_MISSING:
        message.error = DRAFT_MISSING
        session.flush()
        return False
    message.status = MessageStatus.DISCARDED
    message.error = None
    message.gmail_draft_id = None  # gone from Gmail already
    enrollment = _enrollment(session, user, message.enrollment_id)
    if enrollment.status in REMOVABLE_STATUSES:
        _end(session, user, enrollment, EnrollmentStatus.REMOVED, DRAFT_DISCARDED_REASON)
    session.flush()
    log.info("message %d: its draft was deleted, not sent; discarded", message_id)
    return True


def settle_draft_present(session: Session, user: User, message_id: int) -> None:
    """The draft is still there: a mark from an earlier poll no longer holds."""
    _require_writer(session, "settle_draft_present")
    message = _tracked_message(session, user, message_id, (MessageStatus.DRAFTED,))
    if message is not None and message.error == DRAFT_MISSING:
        message.error = None
        session.flush()


def tick_user(
    factory: sessionmaker[Session],
    user_id: int,
    *,
    settings: Settings,
    sender: Sender,
    clock: Callable[[], datetime],
    rng: random.Random,
    stopping: Callable[[], bool] = lambda: False,
) -> TickResult:
    """One user's tick: reconcile, choose and claim, send with no session open, record.
    Blocking.

    A sender that is a :class:`Reconciler` looks up what it sent before, first
    (P3-07). The time is read once the writer session holds the write lock, not
    before: the lock can take up to the busy timeout to get, and a claim decided on
    the time from before the wait could land after the window closed (#264 review).
    Nothing is claimed once ``stopping()`` is true: a claim is a send to come.
    """
    result = TickResult(user_id)
    if isinstance(sender, Reconciler) and not stopping():
        try:
            sender.reconcile(factory, user_id, settings=settings, now=clock())
        except Exception:  # what it could not settle waits for the next tick
            log.exception("reconciling campaign messages failed for user %d", user_id)
    claims: list[_Claim] = []
    for _ in range(BATCH_PER_TICK):
        if stopping():
            break
        with session_scope(factory, write=True) as session:
            user = session.get(User, user_id)  # the first statement: the write lock is held
            if user is None:
                return result
            now = clock()
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
    stopping: Callable[[], bool] = lambda: False,
) -> list[TickResult]:
    """One tick for every local user. Blocking: run it off the event loop.

    Without a sender nothing is read or written. Once ``stopping()`` is true, no
    user's tick starts and nothing more is claimed (:meth:`CampaignEngine.stop`).
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
        if stopping():
            break
        try:
            results.append(
                tick_user(
                    factory,
                    user_id,
                    settings=settings,
                    sender=sender,
                    clock=clock,
                    rng=draw,
                    stopping=stopping,
                )
            )
        except Exception:  # one user's failure is not the next user's
            log.exception("campaign tick failed for user %d", user_id)
    return results


# --- the minute loop ----------------------------------------------------------------


STOP_WAIT_S: Final = 45.0
"""How long :meth:`CampaignEngine.stop` waits for a tick in flight: a send and the
search after an unknown outcome, each under the client's 30-second timeout."""


class CampaignEngine:
    """The minute tick ``netkeeper serve`` runs (spec 11.4), each tick in a worker thread.

    Like :class:`~netkeeper.services.mailboxes.MailboxMonitor`: no SQLite write
    happens on the event loop's thread, so a request holding the write lock can
    always reach its commit (#259).

    **Stopping** (#269). A tick's thread cannot be cancelled, and a send in it
    keeps going after the loop's task is. :meth:`stop` therefore asks the tick
    to claim nothing more, and waits for it, up to ``stop_wait_s``, so its send
    is recorded before the database is disposed. A tick that outlasts the wait
    is left to finish on its own: whatever it cannot record stays ``scheduled``,
    and the next start's reconcile finds it by its Message-ID. Nothing is sent
    twice either way.
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
        stop_wait_s: float = STOP_WAIT_S,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("the campaign tick interval must be positive")
        if stop_wait_s < 0:
            raise ValueError("the stop wait cannot be negative")
        self._factory = factory
        self._settings = settings
        self._sender = sender
        self._interval_s = interval_s
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 -- spacing, not crypto
        self._stop_wait_s = stop_wait_s
        self._task: asyncio.Task[None] | None = None
        self._stopping = threading.Event()
        self._inflight: asyncio.Future[list[TickResult]] | None = None

    @property
    def sender(self) -> Sender | None:
        return self._sender

    def start(self) -> None:
        if self._sender is None:
            log.info("the campaign engine has no sender; nothing will fire")
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.get_running_loop().create_task(self._run(), name="campaign-tick")

    async def stop(self) -> bool:
        """Stop the minute loop and wait for a tick in flight (see the class).

        True when nothing is left running; False when a tick outlasted the wait.
        """
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        inflight = self._inflight
        if inflight is None or inflight.done():
            return True
        try:
            await asyncio.wait_for(asyncio.shield(inflight), timeout=self._stop_wait_s)
        except TimeoutError:
            log.warning(
                "a campaign tick is still running after %.0f s; what it cannot record "
                "is reconciled on the next start",
                self._stop_wait_s,
            )
            return False
        except Exception:
            log.exception("the campaign tick in flight failed while stopping")
        return True

    async def tick_once(self) -> list[TickResult]:
        """One tick, off the loop. Nothing is claimed once :meth:`stop` has begun."""
        if self._inflight is not None and not self._inflight.done():
            raise RuntimeError("a campaign tick is already running")
        self._inflight = asyncio.ensure_future(
            asyncio.to_thread(
                run_tick,
                self._factory,
                settings=self._settings,
                sender=self._sender,
                clock=self._clock,
                rng=self._rng,
                stopping=self._stopping.is_set,
            )
        )
        # Shielded: cancelling the loop's task must not orphan the thread's future,
        # which stop() waits on.
        return await asyncio.shield(self._inflight)

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
