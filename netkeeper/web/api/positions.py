"""``/me/positions``: the user's own job history (spec 8.1, 10.2; P1-26, #84).

Filled from the LinkedIn archive's ``Positions.csv`` (:mod:`netkeeper.crm.archive`)
and editable by hand through this router; :mod:`netkeeper.crm.triage` reads the
table for the you-and-them overlap on the triage card. A position that is not
the current user's answers ``404``, the same rule every other resource here
follows.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response

from netkeeper.crm import positions as service
from netkeeper.crm.positions import MISSING, InvalidPosition, NotFound
from netkeeper.web.deps import CurrentUser, SessionDep
from netkeeper.web.schemas import UserPositionIn, UserPositionOut, UserPositionPatch

router = APIRouter(tags=["positions"])

Responses = dict[int | str, dict[str, Any]]
NO_SUCH_POSITION: Responses = {404: {"description": "No such position"}}
INVALID_POSITION: Responses = {422: {"description": "Neither a title nor a company"}}


@router.get("/me/positions", operation_id="list_my_positions")
def list_my_positions(user: CurrentUser, session: SessionDep) -> list[UserPositionOut]:
    """The user's own job history, current first, then most recently started."""
    return [UserPositionOut.model_validate(row) for row in service.list_positions(session, user)]


@router.post(
    "/me/positions",
    operation_id="create_my_position",
    status_code=201,
    responses=INVALID_POSITION,
)
def create_my_position(
    body: UserPositionIn, user: CurrentUser, session: SessionDep
) -> UserPositionOut:
    """Add one stint to the user's own job history by hand."""
    try:
        row = service.add_position(
            session,
            user,
            title=body.title,
            company=body.company,
            company_urn=body.company_urn,
            started_on=body.started_on,
            ended_on=body.ended_on,
            is_current=body.is_current,
        )
    except InvalidPosition as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return UserPositionOut.model_validate(row)


@router.patch(
    "/me/positions/{position_id}",
    operation_id="update_my_position",
    responses={**NO_SUCH_POSITION, **INVALID_POSITION},
)
def update_my_position(
    position_id: int, body: UserPositionPatch, user: CurrentUser, session: SessionDep
) -> UserPositionOut:
    """Change fields of one of the user's own positions; a field left out is untouched."""
    given = body.model_fields_set
    try:
        row = service.update_position(
            session,
            user,
            position_id,
            title=body.title if "title" in given else MISSING,
            company=body.company if "company" in given else MISSING,
            company_urn=body.company_urn if "company_urn" in given else MISSING,
            started_on=body.started_on if "started_on" in given else MISSING,
            ended_on=body.ended_on if "ended_on" in given else MISSING,
            is_current=body.is_current if body.is_current is not None else MISSING,
        )
    except NotFound:
        raise _no_such_position() from None
    except InvalidPosition as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return UserPositionOut.model_validate(row)


@router.delete(
    "/me/positions/{position_id}",
    operation_id="delete_my_position",
    status_code=204,
    responses=NO_SUCH_POSITION,
)
def delete_my_position(position_id: int, user: CurrentUser, session: SessionDep) -> Response:
    """Delete one of the user's own positions."""
    try:
        service.delete_position(session, user, position_id)
    except NotFound:
        raise _no_such_position() from None
    return Response(status_code=204)


def _no_such_position() -> HTTPException:
    return HTTPException(status_code=404, detail="no such position")
