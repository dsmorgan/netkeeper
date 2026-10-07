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
    PrefillResult,
    PrefillSource,
    plan_refusal,
    plan_typing,
)
from netkeeper.linkedin.pacing import TypingPlan, TypingPlanError
from netkeeper.models import (
    Contact,
    Message,
    MessageDirection,
    MessageStatus,
    SyncRunKind,
    SyncRunStatus,
    TemplateChannel,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import budgets, runs
from netkeeper.services import heat as heat_service
from netkeeper.services.budgets import ActionClass, BudgetExceeded
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.linkedin_steps import record_prefill_outcome

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]

#: ADR 0007's decision 2: a claim whose run hasn't started within this long lapses, so
#: a prefill never starts long after the person who asked has stopped watching.
CLAIM_LAPSE: Final = timedelta(seconds=60)

#: How often, at most, a running prefill asks the database whether it was cancelled.
CANCEL_POLL_S: Final = 1.0

_ENDINGS: Final[dict[MessageOutcomeKind, SyncRunStatus]] = {
    MessageOutcomeKind.PREFILLED: SyncRunStatus.COMPLETED,
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
) -> PrefillReport:
    """Record ``outcome`` on run ``run_id``'s claimed message, and end the run, in one
    writer transaction. A run with no claimed message only ends ``failed``.

    ``budget_spent`` is whether the run spent its ``li_prefills`` and ``profile_visits``
    units (step 2 of the module docstring): the queue says whether a ``not_typed`` try
    counted against today's budget (#445). Left out of the counts when unknown."""
    with session_scope(factory, write=True) as session:
        user = _load_user(session, user_id)
        if runs.get_run(session, user, run_id).status is not SyncRunStatus.RUNNING:
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
        record_prefill_outcome(
            session,
            user,
            message.id,
            outcome,
            settings=settings,
            now=now,
            prefilled_at=prefilled_at if outcome.kind is MessageOutcomeKind.PREFILLED else None,
        )
        prefilled = outcome.kind is MessageOutcomeKind.PREFILLED
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
            },
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
) -> None:
    """:func:`record` on the way out of a refusal or a cancel: a failed write is logged."""
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
        )
    except Exception:
        log.exception("could not record how prefill run %d ended", run_id)


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
        message = _claimed(session, user, run_id)
        if message is None:
            claimed = None
        else:
            contact = get_scoped(session, user, Contact, message.contact_id)
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

    def refuse(outcome: MessageOutcome) -> PrefillReport:
        # Before the lock, so no budget. No source ran, so the click keys stay out (the
        # UI then reads the reason's words); no budget spent says no navigation either.
        return record(
            factory, user_id, run_id, outcome, settings=settings, now=clock(), budget_spent=False
        )

    if lapsed(scheduled_at, now):
        return refuse(_not_typed("the claim lapsed"))
    if not body:
        return refuse(_not_typed("the message has no body"))
    try:
        spec = MessageJobSpec(
            recipient_urn=urn or "",
            recipient_public_id=public_id,
            body=body,
            mode="prefill",
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
        )

    def spend() -> str | None:
        """``None`` when the prefill may navigate; otherwise why it stops first."""
        with session_scope(factory, write=True) as session:
            user = _load_user(session, user_id)
            try:
                runs.refuse_if_flagged_or_hot(
                    session, user, prepared.account_id, now=clock(), settings=settings.linkedin
                )
            except runs.SessionFlagged:
                return "the LinkedIn session is flagged"
            except runs.HeatSkipped:
                return "heat is too high"
            if runs.cancel_requested(session, user, run_id):
                return "cancelled"
        try:
            with session_scope(factory, write=True) as session:
                user = _load_user(session, user_id)
                for action in (ActionClass.LI_PREFILLS, ActionClass.PROFILE_VISITS):
                    budgets.consume(
                        session,
                        user,
                        prepared.account_id,
                        action,
                        now=clock(),
                        settings=settings.linkedin.budget,
                    )
        except BudgetExceeded:
            return "today's LinkedIn budget is spent"
        return None

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
        result = await source.prefill(prepared.spec, prepared.plan, cancelled=cancelled)
    except asyncio.CancelledError:
        await off_loop(
            record_quietly,
            factory,
            user_id,
            run_id,
            _after_failure(source.keys_sent, "interrupted"),
            settings=settings,
            now=clock(),
            click_attempted=source.message_click_attempted,
            clicked=source.message_clicked,
            budget_spent=True,
            click_diagnostics=source.message_click_diagnostics,
        )
        raise
    except Exception as exc:
        log.error("prefill run %d failed (%s)", run_id, type(exc).__name__)
        result = PrefillResult(
            _after_failure(source.keys_sent, f"the prefill failed ({type(exc).__name__})")
        )
    if result.wall is not None:
        await off_loop(_record_wall, factory, user_id, prepared.account_id, result, settings, clock)
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
        budget_spent=True,
        click_diagnostics=source.message_click_diagnostics,
    )


def _after_failure(keys_sent: int, reason: str) -> MessageOutcome:
    """``not_typed`` when no key call was attempted, ``unknown`` once one was."""
    if keys_sent == 0:
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
