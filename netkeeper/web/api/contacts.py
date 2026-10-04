"""``/contacts``: the Contacts table, one contact in full, edits, bulk actions (spec 10.1, 14.1).

Routes translate; the rules are :mod:`netkeeper.crm.contacts`. A contact that is
not the current user's answers ``404``, never ``403``: a ``403`` would confirm
that the id exists for someone.

Merged-away contacts (spec 8.2) follow one convention, which P1-10's routes
adopt in a follow-up: ``GET`` on a merged-away id answers with its survivor and
``resolved_from`` set to the id asked for, so a stale link still lands on the
person; every write to a merged-away id answers ``409`` with
``{"detail": "merged", "merged_into_id": <survivor>}`` so the client can retry
against the survivor instead of editing a row nothing will show again.

A bulk action applies to a filter, so the person confirms a count, not a list of
rows. ``POST /contacts/bulk/count`` answers with the count and a signed token
bound to it (:mod:`netkeeper.crm.confirmation`); ``POST /contacts/bulk`` takes
that token, counts the selection again inside the writer transaction, and
answers ``409`` ``{"detail": "count mismatch", ...}`` when the two differ, so
the action never lands on rows the person did not see (spec 10.1).

``POST /contacts/query`` and ``POST /contacts/bulk/count`` are ``POST`` only
because a filter tree does not fit a query string. Both are
:func:`~netkeeper.web.deps.read_only`, so neither takes the SQLite write lock
while it scans the address book (#62).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy.orm import Session

from netkeeper.crm import contacts as service
from netkeeper.crm import duplicates as duplicate_service
from netkeeper.crm import interactions as timeline_service
from netkeeper.crm import new_contact
from netkeeper.crm.confirmation import InvalidToken, selection_digest
from netkeeper.crm.filters import FilterError, FilterTree
from netkeeper.crm.merge_preview import MergeMoves, Moved, preview_merge
from netkeeper.crm.provenance import overridden_fields
from netkeeper.models import Contact, ContactSource, User
from netkeeper.web.deps import Confirmations, CurrentUser, SessionDep, read_only
from netkeeper.web.errors import ApiError
from netkeeper.web.schemas import (
    CONTACT_COLUMNS,
    BulkConfirmable,
    BulkCountIn,
    BulkCountOut,
    BulkIn,
    BulkOut,
    BulkSelection,
    ConfirmationRejected,
    ContactCreate,
    ContactDetail,
    ContactEmailIn,
    ContactEmailOut,
    ContactEmailPatch,
    ContactLinkIn,
    ContactLinkOut,
    ContactLinkPatch,
    ContactPage,
    ContactPatch,
    ContactPhoneIn,
    ContactPhoneOut,
    ContactPhonePatch,
    ContactPositionOut,
    ContactQuery,
    ContactRow,
    ContactStatsOut,
    CountMismatch,
    DuplicateContact,
    MergedConflict,
    MergeIn,
    MergeMovesOut,
    MergePreviewOut,
    MovedOut,
    PossibleDuplicateOut,
    RevertFieldIn,
    SnapshotOut,
    SyncedValueOut,
    given_fields,
    timeline_entry_out,
)

router = APIRouter(tags=["contacts"])

Responses = dict[int | str, dict[str, Any]]
NO_SUCH_CONTACT: Responses = {404: {"description": "No such contact"}}
MERGED: Responses = {
    409: {"model": MergedConflict, "description": "The contact was merged into another"}
}
WRITE: Responses = {**NO_SUCH_CONTACT, **MERGED}
INVALID_FILTER: Responses = {
    422: {"description": "The filter, sort, or page is invalid, or uses a predicate not yet built"}
}
BULK: Responses = {
    409: {
        "model": CountMismatch,
        "description": "The selection no longer counts as shown, or the confirmation expired",
    },
    422: {
        "model": ConfirmationRejected,
        "description": "The filter is invalid, or the confirmation token does not hold up",
    },
}

TIMELINE_IN_DETAIL = 20
"""Timeline entries a contact detail carries; older ones come from the timeline endpoint."""

Limit = Annotated[int, Query(ge=1, le=200, description="Items per page.")]
Offset = Annotated[int, Query(ge=0, description="Items to skip.")]
Search = Annotated[
    str,
    Query(
        description="Case-insensitive substring of any name, company, title, headline, or email."
    ),
]


@contextmanager
def translate_errors() -> Iterator[None]:
    """Map the service's exceptions to HTTP statuses: 404, 409 (with a body), 422."""
    try:
        yield
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.Merged as exc:
        raise ApiError(409, {"detail": "merged", "merged_into_id": exc.survivor_id}) from exc
    except service.CountMismatch as exc:
        raise ApiError(
            409,
            {
                "detail": "count mismatch",
                "expected_count": exc.expected,
                "actual_count": exc.actual,
            },
        ) from exc
    except InvalidToken as exc:
        # An expired confirmation is a 409 like a moved count: the client asks for
        # the count again either way. Everything else never made sense.
        status = 409 if exc.reason == "expired" else 422
        raise ApiError(status, {"detail": str(exc), "reason": exc.reason}) from exc
    except service.Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FilterError as exc:
        raise HTTPException(
            status_code=422,
            detail=[
                {"loc": ["body", "filter", issue.path], "msg": issue.message, "type": "filter"}
                for issue in exc.issues
            ],
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --- lists ------------------------------------------------------------------


@router.post(
    "/contacts/query",
    operation_id="query_contacts",
    response_model_exclude_unset=True,
    responses=INVALID_FILTER,
)
@read_only
def query_contacts(body: ContactQuery, user: CurrentUser, session: SessionDep) -> ContactPage:
    """The Contacts table: filter, sort, page, and the columns to carry (spec 10.1, 10.4).

    A `POST` because the filter tree does not fit a query string; it reads only,
    and its session never takes the write lock. `total` counts every match;
    `describe` reads the filter for the table header.
    """
    tree = body.filter if body.filter is not None else FilterTree()
    with translate_errors():
        page = service.query(session, user, tree, body.sort, limit=body.limit, offset=body.offset)
    return _page(page, body.columns)


@router.get("/contacts", operation_id="search_contacts", response_model_exclude_unset=True)
def search_contacts(
    user: CurrentUser,
    session: SessionDep,
    q: Search = "",
    limit: Limit = 50,
    offset: Offset = 0,
) -> ContactPage:
    """Quick search, by last name then first name; blank ``q`` lists every live contact."""
    with translate_errors():
        page = service.search(session, user, q, limit=limit, offset=offset)
    return _page(page, None)


@router.get("/contacts/stats", operation_id="get_contact_stats")
def get_contact_stats(user: CurrentUser, session: SessionDep) -> ContactStatsOut:
    """Counts and triage progress over the user's contacts, for a dashboard (spec 10.1).

    ``netkeeper.crm.contacts.contact_stats()`` backs this, ``netkeeper contacts
    stats``, and the triage queue's progress bar alike, so the three numbers
    cannot quietly disagree (#90).

    Declared before ``/contacts/{contact_id}``, which would otherwise try to
    read ``stats`` as a contact id and answer ``422`` (see the same note on
    ``/imports/presets`` in ``web/api/imports.py``).
    """
    stats = service.contact_stats(session, user)
    return ContactStatsOut.model_validate(stats)


@router.post(
    "/contacts",
    operation_id="create_contact",
    status_code=201,
    responses={
        409: {"model": DuplicateContact, "description": "The person is already a contact"},
        422: {"description": "A value does not hold up, or a tag or list is not yours"},
    },
)
def create_contact(body: ContactCreate, user: CurrentUser, session: SessionDep) -> ContactDetail:
    """Add one contact by hand (#303), with the dedup and checks an import runs.

    The contact's source is `manual`. A match by LinkedIn URL or email answers
    `409` naming the contact already there, never a second one; so does a match
    by first name, last name, and company unless `allow_name_match` is set. The
    new contact takes `tag_ids` and joins the static list `list_id`, and the
    auto-tag rules run over it. A `422` names the field in `loc`, as a schema
    refusal does.
    """
    new = new_contact.NewContact(
        first_name=body.first_name,
        last_name=body.last_name,
        email=body.email,
        current_company=body.current_company,
        current_title=body.current_title,
        li_url=body.li_url,
        tag_ids=tuple(body.tag_ids),
        list_id=body.list_id,
    )
    try:
        contact = new_contact.create_contact(
            session, user, new, allow_name_match=body.allow_name_match
        )
    except new_contact.Duplicate as exc:
        raise ApiError(
            409,
            {
                "detail": "duplicate",
                "contact_id": exc.contact_id,
                "contact_ids": list(exc.contact_ids),
                "matched_by": exc.matched_by,
                "archived": exc.archived,
            },
        ) from exc
    except new_contact.Invalid as exc:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["body", exc.field], "msg": exc.message, "type": "value_error"}],
        ) from exc
    return _detail(session, user, contact, None)


