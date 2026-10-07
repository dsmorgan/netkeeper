"""A campaign's LinkedIn steps: ready to prefill, claimed by a person, recorded (spec 11.6; P4-09).

The minute tick (:mod:`netkeeper.services.campaign_engine`) never fires a LinkedIn
step, never claims one, and never touches a browser. A due LinkedIn step is
**ready to prefill**: :func:`ready_to_prefill` lists it from the tick's own
selection (``_selected``: an ``active`` enrollment of an ``active`` campaign whose
scheduled start has come, due now), oldest due first.

A person asks for one prefill at a time, while they watch Chrome
(``POST /campaigns/linkedin/prefill``, ``netkeeper campaigns linkedin prefill``).
:func:`claim_prefill` runs, in one writer transaction, every check an email fire
runs, and the LinkedIn ones besides. It refuses with the reasons when:

- the campaign or enrollment is not ``active``, or the campaign has not started
  (``starts_at`` unset or still to come, #338);
- the enrollment's next step is not a LinkedIn step, or is not due yet;
- the user's sending hours hold it (:func:`netkeeper.campaigns.schedule.hold`, the
  tick's own rule, #338): only step 1 on the start's local day is exempt;
- a reply is on the enrollment (it then ends ``replied``);
- the step already has an outbound message, whatever its status (never twice);
- another outbound message on the enrollment is still waiting;
- the cadence (the step's delay after the latest step fired: a send, or a prefill
  the person discarded, :func:`~netkeeper.services.campaign_engine.latest_fired`) has
  not passed;
- the guards (:func:`netkeeper.services.campaign_guards.check_step`) exclude the
  contact. A LinkedIn step needs the contact's ``li_urn``;
- a prefill is already open for the user: a LinkedIn message ``scheduled`` (claimed,
  its run not recorded) or ``prefilled`` less than three days ago (spec 11.6: one at a
  time). The refusal names the open message. A partial unique index (0036) holds the
  same rule in the database;
- it is outside ``[linkedin] active_hours`` (spec 9.5);
- the channel guard (:func:`netkeeper.services.campaign_guards.check_channel`)
  refuses: the session is flagged, heat is at its skip threshold, no evidence says
  the session is logged in, or today's ``li_prefills`` (or ``profile_visits``)
  budget is spent;
- the LinkedIn inbox poll is stale (:mod:`netkeeper.services.inbox_hold`, #417): no
  complete poll has run, or the newest is too old, so a reply could go unseen. The claim
  waits (``linkedin_inbox_stale``) and nothing changes; a complete poll releases it;
- the template has lint errors, the body does not render or renders empty, or the
  rendered message has an error for this contact (``rendered_errors``: the enrollment
  is parked with ``not_sent_error`` "blocked: <rules>");
- the run cannot be recorded (:func:`netkeeper.services.runs.create_run`: one running
  run per account; a ``message_send`` run is never scheduled, and is recorded only
  with the claim's gate token, :data:`netkeeper.services.runs.MESSAGE_SEND_GATE`).

**Auto-send** (ADR 0008, P4-04). With ``[campaigns] linkedin_auto_send`` on, the
scheduler (never a person) claims a due ``auto_send`` step through
:func:`claim_auto_send`: every check above, the step's mode ``auto_send``, and the
``li_messages_auto`` budget in place of ``li_prefills``. Its run is a **scheduled**
``message_send`` run, recorded only with :data:`netkeeper.services.runs.AUTO_SEND_GATE`.
A person may still prefill an ``auto_send`` step by hand; that never clicks Send. An
auto-sent message (``send_clicked``) is recorded ``prefilled`` with ``send_clicked_at``:
it waits on the inbox poll, not on the person, so it holds no open slot, and
:func:`waiting_for_you` lists it only once it goes ``stale``.

**Step approvals** (#339) are the review gate's: a campaign is ``active`` only once
every step, LinkedIn steps included, was approved, so a claim on a campaign that is
not ``active`` is refused first. LinkedIn steps have no test send.

On success the message is written ``scheduled`` with its rendered body, a manual
``message_send`` run is recorded and named on it (``sync_run_id``), and the
enrollment is parked (``next_action_at`` cleared), all in the one transaction. The
body is read from the message at run time; it never goes on the run row, in
``progress_json``, in a log, or in an error.

**Never twice.** A crash after the claim commits leaves the message ``scheduled``
and the enrollment parked; the step already has a message, so nothing claims it
again. The run left ``running`` is failed at the next start.

**Recording** (:func:`record_prefill_outcome`, called by P4-03's runner):

- ``prefilled``: ``prefilled_at`` (when typing started, before the first key), the
  conversation if the page loaded it, and the step counts as fired. The enrollment
  waits until the message is seen sent (P4-02).
- ``not_typed`` or ``too_long`` (refused before any key): the claim is given back: the
  message row is deleted, the reason and the run go on the enrollment
  (``not_sent_error``, ``last_prefill_run_id``), and it is parked for a person. Nothing
  claims it again on its own (#445).
- ``partially_typed`` or ``unknown``: ``failed`` with the reason, and the enrollment
  parked. Never retyped: a person clears the composer.

**Try again** (#445). A step whose latest prefill ended ``not_typed``
(:func:`needs_try_again`) leaves the ready list and is listed by :func:`try_again`
instead. Only a claim with ``retry=True``, which a person asks for one enrollment at a
time, claims it again (``try_again_needed`` otherwise), and every check above still
runs, except the due time. When the failed run clicked **Message**, or nothing says it
didn't, the retry also needs ``no_bubble_open=True``: the person says no message bubble
for that contact is open in Chrome (``confirm_no_bubble`` otherwise), since a page with
two bubbles refuses and a bubble may hold a draft. ``too_long``, ``partially_typed``
and ``unknown`` are never retried (``nothing_to_retry``): a body too long waits for the
template to change, and a half-typed message waits for a discard.

**Stale.** The engine's tick turns a ``prefilled`` message ``stale``
:data:`~netkeeper.services.campaign_engine.PREFILL_STALE_AFTER` after its prefill
(:func:`~netkeeper.services.campaign_engine.mark_stale`). The enrollment stays
parked, and the message waits for a person: :func:`waiting_for_you` lists it. A
claim marks them too, before it decides, and a ``prefilled`` message that old never
holds the one open slot.

**Interrupted.** A claimed message whose run ended without an outcome (a crash) stays
``scheduled`` and holds the one open slot: nobody knows what its composer holds.
:func:`waiting_for_you` lists it as interrupted, and :func:`discard` lets it go.

**Discard** (:func:`discard`): the message is ``discarded`` with ``discarded_at``, the
step counts as fired, and the enrollment moves to its next step, due that step's delay
after the discard (or after a later send), or completes.

Nothing here imports browser code (``tests/test_browser_safety.py``).
"""

from __future__ import annotations

import contextlib
import enum
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, Select, and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from netkeeper.campaigns import schedule
from netkeeper.campaigns.render import MergeValues, Severity, TemplateRenderError, render
from netkeeper.campaigns.templates import block_reason, contact_fields
from netkeeper.config import Settings
from netkeeper.linkedin.messaging import MessageOutcome, MessageOutcomeKind
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
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    Template,
    TemplateChannel,
    User,
)
from netkeeper.scoping import get_scoped, not_self, scoped, scoped_contacts
from netkeeper.services import budgets, inbox_hold, runs, sending_hours
from netkeeper.services import campaign_engine as engine
from netkeeper.services import heat as heat_service
from netkeeper.services.campaign_engine import PREFILL_STALE_AFTER, Skip
from netkeeper.services.campaign_guards import (
    ChannelState,
    Reason,
    check_channel,
    check_step,
)
from netkeeper.services.linkedin_accounts import account_id_for, ensure_account
from netkeeper.services.linkedin_session import last_session_evidence, session_flag
from netkeeper.services.settings_kv import delete_setting, get_setting, set_setting

