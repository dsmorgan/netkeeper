"""When each background check last ran and runs next (#401). Read-only.

The app header and the campaign page ask "when will this page update?". The answer
is the background checks ``netkeeper serve`` runs, read from what they already
store, never by running one:

- **Gmail replies** (spec 11.7): every ``[campaigns] reply_poll_minutes``, in the
  campaign tick, for every armed mailbox. Last and next come from one source, the
  running sender's own gate (:meth:`GmailSender.replies_polled_at`): the next poll
  is that plus the sender's interval, and with none (a fresh start) the next tick
  polls, so the check is ``due``. A ready mailbox the sender polls again sooner,
  alone (:meth:`GmailSender.replies_due`: its poll stopped part way, or it holds a
  follow-up), is ``due`` too, and so is the check; the others keep their time
  (#413). An ``ok`` mailbox the sender could not open (a locked Keychain) is
  ``blocked`` with that reason, and the check names it, rather than ``due`` at every
  tick. Without ``serve``
  there is no next poll, and the last one is read from the armed mailboxes'
  ``replies_polled_at``: the oldest, or none while one was never polled. An armed
  mailbox that needs signing in again is named in the check's reason; it blocks
  the check only when every armed mailbox does. A person can ask for it sooner
  ("Check now", #409): the check is then ``due`` and ``requested`` until the next
  tick starts the poll, and :func:`replies_check_refused` says when they cannot.
- **Gmail drafts** (spec 11.5): every :data:`~netkeeper.services.campaign_sender.DRAFTS_POLL_EVERY`,
  only while a campaign draft waits in an armed mailbox. Its last run is kept only
  in the running sender's memory (:meth:`GmailSender.drafts_polled_at`).
- **LinkedIn**: each kind the scheduler serves, from its stored due time
  (:func:`~netkeeper.services.scheduler.stored_due`), and its last completed run.
  The gates are the scheduler's own, in its order: disarmed, paused, the session
  flag, heat, the connections breakers. Then active hours, which the scheduler does
  not check at fire time (it snaps every due time into the window), so they come
  last; the manual-run check words them
  (:func:`~netkeeper.services.runs.refuse_if_outside_active_hours`).
- **LinkedIn inbox**: served like the other LinkedIn kinds since P4-01 (#380). A kind
  the scheduler does not serve would be ``not_wired``: never shown as running.

The campaign engine's one-minute tick is left out on purpose: it is not a check.

A check that cannot run says why instead of giving a time: ``next_at`` is set only
for a ``scheduled`` check. Gmail and LinkedIn check their gates in one order:
``serve`` not running, then turned off (disarmed, not connected), then paused
(LinkedIn only), then blocked. No function here writes, calls Gmail, or attaches
to a browser.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
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
from netkeeper.services import campaign_review as review
from netkeeper.services import heat as heat_service
from netkeeper.services import route_breaker, runs
from netkeeper.services.campaign_sender import DRAFTS_POLL_EVERY
from netkeeper.services.linkedin_accounts import (
    find_account,
    schedule_paused,
    scheduled_runs_armed,
)
from netkeeper.services.linkedin_session import session_flag
from netkeeper.services.mailboxes import REASON_KEYCHAIN_UNAVAILABLE
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
    """Its time has come, and it runs soon: a Gmail poll at the next minute tick, a
    LinkedIn kind at the next heartbeat, or, overdue by a whole interval (the
    machine slept), as a catch-up 5 to 20 minutes after the scheduler notices."""
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
NOT_WIRED: Final = "This check isn't running yet: netkeeper serve doesn't schedule it"
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
    """Why it has no next time, in a sentence. A ``scheduled`` or ``due`` Gmail reply
    check may carry one too: an armed mailbox it cannot read."""
    requested_at: datetime | None = None
    """When a person asked for it sooner ("Check now", #409); it runs at the next tick."""


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
    replies_polled_at: datetime | None = None
    replies_every: timedelta | None = None
    """The running sender's reply interval; None reads ``[campaigns] reply_poll_minutes``."""
    replies_due: frozenset[int] = frozenset()
    """Mailboxes the running sender polls again at the next tick, alone (#413)."""
    replies_not_ready: Mapping[int, str] = field(default_factory=dict)
    """Mailboxes the running sender's last poll could not open, with the code (#413)."""
    replies_retry_at: Mapping[int, datetime] = field(default_factory=dict)
    """Due mailboxes still in their backoff after a failed poll, with their retry time:
    neither the next tick nor a "Check now" reads them before it (#409). The full
    poll each interval reads them anyway, if that comes first."""
    drafts_polled_at: datetime | None = None
    drafts_every: timedelta = DRAFTS_POLL_EVERY
    replies_requested_at: datetime | None = None
    """When the "Check now" that waits for the next tick was asked for
    (:meth:`GmailSender.request_replies_poll`); None when none waits."""


def poll_status(
    session: Session, user: User, *, now: datetime, settings: Settings, serving: Serving
) -> PollStatus:
    """Every check's last and next run for ``user``. Read-only; runs nothing."""
    reply_every = serving.replies_every or timedelta(
        minutes=max(settings.campaigns.reply_poll_minutes, 1)
    )
    mailboxes = list(session.scalars(scoped(user, Mailbox).order_by(Mailbox.id)))
    polls = tuple(
        _mailbox_poll(mailbox, now=now, every=reply_every, serving=serving) for mailbox in mailboxes
    )
    checks = [
        _gmail_replies(mailboxes, polls, now=now, every=reply_every, serving=serving),
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
    last: datetime | None, every: timedelta, now: datetime, *, requested: bool = False
) -> tuple[CheckState, datetime | None]:
    """``scheduled`` at ``last + every`` while that is ahead; else, or when a person asked
    for it sooner, ``due`` (the next tick)."""
    if requested or last is None or last + every <= now:
        return CheckState.DUE, None
    return CheckState.SCHEDULED, last + every


def _mailbox_poll(
    mailbox: Mailbox, *, now: datetime, every: timedelta, serving: Serving
) -> MailboxPoll:
    """One mailbox, gated like the check. Its next poll is the user's (the sender polls
    every armed mailbox together), or the next tick when it is due again alone (#413)."""
    armed = mailbox.arm is not None
    state: CheckState
    next_at: datetime | None = None
    reason: str | None = None
    if not serving.campaign_engine:
        state, reason = CheckState.NOT_RUNNING, NOT_SERVING
    elif mailbox.status is MailboxStatus.DISABLED:
        state, reason = CheckState.OFF, f"{mailbox.email} is disconnected"
    elif not armed:
        state, reason = (
            CheckState.OFF,
            f"{mailbox.email} is disarmed, so its replies aren't checked",
        )
    elif mailbox.status is not MailboxStatus.OK:
        state, reason = CheckState.BLOCKED, _needs_sign_in([mailbox])
    elif (code := serving.replies_not_ready.get(mailbox.id)) is not None:
        state, reason = CheckState.BLOCKED, _cannot_open(mailbox, code)
    elif mailbox.id in serving.replies_due:
        state, next_at = _backing_off(serving, mailbox.id, every, now)
    else:
        state, next_at = _next(
            serving.replies_polled_at,
            every,
            now,
            requested=serving.replies_requested_at is not None,
        )
    return MailboxPoll(
        mailbox_id=mailbox.id,
        email=mailbox.email,
        armed=armed,
        state=state,
        replies_polled_at=mailbox.replies_polled_at,
        next_at=next_at,
        reason=reason,
    )


def _backing_off(
    serving: Serving, mailbox_id: int, every: timedelta, now: datetime
) -> tuple[CheckState, datetime | None]:
    """A due mailbox: ``due`` (the next tick), unless it is backing off after a failed
    poll. Then it is read at its retry time or at the next full poll, whichever comes
    first; ``due`` once that has passed."""
    retry_at = serving.replies_retry_at.get(mailbox_id)
    if retry_at is None:
        return CheckState.DUE, None
    if serving.replies_polled_at is None:  # no full poll yet: the next tick runs one
        return CheckState.DUE, None
    next_at = min(retry_at, serving.replies_polled_at + every)
    if next_at <= now:
        return CheckState.DUE, None
    return CheckState.SCHEDULED, next_at


def _needs_sign_in(mailboxes: Sequence[Mailbox]) -> str:
    names = ", ".join(m.email for m in mailboxes)
    verb = "needs" if len(mailboxes) == 1 else "need"
    return f"{names} {verb} you to sign in to Gmail again (Settings, Gmail)"


def _cannot_open(mailbox: Mailbox, code: str) -> str:
    """An ``ok`` mailbox the sender could not open (#413): most often a locked Keychain."""
    if code == REASON_KEYCHAIN_UNAVAILABLE:
        return (
            f"The Keychain is locked, so netkeeper can't read {mailbox.email};"
            " unlock it and the next minute's check reads it"
        )
    return f"netkeeper can't open {mailbox.email} ({code}); see Settings, Gmail"


def _gmail_gate(mailboxes: Sequence[Mailbox], serving: Serving) -> tuple[CheckState, str] | None:
    """Why no Gmail poll runs at all, or None when one can. The LinkedIn order: not
    running, off, blocked."""
    if not serving.campaign_engine:
        return CheckState.NOT_RUNNING, NOT_SERVING
    armed = [m for m in mailboxes if m.arm is not None]
    if not armed:
        if all(m.status is MailboxStatus.DISABLED for m in mailboxes):
            return CheckState.OFF, "Gmail isn't connected"
        return CheckState.OFF, "No mailbox is armed, so netkeeper doesn't read Gmail"
    if all(m.status is not MailboxStatus.OK for m in armed):
        return CheckState.BLOCKED, _needs_sign_in(armed)
    return None


def _gmail_replies(
    mailboxes: Sequence[Mailbox],
    polls: Sequence[MailboxPoll],
    *,
    now: datetime,
    every: timedelta,
    serving: Serving,
) -> Check:
    """The reply poll, from the running sender's gate (the module)."""
    base = Check(
        key=GMAIL_REPLIES,
        group=CheckGroup.GMAIL,
        label=LABELS[GMAIL_REPLIES],
        state=CheckState.DUE,
        interval=every,
    )
    gate = _gmail_gate(mailboxes, serving)
    if gate is not None:
        return replace(base, state=gate[0], reason=gate[1], last_at=_stored_last(polls))
    stuck = [m for m in mailboxes if m.arm is not None and m.status is not MailboxStatus.OK]
    reasons = [_needs_sign_in(stuck)] if stuck else []
    reasons += [
        p.reason
        for p in polls
        if p.state is CheckState.BLOCKED and p.mailbox_id in serving.replies_not_ready and p.reason
    ]
    reason = "; ".join(reasons) or None
    requested_at = serving.replies_requested_at
    state, next_at = _next(
        serving.replies_polled_at, every, now, requested=requested_at is not None
    )
    if any(p.state is CheckState.DUE for p in polls):  # one ready mailbox is due again
        state, next_at = CheckState.DUE, None
    return replace(
        base,
        state=state,
        last_at=serving.replies_polled_at,
        next_at=next_at,
        reason=reason,
        requested_at=requested_at,
    )


def replies_check_refused(session: Session, user: User, *, serving: Serving) -> str | None:
    """Why a person cannot ask for the reply poll now ("Check now", #409), or None when
    they can. The check's own gate, so a refusal reads like the popover: ``serve`` not
    running, no mailbox armed or connected, every armed mailbox needing sign-in. Reads
    only; the request itself is :meth:`GmailSender.request_replies_poll`."""
    mailboxes = list(session.scalars(scoped(user, Mailbox).order_by(Mailbox.id)))
    gate = _gmail_gate(mailboxes, serving)
    return None if gate is None else gate[1]


def _stored_last(polls: Sequence[MailboxPoll]) -> datetime | None:
    """Without a running sender, the last poll the armed mailboxes recorded: the oldest,
    or None while one was never polled."""
    armed = [p.replies_polled_at for p in polls if p.armed]
    if not armed or None in armed:
        return None
    return min(at for at in armed if at is not None)


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
    test_drafts = [c for c in review.test_drafts_to_verify(session, user) if c.mailbox_id in ready]
    if _waiting_drafts(session, user, ready) == 0 and not test_drafts:
        return replace(
            base, state=CheckState.IDLE, reason="No draft is waiting to be sent or checked"
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
            checks.append(replace(base, state=CheckState.NOT_WIRED, reason=NOT_WIRED))
            continue
        blocked = gate
        if blocked is None and account is not None and kind in _CONNECTIONS_KINDS:
            blocked = _breaker(session, user, account.id)
        if blocked is None and account is not None and kind is JobKind.ENRICH:
            blocked = _contact_info_breaker(session, user, account.id)
        if blocked is None and kind is JobKind.INBOX and base.last_at is None:
            blocked = (
                CheckState.BLOCKED,
                "The first LinkedIn inbox poll is run by hand: run `netkeeper linkedin inbox`",
            )
        if blocked is None:
            blocked = _outside_hours(settings, now=now)
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
    """What stops every scheduled LinkedIn kind, in the scheduler's order; None when nothing.
    The connections breakers and enrichment's Contact info breaker (per kind) and active
    hours follow, in :func:`_linkedin`."""
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
    return None


def _outside_hours(settings: Settings, *, now: datetime) -> tuple[CheckState, str] | None:
    """Last: the scheduler never fires outside the window, since it snaps due times into it."""
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


def _contact_info_breaker(
    session: Session, user: User, account_id: int
) -> tuple[CheckState, str] | None:
    if route_breaker.contact_info_tripped(session, user, account_id):
        return (
            CheckState.BLOCKED,
            "Enrichment is stopped after several runs lost too many Contact info answers;"
            " see Posture on the Settings page",
        )
    return None