# --- one contact ------------------------------------------------------------


@router.get("/contacts/{contact_id}", operation_id="get_contact", responses=NO_SUCH_CONTACT)
def get_contact(contact_id: int, user: CurrentUser, session: SessionDep) -> ContactDetail:
    """The contact in full. A merged-away id answers with its survivor and `resolved_from`."""
    with translate_errors():
        contact, resolved_from = service.get_contact(session, user, contact_id)
    return _detail(session, user, contact, resolved_from)


@router.patch("/contacts/{contact_id}", operation_id="update_contact", responses=WRITE)
def update_contact(
    contact_id: int, body: ContactPatch, user: CurrentUser, session: SessionDep
) -> ContactDetail:
    """Edit fields by hand. A LinkedIn field edited here sticks until reverted (spec 10.5)."""
    changes = given_fields(body)
    # met and do_not_contact cannot be cleared; null means "leave it".
    for name in ("met", "do_not_contact"):
        if changes.get(name, ...) is None:
            del changes[name]
    with translate_errors():
        contact = service.update_contact(session, user, contact_id, changes)
    return _detail(session, user, contact, None)


@router.post(
    "/contacts/{contact_id}/revert-field",
    operation_id="revert_contact_field",
    responses={
        **WRITE,
        409: {"description": "Nothing synced to revert to, or the contact was merged"},
    },
)
def revert_contact_field(
    contact_id: int, body: RevertFieldIn, user: CurrentUser, session: SessionDep
) -> ContactDetail:
    """Put a LinkedIn field back to its last synced value and provenance (CP1, #28)."""
    with translate_errors():
        contact = service.revert_field(session, user, contact_id, body.field)
    return _detail(session, user, contact, None)