log = logging.getLogger(__name__)

__all__ = [
    "PREFILL_STALE_AFTER",
    "PrefillClaim",
    "Refusal",
    "claim_auto_send",
    "claim_prefill",
    "discard",
    "linkedin_positions",
    "needs_try_again",
    "ready_to_prefill",
    "record_prefill_outcome",
    "try_again",
]

NOT_TYPED_PREFIX: Final = engine.PREFILL_NOT_TYPED_PREFIX
"""How an enrollment's ``not_sent_error`` starts after a prefill that typed nothing
(:func:`record_prefill_outcome`). With ``not_sent_count`` above zero, the step waits for
a person to try it again (:func:`needs_try_again`, #445)."""

READY_PAGE_MAX: Final = 50
"""The most rows one :func:`ready_to_prefill` or :func:`waiting_for_you` call answers."""

OPEN_STATUSES: Final[frozenset[MessageStatus]] = frozenset(
    {MessageStatus.SCHEDULED, MessageStatus.PREFILLED}
)
"""A LinkedIn message in one of these is an open prefill: claimed and not recorded yet, or
typed and not seen sent. A user has at most one (spec 11.6)."""

WAITING_STATUSES: Final[frozenset[MessageStatus]] = frozenset(
    {MessageStatus.PREFILLED, MessageStatus.STALE}
)
"""A LinkedIn message in one of these waits for the person: to send it, or to discard it."""


PARTLY_TYPED_PREFIXES: Final = ("partially_typed:", "unknown:")
"""How a ``failed`` LinkedIn message's ``error`` starts when the prefill stopped part way
through typing (``partially_typed``) or lost track of the composer (``unknown``)
(:func:`record_prefill_outcome`). Part of the body may sit in an open message bubble."""


def _is_partly_typed() -> ColumnElement[bool]:
    """SQL: a ``failed`` message that stopped part way. It holds the one-prefill slot and
    is listed in "waiting for you" until the person discards it (#383, ADR 0007). Keyed on
    the message alone, never on its enrollment: re-advancing the enrollment or merging
    its contact must not hide a bubble that may still hold the text. Callers add the
    LinkedIn outbound conditions."""
    return and_(
        Message.status == MessageStatus.FAILED,
        or_(
            *(Message.error.startswith(prefix, autoescape=True) for prefix in PARTLY_TYPED_PREFIXES)
        ),
    )


class Refusal(enum.StrEnum):
    """Why a claim was refused, beyond the engine's :class:`Skip` and the guards' reasons."""

    NOT_A_LINKEDIN_STEP = "not_a_linkedin_step"
    NO_STEP = "no_step"
    PREFILL_OPEN = "prefill_open"
    OUTSIDE_ACTIVE_HOURS = "outside_active_hours"
    BAD_ACTIVE_HOURS = "bad_active_hours"
    RUN_IN_PROGRESS = "run_in_progress"
    RUN_REFUSED = "run_refused"
    RENDERED_ERRORS = "rendered_errors"
    INBOX_STALE = "linkedin_inbox_stale"
    TRY_AGAIN_NEEDED = "try_again_needed"
    """The latest prefill typed nothing: only Try again (``retry``) claims it (#445)."""
    NOTHING_TO_RETRY = "nothing_to_retry"
    """A retry of a step whose latest prefill did not end ``not_typed`` (#445)."""
    CONFIRM_NO_BUBBLE = "confirm_no_bubble"
    """A retry after a run that clicked Message, without the person saying no message
    bubble for the contact is open (#445)."""
    AUTO_SEND_OFF = "auto_send_off"
    NOT_AUTO_SEND = "not_an_auto_send_step"


#: Refusals about the user, not the enrollment: every other enrollment would get the
#: same, so "prefill next" stops at the first. The sending hours are not one: a step 1
#: on its campaign's start day is exempt from them (#338).
USER_REFUSALS: Final[frozenset[str]] = frozenset(
    {
        Refusal.PREFILL_OPEN,
        Refusal.OUTSIDE_ACTIVE_HOURS,
        Refusal.BAD_ACTIVE_HOURS,
        Refusal.RUN_IN_PROGRESS,
        Refusal.RUN_REFUSED,
        Refusal.INBOX_STALE,
        Skip.BAD_SCHEDULE,
        "browser_unknown",
        "browser_unhealthy",
        "browser_out_of_budget",
    }
)


@dataclass(frozen=True, slots=True)
class PrefillClaim:
    """What :func:`claim_prefill` did. ``reasons`` is empty when it claimed; ``detail`` is
    one line for a person when a reason needs one (the active-hours window, a run's
    refusal). Never a message body."""

    enrollment_id: int
    message_id: int | None = None
    run_id: int | None = None
    reasons: tuple[str, ...] = ()
    detail: str | None = None

    @property
    def claimed(self) -> bool:
        return not self.reasons


StartRun = Callable[[Session, User, datetime], SyncRun]


def start_message_send_run(session: Session, user: User, now: datetime) -> SyncRun:
    """Record the manual ``message_send`` run a claim names (:func:`runs.create_run`)."""
    return runs.create_run(
        session,
        user,
        SyncRunKind.MESSAGE_SEND,
        trigger=SyncRunTrigger.MANUAL,
        now=now,
        gate=runs.MESSAGE_SEND_GATE,
    )


def auto_send_budget_warning(settings: Settings) -> str | None:
    """#447's warning for a daily ``li_messages_auto`` limit above 20
    (:func:`netkeeper.services.budgets.li_message_risk_warning`), to show wherever
    auto-send is on (ADR 0008). ``None`` while auto-send is off."""
    if not settings.campaigns.linkedin_auto_send:
        return None
    return budgets.li_message_risk_warning(
        budgets.ActionClass.LI_MESSAGES_AUTO, settings.linkedin.budget
    )


#: The run notes' prefixes for an auto-send (ADR 0008): :mod:`netkeeper.services.message_send`
#: writes them and the Waiting for you API reads them, so both split on the same words.
NOT_SENT_NOTE: Final = "not sent: "
SEND_UNCONFIRMED_NOTE: Final = "send not confirmed: "
BUBBLE_LEFT_OPEN_NOTE: Final = "bubble left open: "
NOT_STARTED_NOTE: Final = "not started: "

# --- the auto-send hold (ADR 0008, #384 review B1) -----------------------------------

#: Why auto-send stops until the person acts: an auto-send left a message bubble open in
#: Chrome (any refusal after its Message click, a Send whose bubble it could not close),
#: or refused a page that already held one. The next auto-send would refuse the same page,
#: so working through the next enrollments would only spend clicks and profile visits.
AUTO_SEND_HOLD_BUBBLE: Final = "a message bubble is open in Chrome"

#: Why auto-send stops when an auto-send run stopped mid-run with no outcome recorded (the
#: process went away): nobody knows what its tab and bubble hold.
AUTO_SEND_HOLD_INTERRUPTED: Final = "an auto-send stopped mid-run, so a message bubble may be open"

#: Why auto-send stops when a sent message's bubble closed but its tab didn't.
AUTO_SEND_HOLD_TAB: Final = "a tab is left open in Chrome"

#: What the person does to let auto-send go again.
AUTO_SEND_HOLD_CLEAR: Final = (
    "Close every LinkedIn message bubble (and the tab netkeeper left) in the netkeeper"
    " Chrome window, sending or discarding what is in it first. Then resume auto-send:"
    ' click "I closed the bubbles, resume auto-send" on the LinkedIn queue, or run'
    " `netkeeper linkedin auto-send-resume`."
)


@dataclass(frozen=True, slots=True)
class AutoSendHold:
    """Auto-send is held for the account: why (fixed words), since when, and the run."""

    reason: str
    since: datetime
    run_id: int | None


