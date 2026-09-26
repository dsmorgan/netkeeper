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
  raises :class:`~netkeeper.crm.filters.FilterError` for a malformed tree,
  :class:`~netkeeper.crm.filters.UnsupportedPredicate` for one that uses a
  predicate that does not compile yet, and
  :class:`~netkeeper.crm.filters.ListReferenceError` for a ``list_member``
  that would make two lists define each other. So a smart list can never be
  saved broken, and a broken filter is never discovered only when someone
  opens the list.
- A filter may name another list (``list_member``), which is why compiling one
  now takes the session: a smart list it names is inlined from the ``lists``
  table. That makes a write on one list able to break *another*, so three
  guards stand between a ``POST`` and a list nobody can read:

  1. :func:`check_list_references` refuses a ``list_member`` naming a list
     this user does not have. On a read the same reference matches nobody, on
     purpose — deleting a list must not take down the filters that name it —
     but at write time the leniency was a hole: the id a new list is *about*
     to be given does not exist yet, so a filter could name it and then be
     handed that row, which closed a cycle in two ordinary requests.
  2. :func:`update_list` passes the list being written as ``list_id``, so a
     tree that reaches back to its own list is refused at the write that
     would close the cycle, rather than at everyone's next read.
  3. :func:`_guard_dependents` walks the other way. The first two look *down*
     from the tree being stored and cannot see the lists that name it: one at
     the expansion cap is broken by an edit one link below, whose own save
     costs a single inline. So every write compiles the user's smart lists
     before and after itself and refuses to be the change that broke one.

  And if a broken list ever exists anyway — data edited around the API, or a
  fourth way nobody has thought of — :func:`member_counts` marks that one list
  broken instead of failing the whole page (:class:`ListCount`). One list must
  not be able to hide every other.
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
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Select, func
from sqlalchemy.orm import Session

