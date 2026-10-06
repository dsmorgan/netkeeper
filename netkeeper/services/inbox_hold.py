"""Hold LinkedIn steps while the LinkedIn inbox poll is stale (#417).

Gmail sending refuses while its reply poll is behind (``GmailSender._replies_stale``),
so a broken poll can never mean a follow-up goes to someone who replied. This is the
LinkedIn twin. If the inbox poll fails or stops running, LinkedIn replies are not seen,
and a prefill, or an email step that follows a LinkedIn conversation, would go to
someone who may already have answered.

**The rule** (:func:`is_stale`, pure). The inbox is stale when its newest *complete*
poll is older than :data:`STALE_AFTER_POLLS` poll intervals plus :data:`SLACK`, or no
complete poll has ever run. A poll that stopped part way (``aborted``, including
``inbox_incomplete``) never refreshes it, as a Gmail mailbox that is "behind" does not.
The age counts from when the poll *started*: it read up to that moment, and the next
poll reads from there.

- ``N = 2``, Gmail's :data:`~netkeeper.services.campaign_replies.STALE_AFTER_POLLS`. One
  missed poll is noise (a busy browser, a retry); two in a row is a poll that is not
  running.
- The :data:`SLACK` is the scheduler's longest retry (``RETRY_MAX_MINUTES``). A fire that
  finds the browser busy parks one retry 20 to 50 minutes out, so one missed fire can
  legitimately make two polls start 2 intervals plus 50 minutes apart. Without it the
  hold would close on a single late poll.

**No poll, or polls off.** Whether LinkedIn is armed, paused, or not served changes
nothing: a poll that did not run did not read the inbox. An unarmed install holds until
a person runs ``netkeeper linkedin inbox`` (it works while scheduled runs are
disarmed). A person is never messaged blind because a switch was off.

**What is held** (the callers):

- a LinkedIn prefill claim (:mod:`netkeeper.services.linkedin_steps`), always: it is a
  LinkedIn step, so LinkedIn is live;
- any step, email included, of an enrollment whose contact is *watched*
  (:func:`contact_watched`): a LinkedIn conversation with them is on file (the poll has
  seen one), or a campaign message was claimed for them on LinkedIn (a prefill not
  discarded, or discarded after it was typed, with no time limit). A contact with a
  LinkedIn URN and neither is not watched: nothing says they talk to you there.

A hold means wait: nothing changes on the enrollment, the step stays due, and the next
tick or claim asks again. It releases the moment a poll *completes*; no timer ends it.
It only ever adds a refusal: every check that already holds still runs first.

No function here writes, calls Gmail, or attaches to a browser.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from netkeeper.models import (
    Campaign,
    CampaignStep,
    Enrollment,
    LiConversation,
    Message,
    MessageDirection,
    MessageStatus,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    TemplateChannel,
    User,
)
from netkeeper.scoping import scoped
from netkeeper.services.campaign_guards import LIVE_ENROLLMENT_STATUSES, RUNNING_CAMPAIGN_STATUSES
from netkeeper.services.scheduler import DEFAULT_SCHEDULES, RETRY_MAX_MINUTES, JobKind

STALE_AFTER_POLLS: Final = 2
"""The inbox is stale once its newest complete poll is older than this many intervals
(plus :data:`SLACK`). Gmail's number, ``campaign_replies.STALE_AFTER_POLLS``."""

SLACK: Final = timedelta(minutes=RETRY_MAX_MINUTES)
"""Added to the limit: the longest the scheduler parks a retry after one missed fire."""

INTERVAL: Final = DEFAULT_SCHEDULES[JobKind.INBOX].interval
"""How often the inbox poll runs, by the scheduler's own default."""

_REASON: Final = "The LinkedIn inbox hasn't been read recently"


def stale_after(interval: timedelta = INTERVAL) -> timedelta:
    """How old the newest complete poll may be before the inbox is stale."""
    return STALE_AFTER_POLLS * interval + SLACK


def is_stale(
    last_complete: datetime | None,
    *,
    now: datetime,
    interval: timedelta = INTERVAL,
    linkedin_live: bool = True,
) -> bool:
    """Whether the inbox is stale. Pure.

    ``last_complete`` is when the newest complete poll started, ``None`` before the first.
    With none, it is stale when ``linkedin_live`` (a LinkedIn step or a watched contact
    exists); with nothing live there is nothing to hold. A poll exactly
    ``stale_after()`` old is still fresh, and one that starts in the future (a clock set
    back) counts as fresh: only an old poll is stale.
    """
    if last_complete is None:
        return linkedin_live
    return now - last_complete > stale_after(interval)