def _hold_key(account_id: int) -> str:
    return f"linkedin.auto_send.hold.{account_id}"


def auto_send_hold(session: Session, user: User, account_id: int) -> AutoSendHold | None:
    """The account's auto-send hold, or ``None``. Read-only."""
    raw = get_setting(session, user, _hold_key(account_id))
    if not isinstance(raw, dict):
        return None
    run_id = raw.get("run_id")
    return AutoSendHold(
        reason=str(raw.get("reason", AUTO_SEND_HOLD_BUBBLE)),
        since=datetime.fromisoformat(str(raw["since"])),
        run_id=run_id if isinstance(run_id, int) else None,
    )


def hold_auto_send(
    session: Session, user: User, account_id: int, *, reason: str, now: datetime, run_id: int
) -> None:
    """Hold auto-send for the account until :func:`resume_auto_send`. Needs a writer."""
    engine._require_writer(session, "hold_auto_send")
    set_setting(
        session,
        user,
        _hold_key(account_id),
        {"reason": reason, "since": now.isoformat(), "run_id": run_id},
    )
    log.warning("auto-send held for account %d after run %d: %s", account_id, run_id, reason)


def resume_auto_send(session: Session, user: User, account_id: int) -> bool:
    """The person closed the bubbles: auto-send may go again. True when a hold was lifted.
    Needs a writer."""
    engine._require_writer(session, "resume_auto_send")
    lifted = delete_setting(session, user, _hold_key(account_id))
    if lifted:
        log.info("auto-send resumed for account %d", account_id)
    return lifted


def start_auto_send_run(session: Session, user: User, now: datetime) -> SyncRun:
    """Record the **scheduled** ``message_send`` run an auto-send claim names (ADR 0008).
    :func:`runs.create_run` refuses it on a disarmed account, like any scheduled run."""
    return runs.create_run(
        session,
        user,
        SyncRunKind.MESSAGE_SEND,
        trigger=SyncRunTrigger.SCHEDULED,
        now=now,
        gate=runs.AUTO_SEND_GATE,
    )


# --- the ready list -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadyPrefill:
    """One due LinkedIn step. ``held_until`` is when the sending hours let it go, or None
    for now."""

    enrollment: Enrollment
    campaign: Campaign
    step: CampaignStep
    contact: Contact
    due: datetime
    held_until: datetime | None


def needs_try_again(enrollment: Enrollment) -> bool:
    """Whether the enrollment's step waits for a person to try its prefill again: the
    latest prefill typed nothing (``not_typed``, #445). :func:`_needs_try_again_sql` is
    the same rule in SQL; a test holds the two together."""
    return enrollment.not_sent_count > 0 and (enrollment.not_sent_error or "").startswith(
        NOT_TYPED_PREFIX
    )


def linkedin_positions(session: Session, user: User, campaign_id: int) -> frozenset[int]:
    """The positions of a campaign's LinkedIn steps: an enrollment row says it waits for
    Try again only when its next step is one of them (#445). Read-only."""
    return frozenset(
        step.position
        for step in engine._steps(session, user, campaign_id)
        if step.channel is TemplateChannel.LINKEDIN
    )


def _needs_try_again_sql() -> ColumnElement[bool]:
    """:func:`needs_try_again` in SQL (the engine's own, so its tick and dashboard leave
    these steps out too)."""
    return engine.waits_for_try_again()


def _ready_statement(user: User, now: datetime, campaign_id: int | None) -> Select[Enrollment]:
    """The tick's selection, narrowed to LinkedIn steps (and one campaign's, if given).
    A step waiting for Try again is :func:`try_again`'s, never here (#445)."""
    statement = (
        engine._selected(user, now)
        .join(Contact, Contact.id == Enrollment.contact_id)
        .where(
            Contact.user_id == user.id,
            not_self(),  # never the self contact (#342)
            CampaignStep.channel == TemplateChannel.LINKEDIN,
            ~_needs_try_again_sql(),
        )
    )
    if campaign_id is not None:
        statement = statement.where(Enrollment.campaign_id == campaign_id)
    return statement


def ready_by_step(
    session: Session, user: User, *, now: datetime, campaign_id: int
) -> dict[int, int]:
    """How many of one campaign's LinkedIn steps are ready to prefill, by step position:
    the campaign page's per-step count (#383). The same selection as
    :func:`ready_to_prefill`, counted rather than paged. Read-only."""
    rows = session.execute(
        _ready_statement(user, now, campaign_id)
        .with_only_columns(CampaignStep.position, func.count(Enrollment.id))
        .group_by(CampaignStep.position)
        .order_by(None)
    )
    return {position: n for position, n in rows}


def ready_to_prefill(
    session: Session,
    user: User,
    *,
    now: datetime,
    settings: Settings,
    limit: int,
    offset: int = 0,
    campaign_id: int | None = None,
) -> tuple[list[ReadyPrefill], int]:
    """Due enrollments whose next step is on LinkedIn, oldest due first, and how many.

    The tick's own selection (an ``active`` enrollment of an ``active`` campaign whose
    start has come, due by ``now``), so the two never drift. ``campaign_id`` keeps one
    campaign's (the campaign page's queue, #383). Read-only."""
    statement = _ready_statement(user, now, campaign_id)
    total = session.scalar(statement.with_only_columns(func.count(Enrollment.id)).order_by(None))
    rows = session.execute(
        statement.add_columns(Campaign, CampaignStep, Contact)
        .order_by(Enrollment.next_action_at, Enrollment.id)
        .offset(offset)
        .limit(min(limit, READY_PAGE_MAX))
    )
    hours = engine.hours_for(session, user)
    slots: schedule.Suggested | None = None
    with contextlib.suppress(schedule.ScheduleError):
        slots = engine.slots_for(settings, user)
    ready: list[ReadyPrefill] = []
    for enrollment, campaign, step, contact in rows:
        due = enrollment.next_action_at
        if due is None or step is None:  # the query's own conditions
            continue
        held = (
            None
            if hours is None or slots is None
            else schedule.hold(
                due,
                now,
                slots=slots,
                hours=hours,
                starts_at=campaign.starts_at if campaign.start_chosen else None,
                first_step=enrollment.current_step is None,
            )
        )
        ready.append(ReadyPrefill(enrollment, campaign, step, contact, due, held))
    return ready, total or 0


# --- try again (#445) -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LastTry:
    """What the latest prefill of a step waiting for Try again did. The run's facts are
    None when it isn't on file (a run deleted, or a try recorded before 0037)."""

    reason: str
    """The run's reason, the fixed words after ``not_typed: ``. Never message text."""
    tries: int
    """Prefills in a row that typed nothing."""
    run_id: int | None
    at: datetime | None
    """When the run ended."""
    click_attempted: bool | None
    """Whether the run clicked Message, so a message bubble may be open."""
    budget_spent: bool | None
    """Whether the run spent a ``li_prefills`` unit (and a ``profile_visits`` one)."""
    counted_today: bool | None
    """``budget_spent``, and spent on today's budget (the user's local day)."""

    @property
    def needs_confirmation(self) -> bool:
        """A retry asks the person to say no bubble is open unless the run surely never
        clicked Message: it said so, or it stopped before spending its budget, which comes
        before the navigation and so before any click (ADR 0007)."""
        return self.click_attempted is not False and self.budget_spent is not False


def last_try(session: Session, user: User, enrollment: Enrollment, *, now: datetime) -> LastTry:
    """The latest try of ``enrollment``'s step, which :func:`needs_try_again`. Read-only."""
    run = (
        None
        if enrollment.last_prefill_run_id is None
        else get_scoped(session, user, SyncRun, enrollment.last_prefill_run_id)
    )
    return _last_try(user, enrollment, run, now=now)