from netkeeper.crm.filters import (
    FilterError,
    FilterTree,
    ListReferenceError,
    SortKey,
    compile_count,
    compile_filter,
    compile_where,
    list_ids_in,
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
from netkeeper.scoping import get_scoped, scoped, scoped_delete
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


class BreaksAnotherList(ListError, ValueError):
    """The write is fine in itself but would leave one of the user's other lists unreadable."""


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


def check_list_references(session: Session, user: User, tree: FilterTree) -> None:
    """Refuse a ``list_member`` naming a list ``user`` does not have. Write time only.

    Compiling such a reference matches nobody, deliberately, so that deleting
    one list cannot take down every page that reads a filter naming it
    (:mod:`netkeeper.crm.filters`). That leniency is safe on a *read* and unsafe
    on a *write*: the id a new list is about to be given does not exist yet, so
    a filter could name it, be accepted, and then be handed that very row —
    which is how two ordinary ``POST``\\ s used to close a cycle (#126 review).
    Refusing the reference here closes it, and no legitimate write needs it: the
    list you mean to name already exists when you name it.
    """
    for list_id, path in list_ids_in(tree).items():
        if get_scoped(session, user, ContactList, list_id) is None:
            raise ListReferenceError(path, f"there is no list {list_id}", (list_id,))


def _check_filter(
    session: Session, user: User, tree: FilterTree, *, list_id: int | None = None
) -> None:
    """Compile ``tree`` for ``user``; never executed, but raises for a broken or not-yet-
    supported filter so a smart list or a saved view is never stored broken.

    ``list_id`` is the list this tree is about to become, if any. Compiling
    inlines every smart list the tree names (:mod:`netkeeper.crm.filters`), so
    passing it makes a tree that reaches back to its own list — directly or
    through others — a :class:`~netkeeper.crm.filters.ListReferenceError` here,
    at the write that would close the cycle. Without it the write would succeed
    against the list's *stored* tree and the cycle would surface later as a 422
    on every page that reads either list.

    This is the guard walking *down* from the tree being written.
    :func:`_guard_dependents` walks up.
    """
    check_list_references(session, user, tree)
    compile_where(user, tree, session=session, expanding=() if list_id is None else (list_id,))


def all_lists(session: Session, user: User) -> dict[int, ContactList]:
    """Every list of ``user`` by id, in one query, for the compiler to resolve names against.

    Compiling looks a list up per distinct id otherwise, which is right for one
    tree and wrong for a caller compiling every list the user has: that turned
    one lists page over a long chain into hundreds of statements.
    """
    return {row.id: row for row in session.scalars(scoped(user, ContactList))}


def broken_lists(session: Session, user: User) -> dict[int, str]:
    """Every smart list of ``user`` whose stored filter does not compile now, and why.

    Empty in every ordinary state. A list gets in here by naming lists that
    reach back to it, or by pulling in more than
    :data:`~netkeeper.crm.filters.MAX_LIST_EXPANSIONS` of them — states the
    writes below refuse to create, but which data edited around the API can
    still reach, and which the read side has to survive rather than compound.
    """
    broken: dict[int, str] = {}
    everything = all_lists(session, user)
    for row in everything.values():
        if row.kind is not ListKind.SMART:
            continue
        try:
            compile_where(
                user,
                parse_filter(row.filter_json),
                session=session,
                expanding=(row.id,),
                all_lists=everything,
            )
        except FilterError as exc:
            broken[row.id] = str(exc)
    return broken


def _guard_dependents(session: Session, user: User, before: dict[int, str]) -> None:
    """Undo-by-raising: a write may not break a list that was working before it.

    The write-time checks above walk *down* from the tree being stored, which
    is blind to the lists that name *it*: a list at the expansion cap is broken
    by an edit one link below it, whose own save costs a single inline and
    passes (#126 review). This walks up instead, by the cheap route — compile
    every smart list of this user before the write and after it, and refuse the
    write if that turned a working list into a broken one.

    Compared against ``before`` rather than against "nothing is broken", so a
    list that was already broken never blocks the edit that would fix it, or
    any unrelated edit.

    The cost is two passes over one user's smart lists, on a list write: four
    statements for an ordinary user, and bounded by :func:`all_lists` rather
    than by the depth of their chains. A list write is a human action and lists
    are few, so that is the right side to spend on.
    """
    newly = {
        list_id: reason
        for list_id, reason in broken_lists(session, user).items()
        if list_id not in before
    }
    if not newly:
        return
    list_id, reason = next(iter(sorted(newly.items())))
    row = get_scoped(session, user, ContactList, list_id)
    name = f" ({row.name!r})" if row is not None else ""
    raise BreaksAnotherList(f"this change would leave list {list_id}{name} unreadable: {reason}")


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

    Creating a list can break an existing one, which is why this takes the same
    :func:`_guard_dependents` pass an edit does: ids are handed out by the
    database, and a filter saved when list 3 existed, outliving that list,
    means the *new* list 3 the moment one is created (SQLite hands the id back;
    a sequence does not). :class:`BreaksAnotherList` when that closes a cycle
    or overruns the expansion cap.
    """
    _require_writer(session)
    cleaned = _clean_name(name, max_length=LIST_NAME_MAX_LENGTH, what="list")
    if find_list(session, user, cleaned) is not None:
        raise DuplicateListName(f"a list named {cleaned!r} already exists")
    before = broken_lists(session, user)
    stored = _prepare_filter(session, user, kind, filter)
    row = ContactList(user_id=user.id, name=cleaned, kind=kind, filter_json=stored)
    session.add(row)
    session.flush()
    _guard_dependents(session, user, before)
    log.debug("created list %d %r (%s) for user %d", row.id, row.name, kind.value, user.id)
    return row


def _prepare_filter(
    session: Session, user: User, kind: ListKind, filter: FilterTree | None
) -> dict[str, Any] | None:
    if kind is ListKind.STATIC:
        if filter is not None:
            raise InvalidListValue("a static list has no filter; add members instead")
        return None
    if filter is None:
        raise InvalidListValue("a smart list needs a filter")
    _check_filter(session, user, filter)
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
    or name; :class:`DuplicateListName` for a name collision;
    :class:`BreaksAnotherList` when the new filter is fine in itself but leaves
    a list that names this one unreadable (:func:`_guard_dependents`).
    """
    _require_writer(session)
    before = broken_lists(session, user)
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
        _check_filter(session, user, filter, list_id=row.id)
        row.filter_json = filter.model_dump(mode="json")
    session.flush()
    _guard_dependents(session, user, before)
    return row


def delete_list(session: Session, user: User, list_id: int) -> None:
    """Delete a list; the database cascades to its members (if static).

    Never refused: a filter naming a deleted list matches nobody, so a delete
    cannot leave another list unreadable. It can leave one quietly meaning less
    than it says, so the lists that name this one are logged as they lose it.
    """
    _require_writer(session)
    row = get_list(session, user, list_id)
    naming = _lists_naming(session, user, row.id)
    session.execute(scoped_delete(user, ContactList).where(ContactList.id == row.id))
    session.expunge(row)
    if naming:
        log.warning(
            "deleted list %d for user %d; %s now names a list that is gone and matches nobody",
            list_id,
            user.id,
            ", ".join(f"list {other}" for other in naming),
        )
    log.debug("deleted list %d for user %d", list_id, user.id)


def _lists_naming(session: Session, user: User, list_id: int) -> list[int]:
    """The ids of ``user``'s smart lists whose own filter names ``list_id`` directly."""
    smart = session.scalars(scoped(user, ContactList).where(ContactList.kind == ListKind.SMART))
    return [row.id for row in smart if list_id in list_ids_in(parse_filter(row.filter_json))]


# --- membership -------------------------------------------------------------


def add_members(session: Session, user: User, list_id: int, contact_ids: Sequence[int]) -> int:
    """Add contacts to a static list; returns how many were newly added.

    An id already a member, or repeated in ``contact_ids``, is not recounted.
    :class:`WrongListKind` for a smart list; :class:`ContactNotFound` for an id
    that is not one of ``user``'s contacts. Checks ownership of every id in one
    query, not one per id: "First 100" is exactly the batch this is for.
    """
    _require_writer(session)
    row = get_list(session, user, list_id)
    if row.kind is not ListKind.STATIC:
        raise WrongListKind("only a static list has members to add")
    unique_ids = list(dict.fromkeys(contact_ids))
    if not unique_ids:
        return 0
    owned_ids = set(
        session.scalars(
            scoped(user, Contact).with_only_columns(Contact.id).where(Contact.id.in_(unique_ids))
        )
    )
    missing = [contact_id for contact_id in unique_ids if contact_id not in owned_ids]
    if missing:
        raise ContactNotFound(f"no contact {missing[0]}")
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


def _static_members_base(user: User, row: ContactList) -> Select[tuple[Contact]]:
    """The live contacts explicitly in static list ``row``: joined to their membership row.

    "Live" matches what every smart list means by it by default
    (:func:`netkeeper.crm.filters.compile_where`): not merged away
    (``merged_into_id``) and not archived (``archived_at``). Excluding a
    merged-away contact is not a choice — ``identity.merge()`` strips its
    ``li_urn``/``li_public_id`` and moves its emails and phones to the
    survivor, so the membership row would otherwise point at a tombstone with
    a real person missing from the list. Excluding an archived one is a
    deliberate choice to keep a static list's members reading the same as a
    smart list's default (``include_archived=False``); a static list has no
    override for it, so an archived member never shows here even though the
    ``list_members`` row itself is left alone (it reappears if the contact is
    unarchived). :func:`list_members` and :func:`member_count` share this
    query so the two can never drift on what "live" means.
    """
    return (
        scoped(user, Contact)
        .join(ListMember, ListMember.contact_id == Contact.id)
        .where(
            ListMember.list_id == row.id,
            ListMember.user_id == user.id,
            Contact.merged_into_id.is_(None),
            Contact.archived_at.is_(None),
        )
    )


def list_members(
    session: Session, user: User, list_id: int, *, limit: int, offset: int = 0
) -> tuple[list[Contact], int]:
    """One page of the list's members, newest to oldest for a static list, and the total count.

    A smart list's members are exactly what :func:`netkeeper.crm.filters.compile_filter`
    returns for its stored tree, run fresh: nothing here materializes them, so this
    always agrees with a direct run of the filter (see the "done when" for P1-08). A
    static list's members are its live rows (see :func:`_static_members_base`) — the
    same liveness rule a smart list applies by default, so the two never quietly
    disagree about who exists. :class:`ListNotFound`; ``ValueError`` for a bad page.
    """
    _check_page(limit, offset)
    row = get_list(session, user, list_id)
    if row.kind is ListKind.STATIC:
        base = _static_members_base(user, row)
        total = session.scalar(base.with_only_columns(func.count())) or 0
        contacts = session.scalars(
            base.order_by(ListMember.added_at.desc(), Contact.id.desc()).limit(limit).offset(offset)
        ).all()
        return list(contacts), total
    tree = parse_filter(row.filter_json)
    total = session.scalar(compile_count(user, tree, session=session)) or 0
    contacts = session.scalars(
        compile_filter(user, tree, session=session).order_by(Contact.id).limit(limit).offset(offset)
    ).all()
    return list(contacts), total


def _member_count_for(
    session: Session,
    user: User,
    row: ContactList,
    everything: dict[int, ContactList] | None = None,
) -> int:
    """:func:`member_count`'s logic, given the row already in hand (no re-fetch).

    ``everything`` is :func:`all_lists`' result when the caller has it, so a
    page of counts resolves every ``list_member`` in it from one query.
    """
    if row.kind is ListKind.STATIC:
        base = _static_members_base(user, row)
        return session.scalar(base.with_only_columns(func.count())) or 0
    tree = parse_filter(row.filter_json)
    statement = compile_count(user, tree, session=session, all_lists=everything)
    return session.scalar(statement) or 0


def member_count(session: Session, user: User, list_id: int) -> int:
    """How many live contacts are in the list right now (see :func:`_static_members_base`)."""
    row = get_list(session, user, list_id)
    return _member_count_for(session, user, row)


@dataclass(frozen=True)
class ListCount:
    """How many members a list has, or why nobody can say.

    ``broken`` is the reason its filter does not compile, for the one list it
    belongs to; ``count`` is 0 then, because there is no number to give.
    """

    count: int
    broken: str | None = None


def member_counts(
    session: Session, user: User, rows: Sequence[ContactList]
) -> dict[int, ListCount]:
    """:func:`member_count` for several already-loaded lists at once, by id (0 for an empty one).

    Takes the rows themselves, typically ``list_lists(session, user)``'s result, not ids:
    a caller that already has them (every current one does) never re-fetches what it holds,
    which is what turned a single ``GET /lists`` into two statements per list.

    **One list may not take the page down with it.** A filter that does not
    compile is counted as :class:`ListCount` with its reason rather than
    raised, so the answer still names every list the user has — including the
    broken one, which is the only place they can see that it is broken and
    which one it is. The writes above try hard to make this state unreachable;
    if one gets through anyway, the page that would let someone fix it is the
    last thing that should fail. :func:`member_count`, asked about one list,
    still raises: a caller who named that list wants the error.
    """
    counts: dict[int, ListCount] = {}
    everything = all_lists(session, user)
    for row in rows:
        try:
            counts[row.id] = ListCount(_member_count_for(session, user, row, everything))
        except FilterError as exc:
            log.warning("list %d of user %d does not compile: %s", row.id, user.id, exc)
            counts[row.id] = ListCount(0, str(exc))
    return counts


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
        _check_filter(session, user, filter)
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
            _check_filter(session, user, filter)
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
    """Seed the built-in "Validated" smart list for ``user`` once; the row created or
    adopted, or None once already seeded.

    Idempotent: after the first call ``settings_kv`` records
    :data:`VALIDATED_SEEDED_KEY` and later calls return None, so a list the
    user deleted or renamed is never re-created. The list it creates is marked
    ``builtin`` (#133), a record of where the row came from that survives any
    rename or edit and protects nothing. A list the user already has
    named exactly :data:`VALIDATED_LIST_NAME` is *adopted* as it is, and stays
    unmarked, because the user made it; it is adopted the way
    :func:`netkeeper.crm.tags.ensure_default_rules` reuses an existing tag by
    name — including when that list is a static one, which leaves the user
    with a built-in named "Validated" that is not smart at all. Unreachable
    today (nothing else creates a list by that name before this runs), and
    ``tags.py``'s defaults have the same limitation for a tag a user
    pre-creates by a default's name, so this is documented rather than
    guarded against.

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
        row.builtin = True
        session.flush()
        log.info("seeded built-in list %r for user %d", VALIDATED_LIST_NAME, user.id)
    set_setting(session, user, VALIDATED_SEEDED_KEY, True)
    return row
