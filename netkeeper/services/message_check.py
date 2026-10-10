"""The Message check's bookkeeping (#473): its run row, its gates, and its one visit.

``netkeeper linkedin message-check <contact id>`` runs a prefill's steps up to the
Message click and stops (:class:`~netkeeper.linkedin.page_messaging.PageMessageCheck`,
driven by :func:`netkeeper.worker.run_message_check`). This module holds what it
writes, and nothing touches a browser here:

1. **Before the lock** (:func:`start`): the contact must have a public profile id and a
   usable ``urn:li:fsd_profile`` URN; the run must be inside ``[linkedin] active_hours``,
   with the session unflagged and heat under its skip threshold. Then it records a
   ``message_send`` run, by hand, through :data:`~netkeeper.services.runs.MESSAGE_CHECK_GATE`.
   It is that kind so the scheduler's gap after a prefill (spec 9.5) and auto-send's
   spacing (ADR 0008) count it, and so a prefill can't start while it runs. No message
   is claimed, so no message or step changes.
2. **Under the lock** (:func:`spend`): the session flag, heat, and a cancel again, then
   one ``profile_visits`` unit, before the navigation, as a prefill spends one. No
   ``li_prefills`` or ``li_messages_auto`` unit is spent. The bubble check (#495,
   ``message-check --bubble``) opens no profile: it rechecks the same (:func:`recheck`)
   and spends nothing.
3. **After** (:func:`finish`): a wall at the profile sets the session flag or raises
   heat, as a prefill's does; the run ends ``completed`` with stop reason
   :data:`MESSAGE_CHECK_STOP`, or ``failed`` with why. What the check read is printed by
   the CLI and never stored: the run row holds fixed words only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.messaging import PROFILE_URN_PREFIX
from netkeeper.models import Contact, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.scoping import get_scoped
from netkeeper.services import budgets, runs
from netkeeper.services import heat as heat_service
from netkeeper.services.budgets import ActionClass, BudgetExceeded
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import flag_session

log = logging.getLogger(__name__)

#: The stop reason of a check that ran its steps: completed, nothing clicked.
MESSAGE_CHECK_STOP: Final = "message_check"
#: The note on every check's run, so a ``message_send`` run with no message reads as one.
MESSAGE_CHECK_NOTE: Final = (
    "message check (#473): a dry run of a prefill up to the Message click;"
    " nothing was clicked, typed, or focused"
)
#: #495: the stop reason and note of a bubble check (``message-check --bubble``), a read
#: of a message bubble a person opened by hand: no profile opened, nothing clicked.
BUBBLE_CHECK_STOP: Final = "bubble_check"
BUBBLE_CHECK_NOTE: Final = (
    "bubble check (#495): a read of an open message bubble's close control;"
    " no profile was opened, and nothing was clicked, typed, or focused"
)
_HEAT_WALLS: Final = frozenset({Outcome.THROTTLED, Outcome.CHECKPOINT})
_FLAG_WALLS: Final = frozenset({Outcome.CHECKPOINT, Outcome.LOGGED_OUT})
_BAD_ID_CHARACTERS: Final = frozenset("/?#&=,()% ")


class CheckRefused(Exception):
    """The check can't start for this contact; the message says why, in fixed words."""


@dataclass(frozen=True, slots=True)
class CheckTarget:
    """A recorded check: its run, its account, and the contact's profile to open."""

    run_id: int
    account_id: int
    public_id: str
    profile_id: str


def start(
    session: Session,
    user: User,
    contact_id: int,
    *,
    now: datetime,
    settings: Settings,
    bubble: bool = False,
) -> CheckTarget:
    """Step 1 of the module docstring. Raises ``LookupError`` for no such contact,
    :class:`CheckRefused` for a contact the check can't open, and what
    :func:`runs.refuse_if_outside_active_hours`, :func:`runs.refuse_if_flagged_or_hot`
    and :func:`runs.create_run` raise. Needs a writer session. ``bubble`` records a bubble
    check (#495) instead: the same gates and run, its own note."""
    contact = get_scoped(session, user, Contact, contact_id)
    if contact is None:
        raise LookupError(f"no contact {contact_id}")
    public_id = contact.li_public_id
    if not public_id:
        raise CheckRefused(f"contact {contact_id} has no LinkedIn public profile id to open")
    urn = contact.li_urn or ""
    profile_id = urn.removeprefix(PROFILE_URN_PREFIX)
    if (
        not urn.startswith(PROFILE_URN_PREFIX)
        or not profile_id
        or _BAD_ID_CHARACTERS & set(profile_id)
    ):
        raise CheckRefused(f"contact {contact_id} has no usable LinkedIn profile URN")
    runs.refuse_if_outside_active_hours(settings.linkedin, now=now)
    account_id = ensure_account(session, user).id
    runs.refuse_if_flagged_or_hot(session, user, account_id, now=now, settings=settings.linkedin)
    run = runs.create_run(
        session,
        user,
        SyncRunKind.MESSAGE_SEND,
        trigger=SyncRunTrigger.MANUAL,
        now=now,
        gate=runs.MESSAGE_CHECK_GATE,
    )
    run.notes = BUBBLE_CHECK_NOTE if bubble else MESSAGE_CHECK_NOTE
    return CheckTarget(run.id, account_id, public_id, profile_id)