def _last_try(user: User, enrollment: Enrollment, run: SyncRun | None, *, now: datetime) -> LastTry:
    reason = (enrollment.not_sent_error or "").removeprefix(NOT_TYPED_PREFIX).strip()
    if run is None or run.stop_reason != MessageOutcomeKind.NOT_TYPED.value:
        return LastTry(reason, enrollment.not_sent_count, None, None, None, None, None)
    counts = run.counts_json or {}
    attempted = counts.get("message_click_attempted")
    spent = counts.get("li_prefills_spent")
    click_attempted = attempted if isinstance(attempted, bool) else None
    budget_spent = spent if isinstance(spent, bool) else None
    counted_today = (
        None
        if budget_spent is None
        else budget_spent
        and budgets.LocalPeriod.at(user, run.started_at).day
        == budgets.LocalPeriod.at(user, now).day
    )
    return LastTry(
        reason,
        enrollment.not_sent_count,
        run.id,
        run.completed_at,
        click_attempted,
        budget_spent,
        counted_today,
    )


@dataclass(frozen=True, slots=True)
class TryAgainPrefill:
    """One LinkedIn step whose latest prefill typed nothing. ``held_until`` is when the
    sending hours let a retry go, or None for now."""

    enrollment: Enrollment
    campaign: Campaign
    step: CampaignStep
    contact: Contact
    last: LastTry
    held_until: datetime | None


def try_again(
    session: Session,
    user: User,
    *,
    now: datetime,
    settings: Settings,
    campaign_id: int | None = None,
) -> list[TryAgainPrefill]:
    """Steps whose latest prefill ended ``not_typed``, oldest try first, at most
    :data:`READY_PAGE_MAX`: an ``active`` enrollment of an ``active`` campaign that has
    started, its next step on LinkedIn and not claimed, whatever its due time. Each waits
    for a person to click Try again. Read-only."""
    statement = (
        scoped(user, Enrollment)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .join(CampaignStep, engine._next_step_join(user))
        .join(Contact, Contact.id == Enrollment.contact_id)
        .where(
            Campaign.user_id == user.id,
            Contact.user_id == user.id,
            not_self(),
            Campaign.status == CampaignStatus.ACTIVE,
            Campaign.starts_at.is_not(None),
            Campaign.starts_at <= now,
            Enrollment.status == EnrollmentStatus.ACTIVE,
            CampaignStep.channel == TemplateChannel.LINKEDIN,
            _needs_try_again_sql(),
            # A retry claimed and not recorded yet is the open prefill, not a row here.
            ~scoped(user, Message)
            .with_only_columns(Message.id)
            .where(
                Message.enrollment_id == Enrollment.id,
                Message.step_id == CampaignStep.id,
                Message.direction == MessageDirection.OUT,
            )
            .exists(),
        )
    )
    if campaign_id is not None:
        statement = statement.where(Enrollment.campaign_id == campaign_id)
    rows = session.execute(
        statement.add_columns(Campaign, CampaignStep, Contact)
        .order_by(Enrollment.not_sent_since, Enrollment.id)
        .limit(READY_PAGE_MAX)
    )
    hours = engine.hours_for(session, user)
    slots: schedule.Suggested | None = None
    with contextlib.suppress(schedule.ScheduleError):
        slots = engine.slots_for(settings, user)
    listed = list(rows)
    # Every row's run in one query, not one each.
    run_ids = {e.last_prefill_run_id for e, *_ in listed if e.last_prefill_run_id is not None}
    runs_by_id = (
        {
            run.id: run
            for run in session.scalars(scoped(user, SyncRun).where(SyncRun.id.in_(run_ids)))
        }
        if run_ids
        else {}
    )
    found: list[TryAgainPrefill] = []
    for enrollment, campaign, step, contact in listed:
        held = (
            None
            if hours is None or slots is None
            else schedule.hold(
                now,
                now,
                slots=slots,
                hours=hours,
                starts_at=campaign.starts_at if campaign.start_chosen else None,
                first_step=enrollment.current_step is None,
            )
        )
        run = (
            None
            if enrollment.last_prefill_run_id is None
            else runs_by_id.get(enrollment.last_prefill_run_id)
        )
        last = _last_try(user, enrollment, run, now=now)
        found.append(TryAgainPrefill(enrollment, campaign, step, contact, last, held))
    return found


def prefills_left_today(session: Session, user: User, *, now: datetime, settings: Settings) -> int:
    """How many ``li_prefills`` units today's budget still has. Read-only."""
    snapshot = budgets.status(
        session,
        user,
        account_id_for(session, user),
        budgets.ActionClass.LI_PREFILLS,
        now=now,
        settings=settings.linkedin.budget,
    )
    return snapshot.day.remaining


def _no_retry_detail(enrollment: Enrollment) -> str:
    """Why a retry was refused, in one line: what the latest prefill ended as."""
    error = enrollment.not_sent_error or ""
    if error.startswith("too_long:"):
        return "the message is too long to type; it waits until the template changes"
    if error.startswith(PARTLY_TYPED_PREFIXES):
        return (
            "part of the message may be in the composer: clear it in Chrome, then discard"
            " it in Waiting for you"
        )
    return "only a step whose latest prefill typed nothing (not_typed) can be tried again"


# --- the claim ------------------------------------------------------------------------


def _open_prefill(session: Session, user: User, now: datetime) -> Message | None:
    """The user's open prefill, if there is one. A ``prefilled`` message
    :data:`PREFILL_STALE_AFTER` old no longer holds the slot, even before the tick
    marks it ``stale``."""
    return session.scalars(
        scoped(user, Message)
        .where(
            Message.channel == TemplateChannel.LINKEDIN,
            Message.direction == MessageDirection.OUT,
            or_(
                Message.status == MessageStatus.SCHEDULED,
                and_(
                    Message.status == MessageStatus.PREFILLED,
                    # An auto-sent message waits on the inbox poll, not the person.
                    Message.send_clicked_at.is_(None),
                    or_(
                        Message.prefilled_at.is_(None),
                        Message.prefilled_at > now - PREFILL_STALE_AFTER,
                    ),
                ),
                # Half-typed text may sit in an open bubble until the person discards it.
                _is_partly_typed(),
            ),
        )
        .order_by(Message.id)
        .limit(1)
    ).first()


def _open_detail(message: Message) -> str:
    return f"message {message.id} is {message.status.value}; send or discard it first"


def channel_state(
    session: Session,
    user: User,
    *,
    now: datetime,
    settings: Settings,
    action: budgets.ActionClass = budgets.ActionClass.LI_PREFILLS,
) -> ChannelState:
    """Spec 11.9's last bullet for LinkedIn. ``browser_ok``: no session flag, heat under its
    skip threshold, and the last evidence says the session is logged in (None, unknown,
    without any). ``browser_budget_left``: what today's ``li_prefills`` and
    ``profile_visits`` budgets (and this week's visits) still allow, the least of them.
    An auto-send claim passes ``action`` ``li_messages_auto`` in place of ``li_prefills``."""
    account_id = account_id_for(session, user)
    linkedin = settings.linkedin
    browser_ok: bool | None
    flagged = session_flag(session, user) is not None
    if flagged or heat_service.should_skip(
        session, user, account_id, now=now, settings=linkedin.heat
    ):
        browser_ok = False
    else:
        evidence = last_session_evidence(session, user, account_id)
        browser_ok = None if evidence is None else evidence.logged_in
    left: list[int] = []
    for spent in (action, budgets.ActionClass.PROFILE_VISITS):
        snapshot = budgets.status(
            session, user, account_id, spent, now=now, settings=linkedin.budget
        )
        left.append(snapshot.day.remaining)
        if snapshot.week is not None:
            left.append(snapshot.week.remaining)
    return ChannelState(browser_ok=browser_ok, browser_budget_left=min(left))