@router.post("/contacts/{contact_id}/archive", operation_id="archive_contact", responses=WRITE)
def archive_contact(contact_id: int, user: CurrentUser, session: SessionDep) -> ContactDetail:
    """Archive. Contacts are never deleted (spec 8); an archived one leaves the table."""
    with translate_errors():
        contact = service.archive_contact(session, user, contact_id)
    return _detail(session, user, contact, None)


@router.post("/contacts/{contact_id}/unarchive", operation_id="unarchive_contact", responses=WRITE)
def unarchive_contact(contact_id: int, user: CurrentUser, session: SessionDep) -> ContactDetail:
    with translate_errors():
        contact = service.unarchive_contact(session, user, contact_id)
    return _detail(session, user, contact, None)


@router.post("/contacts/{contact_id}/confirm", operation_id="confirm_contact", responses=WRITE)
def confirm_contact(contact_id: int, user: CurrentUser, session: SessionDep) -> ContactDetail:
    """Confirm a contact netkeeper read off a connections-page card (#184).

    Clears `needs_review_at`. A contact not waiting for review is returned as it
    is. Until confirmed, or until a sync attaches a URN, such a contact is never
    enriched, enrolled, or aged.
    """
    with translate_errors():
        contact = service.confirm_contact(session, user, contact_id)
    return _detail(session, user, contact, None)


@router.post("/contacts/{contact_id}/reject", operation_id="reject_contact", responses=WRITE)
def reject_contact(contact_id: int, user: CurrentUser, session: SessionDep) -> ContactDetail:
    """Reject a contact netkeeper read off a connections-page card: archive it (#184).

    Never a delete. `needs_review_at` stays set, so unarchiving brings back an
    unconfirmed contact. `409` for a contact that is not waiting for review.
    """
    with translate_errors():
        contact = service.reject_contact(session, user, contact_id)
    return _detail(session, user, contact, None)


@router.post(
    "/contacts/{contact_id}/merge",
    operation_id="merge_contacts",
    responses={**WRITE, 409: {"description": "The two cannot be merged"}},
)
def merge_contacts(
    contact_id: int, body: MergeIn, user: CurrentUser, session: SessionDep
) -> ContactDetail:
    """Fold `loser_id` into this contact (spec 8.2); the loser resolves here from then on.

    Merging into a merged-away id answers `409 merged` like every other write to
    one, so the client retries against the survivor it names.
    """
    with translate_errors():
        contact = service.merge_contacts(session, user, contact_id, body.loser_id)
    return _detail(session, user, contact, None)


