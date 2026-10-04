"""``/settings/self-contact``: your own details, held as the self contact (#342).

A test send renders a template's contact fields with these, and goes to the
campaign mailbox's own address. The self contact is never in your contact lists,
search, triage, exports or campaigns (:mod:`netkeeper.crm.self_contact`); this is
the only place to see or edit it, on the Settings page under "About you".

- ``GET /settings/self-contact`` answers the details, empty before the self contact
  is first created (netkeeper creates it at startup).
- ``PUT /settings/self-contact`` replaces them, creating the self contact if needed.
  A blank field clears it. ``422`` for a value that is too long.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from netkeeper.crm import self_contact as service
from netkeeper.web.deps import CurrentUser, SessionDep

router = APIRouter(tags=["settings"])


class SelfContactIn(BaseModel):
    first_name: Annotated[str, Field(max_length=200)] = ""
    """What ``{{ first_name }}`` renders in a test send."""
    last_name: Annotated[str, Field(max_length=200)] = ""
    current_company: Annotated[str, Field(max_length=300)] = ""
    """What ``{{ company }}`` renders in a test send."""
    current_title: Annotated[str, Field(max_length=300)] = ""
    """What ``{{ title }}`` renders in a test send."""
    location: Annotated[str, Field(max_length=300)] = ""


class SelfContactOut(SelfContactIn):
    exists: bool
    """False before the self contact is first created: every field is empty."""


def _out(details: service.SelfDetails, *, exists: bool) -> SelfContactOut:
    return SelfContactOut(
        first_name=details.first_name,
        last_name=details.last_name,
        current_company=details.current_company,
        current_title=details.current_title,
        location=details.location,
        exists=exists,
    )


@router.get("/settings/self-contact", operation_id="get_self_contact")
def get_self_contact(session: SessionDep, user: CurrentUser) -> SelfContactOut:
    """Your own details: what a test send renders the contact fields with."""
    contact = service.get_self_contact(session, user)
    return _out(service.SelfDetails.of(contact), exists=contact is not None)


@router.put(
    "/settings/self-contact",
    operation_id="set_self_contact",
    responses={422: {"description": "A value that is too long"}},
)
def set_self_contact(
    body: SelfContactIn, session: SessionDep, user: CurrentUser
) -> SelfContactOut:
    """Replace your own details. The next test send renders with them."""
    try:
        contact = service.update_self_contact(session, user, body.model_dump())
    except service.InvalidSelfValue as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _out(service.SelfDetails.of(contact), exists=True)
