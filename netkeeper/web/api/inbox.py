"""``/inbox``: what reply detection found, and marking it handled (spec 11.7; P3-11b).

- ``GET /inbox`` lists replies, unsubscribe replies and bounces, newest first.
- ``PUT /inbox/{message_id}/handled`` marks one handled or not, idempotently.

A note on an item is the contact's own ``note`` interaction
(``POST /contacts/{contact_id}/interactions``); nothing here adds a second way.
A message that is not the user's, or not an inbox item, answers ``404``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from netkeeper.models import EnrollmentStatus, TemplateChannel
from netkeeper.services import inbox as service
from netkeeper.services.inbox import InboxKind
from netkeeper.web.deps import CurrentUser, SessionDep

router = APIRouter(tags=["inbox"])

Responses = dict[int | str, dict[str, Any]]
NOT_FOUND: Responses = {404: {"description": "No such inbox item"}}

Limit = Annotated[int, Query(ge=1, le=service.PAGE_MAX, description="Items per page.")]
Offset = Annotated[int, Query(ge=0, description="Items to skip.")]


class InboxItemOut(BaseModel):
    id: int
    """The message's id."""
    kind: InboxKind
    contact_id: int
    contact_name: str
    campaign_id: int
    campaign_name: str
    enrollment_id: int
    enrollment_status: EnrollmentStatus
    channel: TemplateChannel
    """Where it arrived: ``email`` (Gmail) or ``linkedin`` (the inbox poll, P4-02)."""
    li_conversation_urn: str | None
    """A LinkedIn item's conversation, for a plain link to the thread (#383); ``null``
    for email, or when the poll did not know the conversation."""
    subject: str | None
    """An email's subject; ``null`` for a LinkedIn message, which has none."""
    snippet: str | None
    """The snippet as plain text (Gmail's, or at most 200 characters of a LinkedIn
    message); ``null`` for a bounce, whose notice is not stored."""
    received_at: datetime | None
    handled_at: datetime | None


class InboxPageOut(BaseModel):
    items: list[InboxItemOut]
    total: int
    """Items matching the filters."""
    unhandled: int
    """Unhandled items of every kind, whatever the filters."""


class HandledIn(BaseModel):
    handled: bool


def _out(item: service.InboxItem) -> InboxItemOut:
    m = item.message
    return InboxItemOut(
        id=m.id,
        kind=item.kind,
        contact_id=m.contact_id,
        contact_name=item.contact_name,
        campaign_id=item.campaign_id,
        campaign_name=item.campaign_name,
        enrollment_id=m.enrollment_id,
        enrollment_status=item.enrollment_status,
        channel=m.channel,
        li_conversation_urn=(
            m.li_conversation_urn if m.channel is TemplateChannel.LINKEDIN else None
        ),
        subject=m.subject,
        snippet=m.snippet if item.kind is not InboxKind.BOUNCE else None,
        received_at=item.received_at,
        handled_at=m.handled_at,
    )


@router.get("/inbox", operation_id="list_inbox")
def list_inbox(
    session: SessionDep,
    user: CurrentUser,
    handled: Annotated[
        bool | None,
        Query(description="`false` for the unhandled items, `true` for the handled; omit for all."),
    ] = None,
    kind: InboxKind | None = None,
    enrollment_id: int | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> InboxPageOut:
    """Replies, unsubscribe replies and bounces, newest first."""
    page = service.list_inbox(
        session,
        user,
        handled=handled,
        kind=kind,
        enrollment_id=enrollment_id,
        limit=limit,
        offset=offset,
    )
    return InboxPageOut(
        items=[_out(item) for item in page.items], total=page.total, unhandled=page.unhandled
    )


@router.put("/inbox/{message_id}/handled", operation_id="set_inbox_handled", responses=NOT_FOUND)
def set_handled(
    message_id: int, body: HandledIn, session: SessionDep, user: CurrentUser
) -> InboxItemOut:
    """Mark an item handled or not. Marking it handled again keeps the first time."""
    try:
        item = service.set_handled(session, user, message_id, body.handled)
    except service.InboxItemNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _out(item)