class _Claimer:
    """One claim: see :func:`claim_prefill`."""

    def __init__(
        self,
        session: Session,
        user: User,
        now: datetime,
        settings: Settings,
        start_run: StartRun,
        *,
        retry: bool = False,
        no_bubble_open: bool = False,
        auto: bool = False,
    ) -> None:
        self.session = session
        self.user = user
        self.now = now
        self.settings = settings
        self.start_run = start_run
        self.retry = retry
        self.no_bubble_open = no_bubble_open
        #: An auto-send claim (ADR 0008): the scheduler's, for an ``auto_send`` step.
        self.auto = auto

    def refuse(
        self, enrollment: Enrollment, *reasons: str, detail: str | None = None
    ) -> PrefillClaim:
        log.info("enrollment %d not claimed for a prefill: %s", enrollment.id, ", ".join(reasons))
        return PrefillClaim(enrollment.id, reasons=tuple(reasons), detail=detail)

    def park(self, enrollment: Enrollment, *reasons: str) -> PrefillClaim:
        enrollment.next_action_at = None
        log.warning("enrollment %d parked: %s", enrollment.id, ", ".join(reasons))
        return self.refuse(enrollment, *reasons)

    def claim(self, enrollment_id: int) -> PrefillClaim:
        session, user, now = self.session, self.user, self.now
        # Whatever the tick has not marked yet: a prefill nobody sent goes stale.
        engine.mark_stale(session, user, now=now)
        enrollment = engine._enrollment(session, user, enrollment_id)
        campaign = engine._campaign(session, user, enrollment.campaign_id)

        # The state machine first: approved, activated, started (#338, #339).
        state: list[str] = []
        if campaign.status is not CampaignStatus.ACTIVE:
            state.append(Reason.CAMPAIGN_NOT_ACTIVE.value)
        if enrollment.status is not EnrollmentStatus.ACTIVE:
            state.append(Reason.ENROLLMENT_NOT_ACTIVE.value)
        if state:
            return self.refuse(enrollment, Skip.GUARD_EXCLUDED, *state)
        if campaign.starts_at is None or campaign.starts_at > now:
            return self.refuse(enrollment, Skip.NOT_STARTED)
        step = next(
            (
                s
                for s in engine._steps(session, user, campaign.id)
                if s.position > (enrollment.current_step or 0)
            ),
            None,
        )
        if step is None:
            return self.refuse(enrollment, Refusal.NO_STEP)
        if step.channel is not TemplateChannel.LINKEDIN:
            return self.refuse(enrollment, Refusal.NOT_A_LINKEDIN_STEP)
        if self.auto and not self.settings.campaigns.linkedin_auto_send:
            return self.refuse(enrollment, Refusal.AUTO_SEND_OFF)
        if self.auto and step.mode is not StepMode.AUTO_SEND:
            return self.refuse(enrollment, Refusal.NOT_AUTO_SEND)
        # Try again (#445): a step whose latest prefill typed nothing is claimed only by a
        # retry, and a retry claims only such a step. Neither refusal changes anything.
        waiting = needs_try_again(enrollment)
        if self.retry and not waiting:
            return self.refuse(
                enrollment, Refusal.NOTHING_TO_RETRY, detail=_no_retry_detail(enrollment)
            )
        if waiting and not self.retry:
            return self.refuse(
                enrollment,
                Refusal.TRY_AGAIN_NEEDED,
                detail="the latest prefill typed nothing; use Try again",
            )
        due: datetime | None
        if self.retry:
            if (
                last_try(session, user, enrollment, now=now).needs_confirmation
                and not self.no_bubble_open
            ):
                return self.refuse(
                    enrollment,
                    Refusal.CONFIRM_NO_BUBBLE,
                    detail="the last try clicked Message; close any message bubble for this"
                    " contact in Chrome, then confirm",
                )
            # A person asked now: the step's own due time (it is parked) doesn't hold it.
            due = now
        else:
            due = enrollment.next_action_at
            if due is None or due > now:
                return self.refuse(enrollment, Skip.NOT_DUE)

        # The sending hours, by the tick's own rule (#338).
        try:
            slots = engine.slots_for(self.settings, user)
            hours = sending_hours.read(session, user)
        except schedule.ScheduleError as exc:
            log.warning("campaign %d prefills nothing: %s", campaign.id, exc)
            return self.refuse(enrollment, Skip.BAD_SCHEDULE)
        held = schedule.hold(
            due,
            now,
            slots=slots,
            hours=hours,
            starts_at=campaign.starts_at if campaign.start_chosen else None,
            first_step=enrollment.current_step is None,
        )
        if held is not None:
            reason = Skip.OUTSIDE_SENDING_HOURS if hours.enabled else Skip.SPILLED
            return self.refuse(enrollment, reason, detail=f"it may go at {held.isoformat()}")

        # What the enrollment holds.
        replied = engine._reply_at(session, user, enrollment.id)
        if replied is not None:
            enrollment.replied_at = enrollment.replied_at or replied
            engine._end(session, user, enrollment, EnrollmentStatus.REPLIED, "replied")
            return self.refuse(enrollment, Skip.ENDED, Skip.REPLIED)
        if engine.step_has_message(session, user, enrollment.id, step.id):
            return self.park(enrollment, Skip.STEP_ALREADY_SENT)
        if engine._waiting(session, user, enrollment.id):
            return self.park(enrollment, Skip.WAITING_ON_UNSENT)
        latest = engine._latest_sent(session, user, enrollment.id)
        # A discarded prefill counts as fired: the next step's delay counts from it.
        anchor = engine.latest_fired(session, user, enrollment.id)
        if anchor is None and step.position > 1:
            return self.park(enrollment, Skip.WAITING_ON_UNSENT)
        if anchor is not None:
            next_due = engine.follow_up_due(self.settings, user, step, anchor, hours)
            if next_due > now:
                enrollment.next_action_at = next_due
                return self.refuse(enrollment, Skip.NOT_DUE)

        verdict = check_step(session, user, enrollment, step, now=now)
        if not verdict.eligible:
            return self._excluded(enrollment, verdict.reasons, slots, hours)

        # The user and the browser: one open prefill, active hours, the channel.
        open_prefill = _open_prefill(session, user, now)
        if open_prefill is not None:
            return self.refuse(enrollment, Refusal.PREFILL_OPEN, detail=_open_detail(open_prefill))
        try:
            runs.refuse_if_outside_active_hours(self.settings.linkedin, now=now)
        except runs.OutsideActiveHours as exc:
            return self.refuse(enrollment, Refusal.OUTSIDE_ACTIVE_HOURS, detail=str(exc))
        except runs.RunError as exc:
            return self.refuse(enrollment, Refusal.BAD_ACTIVE_HOURS, detail=str(exc))
        channel = check_channel(
            TemplateChannel.LINKEDIN,
            channel_state(
                session,
                user,
                now=now,
                settings=self.settings,
                action=(
                    budgets.ActionClass.LI_MESSAGES_AUTO
                    if self.auto
                    else budgets.ActionClass.LI_PREFILLS
                ),
            ),
        )
        if channel:
            return self.refuse(enrollment, *(r.value for r in channel))
        # Last, so it only ever adds a refusal (#417): LinkedIn replies are not being
        # read, so a prefill could go to someone who already answered. It waits; a
        # complete inbox poll releases it, and nothing is changed on the enrollment.
        stale = inbox_hold.claim_hold(session, user, now=now)
        if stale is not None:
            return self.refuse(enrollment, Refusal.INBOX_STALE, detail=stale)

        return self._claim(campaign, enrollment, step, latest, slots)

    def _excluded(
        self,
        enrollment: Enrollment,
        reasons: tuple[Reason, ...],
        slots: schedule.Suggested,
        hours: schedule.SendingHours,
    ) -> PrefillClaim:
        """As the tick: an ending reason ends the enrollment, any other is re-checked later."""
        values = tuple(r.value for r in reasons)
        if Reason.CAMPAIGN_NOT_ACTIVE in reasons or Reason.ENROLLMENT_NOT_ACTIVE in reasons:
            return self.refuse(enrollment, Skip.GUARD_EXCLUDED, *values)
        for reason, status in engine.ENDING_REASONS.items():
            if reason in reasons:
                engine._end(self.session, self.user, enrollment, status, reason.value)
                return self.refuse(enrollment, Skip.ENDED, *values)
        enrollment.next_action_at = schedule.next_opening(
            self.now + engine.RECHECK_AFTER, hours, slots
        )
        return self.refuse(enrollment, Skip.GUARD_EXCLUDED, *values)

    def _claim(
        self,
        campaign: Campaign,
        enrollment: Enrollment,
        step: CampaignStep,
        latest: datetime | None,
        slots: schedule.Suggested,
    ) -> PrefillClaim:
        session, user, now = self.session, self.user, self.now
        template = get_scoped(session, user, Template, step.template_id)
        blocked = block_reason(template)
        if template is None or blocked is not None:
            # Said on the enrollment, as the email engine does (#342).
            enrollment.not_sent_error = blocked
            return self.park(enrollment, Skip.TEMPLATE_ERRORS)
        contact = session.scalars(
            scoped_contacts(user)
            .where(Contact.id == enrollment.contact_id)
            .options(selectinload(Contact.positions))
            .execution_options(populate_existing=True)
        ).first()
        if contact is None or contact.li_urn is None:  # the guards passed it a moment ago
            return self.park(enrollment, Skip.GUARD_EXCLUDED, Reason.NO_LINKEDIN.value)
        today = slots.local_date(now)
        values = MergeValues(
            contact=contact_fields(contact, today),
            campaign_name=campaign.name,
            step_number=step.position,
            previous_send_date=latest,
        )
        try:
            rendered = render(template.channel, None, template.body, values, today=today)
        except TemplateRenderError as exc:
            log.warning(
                "enrollment %d step %d did not render: %s", enrollment.id, step.position, exc
            )
            return self.park(enrollment, Skip.RENDER_FAILED)
        if not rendered.body.strip():
            return self.park(enrollment, Skip.RENDER_FAILED)
        errors = sorted({i.rule.value for i in rendered.issues if i.severity is Severity.ERROR})
        if errors:
            # What this contact's message would say is wrong (an empty merge value, a
            # body over LinkedIn's limit ...): a person looks at it, never a retry.
            enrollment.not_sent_error = f"blocked: {', '.join(errors)}"[: engine.ERROR_MAX_LENGTH]
            return self.park(enrollment, Refusal.RENDERED_ERRORS, *errors)
        try:
            # One savepoint for the run and the message: the database's one-open-prefill
            # index (0036) refusing the message takes the run back with it.
            with session.begin_nested():
                run = self.start_run(session, user, now)
                message = Message(
                    user_id=user.id,
                    enrollment_id=enrollment.id,
                    step_id=step.id,
                    contact_id=enrollment.contact_id,
                    channel=TemplateChannel.LINKEDIN,
                    direction=MessageDirection.OUT,
                    status=MessageStatus.SCHEDULED,
                    body_rendered=rendered.body,
                    scheduled_at=now,
                    sync_run_id=run.id,
                )
                session.add(message)
                session.flush()
        except runs.RunAlreadyRunning as exc:
            return self.refuse(enrollment, Refusal.RUN_IN_PROGRESS, detail=str(exc))
        except runs.RunError as exc:
            return self.refuse(enrollment, Refusal.RUN_REFUSED, detail=str(exc))
        except IntegrityError:
            # The savepoint is rolled back. Only the one-open-prefill index is a refusal;
            # any other integrity failure is a bug, and is raised.
            opened = _open_prefill(session, user, now)
            if opened is None:
                raise
            log.warning("enrollment %d: another prefill opened meanwhile", enrollment.id)
            return self.refuse(enrollment, Refusal.PREFILL_OPEN, detail=_open_detail(opened))
        enrollment.next_action_at = None
        session.flush()
        log.info(
            "campaign %d: step %d for enrollment %d claimed for %s as message %d (run %d)",
            campaign.id,
            step.position,
            enrollment.id,
            "an auto-send" if self.auto else "a prefill",
            message.id,
            run.id,
        )
        return PrefillClaim(enrollment.id, message_id=message.id, run_id=run.id)


