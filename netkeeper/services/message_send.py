"""Run one LinkedIn prefill: the claimed message in, its outcome recorded (P4-03, ADR 0007).

The runner of a ``message_send`` run, the seam between P4-09's claim
(:mod:`netkeeper.services.linkedin_steps`) and the page work
(:class:`~netkeeper.linkedin.messaging.PrefillSource`, which
:mod:`netkeeper.worker` builds). Nothing here touches a browser.

In order, and nothing later runs when an earlier step refuses:

1. **Before the lock** (:func:`prepare`): read the claimed message and its contact; refuse
   a claim older than :data:`CLAIM_LAPSE` ("the claim lapsed", ADR 0007 decision 2);
   build the whole typing plan (:func:`~netkeeper.linkedin.messaging.plan_typing`).
   ``TypingTooLong`` records ``too_long``; any other refusal ``not_typed``. Nothing is
   spent and nothing is opened.
2. **Under the lock** (:func:`run_prefill`): refuse on the session flag or heat, then
   spend one ``li_prefills`` and one ``profile_visits`` unit, both before the
   navigation, in one transaction.
3. Run the source. A wall at the profile sets the session flag or raises heat.
4. Record the outcome (:func:`~netkeeper.services.linkedin_steps.record_prefill_outcome`)
   and the run's ending in one transaction. A ``prefilled`` outcome's ``prefilled_at``
   is the moment typing started, before the first key.

**Auto-send** (ADR 0008). A ``message_send`` run the scheduler recorded (``scheduled``,
:func:`netkeeper.services.linkedin_steps.claim_auto_send`) is an auto-send. Before the
lock it must still be one: ``[campaigns] linkedin_auto_send`` on and the step's mode
``auto_send``, or it records ``not_typed`` and nothing opens. Under the lock it must be
inside active hours, the session unflagged and heat under its skip threshold (heat pauses
an auto-send outright, as it does a prefill), and today's ``li_messages_auto`` budget
(:func:`auto_send_allowance`) not spent;
then it spends one ``li_messages_auto`` and one ``profile_visits`` unit (never
``li_prefills``). Only then is a :class:`~netkeeper.linkedin.messaging.SendPermit` built,
here and nowhere else, and handed to the source with the spec. Its ``recheck`` asks the
flag, active hours, the session flag, heat and a cancel again just before the click.
``send_clicked`` is recorded like ``prefilled`` (the inbox poll confirms the send), with
``send_clicked_at``; a refusal after the whole body was typed is ``prefilled``.

A prefill is never retried or resumed: ``partially_typed`` and ``unknown`` are failures
a person clears. Every refusal before the first key is ``not_typed``, which gives the
claim back (P4-09). The body is read from the database here and goes nowhere but the
source: never on the run row, in ``progress_json``, in a log, or in an error.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.db import off_loop, session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.messaging import (
    MessageJobSpec,
    MessageOutcome,
    MessageOutcomeKind,
    PreClickHold,
    PrefillResult,
    PrefillSource,
    SendPermit,
    plan_refusal,
    plan_typing,
)
from netkeeper.linkedin.pacing import TypingPlan, TypingPlanError
from netkeeper.models import (
    CampaignStep,
    Contact,
    Enrollment,
    Message,
    MessageDirection,
    MessageStatus,
    StepMode,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    TemplateChannel,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import budgets, runs
from netkeeper.services import campaign_engine as engine
from netkeeper.services import heat as heat_service
from netkeeper.services.budgets import ActionClass, BudgetExceeded
from netkeeper.services.campaign_guards import check_step
from netkeeper.services.linkedin_accounts import schedule_paused, scheduled_runs_armed
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.linkedin_steps import (
    AUTO_SEND_HOLD_BUBBLE,
    AUTO_SEND_HOLD_COVERED,
    AUTO_SEND_HOLD_TAB,
    BUBBLE_LEFT_OPEN_NOTE,
    NOT_SENT_NOTE,
    NOT_STARTED_NOTE,
    SEND_UNCONFIRMED_NOTE,
    auto_send_hold,
    give_back_unopened,
    hold_auto_send,
    record_prefill_outcome,
)

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]

#: ADR 0007's decision 2: a claim whose run hasn't started within this long lapses, so
#: a prefill never starts long after the person who asked has stopped watching.
CLAIM_LAPSE: Final = timedelta(seconds=60)

#: How often, at most, a running prefill asks the database whether it was cancelled.
CANCEL_POLL_S: Final = 1.0

_ENDINGS: Final[dict[MessageOutcomeKind, SyncRunStatus]] = {
    MessageOutcomeKind.PREFILLED: SyncRunStatus.COMPLETED,
    MessageOutcomeKind.SEND_CLICKED: SyncRunStatus.COMPLETED,
    MessageOutcomeKind.NOT_TYPED: SyncRunStatus.ABORTED,
    MessageOutcomeKind.TOO_LONG: SyncRunStatus.ABORTED,
    MessageOutcomeKind.PARTIALLY_TYPED: SyncRunStatus.FAILED,
    MessageOutcomeKind.UNKNOWN: SyncRunStatus.FAILED,
}
_HEAT_WALLS: Final = frozenset({Outcome.THROTTLED, Outcome.CHECKPOINT})
_FLAG_WALLS: Final = frozenset({Outcome.CHECKPOINT, Outcome.LOGGED_OUT})


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class PreparedPrefill:
    """A claim that may go to the browser: its message, its account, its spec and plan.
    The spec and the plan hold the body; neither is ever logged."""

    run_id: int
    message_id: int
    account_id: int
    scheduled_at: datetime
    spec: MessageJobSpec
    plan: TypingPlan = field(repr=False)

    @property
    def auto_send(self) -> bool:
        """Whether this run is an auto-send (ADR 0008): its spec's mode, which
        :func:`prepare` sets only for a scheduled run of an ``auto_send`` step with
        ``[campaigns] linkedin_auto_send`` on."""
        return self.spec.mode == "auto_send"


@dataclass(frozen=True, slots=True)
class PrefillReport:
    """How one prefill run ended: the outcome recorded, or ``None`` when there was no
    claimed message to record it on."""

    run_id: int
    outcome: MessageOutcome | None


def _not_typed(reason: str) -> MessageOutcome:
    return MessageOutcome(MessageOutcomeKind.NOT_TYPED, reason, None, 0)


def _claimed(session: Session, user: User, run_id: int) -> Message | None:
    return session.scalars(
        scoped(user, Message).where(
            Message.sync_run_id == run_id,
            Message.channel == TemplateChannel.LINKEDIN,
            Message.direction == MessageDirection.OUT,
            Message.status == MessageStatus.SCHEDULED,
        )
    ).first()


def _load_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"no user {user_id}")
    return user


def record(
    factory: sessionmaker[Session],
    user_id: int,
    run_id: int,
    outcome: MessageOutcome,
    *,
    settings: Settings,
    now: datetime,
    prefilled_at: datetime | None = None,
    click_attempted: bool | None = None,
    clicked: bool | None = None,
    budget_spent: bool | None = None,
    click_diagnostics: Mapping[str, str | None] | None = None,
    send_clicked_at: datetime | None = None,
    send_refusal: str | None = None,
    bubble_closed: bool | None = None,
    close_refusal: str | None = None,
    tab_closed: bool = False,
    send_unconfirmed: str | None = None,
    opened: bool | None = None,
    pre_click_hold: PreClickHold | None = None,
) -> PrefillReport:
    """Record ``outcome`` on run ``run_id``'s claimed message, and end the run, in one
    writer transaction. A run with no claimed message only ends ``failed``.
    ``send_clicked_at`` is an auto-send's click (ADR 0008); ``send_refusal`` is why an
    auto-send that typed the whole body did not click, kept in the run's notes, as is
    ``close_refusal``, why a sent bubble was left open (D1).

    An auto-send whose Message click was attempted and whose tab it did not close
    (anything but a landed Send with its bubble closed, D3) left a message bubble open
    in Chrome: auto-send is held (:func:`~netkeeper.services.linkedin_steps.hold_auto_send`)
    until the person closes it and resumes, in the same transaction.

    ``budget_spent`` is whether the run spent its ``li_prefills`` and ``profile_visits``
    units (step 2 of the module docstring): the queue says whether a ``not_typed`` try
    counted against today's budget (#445). An auto-send's runner passes ``None`` once
    its source ran, since it spends ``li_messages_auto`` instead, so the count is left
    out and a person's Try again asks them to confirm no bubble is open.

    ``opened`` false says the run stopped before it opened anything (a refusal before the
    navigation: a lapse, a gate, a busy browser). An auto-send stopped there is not
    ``not_typed``: its step is given back with its due time
    (:func:`~netkeeper.services.linkedin_steps.give_back_unopened`), so a later fire tries
    it again by itself, and it is never listed for Try again (#458 final review)."""
    with session_scope(factory, write=True) as session:
        user = _load_user(session, user_id)
        run = runs.get_run(session, user, run_id)
        auto = run.trigger is SyncRunTrigger.SCHEDULED
        if auto and click_attempted and not tab_closed:
            # Whatever else happens to the record, a bubble may be open: hold auto-send,
            # whether or not the run is still running (#458 review, SF3).
            hold_auto_send(
                session,
                user,
                run.linkedin_account_id,
                reason=AUTO_SEND_HOLD_TAB if bubble_closed else AUTO_SEND_HOLD_BUBBLE,
                now=now,
                run_id=run_id,
            )
        elif auto and pre_click_hold is not None:
            # #444's pre-click refusals a person must clear: no click, but the next try
            # would refuse the same page (ADR 0008, "The auto-send hold").
            hold_auto_send(
                session,
                user,
                run.linkedin_account_id,
                reason=(
                    AUTO_SEND_HOLD_COVERED
                    if pre_click_hold is PreClickHold.COVERED
                    else AUTO_SEND_HOLD_BUBBLE
                ),
                now=now,
                run_id=run_id,
            )
        if run.status is not SyncRunStatus.RUNNING:
            log.debug("prefill run %d already ended; its outcome stays as recorded", run_id)
            return PrefillReport(run_id, None)
        message = _claimed(session, user, run_id)
        if message is None:
            log.error("prefill run %d has no claimed message; nothing to record", run_id)
            runs.finish_run(
                session,
                user,
                run_id,
                status=SyncRunStatus.FAILED,
                now=now,
                stop_reason="no_claim",
                error="the run has no claimed LinkedIn message",
            )
            return PrefillReport(run_id, None)
        if auto and opened is False and outcome.kind is MessageOutcomeKind.NOT_TYPED:
            give_back_unopened(session, user, message.id, now=now)
            runs.finish_run(
                session,
                user,
                run_id,
                status=SyncRunStatus.ABORTED,
                now=now,
                stop_reason=outcome.kind.value,
                counts={"opened": False, "typed_chars": 0},
                notes=(f"{NOT_STARTED_NOTE}{outcome.reason}",),
                error=outcome.reason,
            )
            log.info("auto-send run %d stopped before opening anything; the step stays due", run_id)
            return PrefillReport(run_id, outcome)
        typed_whole = outcome.kind in (
            MessageOutcomeKind.PREFILLED,
            MessageOutcomeKind.SEND_CLICKED,
        )
        record_prefill_outcome(
            session,
            user,
            message.id,
            outcome,
            settings=settings,
            now=now,
            prefilled_at=prefilled_at if typed_whole else None,
            send_clicked_at=(
                send_clicked_at if outcome.kind is MessageOutcomeKind.SEND_CLICKED else None
            ),
        )
        prefilled = typed_whole
        runs.finish_run(
            session,
            user,
            run_id,
            status=_ENDINGS[outcome.kind],
            now=now,
            stop_reason=outcome.kind.value,
            counts={
                "typed_chars": outcome.typed_chars,
                # For CP8: how often the never-messaged chip was checked against the h1.
                "recipient_name_checked": outcome.recipient_name_checked,
                # For the UI: whether a message bubble may be open (ADR 0007). Left out
                # when the path doesn't know, so the UI falls back to the reason's words
                # instead of reading "no bubble". Only a source that ran says either.
                **(
                    {}
                    if click_attempted is None or clicked is None
                    else {"message_click_attempted": click_attempted, "message_clicked": clicked}
                ),
                **({} if budget_spent is None else {"li_prefills_spent": budget_spent}),
                # For CP8 (#444): which Message control was chosen, and why a click that
                # raised failed, as fixed categories. Empty when no control was chosen.
                **(click_diagnostics or {}),
                # ADR 0008: whether an auto-send's one Send click landed, and whether it
                # then closed the sent bubble (D1) and its own tab (D3).
                **(
                    {
                        "send_clicked": outcome.kind is MessageOutcomeKind.SEND_CLICKED,
                        "bubble_closed": bool(bubble_closed),
                        "tab_closed": tab_closed,
                    }
                    if auto
                    else {}
                ),
            },
            notes=tuple(
                note
                for note in (
                    None if send_refusal is None else f"{NOT_SENT_NOTE}{send_refusal}",
                    None
                    if send_unconfirmed is None
                    else f"{SEND_UNCONFIRMED_NOTE}{send_unconfirmed}",
                    None if close_refusal is None else f"{BUBBLE_LEFT_OPEN_NOTE}{close_refusal}",
                )
                if note is not None
            ),
            error=None if prefilled else outcome.reason,
        )
    log.info(
        "prefill run %d ended %s (%d characters typed)",
        run_id,
        outcome.kind.value,
        outcome.typed_chars,
    )
    return PrefillReport(run_id, outcome)


def record_quietly(
    factory: sessionmaker[Session],
    user_id: int,
    run_id: int,
    outcome: MessageOutcome,
    *,
    settings: Settings,
    now: datetime,
    click_attempted: bool | None = None,
    clicked: bool | None = None,
    budget_spent: bool | None = None,
    click_diagnostics: Mapping[str, str | None] | None = None,
    tab_closed: bool = False,
    opened: bool | None = None,
    pre_click_hold: PreClickHold | None = None,
) -> None:
    """:func:`record` on the way out of a refusal or a cancel: a failed write is logged,
    and an auto-send's hold is still written (:func:`hold_quietly`)."""
    try:
        record(
            factory,
            user_id,
            run_id,
            outcome,
            settings=settings,
            now=now,
            click_attempted=click_attempted,
            clicked=clicked,
            budget_spent=budget_spent,
            click_diagnostics=click_diagnostics,
            tab_closed=tab_closed,
            opened=opened,
            pre_click_hold=pre_click_hold,
        )
    except Exception:
        log.exception("could not record how prefill run %d ended", run_id)
        if (click_attempted and not tab_closed) or pre_click_hold is not None:
            hold_quietly(factory, user_id, run_id, now=now, pre_click_hold=pre_click_hold)


def hold_quietly(
    factory: sessionmaker[Session],
    user_id: int,
    run_id: int,
    *,
    now: datetime,
    pre_click_hold: PreClickHold | None = None,
) -> None:
    """Hold auto-send, in a transaction of its own, when the outcome could not be recorded
    after an auto-send's Message click or one of #444's pre-click refusals (#458 review,
    SF3). A manual run holds nothing. A failed write is logged."""
    try:
        with session_scope(factory, write=True) as session:
            user = _load_user(session, user_id)
            run = runs.get_run(session, user, run_id)
            if run.trigger is SyncRunTrigger.SCHEDULED:
                hold_auto_send(
                    session,
                    user,
                    run.linkedin_account_id,
                    reason=(
                        AUTO_SEND_HOLD_COVERED
                        if pre_click_hold is PreClickHold.COVERED
                        else AUTO_SEND_HOLD_BUBBLE
                    ),
                    now=now,
                    run_id=run_id,
                )
    except Exception:
        log.exception("could not hold auto-send after run %d", run_id)


def prepare(
    factory: sessionmaker[Session],
    user_id: int,
    run_id: int,
    *,
    settings: Settings,
    clock: Clock = _utcnow,
    seed: int | None = None,
) -> PreparedPrefill | PrefillReport:
    """Step 1 of the module docstring, before the lock: the claim, the lapse, the plan.

    A :class:`PrefillReport` means the run is over and recorded; nothing may touch the
    browser. ``seed`` fixes the typing plan for a test; live, it is random."""
    with session_scope(factory) as session:
        user = _load_user(session, user_id)
        run = runs.get_run(session, user, run_id)
        if run.kind is not SyncRunKind.MESSAGE_SEND:
            raise ValueError(f"run {run_id} is a {run.kind.value} run, not message_send")
        account_id = run.linkedin_account_id
        scheduled = run.trigger is SyncRunTrigger.SCHEDULED
        message = _claimed(session, user, run_id)
        if message is None:
            claimed = None
            step_mode = None
        else:
            contact = get_scoped(session, user, Contact, message.contact_id)
            step = (
                None
                if message.step_id is None
                else get_scoped(session, user, CampaignStep, message.step_id)
            )
            step_mode = None if step is None else step.mode
            claimed = (
                message.id,
                message.scheduled_at,
                message.body_rendered,
                None if contact is None else contact.li_urn,
                None if contact is None else contact.li_public_id,
            )
    now = clock()
    if claimed is None:
        record(factory, user_id, run_id, _not_typed("no claim"), settings=settings, now=now)
        return PrefillReport(run_id, None)
    message_id, scheduled_at, body, urn, public_id = claimed

    def refuse(outcome: MessageOutcome, *, opened: bool | None = None) -> PrefillReport:
        # Before the lock, so no budget. No source ran, so the click keys stay out (the
        # UI then reads the reason's words); no budget spent says no navigation either.
        return record(
            factory,
            user_id,
            run_id,
            outcome,
            settings=settings,
            now=clock(),
            budget_spent=False,
            opened=opened,
        )

    if lapsed(scheduled_at, now):
        return refuse(_not_typed("the claim lapsed"), opened=False)
    if scheduled:
        # ADR 0008: a run nobody watches types only as an auto-send, and only while it
        # still is one. Nothing is opened, spent, or typed otherwise.
        refusal = auto_send_refusal(settings, step_mode)
        if refusal is not None:
            return refuse(_not_typed(refusal), opened=False)
    if not body:
        return refuse(_not_typed("the message has no body"))
    try:
        spec = MessageJobSpec(
            recipient_urn=urn or "",
            recipient_public_id=public_id,
            body=body,
            mode="auto_send" if scheduled else "prefill",
            typing_seed=secrets.randbits(64) if seed is None else seed,
        )
    except ValueError:
        return refuse(_not_typed("the contact has no usable LinkedIn URN"))
    try:
        plan = plan_typing(spec.body, spec.typing_seed)
    except TypingPlanError as exc:
        log.warning("prefill run %d: the typing plan refused (%s)", run_id, type(exc).__name__)
        return refuse(plan_refusal(exc))
    assert scheduled_at is not None  # lapsed() refuses a claim without one
    return PreparedPrefill(run_id, message_id, account_id, scheduled_at, spec, plan)


def still_wanted(session: Session, user: User, run_id: int, *, now: datetime) -> str | None:
    """Whether the claimed message is still one to send (ADR 0008, #458 final review),
    read live, before the navigation and again just before the Send click: still
    ``scheduled``; its step still ``auto_send``; no reply on the enrollment; and the
    step still passes its guards (:func:`~netkeeper.services.campaign_guards.check_step`:
    the enrollment and the campaign ``active``, the contact not do-not-contact, and the
    rest). ``None`` when it is; otherwise why not, in fixed words. Read-only."""
    message = _claimed(session, user, run_id)
    if message is None:
        return "the claimed message is no longer waiting to be sent"
    step = (
        None
        if message.step_id is None
        else get_scoped(session, user, CampaignStep, message.step_id)
    )
    if step is None or step.mode is not StepMode.AUTO_SEND:
        return "the step is not an auto-send step"
    enrollment = get_scoped(session, user, Enrollment, message.enrollment_id)
    if enrollment is None:
        return "the enrollment is gone"
    if engine._reply_at(session, user, enrollment.id) is not None:
        return "a reply arrived"
    verdict = check_step(session, user, enrollment, step, now=now)
    if not verdict.eligible:
        return "the step no longer passes its guards"
    return None


def auto_send_refusal(settings: Settings, mode: StepMode | None) -> str | None:
    """Why an auto-send may not run (ADR 0008), in fixed words, or ``None``: the config
    flag off, or a step whose mode is not ``auto_send``."""
    if not settings.campaigns.linkedin_auto_send:
        return "auto-send is off"
    if mode is not StepMode.AUTO_SEND:
        return "the step is not an auto-send step"
    return None


def auto_send_allowance(
    session: Session, user: User, account_id: int, *, now: datetime, settings: Settings
) -> tuple[int, int]:
    """Today's ``li_messages_auto`` count and its daily limit (ADR 0008): the configured
    budget clamped to its hard maximum (:func:`netkeeper.services.budgets.status`).

    An auto-send runs only while the count is below the limit: unlike
    ``budgets.consume`` alone, which lets one unit over, this never sends past it. Heat
    doesn't shrink the limit. Like every LinkedIn message run, an auto-send is paused
    outright while heat is at its skip threshold (:func:`runs.refuse_if_flagged_or_hot`).
    Read-only."""
    snapshot = budgets.status(
        session,
        user,
        account_id,
        ActionClass.LI_MESSAGES_AUTO,
        now=now,
        settings=settings.linkedin.budget,
    )
    return snapshot.day.count, snapshot.day.limit


def lapsed(scheduled_at: datetime | None, now: datetime) -> bool:
    """ADR 0007's claim lapse: a claim with no time, one from the future (a clock that
    moved), or one more than :data:`CLAIM_LAPSE` old has lapsed."""
    return scheduled_at is None or now < scheduled_at or now - scheduled_at > CLAIM_LAPSE


async def run_prefill(
    factory: sessionmaker[Session],
    user_id: int,
    prepared: PreparedPrefill,
    source: PrefillSource,
    *,
    settings: Settings,
    clock: Clock = _utcnow,
) -> PrefillReport:
    """Steps 2 to 4 of the module docstring. The caller holds the account's browser lock
    for the length of the call and built ``source`` on the run's tab."""
    run_id = prepared.run_id
    if lapsed(prepared.scheduled_at, clock()):
        # Checked again after the attach: the lock and the attach take time too.
        return await off_loop(
            record,
            factory,
            user_id,
            run_id,
            _not_typed("the claim lapsed"),
            settings=settings,
            now=clock(),
            click_attempted=False,
            clicked=False,
            budget_spent=False,
            opened=False,
        )

    auto = prepared.auto_send

    def gates(session: Session, user: User) -> str | None:
        """The gates checked before the navigation and again before an auto-send's click."""
        now = clock()
        if auto:
            refusal = auto_send_refusal(settings, StepMode.AUTO_SEND)
            if refusal is not None:
                return refusal
            # Live reads, asked again before the click: disarming or pausing scheduled
            # runs, or a hold, stops an auto-send mid-run (#458 review).
            if not scheduled_runs_armed(session, user, prepared.account_id):
                return "scheduled runs are disarmed"
            if schedule_paused(session, user, prepared.account_id):
                return "the schedule is paused"
            if auto_send_hold(session, user, prepared.account_id) is not None:
                return "auto-send is held until the open message bubbles are closed"
            changed = still_wanted(session, user, run_id, now=now)
            if changed is not None:
                return changed
            try:
                runs.refuse_if_outside_active_hours(settings.linkedin, now=now)
            except runs.OutsideActiveHours:
                return "outside LinkedIn's active hours"
            except runs.RunError:
                return "LinkedIn's active hours do not parse"
        try:
            runs.refuse_if_flagged_or_hot(
                session, user, prepared.account_id, now=now, settings=settings.linkedin
            )
        except runs.SessionFlagged:
            return "the LinkedIn session is flagged"
        except runs.HeatSkipped:
            return "heat is too high"
        if runs.cancel_requested(session, user, run_id):
            return "cancelled"
        return None

    spent_at: datetime | None = None

    def spend() -> str | None:
        """``None`` when the prefill may navigate; otherwise why it stops first."""
        nonlocal spent_at
        with session_scope(factory, write=True) as session:
            refusal = gates(session, _load_user(session, user_id))
            if refusal is not None:
                return refusal
        try:
            with session_scope(factory, write=True) as session:
                user = _load_user(session, user_id)
                if auto:
                    count, limit = auto_send_allowance(
                        session, user, prepared.account_id, now=clock(), settings=settings
                    )
                    if count >= limit:
                        return "today's auto-send budget is spent"
                # An auto-send spends li_messages_auto, never li_prefills (ADR 0008).
                first = ActionClass.LI_MESSAGES_AUTO if auto else ActionClass.LI_PREFILLS
                at = clock()
                for action in (first, ActionClass.PROFILE_VISITS):
                    budgets.consume(
                        session,
                        user,
                        prepared.account_id,
                        action,
                        now=at,
                        settings=settings.linkedin.budget,
                    )
            spent_at = at
        except BudgetExceeded:
            return "today's LinkedIn budget is spent"
        return None

    def recheck_gates() -> str | None:
        with session_scope(factory) as session:
            return gates(session, _load_user(session, user_id))

    async def recheck() -> str | None:
        return await off_loop(recheck_gates)

    refused = await off_loop(spend)
    if refused is not None:
        return await off_loop(
            record,
            factory,
            user_id,
            run_id,
            _not_typed(refused),
            settings=settings,
            now=clock(),
            click_attempted=False,
            clicked=False,
            budget_spent=False,
            opened=False,
        )

    asked_at = 0.0
    was_cancelled = False

    def cancel_requested() -> bool:
        with session_scope(factory) as session:
            return runs.cancel_requested(session, _load_user(session, user_id), run_id)

    async def cancelled() -> bool:
        nonlocal asked_at, was_cancelled
        loop = asyncio.get_running_loop()
        if not was_cancelled and loop.time() - asked_at >= CANCEL_POLL_S:
            asked_at = loop.time()
            was_cancelled = await off_loop(cancel_requested)
        return was_cancelled

    try:
        if auto:
            # ADR 0008: the one place a SendPermit is built, after every gate above held
            # and li_messages_auto was spent.
            permit = SendPermit(recheck=recheck)
            result = await source.prefill(
                prepared.spec, prepared.plan, cancelled=cancelled, permit=permit
            )
        else:
            result = await source.prefill(prepared.spec, prepared.plan, cancelled=cancelled)
    except asyncio.CancelledError:
        await off_loop(
            record_quietly,
            factory,
            user_id,
            run_id,
            _after_failure(source.keys_sent, "interrupted", send_attempted=_sent(source)),
            settings=settings,
            now=clock(),
            click_attempted=source.message_click_attempted,
            clicked=source.message_clicked,
            budget_spent=None if auto else True,
            click_diagnostics=source.message_click_diagnostics,
        )
        raise
    except Exception as exc:
        log.error("prefill run %d failed (%s)", run_id, type(exc).__name__)
        result = PrefillResult(
            _after_failure(
                source.keys_sent,
                f"the prefill failed ({type(exc).__name__})",
                send_attempted=_sent(source),
            )
        )
    tab_closed = bool(getattr(source, "tab_closed", False))
    try:
        # Neither of these may keep the outcome (and an auto-send's hold) from being
        # recorded below (#458 review): a failure is logged, a cancel records first.
        if result.wall is not None:
            await off_loop(
                _record_wall, factory, user_id, prepared.account_id, result, settings, clock
            )
        if (
            auto
            and spent_at is not None
            and source.keys_sent == 0
            and not _sent(source)
            and result.outcome.kind in _NOTHING_TYPED
        ):
            # Nothing was typed, so nothing was sent: the li_messages_auto unit goes back,
            # to the day it was spent. The profile visit doesn't: the profile was (almost
            # always) opened.
            await off_loop(
                _release_auto_send, factory, user_id, prepared.account_id, settings, spent_at
            )
    except asyncio.CancelledError:
        await off_loop(
            record_quietly,
            factory,
            user_id,
            run_id,
            result.outcome,
            settings=settings,
            now=clock(),
            click_attempted=source.message_click_attempted,
            clicked=source.message_clicked,
            tab_closed=tab_closed,
            click_diagnostics=source.message_click_diagnostics,
            pre_click_hold=result.pre_click_hold,
        )
        raise
    except Exception:
        log.exception("prefill run %d: the wall or the refund could not be written", run_id)
    try:
        return await off_loop(
            record,
            factory,
            user_id,
            run_id,
            result.outcome,
            settings=settings,
            now=clock(),
            prefilled_at=result.typing_started_at,
            click_attempted=source.message_click_attempted,
            clicked=source.message_clicked,
            send_clicked_at=result.send_clicked_at,
            send_refusal=result.send_refusal,
            bubble_closed=result.bubble_closed,
            close_refusal=result.close_refusal,
            tab_closed=tab_closed,
            send_unconfirmed=result.send_unconfirmed,
            pre_click_hold=result.pre_click_hold,
            budget_spent=None if auto else True,
            click_diagnostics=source.message_click_diagnostics,
        )
    except Exception:
        if (source.message_click_attempted and not tab_closed) or result.pre_click_hold is not None:
            await off_loop(
                hold_quietly,
                factory,
                user_id,
                run_id,
                now=clock(),
                pre_click_hold=result.pre_click_hold,
            )
        raise


def _sent(source: PrefillSource) -> bool:
    """Whether the source attempted its Send click (ADR 0008); a source with no Send says no."""
    return bool(getattr(source, "send_attempted", False))


_NOTHING_TYPED: Final = frozenset({MessageOutcomeKind.NOT_TYPED, MessageOutcomeKind.TOO_LONG})


def _release_auto_send(
    factory: sessionmaker[Session],
    user_id: int,
    account_id: int,
    settings: Settings,
    spent_at: datetime,
) -> None:
    with session_scope(factory, write=True) as session:
        budgets.release(
            session,
            _load_user(session, user_id),
            account_id,
            ActionClass.LI_MESSAGES_AUTO,
            spent_at=spent_at,
            settings=settings.linkedin.budget,
        )


def _after_failure(keys_sent: int, reason: str, *, send_attempted: bool = False) -> MessageOutcome:
    """``not_typed`` when no key call was attempted, ``unknown`` once one was. An attempted
    Send click (ADR 0008) counts as a key: after it the outcome is never ``not_typed``, so
    it is never given back or offered again."""
    if keys_sent == 0 and not send_attempted:
        return _not_typed(reason)
    return MessageOutcome(MessageOutcomeKind.UNKNOWN, reason, None, 0)


def _record_wall(
    factory: sessionmaker[Session],
    user_id: int,
    account_id: int,
    result: PrefillResult,
    settings: Settings,
    clock: Clock,
) -> None:
    """A throttle or a checkpoint raises heat; a checkpoint or a login wall sets the
    session flag (spec 9.7)."""
    wall = result.wall
    assert wall is not None
    with session_scope(factory, write=True) as session:
        user = _load_user(session, user_id)
        if wall in _HEAT_WALLS:
            heat_service.raise_heat(
                session, user, account_id, now=clock(), settings=settings.linkedin.heat
            )
        if wall in _FLAG_WALLS:
            flag_session(session, user, wall, url=result.wall_url or "")
