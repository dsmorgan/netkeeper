"""A fake LinkedIn inbox page source, for the inbox poll's core tests (P4-08, #378).

The real source is P4-01's (#380). This one hands back a delta a test built, or
raises the stop a test chose, and records every spec it was asked to read. It opens
no socket and loads no page. Every URN and every message here is invented.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from netkeeper.linkedin.inbox import (
    InboxConversation,
    InboxDelta,
    InboxJobSpec,
    InboxMessage,
    InboxReadStopped,
)
from netkeeper.models import SyncRun, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.services.linkedin_accounts import ensure_account

#: The account owner's own profile URN, invented.
OWNER_URN = "urn:li:fsd_profile:INVENTEDOWNER"


def profile_urn(name: str) -> str:
    """An invented ``fsd_profile`` URN."""
    return f"urn:li:fsd_profile:INVENTED{name.upper()}"


def message(
    number: int,
    *,
    sender: str,
    at: datetime,
    outbound: bool = False,
    text: str = "An invented message.",
    tag: str = "",
) -> InboxMessage:
    return InboxMessage(
        message_urn=f"urn:li:msg_message:INVENTED{tag.upper()}{number}",
        sender_urn=OWNER_URN if outbound else sender,
        outbound=outbound,
        at=at,
        text_snippet=text,
    )


def conversation(
    name: str, counterpart: str, messages: Sequence[InboxMessage], *, last: datetime | None = None
) -> InboxConversation:
    latest = max((m.at for m in messages), default=None)
    activity = last or latest
    assert activity is not None
    return InboxConversation(
        conversation_urn=f"urn:li:msg_conversation:INVENTED{name.upper()}",
        counterpart_urn=counterpart,
        last_activity_at=activity,
        messages=tuple(messages),
    )


def delta(
    *conversations: InboxConversation,
    skipped_group: int = 0,
    skipped_other: int = 0,
    complete: bool = True,
) -> InboxDelta:
    return InboxDelta(
        conversations=tuple(conversations),
        skipped_group=skipped_group,
        skipped_other=skipped_other,
        complete=complete,
    )


def a_thread_with(counterpart: str, at: datetime, *, name: str = "one") -> InboxConversation:
    """A conversation of two messages: one sent, then a reply a minute later."""
    return conversation(
        name,
        counterpart,
        [
            message(1, sender=counterpart, at=at, outbound=True, text="Invented opener.", tag=name),
            message(
                2,
                sender=counterpart,
                at=at + timedelta(minutes=1),
                text="Invented reply.",
                tag=name,
            ),
        ],
    )


class FakeInboxSource:
    """An :class:`~netkeeper.linkedin.inbox.InboxSource` that returns ``answer``.

    ``answer`` is a delta, or an exception to raise (an ``InboxReadStopped`` for a
    wall). ``specs`` records what each read was asked for.
    """

    def __init__(self, answer: InboxDelta | BaseException | None = None) -> None:
        self.answer: InboxDelta | BaseException = answer if answer is not None else delta()
        self.specs: list[InboxJobSpec] = []

    async def read(self, spec: InboxJobSpec) -> InboxDelta:
        self.specs.append(spec)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


FIXTURE_NOTE = "a fixture poll"
"""On every row :func:`record_poll` writes, so a test can tell it from a poll that ran."""


def record_poll(
    session: Session,
    user: User,
    at: datetime,
    *,
    status: SyncRunStatus = SyncRunStatus.COMPLETED,
    reason: str = "inbox_read",
    ended: datetime | None = None,
) -> SyncRun:
    """An inbox poll that started and ended at ``at``: complete by default, or ``aborted``
    (``inbox_incomplete``, a wall) and so never counted as one that read the inbox (#417).
    ``ended`` is when it finished, for a poll that ran long. Written as a row, so it never
    waits on a run that is still running."""
    run = SyncRun(
        user_id=user.id,
        linkedin_account_id=ensure_account(session, user).id,
        kind=SyncRunKind.INBOX,
        status=status,
        trigger=SyncRunTrigger.MANUAL,
        started_at=at,
        completed_at=ended or at,
        stop_reason=reason,
        notes=FIXTURE_NOTE,
    )
    session.add(run)
    session.flush()
    return run


__all__ = [
    "FIXTURE_NOTE",
    "OWNER_URN",
    "FakeInboxSource",
    "InboxReadStopped",
    "a_thread_with",
    "conversation",
    "delta",
    "message",
    "profile_urn",
    "record_poll",
]