@router.post(
    "/contacts/{contact_id}/merge/preview",
    operation_id="preview_merge_contacts",
    responses={**WRITE, 409: {"description": "The two cannot be merged"}},
)
def preview_merge_contacts(
    contact_id: int, body: MergeIn, user: CurrentUser, session: SessionDep
) -> MergePreviewOut:
    """What merging `loser_id` into this contact would do, without doing it (#363).

    Runs the merge itself in a savepoint and rolls it back, so `result` is what
    `POST /contacts/{id}/merge` would answer with now, under every merge rule.
    Answers what the merge answers when the two cannot be merged. A `POST`
    because the savepoint writes before it is undone, which needs a writer
    session; the database keeps nothing.
    """
    with translate_errors():
        preview = preview_merge(
            session,
            user,
            contact_id,
            body.loser_id,
            render=lambda contact: _detail(session, user, contact, None),
        )
    return MergePreviewOut(
        survivor=preview.survivor,
        loser=preview.loser,
        result=preview.result,
        moves=_moves_out(preview.moves),
    )


@router.get(
    "/contacts/{contact_id}/duplicates",
    operation_id="list_possible_duplicates",
    responses=NO_SUCH_CONTACT,
)
def list_possible_duplicates(
    contact_id: int, user: CurrentUser, session: SessionDep
) -> list[PossibleDuplicateOut]:
    """Other live contacts that may be this person, strongest match first (#363).

    A hint for a person to act on, never a merge: by email, phone, the same
    name, or the same name under a LinkedIn slug changed before any sync. Two
    contacts with different LinkedIn URNs never match. Only this user's
    contacts, at most five. A merged-away id stands for its survivor.
    """
    with translate_errors():
        _, found = duplicate_service.possible_duplicates(session, user, contact_id)
    return [
        PossibleDuplicateOut(
            contact_id=match.contact.id,
            first_name=match.contact.first_name,
            last_name=match.contact.last_name,
            preferred_name=match.contact.preferred_name,
            current_title=match.contact.current_title,
            current_company=match.contact.current_company,
            li_public_id=match.contact.li_public_id,
            needs_review=match.contact.needs_review_at is not None,
            matched_by=list(match.matched_by),
        )
        for match in found
    ]


def _moved_out(moved: Moved) -> MovedOut:
    return MovedOut(moved=moved.moved, dropped=moved.dropped)


def _moves_out(moves: MergeMoves) -> MergeMovesOut:
    return MergeMovesOut(
        emails=_moved_out(moves.emails),
        phones=_moved_out(moves.phones),
        links=_moved_out(moves.links),
        positions=_moved_out(moves.positions),
        snapshots=moves.snapshots,
        interactions=moves.interactions,
        tags_added=list(moves.tags_added),
        tags_removed=list(moves.tags_removed),
        lists_added=list(moves.lists_added),
        enrollments_moved=moves.enrollments_moved,
        enrollments_combined=moves.enrollments_combined,
        messages_moved=moves.messages_moved,
        messages_discarded=moves.messages_discarded,
        history_rows=moves.history_rows,
    )


# --- bulk -------------------------------------------------------------------


@router.post(
    "/contacts/bulk/count",
    operation_id="count_bulk_contacts",
    responses=INVALID_FILTER,
)
@read_only
def count_bulk_contacts(
    body: BulkCountIn,
    user: CurrentUser,
    session: SessionDep,
    confirmations: Confirmations,
) -> BulkCountOut:
    """How many contacts the action would touch, and the token that confirms that count.

    Put `count` in the confirmation dialog and send `token` back with the
    action. The token is bound to this user, this action, this selection, and
    this count, and expires in five minutes; a selection that has moved by then
    is refused rather than applied to rows nobody saw. Writes nothing.
    """
    selection = _selection(body.selection)
    with translate_errors():
        count = service.count_selection(session, user, selection)
    token, expires_at = confirmations.issue(
        user_id=user.id,
        action=body.action,
        digest=_digest(body),
        count=count,
    )
    return BulkCountOut(
        count=count,
        describe=service.describe_selection(selection),
        token=token,
        expires_at=expires_at,
    )


