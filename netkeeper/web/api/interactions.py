"""Interactions, the timeline, and notes under ``/contacts/{contact_id}`` (spec 8.1, 10.1).

A contact that is not the current user's answers ``404``, never ``403``: a
``403`` would confirm that the id exists for someone. The service
(:mod:`netkeeper.crm.interactions`) keeps ``last_contacted_at`` current; nothing
here touches it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import AwareDatetime

from netkeeper.crm import interactions as service
from netkeeper.crm.interactions import MISSING, NoSuchMessage, NotFound
from netkeeper.web.deps import CurrentUser, SessionDep
from netkeeper.web.schemas import (
    InteractionIn,
    InteractionOut,
    InteractionPage,
    InteractionPatch,
    NotesIn,
    NotesOut,
    TimelinePage,
    timeline_entry_out,
)

router = APIRouter(tags=["interactions"])

Responses = dict[int | str, dict[str, Any]]
NO_SUCH_CONTACT: Responses = {404: {"description": "No such contact"}}
NO_SUCH_INTERACTION: Responses = {404: {"description": "No such interaction"}}

Limit = Annotated[int, Query(ge=1, le=200, description="Items per page.")]
Offset = Annotated[int, Query(ge=0, description="Items to skip.")]
Before = Annotated[
    AwareDatetime | None,
    Query(description="Cursor: only entries older than this timestamp. From `next_before`."),
]


@router.get(
    "/contacts/{contact_id}/interactions",
    operation_id="list_interactions",
    responses=NO_SUCH_CONTACT,
)
def list_interactions(
    contact_id: int,
    user: CurrentUser,
    session: SessionDep,
    limit: Limit = 50,
    offset: Offset = 0,
) -> InteractionPage:
    """The contact's interactions, newest first."""
    try:
        rows, total = service.list_interactions(
            session, user, contact_id, limit=limit, offset=offset
        )
    except NotFound:
        raise _no_such_contact() from None
    return InteractionPage(items=[InteractionOut.model_validate(row) for row in rows], total=total)


@router.post(
    "/contacts/{contact_id}/interactions",
    operation_id="create_interaction",
    status_code=201,
    responses=NO_SUCH_CONTACT,
)
def create_interaction(
    contact_id: int, body: InteractionIn, user: CurrentUser, session: SessionDep
) -> InteractionOut:
    """Record an interaction. An outbound kind moves the contact's `last_contacted_at`."""
    try:
        row = service.add_interaction(
            session,
            user,
            contact_id,
            body.kind,
            body.at,
            summary=body.summary,
            message_id=body.message_id,
        )
    except NotFound:
        raise _no_such_contact() from None
    except NoSuchMessage:
        raise _no_such_message() from None
    return InteractionOut.model_validate(row)


@router.patch(
    "/interactions/{interaction_id}",
    operation_id="update_interaction",
    responses=NO_SUCH_INTERACTION,
)
def update_interaction(
    interaction_id: int, body: InteractionPatch, user: CurrentUser, session: SessionDep
) -> InteractionOut:
    """Change an interaction's fields; the contact's `last_contacted_at` follows."""
    given = body.model_fields_set
    try:
        row = service.update_interaction(
            session,
            user,
            interaction_id,
            kind=body.kind if body.kind is not None else MISSING,
            at=body.at if body.at is not None else MISSING,
            summary=body.summary if "summary" in given else MISSING,
            message_id=body.message_id if "message_id" in given else MISSING,
        )
    except NotFound:
        raise _no_such_interaction() from None
    except NoSuchMessage:
        raise _no_such_message() from None
    return InteractionOut.model_validate(row)


@router.delete(
    "/interactions/{interaction_id}",
    operation_id="delete_interaction",
    status_code=204,
    responses=NO_SUCH_INTERACTION,
)
def delete_interaction(interaction_id: int, user: CurrentUser, session: SessionDep) -> Response:
    """Delete an interaction; the contact's `last_contacted_at` follows."""
    try:
        service.delete_interaction(session, user, interaction_id)
    except NotFound:
        raise _no_such_interaction() from None
    return Response(status_code=204)


@router.get(
    "/contacts/{contact_id}/timeline",
    operation_id="get_timeline",
    responses=NO_SUCH_CONTACT,
)
def get_timeline(
    contact_id: int,
    user: CurrentUser,
    session: SessionDep,
    limit: Limit = 50,
    before: Before = None,
) -> TimelinePage:
    """Interactions and snapshots interleaved, newest first, cursor paged by `before`."""
    try:
        entries = service.timeline(session, user, contact_id, limit=limit, before=before)
    except NotFound:
        raise _no_such_contact() from None
    # A page shorter than the limit is the last one (see the service). A page of
    # exactly the limit may be followed by an empty one; that is the one cursor
    # the service cannot rule out without another query.
    next_before = entries[-1].at if len(entries) >= limit else None
    return TimelinePage(
        items=[timeline_entry_out(entry) for entry in entries], next_before=next_before
    )


@router.put(
    "/contacts/{contact_id}/notes", operation_id="set_contact_notes", responses=NO_SUCH_CONTACT
)
def set_contact_notes(
    contact_id: int, body: NotesIn, user: CurrentUser, session: SessionDep
) -> NotesOut:
    """Replace the contact's notes (Markdown, stored as sent)."""
    try:
        contact = service.set_notes(session, user, contact_id, body.notes)
    except NotFound:
        raise _no_such_contact() from None
    return NotesOut(contact_id=contact.id, notes=contact.notes, updated_at=contact.updated_at)


def _no_such_contact() -> HTTPException:
    return HTTPException(status_code=404, detail="no such contact")


def _no_such_interaction() -> HTTPException:
    return HTTPException(status_code=404, detail="no such interaction")


def _no_such_message() -> HTTPException:
    # 422 like a body that fails validation, and documented as one: an override in
    # ``responses`` would replace the validation-error schema the client is typed with.
    return HTTPException(status_code=422, detail="no such message for this contact")