def claim_prefill(
    session: Session,
    user: User,
    enrollment_id: int,
    *,
    now: datetime,
    settings: Settings,
    start_run: StartRun = start_message_send_run,
    retry: bool = False,
    no_bubble_open: bool = False,
) -> PrefillClaim:
    """Claim the enrollment's due LinkedIn step for one prefill, or say why not.

    A person asks for it; nothing scheduled ever calls this. Every check is in the
    module docstring. A refusal can still change the enrollment as the tick would (a
    reply ends it, a step that already fired parks it, a guard re-checks it a day
    later), so the caller commits either way. ``start_run`` records the run; tests
    stand in for it while ``message_send`` has no runner (P4-03).

    ``retry`` is Try again (#445): it claims only a step whose latest prefill ended
    ``not_typed``, whatever its due time, and needs ``no_bubble_open`` when that run
    clicked Message (or nothing says it didn't). Only a person's request passes either.

    Raises :class:`LookupError` for an enrollment that is not ``user``'s. Needs a
    writer session.
    """
    engine._require_writer(session, "claim_prefill")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return _Claimer(
        session, user, now, settings, start_run, retry=retry, no_bubble_open=no_bubble_open
    ).claim(enrollment_id)


def _interrupted_auto_send(session: Session, user: User, *, now: datetime) -> int | None:
    """The run of an auto-send whose message is still claimed while its run is over or
    left behind (it stopped mid-run, with no outcome recorded), or ``None``. A run still
    ``running`` counts once :func:`netkeeper.services.runs` would call it stale (older
    than ``STALE_AFTER``, its browser lock free): a restart within that window must still
    hold. A live run doesn't count. Read-only."""
    rows = session.execute(
        scoped(user, Message)
        .join(SyncRun, and_(SyncRun.id == Message.sync_run_id, SyncRun.user_id == user.id))
        .with_only_columns(SyncRun)
        .where(
            Message.channel == TemplateChannel.LINKEDIN,
            Message.direction == MessageDirection.OUT,
            Message.status == MessageStatus.SCHEDULED,
            SyncRun.kind == SyncRunKind.MESSAGE_SEND,
            SyncRun.trigger == SyncRunTrigger.SCHEDULED,
        )
        .order_by(SyncRun.id)
    ).scalars()
    held = runs.browser_held_for(session)
    for run in rows:
        if run.status is not SyncRunStatus.RUNNING or runs._stale(run, now=now, held=held):
            return run.id
    return None


