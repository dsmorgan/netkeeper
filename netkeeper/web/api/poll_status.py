"""``GET /poll-status``: when each background check last ran and runs next (#401).

What the app header's status and the campaign page's "replies checked" line read.
See :mod:`netkeeper.services.poll_status` for where each time comes from.

Read-only and per-user. It never starts a check: no Gmail call, no browser, no
write. A check that cannot run (``netkeeper serve`` not running, disarmed, paused,
outside active hours, the session flagged, Gmail not connected, not built yet)
answers its state and a reason instead of a time.
"""

from __future__ import annotations

import math
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from netkeeper.config import Settings
from netkeeper.models.base import utcnow
from netkeeper.services import poll_status as service
from netkeeper.services.campaign_sender import DRAFTS_POLL_EVERY, GmailSender
from netkeeper.web.deps import CurrentUser, SessionDep

router = APIRouter(tags=["poll-status"])


class PollCheckOut(BaseModel):
    """One background check. ``next_at`` is set only while ``state`` is ``scheduled``."""

    key: str
    group: service.CheckGroup
    label: str
    state: service.CheckState
    interval_minutes: int
    last_at: datetime | None
    """When it last ran: the last completed LinkedIn run of its kind, this process's
    last reply poll that caught up (without ``serve``, the oldest armed mailbox's), this
    process's last drafts poll."""
    next_at: datetime | None
    reason: str | None
    """Why there is no next time, in a sentence. A running Gmail reply check may carry
    one too, naming an armed mailbox it cannot read."""


class MailboxPollOut(BaseModel):
    """One mailbox's reply poll, for the campaign page."""

    mailbox_id: int
    email: str
    armed: bool
    state: service.CheckState
    replies_polled_at: datetime | None
    next_at: datetime | None
    reason: str | None


class PollStatusOut(BaseModel):
    checked_at: datetime
    background_running: bool
    """Whether this process runs background checks at all (``netkeeper serve``)."""
    items: list[PollCheckOut]
    """Every check, Gmail first. The campaign engine's minute tick is not one."""
    mailboxes: list[MailboxPollOut]


def _serving(request: Request, user_id: int) -> service.Serving:
    """What this process runs, read from ``app.state``; None of it is started here."""
    engine = request.app.state.campaign_engine
    sender = None if engine is None else engine.sender
    gmail = sender if isinstance(sender, GmailSender) else None
    return service.Serving(
        scheduler=request.app.state.scheduler is not None,
        campaign_engine=engine is not None,
        replies_polled_at=None if gmail is None else gmail.replies_polled_at(user_id),
        replies_every=None if gmail is None else gmail.replies_every,
        replies_due=frozenset() if gmail is None else gmail.replies_due(user_id),
        replies_not_ready={} if gmail is None else gmail.replies_not_ready(user_id),
        drafts_polled_at=None if gmail is None else gmail.drafts_polled_at(user_id),
        drafts_every=DRAFTS_POLL_EVERY if gmail is None else gmail.drafts_every,
    )


@router.get("/poll-status", operation_id="get_poll_status")
def get_poll_status(request: Request, user: CurrentUser, session: SessionDep) -> PollStatusOut:
    """Each check's last and next run. Read-only: it never triggers one."""
    settings: Settings = request.app.state.settings
    serving = _serving(request, user.id)
    now = utcnow()
    status = service.poll_status(session, user, now=now, settings=settings, serving=serving)
    return PollStatusOut(
        checked_at=now,
        background_running=status.background_running,
        items=[
            PollCheckOut(
                key=check.key,
                group=check.group,
                label=check.label,
                state=check.state,
                interval_minutes=max(1, math.ceil(check.interval.total_seconds() / 60)),
                last_at=check.last_at,
                next_at=check.next_at,
                reason=check.reason,
            )
            for check in status.checks
        ],
        mailboxes=[
            MailboxPollOut(
                mailbox_id=poll.mailbox_id,
                email=poll.email,
                armed=poll.armed,
                state=poll.state,
                replies_polled_at=poll.replies_polled_at,
                next_at=poll.next_at,
                reason=poll.reason,
            )
            for poll in status.mailboxes
        ],
    )