@router.post("/contacts/bulk", operation_id="bulk_update_contacts", responses=BULK)
def bulk_update_contacts(
    body: BulkIn,
    user: CurrentUser,
    session: SessionDep,
    confirmations: Confirmations,
) -> BulkOut:
    """One action on every contact a filter or an id list selects, count confirmed (spec 10.1).

    `token` comes from `POST /contacts/bulk/count`. The selection is counted
    again here, inside the writer transaction, and the action is refused when
    the count has moved.
    """
    selection = _selection(body.selection)
    with translate_errors():
        confirmation = confirmations.verify(
            body.token, user_id=user.id, action=body.action, digest=_digest(body)
        )
        affected = service.bulk_update(
            session,
            user,
            selection,
            body.action,
            value=body.value,
            reason=body.reason,
            expected_count=confirmation.count,
        )
    return BulkOut(affected=affected)


def _selection(selection: BulkSelection) -> service.Selection:
    return service.Selection(
        tree=selection.filter,
        ids=tuple(selection.ids) if selection.ids is not None else None,
    )


def _digest(body: BulkConfirmable) -> str:
    """What a confirmation token is bound to, spelled one way only.

    The selection and what would be written to it: the same count under
    ``value: true`` and under ``value: false`` is two different confirmations,
    so a token for one must not execute the other. The action is left out here
    because it is signed as a field of its own, which is what lets a refusal
    name it.

    Ids are deduplicated and sorted, so the same set of contacts digests the
    same however the client ordered them; the filter tree dumps with every
    field, so a tree that leaves ``include_archived`` out digests like one that
    spells the default out.
    """
    payload: dict[str, Any] = body.model_dump(mode="json", exclude={"action", "token"})
    ids = payload["selection"].get("ids")
    if ids:
        payload["selection"]["ids"] = sorted(set(ids))
    return selection_digest(payload)


# --- emails, phones, links --------------------------------------------------

CHILD_CONFLICT: Responses = {**WRITE, 409: {"description": "The contact already has it"}}
NO_SUCH_CHILD: Responses = {**WRITE, 404: {"description": "No such contact, or no such row on it"}}


@router.post(
    "/contacts/{contact_id}/emails",
    operation_id="add_contact_email",
    status_code=201,
    responses=CHILD_CONFLICT,
)
def add_contact_email(
    contact_id: int, body: ContactEmailIn, user: CurrentUser, session: SessionDep
) -> ContactEmailOut:
    """Add an address. The first one is primary; `is_primary` on a later one demotes the rest."""
    with translate_errors():
        row = service.add_email(
            session,
            user,
            contact_id,
            body.email,
            kind=body.kind,
            is_primary=body.is_primary,
            status=body.status,
        )
    return ContactEmailOut.model_validate(row)


@router.patch(
    "/contacts/{contact_id}/emails/{email_id}",
    operation_id="update_contact_email",
    responses={**NO_SUCH_CHILD, **CHILD_CONFLICT},
)
def update_contact_email(
    contact_id: int,
    email_id: int,
    body: ContactEmailPatch,
    user: CurrentUser,
    session: SessionDep,
) -> ContactEmailOut:
    with translate_errors():
        row = service.update_email(session, user, contact_id, email_id, given_fields(body))
    return ContactEmailOut.model_validate(row)


@router.delete(
    "/contacts/{contact_id}/emails/{email_id}",
    operation_id="delete_contact_email",
    status_code=204,
    responses=NO_SUCH_CHILD,
)
def delete_contact_email(
    contact_id: int, email_id: int, user: CurrentUser, session: SessionDep
) -> Response:
    """Remove an address; when it was primary, the first remaining one takes over."""
    with translate_errors():
        service.delete_email(session, user, contact_id, email_id)
    return Response(status_code=204)


@router.post(
    "/contacts/{contact_id}/phones",
    operation_id="add_contact_phone",
    status_code=201,
    responses=CHILD_CONFLICT,
)
def add_contact_phone(
    contact_id: int, body: ContactPhoneIn, user: CurrentUser, session: SessionDep
) -> ContactPhoneOut:
    """Add a number. The first one is primary; `is_primary` on a later one demotes the rest."""
    with translate_errors():
        row = service.add_phone(
            session,
            user,
            contact_id,
            body.raw,
            number_e164=body.number_e164,
            kind=body.kind,
            is_primary=body.is_primary,
        )
    return ContactPhoneOut.model_validate(row)