def claim_auto_send(
    session: Session,
    user: User,
    *,
    now: datetime,
    settings: Settings,
    start_run: StartRun = start_auto_send_run,
) -> PrefillClaim | None:
    """The scheduler's auto-send claim (ADR 0008): the oldest ready ``auto_send`` step that
    can be claimed, with its scheduled run. ``None`` when ``[campaigns]
    linkedin_auto_send`` is off or no ``auto_send`` step is ready. Every refusal
    :func:`claim_prefill` makes applies, plus the step's mode, the flag, and
    ``li_messages_auto`` in place of ``li_prefills``. Stops at the first refusal about
    the user (:data:`USER_REFUSALS`), as :func:`claim_next` does. ``None`` too while
    auto-send is held (:func:`auto_send_hold`). A person never calls this. Needs a
    writer session."""
    engine._require_writer(session, "claim_auto_send")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not settings.campaigns.linkedin_auto_send:
        return None
    account_id = account_id_for(session, user)
    if auto_send_hold(session, user, account_id) is not None:
        return None
    interrupted = _interrupted_auto_send(session, user, now=now)
    if interrupted is not None:
        # A process that went away mid-run wrote no hold: write it now (#458 final review).
        hold_auto_send(
            session,
            user,
            account_id,
            reason=AUTO_SEND_HOLD_INTERRUPTED,
            now=now,
            run_id=interrupted,
        )
        return None
    # A hot account or a spent day would only abort the run: claim nothing (#458 review).
    if heat_service.should_skip(
        session, user, account_id, now=now, settings=settings.linkedin.heat
    ):
        return None
    spent = budgets.status(
        session,
        user,
        account_id,
        budgets.ActionClass.LI_MESSAGES_AUTO,
        now=now,
        settings=settings.linkedin.budget,
    ).day
    if spent.count >= spent.limit:
        return None
    # Only auto_send steps, oldest due first, so a long queue of prefill steps never
    # hides one (#458 review).
    enrollments = session.scalars(
        _ready_statement(user, now, None)
        .where(CampaignStep.mode == StepMode.AUTO_SEND)
        .order_by(Enrollment.next_action_at, Enrollment.id)
        .limit(READY_PAGE_MAX)
    ).all()
    last: PrefillClaim | None = None
    for enrollment in enrollments:
        last = _Claimer(session, user, now, settings, start_run, auto=True).claim(enrollment.id)
        if last.claimed or USER_REFUSALS.intersection(last.reasons):
            return last
    return last


def claim_next(
    session: Session,
    user: User,
    *,
    now: datetime,
    settings: Settings,
    start_run: StartRun = start_message_send_run,
) -> PrefillClaim | None:
    """ "Prefill next": the oldest ready enrollment that can be claimed. None when nothing
    is ready. Stops at the first refusal about the user (:data:`USER_REFUSALS`), and
    otherwise answers the last refusal when none could be claimed."""
    engine._require_writer(session, "claim_next")
    ready, _ = ready_to_prefill(session, user, now=now, settings=settings, limit=READY_PAGE_MAX)
    last: PrefillClaim | None = None
    for row in ready:
        last = claim_prefill(
            session, user, row.enrollment.id, now=now, settings=settings, start_run=start_run
        )
        if last.claimed or USER_REFUSALS.intersection(last.reasons):
            return last
    return last


# --- recording the run's outcome -----------------------------------------------------


def _claimed_message(session: Session, user: User, message_id: int) -> Message | None:
    message = session.scalars(
        scoped(user, Message)
        .where(Message.id == message_id)
        .execution_options(populate_existing=True)
    ).first()
    if (
        message is None
        or message.channel is not TemplateChannel.LINKEDIN
        or message.direction is not MessageDirection.OUT
        or message.status is not MessageStatus.SCHEDULED
    ):
        log.warning("message %d is not a claimed prefill; its outcome is not recorded", message_id)
        return None
    return message


def record_prefill_outcome(
    session: Session,
    user: User,
    message_id: int,
    outcome: MessageOutcome,
    *,
    settings: Settings,
    now: datetime,
    prefilled_at: datetime | None = None,
    send_clicked_at: datetime | None = None,
) -> bool:
    """Record what the ``message_send`` run did with a claimed message (P4-03 calls this).

    ``send_clicked`` (auto-send, ADR 0008) is recorded as ``prefilled`` is, with
    ``send_clicked_at`` (``now`` when not given): the inbox poll confirms it.

    See the module docstring for each outcome. ``prefilled_at`` is when typing started,
    taken before the first key (#382, ADR 0007): a ``prefilled`` message's
    ``prefilled_at`` is set to it, so a send the person makes while the run is still
    recording is dated after it. P4-03's runner always passes it; ``None`` falls back to
    ``now``. It may not be later than ``now``. False when the message is not a claimed
    (``scheduled``) LinkedIn message of ``user``'s: nothing changes. Needs a writer
    session."""
    engine._require_writer(session, "record_prefill_outcome")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if prefilled_at is not None and (
        prefilled_at.tzinfo is None or prefilled_at.utcoffset() is None or prefilled_at > now
    ):
        raise ValueError("prefilled_at must be timezone-aware and no later than now")
    if outcome.kind is MessageOutcomeKind.NOT_TYPED and outcome.typed_chars != 0:
        # not_typed promises nothing reached the composer, which is what lets Try again
        # retype it (#445). A run that typed something is partially_typed or unknown.
        raise ValueError("a not_typed outcome typed nothing; typed_chars must be 0")
    message = _claimed_message(session, user, message_id)
    if message is None:
        return False
    if send_clicked_at is not None and (
        send_clicked_at.tzinfo is None or send_clicked_at.utcoffset() is None
    ):
        raise ValueError("send_clicked_at must be timezone-aware")
    reason = f"{outcome.kind.value}: {outcome.reason}"[: engine.ERROR_MAX_LENGTH]
    kind = outcome.kind
    # The run goes on the enrollment, whatever the outcome: after a ``not_typed`` it is
    # the only way back to whether the run clicked Message or spent budget (#445).
    engine._enrollment(
        session, user, message.enrollment_id
    ).last_prefill_run_id = message.sync_run_id
    if kind in (MessageOutcomeKind.PREFILLED, MessageOutcomeKind.SEND_CLICKED):
        message.status = MessageStatus.PREFILLED
        message.prefilled_at = now if prefilled_at is None else prefilled_at
        if kind is MessageOutcomeKind.SEND_CLICKED:
            message.send_clicked_at = now if send_clicked_at is None else send_clicked_at
        message.li_conversation_urn = outcome.conversation_urn or message.li_conversation_urn
        message.error = None
        session.flush()
        engine._clear_not_sent(session, user, message.enrollment_id)
        engine._after_settling(session, user, settings, message, fired=True)
        if kind is MessageOutcomeKind.SEND_CLICKED:
            log.info("message %d: Send was clicked; the inbox poll confirms it", message.id)
        else:
            log.info("message %d is prefilled; it waits for the person to send it", message.id)
        return True
    if kind in (MessageOutcomeKind.NOT_TYPED, MessageOutcomeKind.TOO_LONG):
        # Parked either way. A not_typed step waits for Try again (#445); too long now is
        # too long next time, so a too_long one is never retried.
        _give_back(session, user, message, reason, now=now)
        return True
    # partially_typed, unknown: what the composer holds is not known. Never retyped. The
    # enrollment says so, not an earlier try's not_typed, which Try again would act on.
    message.status = MessageStatus.FAILED
    message.error = reason
    engine._clear_not_sent(session, user, message.enrollment_id)
    enrollment = engine._enrollment(session, user, message.enrollment_id)
    enrollment.not_sent_error = reason
    enrollment.next_action_at = None
    session.flush()
    log.warning(
        "message %d: the prefill ended %s; enrollment %d waits for a person",
        message.id,
        kind.value,
        enrollment.id,
    )
    return True


def give_back_unopened(session: Session, user: User, message_id: int, *, now: datetime) -> bool:
    """An auto-send stopped before it opened anything (ADR 0008, #458 final review): the
    claim's row goes, and the enrollment is due again at the time it was claimed, with
    no ``not_typed`` note and no count, so it is never listed for Try again and a later
    auto-send fire claims it by itself. False when the message is not a claimed one.
    Needs a writer session."""
    engine._require_writer(session, "give_back_unopened")
    message = _claimed_message(session, user, message_id)
    if message is None:
        return False
    enrollment = engine._enrollment(session, user, message.enrollment_id)
    due = message.scheduled_at or now
    session.delete(message)
    session.flush()
    if enrollment.status in (EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED):
        enrollment.next_action_at = due
    session.flush()
    log.info("enrollment %d: the auto-send opened nothing; due again at %s", enrollment.id, due)
    return True


