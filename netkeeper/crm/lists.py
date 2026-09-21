"""Static lists, smart lists, and saved table views (spec 10.1 and 10.4; item P1-08).

What this module decides
------------------------
- A static list's members are the :class:`~netkeeper.models.ListMember` rows
  that name it; :func:`add_members` and :func:`remove_member` are the only way
  they change. A smart list's members are whatever
  :func:`netkeeper.crm.filters.compile_filter` returns for its stored
  :class:`~netkeeper.crm.filters.FilterTree` right now: nothing is
  materialized, so :func:`list_members` on a smart list always agrees with a
  direct run of its filter, and neither this module nor the database ever
  writes a ``list_members`` row for one.
- A filter is validated on save, not on read: :func:`create_list` and
  :func:`update_list` parse it (already done by the Pydantic layer at the API
  boundary) and then compile it for this user with
  :func:`netkeeper.crm.filters.compile_where`, which is never executed but
  raises :class:`~netkeeper.crm.filters.FilterError` for a malformed tree or
  :class:`~netkeeper.crm.filters.UnsupportedPredicate` for one that uses a
  predicate that does not compile yet. So a smart list can never be saved
  broken, and a broken filter is never discovered only when someone opens the
  list.
- A saved view (:func:`create_view`, :func:`update_view`) is a name, a column
  set, a sort, and an optional filter for the contacts table (spec 10.1). It
  never has members; it is restored by the frontend, not evaluated here.
- "Validated" ships as a built-in smart list (:func:`ensure_validated_list`),
  seeded once per user the way :mod:`netkeeper.crm.tags` seeds its default
  rules: a ``settings_kv`` key (:data:`VALIDATED_SEEDED_KEY`) records the
  seeding, so a user who deletes or renames it never gets it back. Its filter
  is ``met = "met"`` (:data:`VALIDATED_FILTER`). **This is an inference, not
  a filter the spec spells out**: ``docs/architecture.md`` section 3's mapping
  table, workflow step 1.3 ("keep the validated list"), says only "`met =
  true` is a field; nothing is deleted. A built-in smart list 'Validated'" —
  it names the field and the built-in but never states the predicate as a
  filter-tree expression. ``met = "met"`` (:class:`~netkeeper.models.ContactMet`,
  netkeeper's triage state) is the one concrete reading of that line, but a
  reviewer should check it against the spec rather than trust this module's
  say-so.

Every function that writes needs a writer session (CLAUDE.md): each reads
first, and on SQLite an unmarked read-then-write can fail with "database is
locked".
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import func
from sqlalchemy.orm import Session

from netkeeper.crm.filters import (
    FilterTree,
    SortKey,
    compile_count,
    compile_filter,
    compile_where,
    parse_filter,
)
from netkeeper.db import is_writer
from netkeeper.models import (
    LIST_NAME_MAX_LENGTH,
    VIEW_NAME_MAX_LENGTH,
    Contact,
    ContactList,
    ContactMet,
    ListKind,
    ListMember,
    SavedView,
    User,
)
from netkeeper.scoping import get_scoped, scoped, scoped_count, scoped_delete
from netkeeper.services.settings_kv import get_setting, set_setting

log = logging.getLogger(__name__)

VALIDATED_SEEDED_KEY: Final = "lists.validated_seeded"
"""The ``settings_kv`` key that records that the built-in "Validated" list was seeded."""

VALIDATED_LIST_NAME: Final = "Validated"

VALIDATED_FILTER: Final[dict[str, Any]] = {
    "where": {"op": "eq", "field": "met", "value": ContactMet.MET.value}
}
"""``met = "met"``. An inference from architecture.md section 3, workflow step 1.3, "keep
the validated list" — the spec names the field and the built-in but never spells out the
predicate as a filter tree; see the module docstring."""

MAX_COLUMNS: Final = 50
COLUMN_NAME_MAX_LENGTH: Final = 100

DEFAULT_PAGE_LIMIT: Final = 50


# --- errors -----------------------------------------------------------------


class ListError(Exception):
    """Base of everything this module raises on purpose."""


class ListNotFound(ListError, LookupError):
    """No such list for this user."""


class ViewNotFound(ListError, LookupError):
    """No such saved view for this user."""


class ContactNotFound(ListError, LookupError):
    """No such contact for this user."""


class DuplicateListName(ListError, ValueError):
    """The user already has a list by that name."""


class DuplicateViewName(ListError, ValueError):
    """The user already has a saved view by that name."""


class InvalidListValue(ListError, ValueError):
    """A list name or a filter/kind combination that cannot be stored."""


class InvalidViewValue(ListError, ValueError):
    """A view name, column list, or sort that cannot be stored."""


class WrongListKind(ListError, ValueError):
    """An operation that only makes sense for the other kind of list."""


# --- results ------------------------------------------------------------


class Unset(enum.Enum):
    """The type of :data:`UNSET`."""

    TOKEN = 0


UNSET: Final = Unset.TOKEN
"""For :func:`update_list` and :func:`update_view`: "leave this field alone"."""


# --- validation ---------------------------------------------------------


def _require_writer(session: Session) -> None:
    """Fail at once rather than with "database is locked" on the first write (CLAUDE.md)."""
    if not is_writer(session):
        raise RuntimeError("list operations need a writer session; use session_scope(write=True)")


def _check_page(limit: int, offset: int) -> None:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if offset < 0:
        raise ValueError("offset must not be negative")


def _clean_name(name: str, *, max_length: int, what: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise InvalidListValue(f"{what} name is empty")
    if len(cleaned) > max_length:
        raise InvalidListValue(f"{what} name is longer than {max_length} characters")
    return cleaned


def _clean_columns(columns: Sequence[str]) -> list[str]:
    if not columns:
        raise InvalidViewValue("a view needs at least one column")
    if len(columns) > MAX_COLUMNS:
        raise InvalidViewValue(f"a view may have at most {MAX_COLUMNS} columns")
    cleaned: list[str] = []
    for raw in columns:
        name = raw.strip()
        if not name:
            raise InvalidViewValue("a column name is empty")
        if len(name) > COLUMN_NAME_MAX_LENGTH:
            raise InvalidViewValue(
                f"column name is longer than {COLUMN_NAME_MAX_LENGTH} characters"
            )
        cleaned.append(name)
    return cleaned


def _check_filter(user: User, tree: FilterTree) -> None:
    """Compile ``tree`` for ``user``; never executed, but raises for a broken or not-yet-
    supported filter so a smart list or a saved view is never stored broken."""
    compile_where(user, tree)


def _owned_contact(session: Session, user: User, contact_id: int) -> Contact:
    contact = get_scoped(session, user, Contact, contact_id)
    if contact is None:
        raise ContactNotFound(f"no contact {contact_id}")
    return contact


# --- lists ----------------------------------------------------------------


def list_lists(session: Session, user: User) -> list[ContactList]:
    """Every list of ``user``, by name."""
    statement = scoped(user, ContactList).order_by(ContactList.name, ContactList.id)
    return list(session.scalars(statement))


def get_list(session: Session, user: User, list_id: int) -> ContactList:
    """The list, or :class:`ListNotFound`."""
    row = get_scoped(session, user, ContactList, list_id)
    if row is None:
        raise ListNotFound(f"no list {list_id}")
    return row


def find_list(session: Session, user: User, name: str) -> ContactList | None:
    """The user's list named exactly ``name``, or None."""
    return session.scalars(scoped(user, ContactList).where(ContactList.name == name)).one_or_none()