def last_complete_poll(session: Session, user: User, *, now: datetime) -> datetime | None:
    """When the newest complete inbox poll that had started by ``now`` started, any
    trigger; ``None`` before the first. An aborted poll never counts, and one stamped in
    the future cannot stay fresh (the newest one that has started is read). Read-only."""
    return session.scalar(
        scoped(user, SyncRun)
        .with_only_columns(SyncRun.started_at)
        .where(
            SyncRun.kind == SyncRunKind.INBOX,
            SyncRun.status == SyncRunStatus.COMPLETED,
            SyncRun.started_at <= now,
        )
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(1)
    )


def stale(session: Session, user: User, *, now: datetime, linkedin_live: bool = True) -> bool:
    """:func:`is_stale` for ``user``'s newest complete poll. Read-only."""
    return is_stale(
        last_complete_poll(session, user, now=now), now=now, linkedin_live=linkedin_live
    )


def _typed_and_watched() -> ColumnElement[bool]:
    """A LinkedIn message that watches the contact: anything but a discarded prefill, or
    a discarded one that was typed (``prefilled_at``), with no time limit. The person may
    have sent it anyway, and the next step is due a full delay after the discard (7 days
    by default), so any window shorter than that would lapse before the step is due. Once
    a complete poll reads a real send, a conversation row watches the contact."""
    return or_(Message.status != MessageStatus.DISCARDED, Message.prefilled_at.is_not(None))


def contact_watched(session: Session, user: User, contact_id: int) -> bool:
    """Whether a LinkedIn conversation with the contact is being watched: the poll has
    one on file for them, or a campaign message was claimed for them on LinkedIn and
    not discarded. A discarded one counts if it was typed (``prefilled_at`` set), however
    long ago: the person may have sent it anyway. Read-only."""
    conversation = scoped(user, LiConversation).where(LiConversation.contact_id == contact_id)
    claimed = scoped(user, Message).where(
        Message.contact_id == contact_id,
        Message.channel == TemplateChannel.LINKEDIN,
        Message.direction == MessageDirection.OUT,
        _typed_and_watched(),
    )
    return (
        session.scalar(conversation.with_only_columns(LiConversation.id).limit(1)) is not None
        or session.scalar(claimed.with_only_columns(Message.id).limit(1)) is not None
    )


def linkedin_in_use(session: Session, user: User) -> bool:
    """Whether a live enrollment of a running campaign has a LinkedIn step, or a
    contact being watched. What decides whether "no poll has ever run" is a problem
    worth a posture row. Three ``EXISTS``-style reads, whatever the number of
    enrollments. Read-only."""
    live = (
        scoped(user, Enrollment)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .where(
            Campaign.user_id == user.id,
            Enrollment.status.in_(LIVE_ENROLLMENT_STATUSES),
            Campaign.status.in_(RUNNING_CAMPAIGN_STATUSES),
        )
    )
    has_step = live.join(CampaignStep, CampaignStep.campaign_id == Campaign.id).where(
        CampaignStep.channel == TemplateChannel.LINKEDIN
    )
    has_conversation = live.join(
        LiConversation,
        and_(
            LiConversation.contact_id == Enrollment.contact_id,
            LiConversation.user_id == user.id,
        ),
    )
    has_claimed = live.join(
        Message,
        and_(
            Message.contact_id == Enrollment.contact_id,
            Message.user_id == user.id,
            Message.channel == TemplateChannel.LINKEDIN,
            Message.direction == MessageDirection.OUT,
            _typed_and_watched(),
        ),
    )
    return any(
        session.scalar(query.with_only_columns(Enrollment.id).limit(1)) is not None
        for query in (has_step, has_conversation, has_claimed)
    )


def why_stale(session: Session, user: User, *, now: datetime) -> str:
    """The sentence that says why the inbox is stale, for a refusal or a status line.
    Read-only."""
    last = last_complete_poll(session, user, now=now)
    action = "run `netkeeper linkedin inbox`, or wait for the next scheduled poll"
    if last is None:
        return f"{_REASON}: no poll has completed yet; {action}"
    hours = max((now - last).total_seconds(), 0) / 3600
    return (
        f"{_REASON}: its last complete poll was {hours:.1f} hours ago, and a poll older than"
        f" {stale_after().total_seconds() / 3600:.1f} hours holds LinkedIn steps; {action}"
    )


def claim_hold(session: Session, user: User, *, now: datetime) -> str | None:
    """Why a LinkedIn prefill claim must wait, or ``None``. Read-only."""
    return why_stale(session, user, now=now) if stale(session, user, now=now) else None
