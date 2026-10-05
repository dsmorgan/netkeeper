"""When each background check last ran and runs next (#401). Read-only.

The app header and the campaign page ask "when will this page update?". The answer
is the background checks ``netkeeper serve`` runs, read from what they already
store, never by running one:

- **Gmail replies** (spec 11.7): every ``[campaigns] reply_poll_minutes``, in the
  campaign tick, for every armed mailbox. ``mailboxes.replies_polled_at`` is when
  a poll last read everything up to then, so the next one is that plus the
  interval. A poll that stopped part way leaves it old, and the tick tries again
  each minute: the check is then ``due``. A restart polls on its first tick, which
  is never later than the time shown.
- **Gmail drafts** (spec 11.5): every :data:`~netkeeper.services.campaign_sender.DRAFTS_POLL_EVERY`,
  only while a campaign draft waits in an armed mailbox. Its last run is kept only
  in the running sender's memory (:meth:`GmailSender.drafts_polled_at`).
- **LinkedIn**: each kind the scheduler serves, from its stored due time
  (:func:`~netkeeper.services.scheduler.stored_due`), and its last completed run.
  The gates are the scheduler's own, in its order: disarmed, paused, the session
  flag, heat, the connections breakers; then active hours, which the manual-run
  check words (:func:`~netkeeper.services.runs.refuse_if_outside_active_hours`).
- **LinkedIn inbox**: until a page source is wired (P4-01, #380) the scheduler does
  not serve it, and it is ``not_wired``: never shown as running.

The campaign engine's one-minute tick is left out on purpose: it is not a check.

A check that cannot run says why instead of giving a time: ``next_at`` is set only
for a ``scheduled`` check. No function here writes, calls Gmail, or attaches to a
browser.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import func
from sqlalchemy.orm import Session

from netkeeper.config import Settings
from netkeeper.models import (
    Campaign,
    Enrollment,
    Mailbox,
    MailboxStatus,
    Message,
    MessageStatus,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    TemplateChannel,
    User,
)
from netkeeper.scoping import scoped
from netkeeper.services import heat as heat_service
from netkeeper.services import route_breaker, runs
from netkeeper.services.campaign_sender import DRAFTS_POLL_EVERY
from netkeeper.services.linkedin_accounts import (
    find_account,
    schedule_paused,
    scheduled_runs_armed,
)
from netkeeper.services.linkedin_session import session_flag
from netkeeper.services.scheduled_runs import INBOX_RUN_KIND, RUN_KIND
from netkeeper.services.scheduler import (
    DEFAULT_SCHEDULES,
    SERVED_SCHEDULES,
    JobKind,
    stored_due,
)


class CheckState(enum.StrEnum):
    """Where a check stands. Only ``scheduled`` carries a next time."""

    SCHEDULED = "scheduled"
    """It runs at ``next_at``."""
    DUE = "due"
    """Its time has come: it runs at the next tick or heartbeat, within a minute."""
    IDLE = "idle"
    """It would run, but has nothing to look at (no draft waiting, nothing scheduled)."""
    PAUSED = "paused"
    """A person paused the LinkedIn schedule."""
    OUTSIDE_HOURS = "outside_hours"
    """Outside LinkedIn active hours: nothing starts until the window opens."""
    BLOCKED = "blocked"
    """Something stops it until a person acts: the session flag, heat, a breaker, a
    mailbox that needs reconnecting."""
    OFF = "off"
    """Turned off: disarmed, no armed mailbox, Gmail not connected."""
    NOT_RUNNING = "not_running"
    """This process runs no background checks: it was not started by ``netkeeper serve``."""
    NOT_WIRED = "not_wired"
    """Not built to run yet."""


class CheckGroup(enum.StrEnum):
    GMAIL = "gmail"
    LINKEDIN = "linkedin"


#: Every check, in the order the popover lists them.
GMAIL_REPLIES: Final = "gmail_replies"
GMAIL_DRAFTS: Final = "gmail_drafts"
LINKEDIN_INBOX: Final = "linkedin_inbox"
LINKEDIN_KEYS: Final[dict[JobKind, str]] = {
    JobKind.INBOX: LINKEDIN_INBOX,
    JobKind.ENRICH: "linkedin_enrich",
    JobKind.CONNECTIONS_INCREMENTAL: "linkedin_incremental_sync",
    JobKind.CONNECTIONS_FULL: "linkedin_full_sync",
}
LABELS: Final[dict[str, str]] = {
    GMAIL_REPLIES: "Gmail replies",
    GMAIL_DRAFTS: "Gmail drafts",
    LINKEDIN_INBOX: "LinkedIn inbox",
    "linkedin_enrich": "LinkedIn enrichment",
    "linkedin_incremental_sync": "LinkedIn incremental sync",
    "linkedin_full_sync": "LinkedIn full sync",
}

NOT_SERVING: Final = "netkeeper serve isn't running in this process, so no background check runs"
INBOX_NOT_WIRED: Final = (
    "The LinkedIn inbox poll isn't running yet: it needs the LinkedIn inbox reader,"
    " which comes in a later release"
)
_CONNECTIONS_KINDS: Final = frozenset({JobKind.CONNECTIONS_FULL, JobKind.CONNECTIONS_INCREMENTAL})
_SYNC_RUN_KIND: Final[dict[JobKind, SyncRunKind]] = {**RUN_KIND, **INBOX_RUN_KIND}


@dataclass(frozen=True, slots=True)
class Check:
    key: str
    group: CheckGroup
    label: str
    state: CheckState
    interval: timedelta
    last_at: datetime | None = None
    next_at: datetime | None = None
    reason: str | None = None
    """Why it is not ``scheduled``, in a sentence; None when it is."""


@dataclass(frozen=True, slots=True)
class MailboxPoll:
    """One mailbox's reply poll, for the campaign page's "replies checked" line."""

    mailbox_id: int
    email: str
    armed: bool
    state: CheckState
    replies_polled_at: datetime | None
    next_at: datetime | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class PollStatus:
    background_running: bool
    checks: tuple[Check, ...]
    mailboxes: tuple[MailboxPoll, ...]


@dataclass(frozen=True, slots=True)
class Serving:
    """What this process runs, from ``app.state``. All false and None outside ``serve``."""

    scheduler: bool = False
    campaign_engine: bool = False
    drafts_polled_at: datetime | None = None
    drafts_every: timedelta = DRAFTS_POLL_EVERY


def poll_status(
    session: Session, user: User, *, now: datetime, settings: Settings, serving: Serving
) -> PollStatus:
    """Every check's last and next run for ``user``. Read-only; runs nothing."""
    reply_every = timedelta(minutes=max(settings.campaigns.reply_poll_minutes, 1))
    mailboxes = list(session.scalars(scoped(user, Mailbox).order_by(Mailbox.id)))
    polls = tuple(
        _mailbox_poll(mailbox, now=now, every=reply_every, serving=serving) for mailbox in mailboxes
    )
    checks = [
        _gmail_replies(mailboxes, polls, every=reply_every, serving=serving),
        _gmail_drafts(session, user, mailboxes, now=now, serving=serving),
        *_linkedin(session, user, now=now, settings=settings, serving=serving),
    ]
    return PollStatus(
        background_running=serving.scheduler or serving.campaign_engine,
        checks=tuple(checks),
        mailboxes=polls,
    )


