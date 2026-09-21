"""``/lists`` and ``/views``: static and smart lists, membership, and saved table views
(spec 10.1, 10.4; item P1-08).

A list or a view that is not the current user's answers ``404``, never a status
that would confirm the id exists for someone else. Membership and counts for a
smart list are computed fresh from its filter on every request
(:mod:`netkeeper.crm.lists`); nothing here or in the service materializes them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from netkeeper.crm import lists as service
from netkeeper.crm.filters import FilterError, SortKey, parse_filter
from netkeeper.models import Contact, ContactList, SavedView
from netkeeper.web.deps import CurrentUser, SessionDep
from netkeeper.web.schemas import (
    ContactSummaryOut,
    ListCreate,
    ListMembersAddedOut,
    ListMembersIn,
    ListMembersPage,
    ListOut,
    ListPatch,
    SavedViewCreate,
    SavedViewOut,
    SavedViewPatch,
)

router = APIRouter(tags=["lists"])

Responses = dict[int | str, dict[str, Any]]
NOT_FOUND: Responses = {404: {"description": "No such list, view, or contact for this user"}}
CONFLICT: Responses = {409: {"description": "A list or view by that name already exists"}}
INVALID: Responses = {
    422: {"description": "A name, kind/filter combination, or member request that cannot be stored"}
}

Limit = Annotated[int, Query(ge=1, le=200, description="Members per page.")]
Offset = Annotated[int, Query(ge=0, description="Members to skip.")]


@contextmanager
def translate_errors() -> Iterator[None]:
    """Map the service's exceptions, and a broken filter, to HTTP statuses: 404, 409, 422."""
    try:
        yield
    except (service.ListNotFound, service.ViewNotFound, service.ContactNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (service.DuplicateListName, service.DuplicateViewName) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (
        service.InvalidListValue,
        service.InvalidViewValue,
        service.WrongListKind,
        FilterError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _list_out(row: ContactList, member_count: int) -> ListOut:
    return ListOut(
        id=row.id,
        name=row.name,
        kind=row.kind,
        filter=None if row.filter_json is None else parse_filter(row.filter_json),
        member_count=member_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _view_out(row: SavedView) -> SavedViewOut:
    return SavedViewOut(
        id=row.id,
        name=row.name,
        columns=list(row.columns),
        sort=[SortKey.model_validate(item) for item in row.sort],
        filter=None if row.filter_json is None else parse_filter(row.filter_json),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _member_out(contact: Contact) -> ContactSummaryOut:
    return ContactSummaryOut.model_validate(contact)


# --- lists --------------------------------------------------------------


@router.get("/lists", operation_id="list_lists")
def list_lists(user: CurrentUser, session: SessionDep) -> list[ListOut]:
    """Every list, static and smart, with how many contacts are in it right now."""
    rows = service.list_lists(session, user)
    counts = service.member_counts(session, user, [row.id for row in rows])
    return [_list_out(row, counts.get(row.id, 0)) for row in rows]


@router.post(
    "/lists", operation_id="create_list", status_code=201, responses={**CONFLICT, **INVALID}
)
def create_list(body: ListCreate, user: CurrentUser, session: SessionDep) -> ListOut:
    """Create a static or smart list. A smart list's filter is validated (parsed and
    compiled) before it is stored; a static one starts with no members."""
    with translate_errors():
        row = service.create_list(session, user, body.name, body.kind, filter=body.filter)
    return _list_out(row, 0)


@router.patch(
    "/lists/{list_id}", operation_id="update_list", responses={**NOT_FOUND, **CONFLICT, **INVALID}
)
def update_list(list_id: int, body: ListPatch, user: CurrentUser, session: SessionDep) -> ListOut:
    """Rename a list and/or, for a smart list, replace its filter. Fields left out, or a
    ``filter`` of ``null``, are left alone."""
    filter_arg = service.UNSET if body.filter is None else body.filter
    with translate_errors():
        row = service.update_list(session, user, list_id, name=body.name, filter=filter_arg)
    count = service.member_count(session, user, row.id)
    return _list_out(row, count)


@router.delete("/lists/{list_id}", operation_id="delete_list", status_code=204, responses=NOT_FOUND)
def delete_list(list_id: int, user: CurrentUser, session: SessionDep) -> None:
    """Delete a list. Its members (if static) go with it."""
    with translate_errors():
        service.delete_list(session, user, list_id)


@router.get("/lists/{list_id}/members", operation_id="list_list_members", responses=NOT_FOUND)
def list_list_members(
    list_id: int,
    user: CurrentUser,
    session: SessionDep,
    limit: Limit = service.DEFAULT_PAGE_LIMIT,
    offset: Offset = 0,
) -> ListMembersPage:
    """One page of the list's members. For a smart list this is exactly what running its
    filter returns right now; nothing is materialized."""
    with translate_errors():
        contacts, total = service.list_members(session, user, list_id, limit=limit, offset=offset)
    return ListMembersPage(items=[_member_out(contact) for contact in contacts], total=total)


@router.post(
    "/lists/{list_id}/members",
    operation_id="add_list_members",
    status_code=201,
    responses={**NOT_FOUND, **INVALID},
)
def add_list_members(
    list_id: int, body: ListMembersIn, user: CurrentUser, session: SessionDep
) -> ListMembersAddedOut:
    """Add contacts to a static list. `422` for a smart list, which has no members to add."""
    with translate_errors():
        added = service.add_members(session, user, list_id, body.contact_ids)
    return ListMembersAddedOut(added=added)


@router.delete(
    "/lists/{list_id}/members/{contact_id}",
    operation_id="remove_list_member",
    status_code=204,
    responses={**NOT_FOUND, **INVALID},
)
def remove_list_member(
    list_id: int, contact_id: int, user: CurrentUser, session: SessionDep
) -> None:
    """Take a contact off a static list. `422` for a smart list."""
    with translate_errors():
        if not service.remove_member(session, user, list_id, contact_id):
            raise HTTPException(status_code=404, detail="the contact is not a member of that list")


# --- saved views ----------------------------------------------------------


@router.get("/views", operation_id="list_views")
def list_views(user: CurrentUser, session: SessionDep) -> list[SavedViewOut]:
    """Every saved table view, by name."""
    return [_view_out(row) for row in service.list_views(session, user)]


@router.post(
    "/views", operation_id="create_view", status_code=201, responses={**CONFLICT, **INVALID}
)
def create_view(body: SavedViewCreate, user: CurrentUser, session: SessionDep) -> SavedViewOut:
    """Save a column set, sort, and optional filter for the contacts table to restore."""
    with translate_errors():
        row = service.create_view(
            session, user, body.name, body.columns, sort=body.sort, filter=body.filter
        )
    return _view_out(row)


@router.patch(
    "/views/{view_id}", operation_id="update_view", responses={**NOT_FOUND, **CONFLICT, **INVALID}
)
def update_view(
    view_id: int, body: SavedViewPatch, user: CurrentUser, session: SessionDep
) -> SavedViewOut:
    """Change any of a view's name, columns, sort, and filter. Fields left out are left
    alone; ``filter: null`` clears it."""
    filter_arg = body.filter if "filter" in body.model_fields_set else service.UNSET
    with translate_errors():
        row = service.update_view(
            session,
            user,
            view_id,
            name=body.name,
            columns=body.columns,
            sort=body.sort,
            filter=filter_arg,
        )
    return _view_out(row)


@router.delete("/views/{view_id}", operation_id="delete_view", status_code=204, responses=NOT_FOUND)
def delete_view(view_id: int, user: CurrentUser, session: SessionDep) -> None:
    """Delete a saved view."""
    with translate_errors():
        service.delete_view(session, user, view_id)