def _give_back(
    session: Session, user: User, message: Message, reason: str, *, now: datetime
) -> None:
    """Nothing was typed: the row goes, so the step is free to be claimed again, and the
    enrollment is parked for a person (#445). It never comes back on a timer: a
    ``not_typed`` step waits for Try again (:func:`try_again`), and a ``too_long`` one
    for the template to change."""
    enrollment = engine._enrollment(session, user, message.enrollment_id)
    enrollment.not_sent_count += 1
    enrollment.not_sent_since = enrollment.not_sent_since or now
    enrollment.not_sent_error = reason
    enrollment.next_action_at = None
    session.delete(message)
    session.flush()
    log.info(
        "enrollment %d: %d prefills in a row typed nothing; it waits for a person",
        enrollment.id,
        enrollment.not_sent_count,
    )


# --- what waits for the person ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WaitingPrefill:
    """One LinkedIn message waiting for the person: ``prefilled``, ``stale``, or
    ``interrupted``: claimed (``scheduled``) and its run over without an outcome, so
    nobody knows what the composer holds."""

    message: Message
    enrollment: Enrollment
    campaign: Campaign
    contact: Contact

    @property
    def interrupted(self) -> bool:
        return self.message.status is MessageStatus.SCHEDULED

    @property
    def partly_typed(self) -> bool:
        return self.message.status is MessageStatus.FAILED

    @property
    def auto_sent(self) -> bool:
        """Whether auto-send clicked Send for it (ADR 0008): a ``stale`` one was never
        seen sent by the inbox poll."""
        return self.message.send_clicked_at is not None


def waiting_for_you(
    session: Session, user: User, *, limit: int, offset: int = 0, campaign_id: int | None = None
) -> tuple[list[WaitingPrefill], int]:
    """``prefilled``, ``stale``, interrupted (claimed, their run not running) and partly
    typed LinkedIn messages (:data:`PARTLY_TYPED_PREFIXES`), oldest first, and how many;
    one campaign's with ``campaign_id`` (#383). An interrupted or
    partly typed one blocks every later prefill (one open at a time) until the person
    discards it. Read-only."""
    run_running = and_(
        SyncRun.id == Message.sync_run_id,
        SyncRun.user_id == user.id,
        SyncRun.status == SyncRunStatus.RUNNING,
    )
    statement = (
        scoped(user, Message)
        .join(Enrollment, Enrollment.id == Message.enrollment_id)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .join(Contact, Contact.id == Message.contact_id)
        .outerjoin(SyncRun, run_running)
        .where(
            Enrollment.user_id == user.id,
            Campaign.user_id == user.id,
            Contact.user_id == user.id,
            not_self(),  # never the self contact (#342)
            Message.channel == TemplateChannel.LINKEDIN,
            Message.direction == MessageDirection.OUT,
            or_(
                # An auto-sent message waits on the inbox poll; once stale, on the person.
                and_(Message.status == MessageStatus.PREFILLED, Message.send_clicked_at.is_(None)),
                Message.status == MessageStatus.STALE,
                and_(Message.status == MessageStatus.SCHEDULED, SyncRun.id.is_(None)),
                _is_partly_typed(),
            ),
        )
    )
    if campaign_id is not None:
        statement = statement.where(Enrollment.campaign_id == campaign_id)
    total = session.scalar(statement.with_only_columns(func.count(Message.id)).order_by(None))
    rows = session.execute(
        statement.add_columns(Enrollment, Campaign, Contact)
        .order_by(Message.id)
        .offset(offset)
        .limit(min(limit, READY_PAGE_MAX))
    )
    return [WaitingPrefill(*row) for row in rows], total or 0


class PrefillNotWaiting(LookupError):
    """The message is not a LinkedIn message that waits for the person."""


def _waiting_message(
    session: Session, user: User, message_id: int, *, also_unrun: bool = False
) -> Message:
    """The user's LinkedIn message ``message_id``, if it waits for the person.

    ``also_unrun`` also takes a claimed message whose run ended without recording an
    outcome (a crash, or no runner yet), and a partly typed one: nobody knows what its
    composer holds, so only a person can let it go."""
    message = session.scalars(
        scoped(user, Message)
        .where(Message.id == message_id)
        .execution_options(populate_existing=True)
    ).first()
    if message is None:
        raise LookupError(f"no message {message_id}")
    if message.channel is not TemplateChannel.LINKEDIN or message.direction is not (
        MessageDirection.OUT
    ):
        raise PrefillNotWaiting(f"message {message_id} is not a LinkedIn campaign message")
    if message.status in WAITING_STATUSES:
        return message
    if (
        also_unrun
        and message.status is MessageStatus.SCHEDULED
        and not _run_running(session, user, message.sync_run_id)
    ):
        return message
    if (
        also_unrun
        and message.status is MessageStatus.FAILED
        and (message.error or "").startswith(PARTLY_TYPED_PREFIXES)
    ):
        return message
    raise PrefillNotWaiting(f"message {message_id} is {message.status}; nothing waits on it")


def _run_running(session: Session, user: User, run_id: int | None) -> bool:
    if run_id is None:
        return False
    status = session.scalar(
        scoped(user, SyncRun).with_only_columns(SyncRun.status).where(SyncRun.id == run_id)
    )
    return status is SyncRunStatus.RUNNING


def discard(
    session: Session, user: User, message_id: int, *, settings: Settings, now: datetime
) -> Message:
    """The person will not send it: ``discarded`` at ``now`` (``discarded_at``). The step
    counts as fired (never twice, as for a discarded Gmail draft), and the enrollment
    moves to its next step, due its delay after the discard (or after a later send), or
    completes. netkeeper changes nothing in LinkedIn: the composer is the person's. A
    partly typed message whose enrollment has already moved on (it has a due time) only
    has the message released; the enrollment is not advanced again. On an ended
    enrollment the step still counts as fired, as for any discard (``current_step``
    rises); the enrollment stays ended.

    Raises :class:`LookupError` for a message that is not ``user``'s, and
    :class:`PrefillNotWaiting` for one that does not wait for them. Needs a writer
    session."""
    engine._require_writer(session, "discard")
    message = _waiting_message(session, user, message_id, also_unrun=True)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    partly_typed = message.status is MessageStatus.FAILED
    message.status = MessageStatus.DISCARDED
    message.discarded_at = now
    message.error = None
    enrollment = engine._enrollment(session, user, message.enrollment_id)
    if partly_typed and (enrollment.not_sent_error or "").startswith(PARTLY_TYPED_PREFIXES):
        # The enrollment's note named the partly typed message; it's let go now.
        engine._clear_not_sent(session, user, enrollment.id)
    session.flush()
    if partly_typed and enrollment.next_action_at:
        # The enrollment has moved on since (a send seen, a new due time, a merge): the
        # discard only lets the bubble go. It never advances the enrollment again.
        log.info("message %d discarded; its enrollment had already moved on", message.id)
        return message
    engine._after_settling(session, user, settings, message, fired=True)
    log.info("message %d discarded by the person; its step counts as fired", message.id)
    return message


def check_sent(
    session: Session, user: User, message_id: int, *, now: datetime, settings: Settings
) -> SyncRun:
    """ "I sent it, check now": record a manual inbox poll (P4-08's runner) to look for the
    sent message. The same refusals as any manual run: active hours, a flagged session,
    heat, one running run per account, a kind with no runner.

    Raises :class:`LookupError`, :class:`PrefillNotWaiting`, or what
    :func:`netkeeper.services.runs.create_run` and its checks raise. Needs a writer
    session."""
    engine._require_writer(session, "check_sent")
    _waiting_message(session, user, message_id)
    runs.refuse_if_outside_active_hours(settings.linkedin, now=now)
    account = ensure_account(session, user)
    runs.refuse_if_flagged_or_hot(session, user, account.id, now=now, settings=settings.linkedin)
    return runs.create_run(session, user, SyncRunKind.INBOX, trigger=SyncRunTrigger.MANUAL, now=now)