# --- Gmail ----------------------------------------------------------------------------


def _next(
    last: datetime | None, every: timedelta, now: datetime
) -> tuple[CheckState, datetime | None]:
    """``scheduled`` at ``last + every`` while that is ahead; else ``due`` (the next tick)."""
    if last is None or last + every <= now:
        return CheckState.DUE, None
    return CheckState.SCHEDULED, last + every


def _mailbox_poll(
    mailbox: Mailbox, *, now: datetime, every: timedelta, serving: Serving
) -> MailboxPoll:
    armed = mailbox.arm is not None
    state: CheckState
    next_at: datetime | None = None
    reason: str | None = None
    if mailbox.status is MailboxStatus.DISABLED:
        state, reason = CheckState.OFF, f"{mailbox.email} is disconnected"
    elif not armed:
        state, reason = (
            CheckState.OFF,
            f"{mailbox.email} is disarmed, so its replies aren't checked",
        )
    elif mailbox.status is not MailboxStatus.OK:
        state, reason = (
            CheckState.BLOCKED,
            f"{mailbox.email} needs you to sign in to Gmail again",
        )
    elif not serving.campaign_engine:
        state, reason = CheckState.NOT_RUNNING, NOT_SERVING
    else:
        state, next_at = _next(mailbox.replies_polled_at, every, now)
    return MailboxPoll(
        mailbox_id=mailbox.id,
        email=mailbox.email,
        armed=armed,
        state=state,
        replies_polled_at=mailbox.replies_polled_at,
        next_at=next_at,
        reason=reason,
    )