def create_list(
    session: Session, user: User, name: str, kind: ListKind, *, filter: FilterTree | None = None
) -> ContactList:
    """A new list, flushed. :class:`DuplicateListName` when the name is taken.

    A static list (``kind=ListKind.STATIC``) takes no filter; a smart list needs
    one, validated with :func:`_check_filter`. :class:`InvalidListValue` when
    the kind and the presence of ``filter`` disagree.
    """
    _require_writer(session)
    cleaned = _clean_name(name, max_length=LIST_NAME_MAX_LENGTH, what="list")
    if find_list(session, user, cleaned) is not None:
        raise DuplicateListName(f"a list named {cleaned!r} already exists")
    stored = _prepare_filter(user, kind, filter)
    row = ContactList(user_id=user.id, name=cleaned, kind=kind, filter_json=stored)
    session.add(row)
    session.flush()
    log.debug("created list %d %r (%s) for user %d", row.id, row.name, kind.value, user.id)
    return row


def _prepare_filter(user: User, kind: ListKind, filter: FilterTree | None) -> dict[str, Any] | None:
    if kind is ListKind.STATIC:
        if filter is not None:
            raise InvalidListValue("a static list has no filter; add members instead")
        return None
    if filter is None:
        raise InvalidListValue("a smart list needs a filter")
    _check_filter(user, filter)
    return filter.model_dump(mode="json")


def update_list(
    session: Session,
    user: User,
    list_id: int,
    *,
    name: str | None = None,
    filter: FilterTree | Unset = UNSET,
) -> ContactList:
    """Rename a list and/or, for a smart list, replace its filter.

    ``filter`` left at :data:`UNSET` leaves it untouched. :class:`WrongListKind`
    for a filter on a static list; :class:`InvalidListValue` for a bad filter
    or name; :class:`DuplicateListName` for a name collision.
    """
    _require_writer(session)
    row = get_list(session, user, list_id)
    if name is not None:
        cleaned = _clean_name(name, max_length=LIST_NAME_MAX_LENGTH, what="list")
        other = find_list(session, user, cleaned)
        if other is not None and other.id != row.id:
            raise DuplicateListName(f"a list named {cleaned!r} already exists")
        row.name = cleaned
    if not isinstance(filter, Unset):
        if row.kind is not ListKind.SMART:
            raise WrongListKind("only a smart list's filter can be changed")
        if filter is None:
            raise InvalidListValue("a smart list needs a filter")
        _check_filter(user, filter)
        row.filter_json = filter.model_dump(mode="json")
    session.flush()
    return row