def spend(
    factory: sessionmaker[Session],
    user_id: int,
    target: CheckTarget,
    *,
    settings: Settings,
    now: datetime,
) -> tuple[str, str] | None:
    """Step 2: ``None`` when the check may navigate; otherwise ``(stop reason, why)``,
    and nothing is spent. Under the browser lock, before the navigation."""
    with session_scope(factory, write=True) as session:
        user = _user(session, user_id)
        refused = _recheck(
            session, user, target, settings=settings, now=now, when="before the profile opened"
        )
        if refused is not None:
            return refused
        try:
            budgets.consume(
                session,
                user,
                target.account_id,
                ActionClass.PROFILE_VISITS,
                now=now,
                settings=settings.linkedin.budget,
            )
        except BudgetExceeded:
            return "budget", "today's or this week's profile-visit budget is spent"
    return None


def recheck(
    factory: sessionmaker[Session],
    user_id: int,
    target: CheckTarget,
    *,
    settings: Settings,
    now: datetime,
) -> tuple[str, str] | None:
    """Step 2 for the bubble check (#495), which opens no profile: the session flag, heat,
    and a cancel again, under the browser lock, and nothing spent."""
    with session_scope(factory) as session:
        user = _user(session, user_id)
        return _recheck(
            session, user, target, settings=settings, now=now, when="before the page was read"
        )


def _recheck(
    session: Session,
    user: User,
    target: CheckTarget,
    *,
    settings: Settings,
    now: datetime,
    when: str,
) -> tuple[str, str] | None:
    try:
        runs.refuse_if_flagged_or_hot(
            session, user, target.account_id, now=now, settings=settings.linkedin
        )
    except runs.SessionFlagged as exc:
        return "session_flagged", str(exc)
    except runs.HeatSkipped as exc:
        return "heat_skip", str(exc)
    if runs.cancel_requested(session, user, target.run_id):
        return runs.CANCELLED, f"cancelled {when}"
    return None


def cancel_requested(factory: sessionmaker[Session], user_id: int, run_id: int) -> bool:
    """Whether a person asked to cancel the check (``netkeeper linkedin cancel``)."""
    with session_scope(factory) as session:
        return runs.cancel_requested(session, _user(session, user_id), run_id)


def finish(
    factory: sessionmaker[Session],
    user_id: int,
    target: CheckTarget,
    *,
    status: SyncRunStatus,
    stop_reason: str,
    now: datetime,
    settings: Settings,
    error: str | None = None,
    wall: Outcome | None = None,
    wall_url: str | None = None,
) -> None:
    """Step 3: record a wall as a prefill does, then how the check's run ended. ``error``
    is fixed words or an exception's type name, never a page's text."""
    with session_scope(factory, write=True) as session:
        user = _user(session, user_id)
        if wall in _HEAT_WALLS:
            heat_service.raise_heat(
                session, user, target.account_id, now=now, settings=settings.linkedin.heat
            )
        if wall is not None and wall in _FLAG_WALLS:
            flag_session(session, user, wall, url=wall_url or "")
        runs.finish_run(
            session,
            user,
            target.run_id,
            status=status,
            now=now,
            stop_reason=stop_reason,
            error=error,
        )


def finish_quietly(
    factory: sessionmaker[Session],
    user_id: int,
    target: CheckTarget,
    *,
    status: SyncRunStatus,
    stop_reason: str,
    now: datetime,
    settings: Settings,
    error: str | None = None,
) -> None:
    """:func:`finish` on the way out of a cancel or a failure: logged, never raised."""
    try:
        finish(
            factory,
            user_id,
            target,
            status=status,
            stop_reason=stop_reason,
            now=now,
            settings=settings,
            error=error,
        )
    except Exception:
        log.exception("could not record how message check run %d ended", target.run_id)


def _user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"no user {user_id}")
    return user