def _gmail_gate(mailboxes: Sequence[Mailbox], serving: Serving) -> tuple[CheckState, str] | None:
    """Why no Gmail poll runs at all, or None when one can."""
    armed = [m for m in mailboxes if m.arm is not None]
    if not armed:
        if all(m.status is MailboxStatus.DISABLED for m in mailboxes):
            return CheckState.OFF, "Gmail isn't connected"
        return CheckState.OFF, "No mailbox is armed, so netkeeper doesn't read Gmail"
    if not serving.campaign_engine:
        return CheckState.NOT_RUNNING, NOT_SERVING
    if all(m.status is not MailboxStatus.OK for m in armed):
        return CheckState.BLOCKED, "Gmail needs you to sign in again (Settings, Gmail)"
    return None


def _gmail_replies(
    mailboxes: Sequence[Mailbox],
    polls: Sequence[MailboxPoll],
    *,
    every: timedelta,
    serving: Serving,
) -> Check:
    """The armed mailboxes' reply poll, read conservatively: the oldest poll is the
    last one, and the check is due while any mailbox's poll is."""
    armed = [p.replies_polled_at for p in polls if p.armed]
    last = min((at for at in armed if at is not None), default=None)
    if None in armed:
        last = None  # a mailbox never polled: its replies are not checked yet
    base = Check(
        key=GMAIL_REPLIES,
        group=CheckGroup.GMAIL,
        label=LABELS[GMAIL_REPLIES],
        state=CheckState.DUE,
        interval=every,
        last_at=last,
    )
    gate = _gmail_gate(mailboxes, serving)
    running = [p for p in polls if p.state in (CheckState.DUE, CheckState.SCHEDULED)]
    if gate is not None or not running:
        state, reason = gate or (CheckState.OFF, "No mailbox is armed")
        return replace(base, state=state, reason=reason)
    next_times = [p.next_at for p in running]
    if any(at is None for at in next_times):
        return replace(base, state=CheckState.DUE)
    soonest = min(at for at in next_times if at is not None)
    return replace(base, state=CheckState.SCHEDULED, next_at=soonest)


def _waiting_drafts(session: Session, user: User, mailbox_ids: Sequence[int]) -> int:
    """Campaign drafts in Gmail the drafts poll watches, on the given mailboxes."""
    if not mailbox_ids:
        return 0
    statement = (
        scoped(user, Message)
        .with_only_columns(func.count(Message.id))
        .join(Enrollment, Enrollment.id == Message.enrollment_id)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .where(
            Enrollment.user_id == user.id,
            Campaign.user_id == user.id,
            Campaign.mailbox_id.in_(mailbox_ids),
            Message.channel == TemplateChannel.EMAIL,
            Message.status == MessageStatus.DRAFTED,
            Message.gmail_draft_id.is_not(None),
        )
    )
    return session.scalar(statement) or 0


def _gmail_drafts(
    session: Session, user: User, mailboxes: Sequence[Mailbox], *, now: datetime, serving: Serving
) -> Check:
    every = serving.drafts_every
    base = Check(
        key=GMAIL_DRAFTS,
        group=CheckGroup.GMAIL,
        label=LABELS[GMAIL_DRAFTS],
        state=CheckState.DUE,
        interval=every,
        last_at=serving.drafts_polled_at,
    )
    gate = _gmail_gate(mailboxes, serving)
    if gate is not None:
        return replace(base, state=gate[0], reason=gate[1])
    ready = [m.id for m in mailboxes if m.arm is not None and m.status is MailboxStatus.OK]
    if _waiting_drafts(session, user, ready) == 0:
        return replace(
            base, state=CheckState.IDLE, reason="No campaign draft is waiting to be sent"
        )
    state, next_at = _next(serving.drafts_polled_at, every, now)
    return replace(base, state=state, next_at=next_at)