def delete_list(session: Session, user: User, list_id: int) -> None:
    """Delete a list; the database cascades to its members (if static)."""
    _require_writer(session)
    row = get_list(session, user, list_id)
    session.execute(scoped_delete(user, ContactList).where(ContactList.id == row.id))
    session.expunge(row)
    log.debug("deleted list %d for user %d", list_id, user.id)


# --- membership -------------------------------------------------------------


def add_members(session: Session, user: User, list_id: int, contact_ids: Sequence[int]) -> int:
    """Add contacts to a static list; returns how many were newly added.

    An id already a member, or repeated in ``contact_ids``, is not recounted.
    :class:`WrongListKind` for a smart list; :class:`ContactNotFound` for an id
    that is not one of ``user``'s contacts.
    """
    _require_writer(session)
    row = get_list(session, user, list_id)
    if row.kind is not ListKind.STATIC:
        raise WrongListKind("only a static list has members to add")
    unique_ids = list(dict.fromkeys(contact_ids))
    if not unique_ids:
        return 0
    for contact_id in unique_ids:
        _owned_contact(session, user, contact_id)
    existing = set(
        session.scalars(
            scoped(user, ListMember)
            .with_only_columns(ListMember.contact_id)
            .where(ListMember.list_id == row.id, ListMember.contact_id.in_(unique_ids))
        )
    )
    added = 0
    for contact_id in unique_ids:
        if contact_id in existing:
            continue
        session.add(ListMember(user_id=user.id, list_id=row.id, contact_id=contact_id))
        added += 1
    session.flush()
    log.debug("added %d contacts to list %d for user %d", added, row.id, user.id)
    return added


def remove_member(session: Session, user: User, list_id: int, contact_id: int) -> bool:
    """Remove a contact from a static list; True when it was a member.

    :class:`WrongListKind` for a smart list.
    """
    _require_writer(session)
    row = get_list(session, user, list_id)
    if row.kind is not ListKind.STATIC:
        raise WrongListKind("only a static list has members to remove")
    member = session.scalars(
        scoped(user, ListMember).where(
            ListMember.list_id == row.id, ListMember.contact_id == contact_id
        )
    ).one_or_none()
    if member is None:
        return False
    session.delete(member)
    session.flush()
    return True


def list_members(
    session: Session, user: User, list_id: int, *, limit: int, offset: int = 0
) -> tuple[list[Contact], int]:
    """One page of the list's members, newest to oldest for a static list, and the total count.

    A smart list's members are exactly what :func:`netkeeper.crm.filters.compile_filter`
    returns for its stored tree, run fresh: nothing here materializes them, so this
    always agrees with a direct run of the filter (see the "done when" for P1-08).
    :class:`ListNotFound`; ``ValueError`` for a bad page.
    """
    _check_page(limit, offset)
    row = get_list(session, user, list_id)
    if row.kind is ListKind.STATIC:
        base = (
            scoped(user, Contact)
            .join(ListMember, ListMember.contact_id == Contact.id)
            .where(ListMember.list_id == row.id, ListMember.user_id == user.id)
        )
        total = session.scalar(base.with_only_columns(func.count())) or 0
        contacts = session.scalars(
            base.order_by(ListMember.added_at.desc(), Contact.id.desc()).limit(limit).offset(offset)
        ).all()
        return list(contacts), total
    tree = parse_filter(row.filter_json)
    total = session.scalar(compile_count(user, tree)) or 0
    contacts = session.scalars(
        compile_filter(user, tree).order_by(Contact.id).limit(limit).offset(offset)
    ).all()
    return list(contacts), total


def member_count(session: Session, user: User, list_id: int) -> int:
    """How many contacts are in the list right now."""
    row = get_list(session, user, list_id)
    if row.kind is ListKind.STATIC:
        return (
            session.scalar(scoped_count(user, ListMember).where(ListMember.list_id == row.id)) or 0
        )
    tree = parse_filter(row.filter_json)
    return session.scalar(compile_count(user, tree)) or 0


def member_counts(session: Session, user: User, list_ids: Sequence[int]) -> dict[int, int]:
    """:func:`member_count` for several lists at once, by id (0 for an empty one).

    For ids already known to be ``user``'s own, typically from :func:`list_lists`:
    like :func:`member_count`, :class:`ListNotFound` for one that is not.
    """
    return {list_id: member_count(session, user, list_id) for list_id in list_ids}