@router.patch(
    "/contacts/{contact_id}/phones/{phone_id}",
    operation_id="update_contact_phone",
    responses={**NO_SUCH_CHILD, **CHILD_CONFLICT},
)
def update_contact_phone(
    contact_id: int,
    phone_id: int,
    body: ContactPhonePatch,
    user: CurrentUser,
    session: SessionDep,
) -> ContactPhoneOut:
    with translate_errors():
        row = service.update_phone(session, user, contact_id, phone_id, given_fields(body))
    return ContactPhoneOut.model_validate(row)


@router.delete(
    "/contacts/{contact_id}/phones/{phone_id}",
    operation_id="delete_contact_phone",
    status_code=204,
    responses=NO_SUCH_CHILD,
)
def delete_contact_phone(
    contact_id: int, phone_id: int, user: CurrentUser, session: SessionDep
) -> Response:
    with translate_errors():
        service.delete_phone(session, user, contact_id, phone_id)
    return Response(status_code=204)


@router.post(
    "/contacts/{contact_id}/links",
    operation_id="add_contact_link",
    status_code=201,
    responses=CHILD_CONFLICT,
)
def add_contact_link(
    contact_id: int, body: ContactLinkIn, user: CurrentUser, session: SessionDep
) -> ContactLinkOut:
    with translate_errors():
        row = service.add_link(session, user, contact_id, body.url, kind=body.kind)
    return ContactLinkOut.model_validate(row)


@router.patch(
    "/contacts/{contact_id}/links/{link_id}",
    operation_id="update_contact_link",
    responses={**NO_SUCH_CHILD, **CHILD_CONFLICT},
)
def update_contact_link(
    contact_id: int,
    link_id: int,
    body: ContactLinkPatch,
    user: CurrentUser,
    session: SessionDep,
) -> ContactLinkOut:
    with translate_errors():
        row = service.update_link(session, user, contact_id, link_id, given_fields(body))
    return ContactLinkOut.model_validate(row)


@router.delete(
    "/contacts/{contact_id}/links/{link_id}",
    operation_id="delete_contact_link",
    status_code=204,
    responses=NO_SUCH_CHILD,
)
def delete_contact_link(
    contact_id: int, link_id: int, user: CurrentUser, session: SessionDep
) -> Response:
    with translate_errors():
        service.delete_link(session, user, contact_id, link_id)
    return Response(status_code=204)


# --- shapes -----------------------------------------------------------------


def _page(page: service.Page, columns: Sequence[str] | None) -> ContactPage:
    wanted = CONTACT_COLUMNS if columns is None else tuple(dict.fromkeys(columns))
    return ContactPage(
        items=[_row(contact, wanted) for contact in page.contacts],
        total=page.total,
        describe=page.describe,
    )


def _row(contact: Contact, columns: Sequence[str]) -> ContactRow:
    # Only the columns asked for are set; response_model_exclude_unset drops the rest.
    scalars = {name: getattr(contact, name) for name in columns}
    return ContactRow(
        id=contact.id,
        primary_email=contact.emails[0].email if contact.emails else None,
        primary_phone=(contact.phones[0].number_e164 or contact.phones[0].raw)
        if contact.phones
        else None,
        **scalars,
    )


def _detail(
    session: Session, user: User, contact: Contact, resolved_from: int | None
) -> ContactDetail:
    scalars = {name: getattr(contact, name) for name in CONTACT_COLUMNS}
    return ContactDetail(
        id=contact.id,
        merged_into_id=contact.merged_into_id,
        emails=[ContactEmailOut.model_validate(row) for row in contact.emails],
        phones=[ContactPhoneOut.model_validate(row) for row in contact.phones],
        links=[ContactLinkOut.model_validate(row) for row in contact.links],
        positions=[ContactPositionOut.model_validate(row) for row in contact.positions],
        snapshots=[
            SnapshotOut.model_validate(row)
            for row in service.latest_snapshots(session, user, contact)
        ],
        timeline=[
            timeline_entry_out(entry)
            for entry in timeline_service.timeline(
                session, user, contact.id, limit=TIMELINE_IN_DETAIL
            )
        ],
        field_sources={
            name: ContactSource(source) for name, source in (contact.field_sources or {}).items()
        },
        synced_values={
            name: SyncedValueOut.model_validate(entry)
            for name, entry in (contact.synced_values or {}).items()
        },
        overridden_fields=overridden_fields(contact),
        resolved_from=resolved_from,
        **scalars,
    )
