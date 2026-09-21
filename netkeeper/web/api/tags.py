"""``/tags`` and ``/contacts/{id}/tags``: tag management and tagging a contact (spec 14.1).

The contact routes here are the only two this module adds to ``/contacts``;
the rest of that resource is the contacts API (P1-05). The rules that decide
what tagging and untagging do are in :mod:`netkeeper.crm.tags`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, HTTPException

from netkeeper.crm import tags as service
from netkeeper.models import Tag
from netkeeper.web.deps import CurrentUser, SessionDep
from netkeeper.web.schemas import ContactTagCreate, ContactTagOut, TagCreate, TagOut, TagPatch

router = APIRouter(tags=["tags"])

Responses = dict[int | str, dict[str, Any]]
NOT_FOUND: Responses = {404: {"description": "No such tag, rule, or contact for this user"}}
CONFLICT: Responses = {
    409: {"description": "A tag by that name exists (names are case-insensitive)"}
}
INVALID: Responses = {
    422: {"description": "A name, color, pattern, or reorder request that cannot be stored"}
}


@contextmanager
def translate_errors() -> Iterator[None]:
    """Map the service's exceptions to HTTP statuses: 404, 409, 422."""
    try:
        yield
    except (service.TagNotFound, service.RuleNotFound, service.ContactNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.DuplicateTag as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (service.InvalidTagValue, service.InvalidPattern) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def tag_out(tag: Tag, contact_count: int) -> TagOut:
    return TagOut(
        id=tag.id,
        name=tag.name,
        color=tag.color,
        kind=tag.kind,
        contact_count=contact_count,
        created_at=tag.created_at,
        updated_at=tag.updated_at,
    )


@router.get("/tags", operation_id="list_tags")
def list_tags(user: CurrentUser, session: SessionDep) -> list[TagOut]:
    """Every tag with the number of live contacts carrying it, by name."""
    return [tag_out(row.tag, row.contact_count) for row in service.list_tags(session, user)]


@router.post("/tags", operation_id="create_tag", status_code=201, responses={**CONFLICT, **INVALID})
def create_tag(body: TagCreate, user: CurrentUser, session: SessionDep) -> TagOut:
    with translate_errors():
        tag = service.create_tag(session, user, body.name, color=body.color, kind=body.kind)
    return tag_out(tag, 0)


@router.patch(
    "/tags/{tag_id}",
    operation_id="update_tag",
    responses={**NOT_FOUND, **CONFLICT, **INVALID},
)
def update_tag(tag_id: int, body: TagPatch, user: CurrentUser, session: SessionDep) -> TagOut:
    """Rename or recolor. A field left out is left alone; ``color: null`` clears it."""
    color: str | service.Unset | None = (
        body.color if "color" in body.model_fields_set else service.UNSET
    )
    with translate_errors():
        tag = service.update_tag(session, user, tag_id, name=body.name, color=color)
    count = service.contact_counts(session, user, [tag.id]).get(tag.id, 0)
    return tag_out(tag, count)


@router.delete("/tags/{tag_id}", operation_id="delete_tag", status_code=204, responses=NOT_FOUND)
def delete_tag(tag_id: int, user: CurrentUser, session: SessionDep) -> None:
    """Delete a tag and, with it, its assignments, suppressions, and rules."""
    with translate_errors():
        service.delete_tag(session, user, tag_id)


@router.get(
    "/contacts/{contact_id}/tags",
    operation_id="list_contact_tags",
    responses=NOT_FOUND,
)
def list_contact_tags(
    contact_id: int, user: CurrentUser, session: SessionDep
) -> list[ContactTagOut]:
    """The tags on a contact, by name, each with the source that put it there."""
    with translate_errors():
        rows = service.contact_tags(session, user, contact_id)
    return [ContactTagOut.model_validate(row) for row in rows]


@router.post(
    "/contacts/{contact_id}/tags",
    operation_id="tag_contact",
    status_code=201,
    responses=NOT_FOUND,
)
def tag_contact(
    contact_id: int, body: ContactTagCreate, user: CurrentUser, session: SessionDep
) -> ContactTagOut:
    """Put a tag on a contact by hand. Answers the existing assignment when there is one."""
    with translate_errors():
        row = service.tag_contact(session, user, contact_id, body.tag_id)
    return ContactTagOut.model_validate(row)


@router.delete(
    "/contacts/{contact_id}/tags/{tag_id}",
    operation_id="untag_contact",
    status_code=204,
    responses=NOT_FOUND,
)
def untag_contact(contact_id: int, tag_id: int, user: CurrentUser, session: SessionDep) -> None:
    """Take a tag off a contact. Removing an automatic tag keeps rules from re-adding it."""
    with translate_errors():
        if not service.untag_contact(session, user, contact_id, tag_id):
            raise HTTPException(status_code=404, detail="the contact does not carry that tag")
