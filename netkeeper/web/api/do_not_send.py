"""``/do-not-send``: the addresses no campaign sends to (#238, Part B).

- ``GET /do-not-send`` lists every entry, newest first.
- ``POST /do-not-send`` adds an address by hand (``manual``).
- ``DELETE /do-not-send/{entry_id}`` removes an entry. Only a person does this:
  nothing else ever takes an address off the list.

The CLI mirrors them as ``netkeeper do-not-send list|add|remove``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from netkeeper.crm import do_not_send as service
from netkeeper.models import DoNotSendAddress, DoNotSendReason
from netkeeper.web.deps import CurrentUser, SessionDep

router = APIRouter(tags=["do-not-send"])

Responses = dict[int | str, dict[str, Any]]
NOT_FOUND: Responses = {404: {"description": "No such entry"}}


class DoNotSendOut(BaseModel):
    id: int
    email: str
    """Trimmed and lowercased; a ``+tag`` is part of the address."""
    reason: DoNotSendReason
    contact_id: int | None
    """The contact the address was found on; ``null`` when added by hand or since deleted."""
    created_at: datetime


class DoNotSendIn(BaseModel):
    email: str


def _out(entry: DoNotSendAddress) -> DoNotSendOut:
    return DoNotSendOut(
        id=entry.id,
        email=entry.email,
        reason=entry.reason,
        contact_id=entry.contact_id,
        created_at=entry.created_at,
    )


@router.get("/do-not-send", operation_id="list_do_not_send")
def list_entries(session: SessionDep, user: CurrentUser) -> list[DoNotSendOut]:
    """Every address on the do-not-send list, newest first."""
    return [_out(entry) for entry in service.entries(session, user)]


@router.post(
    "/do-not-send",
    operation_id="add_do_not_send",
    status_code=201,
    responses={422: {"description": "Not one bare email address"}},
)
def add_entry(body: DoNotSendIn, session: SessionDep, user: CurrentUser) -> DoNotSendOut:
    """Put an address on the list by hand. An address already there keeps its reason."""
    try:
        entry = service.add_by_hand(session, user, body.email)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _out(entry)


@router.delete(
    "/do-not-send/{entry_id}",
    operation_id="remove_do_not_send",
    status_code=204,
    responses=NOT_FOUND,
)
def remove_entry(entry_id: int, session: SessionDep, user: CurrentUser) -> None:
    """Take an address off the list, so campaigns may send to it again."""
    try:
        service.remove(session, user, entry_id)
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