# --- LinkedIn -------------------------------------------------------------------------


def _last_run(session: Session, user: User, kind: SyncRunKind) -> datetime | None:
    """When the newest completed run of ``kind`` ended, whoever started it."""
    return session.scalar(
        scoped(user, SyncRun)
        .with_only_columns(func.max(SyncRun.completed_at))
        .where(SyncRun.kind == kind, SyncRun.status == SyncRunStatus.COMPLETED)
    )


def _linkedin(
    session: Session, user: User, *, now: datetime, settings: Settings, serving: Serving
) -> list[Check]:
    account = find_account(session, user)
    gate = _linkedin_gate(
        session,
        user,
        account_id=None if account is None else account.id,
        now=now,
        settings=settings,
        serving=serving,
    )
    checks: list[Check] = []
    for kind, key in LINKEDIN_KEYS.items():
        schedule = DEFAULT_SCHEDULES[kind]
        base = Check(
            key=key,
            group=CheckGroup.LINKEDIN,
            label=LABELS[key],
            state=CheckState.DUE,
            interval=schedule.interval,
            last_at=_last_run(session, user, _SYNC_RUN_KIND[kind]),
        )
        if kind not in SERVED_SCHEDULES:
            checks.append(replace(base, state=CheckState.NOT_WIRED, reason=INBOX_NOT_WIRED))
            continue
        blocked = gate
        if blocked is None and account is not None and kind in _CONNECTIONS_KINDS:
            blocked = _breaker(session, user, account.id)
        if blocked is not None:
            checks.append(replace(base, state=blocked[0], reason=blocked[1]))
            continue
        assert account is not None  # an account with no row is disarmed: gated above
        due = stored_due(session, user, account.id, kind)
        if due is None:
            checks.append(
                replace(
                    base,
                    state=CheckState.IDLE,
                    reason="Not scheduled yet: netkeeper serve schedules it when it starts",
                )
            )
        elif due <= now:
            checks.append(replace(base, state=CheckState.DUE))
        else:
            checks.append(replace(base, state=CheckState.SCHEDULED, next_at=due))
    return checks


def _linkedin_gate(
    session: Session,
    user: User,
    *,
    account_id: int | None,
    now: datetime,
    settings: Settings,
    serving: Serving,
) -> tuple[CheckState, str] | None:
    """What stops every scheduled LinkedIn kind, in the scheduler's order; None when nothing."""
    if not serving.scheduler:
        return CheckState.NOT_RUNNING, NOT_SERVING
    if account_id is None or not scheduled_runs_armed(session, user, account_id):
        return CheckState.OFF, "Scheduled LinkedIn runs are disarmed"
    if schedule_paused(session, user, account_id):
        return CheckState.PAUSED, "The LinkedIn schedule is paused"
    if session_flag(session, user) is not None:
        return (
            CheckState.BLOCKED,
            "The LinkedIn session is flagged; check it and clear the flag on the LinkedIn page",
        )
    if heat_service.should_skip(
        session, user, account_id, now=now, settings=settings.linkedin.heat
    ):
        return CheckState.BLOCKED, "LinkedIn heat is high, so scheduled runs wait until it cools"
    try:
        runs.refuse_if_outside_active_hours(settings.linkedin, now=now)
    except (runs.OutsideActiveHours, runs.RunError) as exc:
        return CheckState.OUTSIDE_HOURS, str(exc)
    return None


def _breaker(session: Session, user: User, account_id: int) -> tuple[CheckState, str] | None:
    if route_breaker.tripped(session, user, account_id) or route_breaker.answer_lost_tripped(
        session, user, account_id
    ):
        return (
            CheckState.BLOCKED,
            "Connections syncs are stopped after several failed runs; see the LinkedIn page",
        )
    return None