# --- saved views --------------------------------------------------------


def list_views(session: Session, user: User) -> list[SavedView]:
    """Every saved view of ``user``, by name."""
    return list(session.scalars(scoped(user, SavedView).order_by(SavedView.name, SavedView.id)))


def get_view(session: Session, user: User, view_id: int) -> SavedView:
    """The view, or :class:`ViewNotFound`."""
    row = get_scoped(session, user, SavedView, view_id)
    if row is None:
        raise ViewNotFound(f"no view {view_id}")
    return row


def find_view(session: Session, user: User, name: str) -> SavedView | None:
    """The user's view named exactly ``name``, or None."""
    return session.scalars(scoped(user, SavedView).where(SavedView.name == name)).one_or_none()


def create_view(
    session: Session,
    user: User,
    name: str,
    columns: Sequence[str],
    *,
    sort: Sequence[SortKey] = (),
    filter: FilterTree | None = None,
) -> SavedView:
    """A new saved view, flushed. :class:`DuplicateViewName` when the name is taken."""
    _require_writer(session)
    cleaned = _clean_name(name, max_length=VIEW_NAME_MAX_LENGTH, what="view")
    if find_view(session, user, cleaned) is not None:
        raise DuplicateViewName(f"a view named {cleaned!r} already exists")
    cleaned_columns = _clean_columns(columns)
    if filter is not None:
        _check_filter(user, filter)
    row = SavedView(
        user_id=user.id,
        name=cleaned,
        columns=cleaned_columns,
        sort=[key.model_dump(mode="json") for key in sort],
        filter_json=None if filter is None else filter.model_dump(mode="json"),
    )
    session.add(row)
    session.flush()
    log.debug("created view %d %r for user %d", row.id, row.name, user.id)
    return row


def update_view(
    session: Session,
    user: User,
    view_id: int,
    *,
    name: str | None = None,
    columns: Sequence[str] | None = None,
    sort: Sequence[SortKey] | None = None,
    filter: FilterTree | Unset | None = UNSET,
) -> SavedView:
    """Change any of a view's name, columns, sort, and filter.

    ``filter=None`` clears it (a view with no filter shows every contact);
    omitted (:data:`UNSET`) leaves it as it is, matching the field's existing
    behavior in the database (unlike a list's filter, a view's is optional).
    """
    _require_writer(session)
    row = get_view(session, user, view_id)
    if name is not None:
        cleaned = _clean_name(name, max_length=VIEW_NAME_MAX_LENGTH, what="view")
        other = find_view(session, user, cleaned)
        if other is not None and other.id != row.id:
            raise DuplicateViewName(f"a view named {cleaned!r} already exists")
        row.name = cleaned
    if columns is not None:
        row.columns = _clean_columns(columns)
    if sort is not None:
        row.sort = [key.model_dump(mode="json") for key in sort]
    if not isinstance(filter, Unset):
        if filter is not None:
            _check_filter(user, filter)
        row.filter_json = None if filter is None else filter.model_dump(mode="json")
    session.flush()
    return row


def delete_view(session: Session, user: User, view_id: int) -> None:
    """Delete a saved view."""
    _require_writer(session)
    row = get_view(session, user, view_id)
    session.execute(scoped_delete(user, SavedView).where(SavedView.id == row.id))
    session.expunge(row)


# --- defaults -----------------------------------------------------------


def ensure_validated_list(session: Session, user: User) -> ContactList | None:
    """Seed the built-in "Validated" smart list for ``user`` once; the row created, or None.

    Idempotent: after the first call ``settings_kv`` records
    :data:`VALIDATED_SEEDED_KEY` and later calls return None, so a list the
    user deleted or renamed is never re-created. A list the user already has
    named exactly :data:`VALIDATED_LIST_NAME` is reused as it is, the way
    :func:`netkeeper.crm.tags.ensure_default_rules` reuses an existing tag.

    The filter is :data:`VALIDATED_FILTER` (``met = "met"``), inferred from
    architecture.md section 3's workflow step 1.3, not a filter tree the spec
    states outright — check that inference against the spec, not this
    function, before trusting it (see the module docstring).
    """
    _require_writer(session)
    if get_setting(session, user, VALIDATED_SEEDED_KEY) is True:
        return None
    row = find_list(session, user, VALIDATED_LIST_NAME)
    if row is None:
        row = create_list(
            session,
            user,
            VALIDATED_LIST_NAME,
            ListKind.SMART,
            filter=parse_filter(VALIDATED_FILTER),
        )
        log.info("seeded built-in list %r for user %d", VALIDATED_LIST_NAME, user.id)
    set_setting(session, user, VALIDATED_SEEDED_KEY, True)
    return row
