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
- :func:`activate`: the campaign goes from ``reviewing`` to ``active`` with a
  scheduled start (``starts_at``, #338), and each ``pending`` enrollment becomes
  ``active``, its first step due at the start, or after the step's delay
  (:func:`netkeeper.campaigns.schedule.step_due`). :func:`set_start` moves the
  start until the campaign's first message fires.
- A step fires (the tick): ``current_step`` advances, and ``next_action_at`` is
  the next step's delay after the enrollment's **latest sent outbound message**,
  at the step's own time of day, or else the next suggested send slot after
  that (#338). Spec 11.3 says the previous step's actual
  ``sent_at``; after a merge the enrollment can hold a newer message from the
  other contact, so the latest one decides (#242 review). After the last step,
  ``completed``.
- :func:`pause_campaign` and :func:`resume_campaign` move the campaign only.
  The tick fires only for an ``active`` enrollment of an ``active`` campaign, so
  a paused campaign fires nothing, and each enrollment keeps its own state and
  ``next_action_at``. Pausing enrollments as well would lose, on resume, which
  of them a person (or a merge) had paused on their own.
- :func:`end_campaign` moves an ``active`` or ``paused`` campaign to
  ``completed`` for good (#345), the campaign only, as a pause does. A
  ``completed`` or ``archived`` campaign is never selected, so it never fires.
- :func:`pause_enrollment`, :func:`resume_enrollment` and :func:`remove_enrollment`
  are the per-enrollment moves.
- ``replied``, ``bounced`` and ``opted_out`` come from detection (P3-08), and
  from the tick's own checks below.

The tick
--------
:func:`run_tick` runs every minute (:class:`CampaignEngine`). For each local user:

1. **Choose**, in a writer session. Candidates are enrollments that are
   ``active``, of a campaign that is ``active`` and whose scheduled start
   (``starts_at``) has come, with ``next_action_at <= now``, oldest due first. A
   campaign with no start, or one still to come, is never a candidate, and is
   refused again below if it ever were (#338). Status is what selects: a held pause keeps
   ``next_action_at``, so a due time alone means nothing (#242 review). A
   candidate whose next step is on LinkedIn is left unfired and reported as ready
   to prefill: a person claims it (:mod:`netkeeper.services.linkedin_steps`,
   P4-09). The tick never claims one, and never touches a browser.
   Blocked and LinkedIn rows are left out of the query itself, so however many
   there are, they never crowd out a row that could fire.
   For the rest, in this order, what the campaign or mailbox decides first:

   - Its campaign or mailbox is already blocked this tick: skipped.
   - The campaign has not started (``starts_at`` unset or still to come): the
     campaign is blocked until then.
   - There is no send window (#338). A time zone or holiday list that cannot be
     read blocks the campaign: nothing is sent under a schedule nobody can read.
   - :func:`netkeeper.campaigns.schedule.hold` decides whether it may go now. The
     campaign's start is unbounded: step 1 on the start's local day goes, whatever
     the hour. Anything else keeps the user's sending hours
     (:mod:`netkeeper.services.sending_hours`): a leftover from an earlier day
     spills to today at its own time of day, and anything outside the hours waits
     for their next opening. Sending hours that cannot be read block the campaign.
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
:data:`NOT_SENT_GIVE_UP_TRIES` tries in a row fail the step, with the reason, for a
person (#280). Any other outcome ends the run.

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
from netkeeper.campaigns.render import MergeValues, TemplateRenderError, render
from netkeeper.campaigns.templates import activation_errors, block_reason, contact_fields
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
    MailboxArm,
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
from netkeeper.scoping import get_scoped, scoped, scoped_contacts
from netkeeper.services import inbox_hold, sending_hours
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
review, in another campaign ...) is checked again this much later."""

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
    {MessageStatus.SCHEDULED, MessageStatus.DRAFTED, MessageStatus.PREFILLED, MessageStatus.STALE}
)
"""An outbound message in one of these has not gone out yet, and nobody knows it failed. A
``stale`` prefill may still be sent by the person (P4-02 then records it)."""


class CampaignEngineError(Exception):
    """A state machine move that is not allowed from where the campaign or enrollment is."""


class Skip(enum.StrEnum):
    """Why the tick did not fire a due enrollment, beyond the guards' own reasons."""

    LINKEDIN_STEP = "linkedin_step"
    READY_TO_PREFILL = "ready_to_prefill"
    CAMPAIGN_BLOCKED = "campaign_blocked"
    REPLIED = "replied"
    STEP_ALREADY_SENT = "step_already_sent"
    WAITING_ON_UNSENT = "waiting_on_unsent"
    NOT_DUE = "not_due"
    NOT_STARTED = "not_started"
    SPILLED = "spilled_to_next_day"
    OUTSIDE_SENDING_HOURS = "outside_sending_hours"
    BAD_SCHEDULE = "bad_schedule"
    CAMPAIGN_AT_CAP = "campaign_at_cap"
    SPACING = "spacing"
    TEMPLATE_ERRORS = "template_errors"
    RENDER_FAILED = "render_failed"
    NO_ADDRESS = "no_address"
    MAILBOX_ADDRESS = "mailbox_address"
    MAILBOX_DISARMED = "mailbox_disarmed"
    GUARD_EXCLUDED = "guard_excluded"
    LINKEDIN_INBOX_STALE = "linkedin_inbox_stale"
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


@runtime_checkable
class ArmGated(Protocol):
    """A sender that fires only on a mailbox a person armed (#277). The engine asks
    :meth:`armed` in each claim's writer session, before anything else about the step:

    - None (disarmed, or no such mailbox): nothing on the mailbox is claimed, and every
      step on it stays due as it is.
    - ``draft``: every email step on it is a draft, a ``send`` step included.
    - ``send``: each step fires in its own mode.

    A sender without it (the tests' fakes) fires on every mailbox."""

    def armed(self, session: Session, user: User, mailbox_id: int) -> MailboxArm | None: ...


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
    overridden: tuple[int, ...] = ()
    override_refused: tuple[tuple[int, str], ...] = ()
    """``(contact_id, why)`` for each override :func:`check_enrollment` refused (#446)."""
    """The contacts enrolled only because a person overrode the recent-contact guard (#446)."""


ENROLLING_STATUSES: Final[frozenset[CampaignStatus]] = frozenset(
    {CampaignStatus.DRAFT, CampaignStatus.REVIEWING}
)
"""A campaign takes new enrollments only before it is activated: after that, every
contact it sends to went through the review (spec 11.8)."""


def enroll(
    session: Session,
    user: User,
    campaign_id: int,
    contact_ids: Collection[int],
    *,
    now: datetime,
    override_recent_contact: Mapping[int, datetime] | None = None,
) -> EnrollResult:
    """Enroll each of ``contact_ids`` the guards pass (spec 11.9) as ``pending``.

    A contact already enrolled in the campaign is left as it is and reported in
    ``already``. Refused for a campaign past review (:data:`ENROLLING_STATUSES`).

    ``override_recent_contact`` (#446) sets aside the recent-contact guard for exactly
    those contacts, and no other guard
    (:func:`~netkeeper.services.campaign_guards.check_enrollment`). An enrollment the
    override let in records when, by whom, and the newest contact it set aside (the
    cutoff), and keeps the override at every step fire for contact dated at or before
    that cutoff (:func:`~netkeeper.services.campaign_guards.check_step`).
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
    verdicts = check_enrollment(
        session, user, campaign, fresh, now=now, override_recent_contact=override_recent_contact
    )
    enrolled = [v.contact_id for v in verdicts if v.eligible]
    overridden = [v.contact_id for v in verdicts if v.eligible and v.overridden]
    _ask_for_linkedin_ids(session, user, campaign_id, verdicts)
    for verdict in verdicts:
        if not verdict.eligible:
            continue
        session.add(
            Enrollment(
                user_id=user.id,
                campaign_id=campaign_id,
                contact_id=verdict.contact_id,
                status=EnrollmentStatus.PENDING,
                recent_contact_override_at=now if verdict.overridden else None,
                recent_contact_override_by=user.id if verdict.overridden else None,
                recent_contact_cutoff=verdict.override_cutoff,
            )
        )
    session.flush()
    if overridden:
        log.info(
            "campaign %d: recent-contact guard overridden for %d contacts by user %d",
            campaign_id,
            len(overridden),
            user.id,
        )
    log.info(
        "campaign %d: %d enrolled, %d excluded, %d already in",
        campaign_id,
        len(enrolled),
        len(verdicts) - len(enrolled),
        len(present),
    )
    refused = tuple(
        (v.contact_id, v.override_refused) for v in verdicts if v.override_refused is not None
    )
    return EnrollResult(
        tuple(enrolled), tuple(sorted(present)), tuple(verdicts), tuple(overridden), refused
    )


LINKEDIN_ENRICH_PRIORITY: Final = 1
"""``contacts.enrich_priority`` a contact gets at enrollment when its campaign has a
LinkedIn step and it has no ``li_urn`` (spec 9.6's first tier; P4-09). A higher ask
already there is kept."""


def _ask_for_linkedin_ids(
    session: Session, user: User, campaign_id: int, verdicts: Iterable[Verdict]
) -> None:
    """Raise the enrichment priority of each contact enrolled, or kept out only for its
    missing LinkedIn member id, when the campaign has a LinkedIn step (P4-09). An
    email-only campaign changes nothing.

    The member id itself comes from a connections sync, not enrichment: enrichment
    visits only contacts that already have one (``enrich_plan._eligible``). The raise
    is spec 9.6's first tier, so the contact is visited first once a sync adds it."""
    has_linkedin_step = session.scalar(
        scoped(user, CampaignStep)
        .with_only_columns(CampaignStep.id)
        .where(
            CampaignStep.campaign_id == campaign_id,
            CampaignStep.channel == TemplateChannel.LINKEDIN,
        )
        .limit(1)
    )
    if has_linkedin_step is None:
        return
    asked = [v.contact_id for v in verdicts if v.eligible or set(v.reasons) == {Reason.NO_LINKEDIN}]
    if not asked:
        return
    contacts = session.scalars(
        scoped_contacts(user).where(  # never the self contact (#342)
            Contact.id.in_(asked),
            Contact.li_urn.is_(None),
            Contact.enrich_priority < LINKEDIN_ENRICH_PRIORITY,
        )
    ).all()
    for contact in contacts:
        contact.enrich_priority = LINKEDIN_ENRICH_PRIORITY
    if contacts:
        session.flush()
        log.info(
            "campaign %d: %d contacts need a LinkedIn member id; enrichment asked for them",
            campaign_id,
            len(contacts),
        )


REVIEW_GATE: Final = object()
"""The token :func:`activate` needs. Only
:func:`netkeeper.services.campaign_review.activate` passes it, after the review gate
(spec 11.8) passed, and tests that stand in for it: nothing else may activate."""


def activate(
    session: Session,
    user: User,
    campaign_id: int,
    *,
    settings: Settings,
    now: datetime,
    starts_at: datetime,
    gate: object = None,
) -> Campaign:
    """``reviewing`` to ``active``, starting at ``starts_at``: every ``pending``
    enrollment becomes ``active``.

    Refused unless called through the review gate (``gate`` is :data:`REVIEW_GATE`:
    call :func:`netkeeper.services.campaign_review.activate`), and unless the campaign
    is ``reviewing`` with ``approved_at`` recorded (spec 11.8), has steps, has a mailbox when a step
    is email, and every step's template is free of lint errors.

    ``starts_at`` is the scheduled start (#338); one already past is ``now``. Nothing
    of the campaign is sent before it. Each first step is due at the start, or after
    its delay (:func:`netkeeper.campaigns.schedule.step_due`).
    """
    _require_writer(session, "activate")
    if gate is not REVIEW_GATE:
        raise CampaignEngineError("activate only through the review gate (campaign_review)")
    if starts_at.tzinfo is None or starts_at.utcoffset() is None:
        raise ValueError("starts_at must be timezone-aware")
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
    for step in steps:
        template = get_scoped(session, user, Template, step.template_id)
        if template is None or activation_errors(template):
            raise CampaignEngineError(f"step {step.position}'s template has lint errors")
    start = max(starts_at, now)
    first_due = first_step_due(settings, user, steps[0], start, hours_for(session, user))
    campaign.status = CampaignStatus.ACTIVE
    campaign.starts_at = start
    campaign.start_chosen = True
    pending = session.scalars(
        scoped(user, Enrollment).where(
            Enrollment.campaign_id == campaign_id, Enrollment.status == EnrollmentStatus.PENDING
        )
    ).all()
    for enrollment in pending:
        enrollment.status = EnrollmentStatus.ACTIVE
        enrollment.next_action_at = first_due
    session.flush()
    log.info(
        "campaign %d active with %d enrollments, starting %s",
        campaign_id,
        len(pending),
        start.isoformat(),
    )
    return campaign


START_EDITABLE: Final[frozenset[CampaignStatus]] = frozenset(
    {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}
)
"""A campaign whose scheduled start can move: activated, and not over."""


def has_fired(session: Session, user: User, campaign_id: int) -> bool:
    """Whether any outbound message of the campaign exists, whatever its status: once
    one does, the campaign has started sending, and its start is fixed (#338)."""
    found = session.scalar(
        scoped(user, Message)
        .with_only_columns(Message.id)
        .join(Enrollment, Enrollment.id == Message.enrollment_id)
        .where(
            Enrollment.user_id == user.id,
            Enrollment.campaign_id == campaign_id,
            Message.direction == MessageDirection.OUT,
        )
        .limit(1)
    )
    return found is not None


def set_start(
    session: Session,
    user: User,
    campaign_id: int,
    *,
    settings: Settings,
    now: datetime,
    starts_at: datetime,
) -> Campaign:
    """Move an ``active`` or ``paused`` campaign's scheduled start, until its first send.

    Refused once any outbound message of the campaign exists (:func:`has_fired`),
    whatever its status, ``scheduled`` with no ``sent_at`` included. The check runs in
    this writer transaction: on SQLite, ``BEGIN IMMEDIATE`` serializes it against the
    tick's claim. Where writers are not serialized that way (PostgreSQL's default
    isolation), a claim could commit between the check and the move; the tick's
    "step already sent" refusal (:func:`step_has_message`) still keeps the step from
    being sent twice. One already past is ``now``. Each live enrollment still waiting for
    step 1 is due again from the new start; a parked one stays parked.
    """
    _require_writer(session, "set_start")
    if starts_at.tzinfo is None or starts_at.utcoffset() is None:
        raise ValueError("starts_at must be timezone-aware")
    campaign = _campaign(session, user, campaign_id)
    if campaign.status not in START_EDITABLE:
        raise CampaignEngineError(
            f"campaign {campaign_id} is {campaign.status}; only an active or paused"
            " campaign has a start to change"
        )
    if has_fired(session, user, campaign_id):
        raise CampaignEngineError(
            f"campaign {campaign_id} has already sent; its start can no longer change"
        )
    steps = _steps(session, user, campaign_id)
    if not steps:
        raise CampaignEngineError(f"campaign {campaign_id} has no steps")
    start = max(starts_at, now)
    first_due = first_step_due(settings, user, steps[0], start, hours_for(session, user))
    campaign.starts_at = start
    campaign.start_chosen = True
    waiting = session.scalars(
        scoped(user, Enrollment).where(
            Enrollment.campaign_id == campaign_id,
            Enrollment.status.in_((EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED)),
            Enrollment.current_step.is_(None),
            Enrollment.next_action_at.is_not(None),
        )
    ).all()
    for enrollment in waiting:
        enrollment.next_action_at = first_due
    session.flush()
    log.info("campaign %d now starts %s", campaign_id, start.isoformat())
    return campaign


def reschedule_step(session: Session, user: User, step: CampaignStep, *, settings: Settings) -> int:
    """After a step's schedule changed: each live enrollment waiting for it is due again.

    Only enrollments with a due time are moved; a parked one stays parked. Step 1
    counts from the campaign's start, a later step from the enrollment's latest sent
    outbound message. Returns how many moved.
    """
    _require_writer(session, "reschedule_step")
    campaign = _campaign(session, user, step.campaign_id)
    steps = _steps(session, user, campaign.id)
    earlier = [s.position for s in steps if s.position < step.position]
    previous = max(earlier) if earlier else None
    statement = scoped(user, Enrollment).where(
        Enrollment.campaign_id == campaign.id,
        Enrollment.status.in_((EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED)),
        Enrollment.next_action_at.is_not(None),
    )
    statement = statement.where(
        Enrollment.current_step.is_(None)
        if previous is None
        else Enrollment.current_step == previous
    )
    moved = 0
    hours = hours_for(session, user)
    for enrollment in session.scalars(statement).all():
        if previous is None:
            if campaign.starts_at is None:
                continue
            enrollment.next_action_at = first_step_due(
                settings, user, step, campaign.starts_at, hours
            )
        else:
            latest = latest_fired(session, user, enrollment.id)
            if latest is None:
                continue
            enrollment.next_action_at = follow_up_due(settings, user, step, latest, hours)
        moved += 1
    session.flush()
    return moved


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


ENDABLE: Final[frozenset[CampaignStatus]] = frozenset(
    {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}
)
"""A campaign a person can end (#345): activated, and not over yet."""


def end_campaign(session: Session, user: User, campaign_id: int) -> Campaign:
    """``active`` or ``paused`` to ``completed``, for good (#345).

    Like a pause, it moves the campaign only: the tick fires nothing for a campaign
    that is not ``active``, and nothing moves a ``completed`` one back. Each
    enrollment keeps its state, so reply and bounce detection still record what
    comes back to a step already sent. A ``scheduled`` message stays for the
    reconcile to settle, and a Gmail draft stays the person's to send or delete,
    as for any other end (:func:`_end`).
    """
    _require_writer(session, "end_campaign")
    campaign = _campaign(session, user, campaign_id)
    if campaign.status not in ENDABLE:
        raise CampaignEngineError(
            f"campaign {campaign_id} is {campaign.status}; only an active or paused"
            " campaign can be ended"
        )
    campaign.status = CampaignStatus.COMPLETED
    session.flush()
    log.info("campaign %d ended", campaign_id)
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


def end_enrollment(
    session: Session,
    user: User,
    enrollment: Enrollment,
    status: EnrollmentStatus,
    reason: str,
) -> None:
    """Detection's end of an enrollment (P3-08): ``replied``, ``bounced`` or ``opted_out``.
    Changes no message, as :func:`_end` does not."""
    _require_writer(session, "end_enrollment")
    _end(session, user, enrollment, status, reason)


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


def latest_fired(session: Session, user: User, enrollment_id: int) -> datetime | None:
    """What the next step's delay counts from: the enrollment's latest sent outbound
    message, or the latest LinkedIn prefill the person discarded (``discarded_at``,
    P4-09), whichever is later. Only :func:`netkeeper.services.linkedin_steps.discard`
    sets ``discarded_at``, so with email alone this is :func:`_latest_sent`."""
    sent = _latest_sent(session, user, enrollment_id)
    discarded = session.scalar(
        scoped(user, Message)
        .with_only_columns(func.max(Message.discarded_at))
        .where(
            Message.enrollment_id == enrollment_id,
            Message.direction == MessageDirection.OUT,
            Message.channel == TemplateChannel.LINKEDIN,
            Message.status == MessageStatus.DISCARDED,
            Message.discarded_at.is_not(None),
        )
    )
    found = [at for at in (sent, discarded) if at is not None]
    return max(found) if found else None


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


# --- schedule, caps and spacing -----------------------------------------------------


def slots_for(settings: Settings, user: User) -> schedule.Suggested:
    """The user's suggested send slots and local day.
    Raises :class:`~netkeeper.campaigns.schedule.ScheduleError`."""
    return schedule.suggested(settings.campaigns, user.timezone)


def hours_for(session: Session, user: User) -> schedule.SendingHours | None:
    """The user's sending hours (``settings_kv``), or None when the stored value cannot be
    read: the tick then blocks every campaign, so nothing is sent under it."""
    try:
        return sending_hours.read(session, user)
    except schedule.ScheduleError:
        return None


def first_step_due(
    settings: Settings,
    user: User,
    step: CampaignStep,
    starts_at: datetime,
    hours: schedule.SendingHours | None = None,
) -> datetime:
    """When step 1 is due for a campaign starting at ``starts_at``: never before it.

    On the start's own local day the sending hours do not apply (the start is
    unbounded); a step 1 due on a later day goes to their next opening.

    A schedule that cannot be read leaves the start plus the delay; the tick then
    blocks the campaign, so nothing is sent under it."""
    try:
        slots = slots_for(settings, user)
        due = schedule.step_due(
            starts_at,
            delay_days=step.delay_days,
            send_time=step.send_time,
            slots=slots,
            first=True,
        )
        if hours is not None and slots.local_date(due) != slots.local_date(starts_at):
            due = schedule.next_opening(due, hours, slots)
        return due
    except schedule.ScheduleError:
        return starts_at + timedelta(days=step.delay_days)


def follow_up_due(
    settings: Settings,
    user: User,
    step: CampaignStep,
    latest: datetime,
    hours: schedule.SendingHours | None = None,
) -> datetime:
    """When a follow-up is due after the latest sent message: its own time of day, or
    the next suggested slot after its delay, then into the sending hours (an explicit
    time outside them waits for their next opening). Unreadable: the delay alone."""
    try:
        slots = slots_for(settings, user)
        due = schedule.step_due(
            latest,
            delay_days=step.delay_days,
            send_time=step.send_time,
            slots=slots,
        )
        return due if hours is None else schedule.next_opening(due, hours, slots)
    except schedule.ScheduleError:
        return latest + timedelta(days=step.delay_days)


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
    """Outbound email of the campaign fired in ``[since, until)``, any status. A LinkedIn
    prefill never counts: it has its own budget (``li_prefills``, P4-09)."""
    fired = func.coalesce(Message.scheduled_at, Message.sent_at)
    return int(
        session.scalar(
            scoped(user, Message)
            .with_only_columns(func.count(Message.id))
            .join(Enrollment, Enrollment.id == Message.enrollment_id)
            .where(
                Enrollment.user_id == user.id,
                Enrollment.campaign_id == campaign_id,
                Message.channel == TemplateChannel.EMAIL,
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
    slots: schedule.Suggested | None = None
    hours: schedule.SendingHours | None = None


def _next_step_join(user: User) -> ColumnElement[bool]:
    return and_(
        CampaignStep.user_id == user.id,
        CampaignStep.campaign_id == Enrollment.campaign_id,
        CampaignStep.position == func.coalesce(Enrollment.current_step, 0) + 1,
    )


def _selectable(user: User) -> Select[Enrollment]:
    """Enrollments the tick selects when their due time comes, whenever that is.

    The one definition of what the tick fires, with the due time left open:
    :func:`_selected` bounds it by ``now`` for the tick, :func:`upcoming` lists it
    for the dashboard, and :func:`_next_due` finds the soonest after ``now``, so
    none of them can drift from the others (#286). Selection is on status, never
    on the due time alone (#242 review).
    """
    return (
        scoped(user, Enrollment)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .outerjoin(CampaignStep, _next_step_join(user))
        .where(
            Campaign.user_id == user.id,
            Campaign.status == CampaignStatus.ACTIVE,
            Enrollment.status == EnrollmentStatus.ACTIVE,
            Enrollment.next_action_at.is_not(None),
        )
    )


PREFILL_NOT_TYPED_PREFIX: Final = "not_typed:"
"""How ``not_sent_error`` starts after a LinkedIn prefill that typed nothing (#445). With
``not_sent_count`` above zero, the step waits for a person to click Try again
(:func:`netkeeper.services.linkedin_steps.needs_try_again`)."""


def waits_for_try_again() -> ColumnElement[bool]:
    """SQL: the enrollment's latest LinkedIn prefill typed nothing, so its step waits for
    Try again (#445), never for a due time. Never NULL, so its negation keeps an
    enrollment with no ``not_sent_error``."""
    return and_(
        Enrollment.not_sent_count > 0,
        func.coalesce(Enrollment.not_sent_error, "").startswith(
            PREFILL_NOT_TYPED_PREFIX, autoescape=True
        ),
    )


def _not_on_linkedin() -> ColumnElement[bool]:
    """The next step, if any, is not on LinkedIn: the tick never fires one that is (P4)."""
    return or_(CampaignStep.id.is_(None), CampaignStep.channel != TemplateChannel.LINKEDIN)


def _selected(user: User, now: datetime) -> Select[Enrollment]:
    """Due enrollments, selected on status (#242 review), never on the due time alone,
    of a campaign whose scheduled start has come (#338)."""
    return _selectable(user).where(
        Enrollment.next_action_at <= now,
        Campaign.starts_at.is_not(None),
        Campaign.starts_at <= now,
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
    statement = _selected(user, now).add_columns(CampaignStep).where(_not_on_linkedin())
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
    )
    return list(rows)


def _linkedin_due(session: Session, user: User, now: datetime) -> list[int]:
    """Some of the due enrollments whose next step is on LinkedIn, to report them as ready
    to prefill (P4-09). :func:`netkeeper.services.linkedin_steps.ready_to_prefill` lists
    them from the same selection."""
    return list(
        session.scalars(
            _selected(user, now)
            .with_only_columns(Enrollment.id)
            .where(CampaignStep.channel == TemplateChannel.LINKEDIN, ~waits_for_try_again())
            .order_by(Enrollment.next_action_at, Enrollment.id)
            .limit(PAGE_SIZE)
        )
    )


PREFILL_STALE_AFTER: Final = timedelta(days=3)
"""A ``prefilled`` LinkedIn message not seen sent this long after its prefill goes
``stale`` (spec 11.6; P4-09). netkeeper never closes the tab it handed over, and a
stale message seen sent later still becomes ``sent`` (P4-02)."""


def mark_stale(session: Session, user: User, *, now: datetime) -> int:
    """Each ``prefilled`` LinkedIn message :data:`PREFILL_STALE_AFTER` or more after its
    prefill becomes ``stale``. Its enrollment stays parked, and the message waits for a
    person. Returns how many changed. Needs a writer session."""
    _require_writer(session, "mark_stale")
    stale = session.scalars(
        scoped(user, Message).where(
            Message.channel == TemplateChannel.LINKEDIN,
            Message.direction == MessageDirection.OUT,
            Message.status == MessageStatus.PREFILLED,
            Message.prefilled_at.is_not(None),
            Message.prefilled_at <= now - PREFILL_STALE_AFTER,
        )
    ).all()
    for message in stale:
        message.status = MessageStatus.STALE
        log.info(
            "message %d not seen sent %s after its prefill; stale", message.id, PREFILL_STALE_AFTER
        )
    if stale:
        session.flush()
    return len(stale)


@dataclass(frozen=True, slots=True)
class UpcomingFire:
    """One enrollment the tick will consider when its ``next_action_at`` comes (P3-12)."""

    enrollment: Enrollment
    due: datetime
    campaign: Campaign
    step: CampaignStep | None
    contact: Contact

    @property
    def on_linkedin(self) -> bool:
        """A LinkedIn step: once due, a person prefills it (P4-09); the tick never fires it."""
        return self.step is not None and self.step.channel is TemplateChannel.LINKEDIN

    def ready_to_prefill(self, now: datetime) -> bool:
        """A LinkedIn step due now, of a campaign that has started: ready to prefill."""
        started = self.campaign.starts_at is not None and self.campaign.starts_at <= now
        return self.on_linkedin and started and self.due <= now


def upcoming(
    session: Session, user: User, *, limit: int, include_linkedin: bool = False
) -> tuple[list[UpcomingFire], int]:
    """The next ``limit`` fires, soonest first, and how many there are in all.

    A read for the dashboard: nothing here changes a row. It is
    :func:`_selectable`, the tick's own selection with no bound on the due time,
    so a row already due is the next tick's and is listed first. As in
    :func:`_due`, a row whose next step is on LinkedIn is left out: the tick
    never fires it. ``include_linkedin`` lists those rows too, for the dashboard,
    where each is ready to prefill once due (:meth:`UpcomingFire.ready_to_prefill`,
    P4-09). A held pause keeps its ``next_action_at`` and is not listed (#242 review).
    """
    statement = (
        _selectable(user)
        .join(Contact, Contact.id == Enrollment.contact_id)
        .where(Contact.user_id == user.id)
    )
    if not include_linkedin:
        statement = statement.where(_not_on_linkedin())
    else:
        # A step waiting for Try again has no due time worth listing (#445).
        statement = statement.where(or_(_not_on_linkedin(), ~waits_for_try_again()))
    total = session.scalar(statement.with_only_columns(func.count(Enrollment.id)).order_by(None))
    rows = session.execute(
        statement.add_columns(Campaign, CampaignStep, Contact)
        .order_by(Enrollment.next_action_at, Enrollment.id)
        .limit(limit)
    )
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
    """The soonest due time, or campaign start, after ``now``. LinkedIn rows count (P4-09):
    the tick then lists them as ready to prefill, and never fires them."""
    due = session.scalar(
        _selectable(user)
        .with_only_columns(func.min(Enrollment.next_action_at))
        .where(Enrollment.next_action_at > now)
    )
    # A campaign still to start: its enrollments may already be due, and wait for it.
    starts = session.scalar(
        _selectable(user)
        .with_only_columns(func.min(Campaign.starts_at))
        .where(Campaign.starts_at > now)
    )
    found = [at for at in (due, starts) if at is not None]
    return min(found) if found else None


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
        gate: ArmGated | None = None,
    ) -> None:
        self.rng = rng
        self.gate = gate
        self.session = session
        self.user = user
        self.settings = settings
        self.now = now
        self.result = result
        self.blocks = _Blocks()
        self.wakes: list[datetime] = []
        self.campaigns: dict[int, Campaign] = {}
        self._stale: bool | None = None

    def _inbox_stale(self) -> bool:
        """Whether the LinkedIn inbox poll is stale (#417), read once per choose phase: a
        tick asks per due enrollment, and the answer does not outlive the phase."""
        if self._stale is None:
            self._stale = inbox_hold.stale(self.session, self.user, now=self.now)
        return self._stale

    def skip(self, enrollment: Enrollment, *reasons: str) -> None:
        self.result.decisions.append(Decision(enrollment.id, False, tuple(reasons)))

    def defer(self, enrollment: Enrollment, until: datetime, *reasons: str) -> None:
        """Move the due time to ``until``. There is no window to push it into (#338)."""
        enrollment.next_action_at = until
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
        # A prefill nobody sent goes stale (P4-09). LinkedIn messages only: nothing a
        # Gmail send left changes here.
        mark_stale(self.session, self.user, now=self.now)
        # Listed as ready to prefill (P4-09): a person claims each one
        # (services.linkedin_steps); the tick never does, and never touches a browser.
        for enrollment_id in _linkedin_due(self.session, self.user, self.now):
            self.result.decisions.append(Decision(enrollment_id, False, (Skip.READY_TO_PREFILL,)))
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
        arm: MailboxArm | None = MailboxArm.SEND
        if self.gate is not None:
            # First: a disarmed mailbox changes nothing on the enrollment (#277).
            arm = (
                None
                if campaign.mailbox_id is None
                else self.gate.armed(session, user, campaign.mailbox_id)
            )
            if arm is None:
                if campaign.mailbox_id is None:
                    self.block_campaign(campaign.id, (Skip.MAILBOX_DISARMED,))
                else:
                    self.block_mailbox(campaign.mailbox_id, (Skip.MAILBOX_DISARMED,))
                self.skip(enrollment, Skip.MAILBOX_DISARMED)
                return None

        # The scheduled start (#338). The query already leaves out a campaign that
        # has not started; this is the same rule again, where the claim is decided.
        if campaign.starts_at is None or campaign.starts_at > now:
            self.block_campaign(campaign.id, (Skip.NOT_STARTED,), campaign.starts_at)
            self.skip(enrollment, Skip.NOT_STARTED)
            return None

        try:
            slots = self.blocks.slots or slots_for(self.settings, user)
            hours = self.blocks.hours or sending_hours.read(session, user)
        except schedule.ScheduleError as exc:
            log.warning("campaign %d sends nothing: %s", campaign.id, exc)
            self.block_campaign(campaign.id, (Skip.BAD_SCHEDULE,))
            self.skip(enrollment, Skip.BAD_SCHEDULE)
            return None
        self.blocks.slots, self.blocks.hours = slots, hours

        # The start is unbounded, but only a start someone chose (``start_chosen``; 0031
        # backfilled ones are not); everything after it keeps the sending hours, and
        # with "any time" a leftover spills to its own time of day (schedule.hold).
        due = enrollment.next_action_at
        resume = (
            None
            if due is None
            else schedule.hold(
                due,
                now,
                slots=slots,
                hours=hours,
                starts_at=campaign.starts_at if campaign.start_chosen else None,
                first_step=enrollment.current_step is None,
            )
        )
        if resume is not None:
            self.defer(
                enrollment, resume, Skip.OUTSIDE_SENDING_HOURS if hours.enabled else Skip.SPILLED
            )
            return None

        # A cap reached today lifts at the local midnight; what is left of the batch
        # then spills to its own time of day (above).
        day_start, day_end = slots.day_bounds(now)
        tomorrow = day_end
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
        # The cadence counts from the latest step fired: a send, or a LinkedIn prefill
        # the person discarded (P4-09). With email alone it is the latest send.
        anchor = latest_fired(session, user, enrollment.id)
        if anchor is None and step.position > 1:
            # A later step with nothing sent before it: the earlier one failed or was
            # never seen going out. Nothing to count its delay from.
            self.park(enrollment, Skip.WAITING_ON_UNSENT)
            return None
        if anchor is not None:
            # The step's own timing (#338 review, S2): an explicit time of day may come
            # before the raw delay, and the suggested slot after it.
            due = follow_up_due(self.settings, user, step, anchor, self.blocks.hours)
            if due > now:
                self.defer(enrollment, due, Skip.NOT_DUE)
                return None

        verdict = check_step(session, user, enrollment, step, now=now)
        if not verdict.eligible:
            self._excluded(enrollment, campaign, verdict)
            return None
        # Last, so every check that already holds or ends the enrollment runs first and
        # this only ever adds a wait (#417): nothing on the enrollment changes, the step
        # stays due, and the next tick asks again. A reply was handled above.
        if self._inbox_stale() and inbox_hold.contact_watched(session, user, enrollment.contact_id):
            self.skip(enrollment, Skip.LINKEDIN_INBOX_STALE)
            return None
        mode = StepMode.DRAFT if arm is MailboxArm.DRAFT else step.mode
        return self._claim(campaign, enrollment, step, latest, mode)

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
        recheck = self.now + RECHECK_AFTER
        if self.blocks.slots is not None and self.blocks.hours is not None:
            recheck = schedule.next_opening(recheck, self.blocks.hours, self.blocks.slots)
        self.defer(enrollment, recheck, Skip.GUARD_EXCLUDED, *reasons)

    def _claim(
        self,
        campaign: Campaign,
        enrollment: Enrollment,
        step: CampaignStep,
        latest: datetime | None,
        mode: StepMode,
    ) -> _Claim | None:
        session, user = self.session, self.user
        template = get_scoped(session, user, Template, step.template_id)
        blocked = block_reason(template)
        if template is None or blocked is not None:
            # Said on the enrollment, so the campaign page shows why nothing sends (#342).
            enrollment.not_sent_error = blocked
            self.park(enrollment, Skip.TEMPLATE_ERRORS)
            return None
        contact = session.scalars(
            scoped_contacts(user)
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
        slots = self.blocks.slots
        assert slots is not None  # _consider set it before any claim
        today = slots.local_date(self.now)
        values = MergeValues(
            contact=contact_fields(contact, today),
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
            mode=mode,
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
    latest = latest_fired(session, user, enrollment.id)
    if latest is None or _waiting(session, user, enrollment.id):
        # A step not yet seen going out (a draft waiting for the person): the next
        # step's delay counts from when it does (schedule_next).
        enrollment.next_action_at = None
        return
    enrollment.next_action_at = follow_up_due(
        settings, user, upcoming, latest, hours_for(session, user)
    )
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
        end = max(at, now)
        if result.outcome is SendOutcome.UNKNOWN:
            # It may still go out after the answer was lost: space the mailbox from the
            # latest it could. Reconcile comes RECONCILE_AFTER later, after the next
            # claim, so re-spacing from Gmail's time then is too late for it (#280).
            end += UNKNOWN_SEND_END_MAX
        _set_next_send_at(session, user, firing.mailbox_id, end + claim.gap)
    session.flush()


UNKNOWN_SEND_END_MAX: Final = timedelta(seconds=30)
"""The latest a send whose answer never came can still go out, after it is recorded:
the Gmail client's request timeout (``campaigns.gmail.DEFAULT_TIMEOUT_S``). The mailbox's
next send is spaced from then, so a message Gmail sent late never has the next one
within the spacing floor of it (#280 review)."""


RETRY_AFTER: Final = timedelta(minutes=15)
"""A step whose send certainly sent nothing (:attr:`SendOutcome.NOT_SENT`) is due again
this much later the first time, and its mailbox sends nothing until then: an outage is
waited out, never turned into failed steps (#273 review). Each further try in a row
waits twice as long as the one before, up to :data:`RETRY_AFTER_MAX` (#280)."""

RETRY_AFTER_MAX: Final = timedelta(hours=2)
"""The longest an enrollment waits between tries that sent nothing (#280). Its mailbox
waits only :data:`RETRY_AFTER`: one enrollment's thread that cannot be read must not
hold every other enrollment on the mailbox for hours."""

NOT_SENT_GIVE_UP_TRIES: Final = 16
"""A step is failed for a person only after this many tries in a row sent nothing.

A try that sent nothing leaves no row behind, so without a limit a step whose thread
could never be read was tried every :data:`RETRY_AFTER` for good (#280).

Counted in tries, not hours (#338): retries wait for the sending hours, so elapsed
time says nothing about how often a step was tried. Sixteen tries at the waits of
:func:`retry_after` (15, 30 and 60 minutes, then two hours) take about 26 hours of
sending time: with "any time" that is the day of tries #280 asked for (8 tries and
24 hours before), and inside sending hours it spans several days. A step whose
campaign was paused between tries still gets all of them."""


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
    fire again, and the enrollment is due :func:`retry_after` later.

    Deleting the row is safe only because nothing reached Gmail: a send whose outcome
    is unknown is never given back. The next claim writes a new row, and so a new
    Message-ID.

    The try is counted on the enrollment. Once :data:`NOT_SENT_GIVE_UP_TRIES` in a
    row have sent nothing, nothing is
    given back: the message keeps its row, with the reason as its ``error``, and
    False is returned for the caller to record it ``failed`` (#280). True when the
    claim was given back.

    The retry is due ``retry_after`` later, or at the next opening of the sending
    hours when that falls outside them (#338). Only the campaign's start is exempt
    from the sending hours; a retry is not.
    """
    reason = (result.error or "no reason given")[:ERROR_MAX_LENGTH]
    # A merge may have moved the message to another enrollment meanwhile: follow it.
    enrollment = _enrollment(session, user, message.enrollment_id)
    enrollment.not_sent_count += 1
    enrollment.not_sent_since = enrollment.not_sent_since or now
    enrollment.not_sent_error = reason
    tries, since = enrollment.not_sent_count, enrollment.not_sent_since
    if tries >= NOT_SENT_GIVE_UP_TRIES:
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
        due = now + wait
        # A retry is never covered by the start-day exemption, even on the start's own
        # day: only the first try of step 1 goes outside the sending hours.
        window = hours_for(session, user)
        if window is not None:
            # A schedule that cannot be read keeps the plain retry: the tick blocks it.
            with contextlib.suppress(schedule.ScheduleError):
                due = schedule.next_opening(due, window, slots_for(settings, user))
        enrollment.next_action_at = due
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

SCHEDULED_IN_GMAIL: Final = "waiting in Gmail's Scheduled; recorded as sent once it goes out"
"""``messages.error`` of a draft (or a leftover) seen in Gmail's Scheduled (#278): the
person used Schedule send. It is never discarded while it is there. For display and
for clearing only: the dating of its send never reads it (:func:`seen_sent_at`)."""

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
    seen_scheduled: bool = False
    #: The last time a search found it waiting in Gmail's Scheduled, so not yet sent
    #: (#278 review); None when that is not known. See :func:`seen_in_scheduled_at`.
    seen_in_scheduled_at: datetime | None = None


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
    session: Session, user: User, statement: Select[Message], *, with_thread: bool
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
    )
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
                seen_scheduled=message.error == SCHEDULED_IN_GMAIL,
                seen_in_scheduled_at=seen_in_scheduled_at(message),
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
    if fired and (enrollment.not_sent_error or "").startswith(PREFILL_NOT_TYPED_PREFIX):
        # The step fired (a discard, say): an earlier try's "typed nothing" belonged to
        # it, and must not make the next step wait for Try again (#445).
        _clear_not_sent(session, user, enrollment.id)
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
    rng: random.Random | None = None,
) -> bool:
    """Gmail has the message as sent: ``sent`` at ``at``, and the next step scheduled
    from it (spec 11.3). For a leftover found by its Message-ID, a draft seen sent,
    and a discarded draft the person sent anyway. False when the message moved on.

    A leftover (``scheduled``) also moves its mailbox's next send time to no sooner
    than a new spacing gap (drawn with ``rng``) after ``at``: the record of an
    unknown outcome spaced the mailbox from the claim, and Gmail may have sent it
    later than that (#280). Never earlier than it already is.
    """
    _require_writer(session, "settle_sent")
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    message = _tracked_message(session, user, message_id, expect)
    if message is None:
        return False
    if message.status is MessageStatus.SCHEDULED:
        _space_after(session, user, settings, message, at, rng)
    message.status = MessageStatus.SENT
    message.sent_at = at
    message.error = None
    message.gmail_message_id = gmail_message_id
    message.gmail_thread_id = gmail_thread_id
    session.flush()
    _after_settling(session, user, settings, message, fired=True)
    log.info("message %d is sent (found in Gmail)", message_id)
    return True


def _space_after(
    session: Session,
    user: User,
    settings: Settings,
    message: Message,
    at: datetime,
    rng: random.Random | None,
) -> None:
    """The mailbox's next send time, no sooner than a spacing gap after ``at``."""
    enrollment = _enrollment(session, user, message.enrollment_id)
    campaign = _campaign(session, user, enrollment.campaign_id)
    if campaign.mailbox_id is None:
        return
    draw = rng if rng is not None else random.Random()  # noqa: S311 -- spacing, not crypto
    try:
        after = at + _spacing_gap(settings, draw)
    except ValueError:  # run_tick refuses to send with it; the record must not fail on it
        log.error("the send spacing is not one; mailbox %d is not re-spaced", campaign.mailbox_id)
        return
    stored = next_send_at(session, user, campaign.mailbox_id)
    if stored is None or after > stored:
        _set_next_send_at(session, user, campaign.mailbox_id, after)


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
    if message is not None and message.error in (DRAFT_MISSING, SCHEDULED_IN_GMAIL):
        message.error = None
        session.flush()


def settle_seen_scheduled(session: Session, user: User, message_id: int, *, now: datetime) -> None:
    """A search at ``now`` found the message waiting in Gmail's Scheduled (#278): marked
    (:data:`SCHEDULED_IN_GMAIL`), never discarded while it is there; a ``drafted`` one
    loses a missing mark.

    ``now`` is stamped in ``reconcile_last_miss_at`` with ``reconcile_first_miss_at``
    and ``reconcile_misses`` cleared (#278 review): a lower bound on the delivery that
    dates the send (:func:`seen_in_scheduled_at`). For a leftover (``scheduled``) it
    is also the search time, so it is searched again only after
    :data:`RECONCILE_SEARCH_EVERY` and never holds a :data:`RECONCILE_BATCH` slot each
    tick; its earlier misses were Gmail's search lagging, since Gmail has it. Nothing
    reads these columns for a ``drafted`` message otherwise."""
    _require_writer(session, "settle_seen_scheduled")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    message = _tracked_message(
        session, user, message_id, (MessageStatus.DRAFTED, MessageStatus.SCHEDULED)
    )
    if message is None:
        return
    message.error = SCHEDULED_IN_GMAIL
    message.reconcile_misses = 0
    message.reconcile_first_miss_at = None
    message.reconcile_last_miss_at = now
    session.flush()


def settle_draft_moved(
    session: Session,
    user: User,
    message_id: int,
    *,
    gmail_draft_id: str,
    gmail_message_id: str,
    gmail_thread_id: str,
) -> None:
    """The draft is back in Gmail's Drafts under a new draft id (#278 review): a canceled
    Schedule send. It is followed under its new ids, and any mark is cleared."""
    _require_writer(session, "settle_draft_moved")
    message = _tracked_message(session, user, message_id, (MessageStatus.DRAFTED,))
    if message is None:
        return
    message.gmail_draft_id = gmail_draft_id
    message.gmail_message_id = gmail_message_id
    message.gmail_thread_id = gmail_thread_id
    message.error = None
    session.flush()
    log.info("message %d: its draft is back in Gmail's Drafts under a new id", message_id)


def seen_in_scheduled_at(message: Message) -> datetime | None:
    """The last time a search found ``message`` in Gmail's Scheduled, or None.

    :func:`settle_seen_scheduled` stamps it in ``reconcile_last_miss_at`` and clears
    ``reconcile_first_miss_at``. :func:`settle_not_sent` always sets the first-miss
    time with the last, so a last time with no first one is a sighting in Scheduled.
    A leftover that missed after one (Gmail's search lagging, or the scheduled
    message deleted) has it overwritten and falls back to Gmail's date. The
    ``SCHEDULED_IN_GMAIL`` mark is never read for this: a missing mark replaces it.
    """
    if message.reconcile_first_miss_at is not None:
        return None
    return message.reconcile_last_miss_at


def seen_sent_at(tracked: Tracked, internal_date: datetime) -> datetime:
    """When a message found sent went out, for the next step's delay and the reply poll.

    Gmail's internal date, but for a message seen waiting in Scheduled no earlier than
    the last time it was seen there (#278 review): Gmail may keep the date it was
    scheduled. Both are no later than the delivery, so a reply that came right after
    it, before the poll that saw it sent, is still after ``sent_at`` and is recorded.
    The follow-up may count from up to a drafts poll before the delivery."""
    seen = tracked.seen_in_scheduled_at
    return internal_date if seen is None else max(internal_date, seen)


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
    the time from before the wait would be decided on a time already past (#264 review).
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
            gate = sender if isinstance(sender, ArmGated) else None
            claim = _Chooser(session, user, settings, now, result, rng, gate).choose()
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
