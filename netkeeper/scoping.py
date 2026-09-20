"""User scoping: the query helper and the runtime guard (spec section 5, ADR 0005).

Every query against a :class:`UserOwned` table goes through :func:`scoped`,
:func:`scoped_update`, :func:`scoped_delete`, or :func:`get_scoped`. Each applies
``WHERE user_id = :id`` and marks the statement with the execution option
``netkeeper_scope``. :func:`install_scope_guard` puts a ``do_orm_execute`` listener
on a session factory that rejects any select, update, or delete touching an owned
table without that mark. :func:`unscoped` is the explicit escape hatch for the few
legitimate cross-user statements (the scheduler iterating users, admin tooling).

What the guard covers:

- Everything that goes through ``Session.execute()`` and its shorthands
  (``scalars()``, ``scalar()``), ``Session.get()``, ORM and Core ``update()`` and
  ``delete()``, and Core ``select(table)``. The statement tree is walked for
  tables, so joins, subqueries, CTEs, aliases, and unions are covered wherever
  the owned table appears.
- A flush that would insert an owned row with no ``user_id`` (``before_flush``),
  so the error names the model instead of surfacing as an ``IntegrityError``.

What it does not:

- Column refreshes and relationship lazy loads. They load by the identity of a
  row the session already holds, which arrived through a scoped statement, and
  the scope option does not propagate to them.
- Unit-of-work flushes of tracked objects (their INSERT, UPDATE, and DELETE).
  Those rows came from scoped statements too; new rows are checked as above.
- ``text()`` statements, and anything executed on a ``Connection`` rather than a
  ``Session``. Neither can be inspected. Keep them out of services.
- Whether a ``netkeeper_scope`` mark is honest. Only the helpers here set it;
  setting it by hand, or passing it to ``Session.get()``, is a review matter.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Delete, Select, Update, delete, event, inspect, select, update
from sqlalchemy.orm import (
    InstanceState,
    ORMExecuteState,
    Session,
    UOWTransaction,
    class_mapper,
    sessionmaker,
)
from sqlalchemy.sql import ClauseElement, visitors
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.expression import TableClause

from netkeeper.models import Base, User, UserOwned

log = logging.getLogger(__name__)

SCOPE_OPTION = "netkeeper_scope"
UNSCOPED_OPTION = "netkeeper_unscoped"
_GUARD_MARKER = "_netkeeper_scope_guard"


class UnscopedQueryError(RuntimeError):
    """A statement touched a user-owned table without going through the scoping helper."""


# --- the helper -------------------------------------------------------------


def scoped[T: UserOwned](user: User, model: type[T]) -> Select[tuple[T]]:
    """``select(model)`` filtered to ``user``'s rows and marked for the guard."""
    return select(model).where(model.user_id == user.id).execution_options(**_scope_of(user))


def scoped_update[T: UserOwned](user: User, model: type[T]) -> Update:
    """``update(model)`` filtered to ``user``'s rows and marked for the guard."""
    return update(model).where(model.user_id == user.id).execution_options(**_scope_of(user))


def scoped_delete[T: UserOwned](user: User, model: type[T]) -> Delete:
    """``delete(model)`` filtered to ``user``'s rows and marked for the guard."""
    return delete(model).where(model.user_id == user.id).execution_options(**_scope_of(user))


def get_scoped[T: UserOwned](session: Session, user: User, model: type[T], id: int) -> T | None:
    """The ``model`` row with primary key ``id`` if it belongs to ``user``, else None.

    The replacement for ``Session.get()``, which has no user filter and which the
    guard rejects for owned models. The filter is in the SQL, so another user's
    row is never loaded, not even into the identity map.
    """
    (key,) = class_mapper(model).primary_key
    return session.scalars(scoped(user, model).where(key == id)).one_or_none()


def unscoped[S: Executable](statement: S) -> S:
    """Mark ``statement`` as deliberately cross-user so the guard lets it through.

    For the scheduler iterating users, admin tooling, and tests that look at a
    whole table. Every call site is a review item (ADR 0005).
    """
    names = [model.__name__ for model in owned_models_in(statement)]
    log.debug("unscoped statement on %s", ", ".join(names) if names else "no owned table")
    return statement.execution_options(**{UNSCOPED_OPTION: True})


def owned_models_in(statement: Executable | ClauseElement) -> list[type[UserOwned]]:
    """The owned models whose tables appear anywhere in ``statement``, sorted by name.

    Tables are matched by name, which also covers Core ``table()`` clauses,
    aliases, and the annotated copies the ORM puts in its statements.
    """
    if not isinstance(statement, ClauseElement):
        return []  # text() and friends: nothing to walk
    by_table = _owned_tables()
    found = {
        by_table[element.name]
        for element in visitors.iterate(statement)
        if isinstance(element, TableClause) and element.name in by_table
    }
    return sorted(found, key=lambda model: model.__name__)


def _scope_of(user: User) -> dict[str, Any]:
    return {SCOPE_OPTION: user.id}


def _owned_tables() -> dict[str, type[UserOwned]]:
    """Table name to model for every mapped ``UserOwned`` class."""
    owned: dict[str, type[UserOwned]] = {}
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if isinstance(table, TableClause) and issubclass(mapper.class_, UserOwned):
            owned[table.name] = mapper.class_
    return owned


# --- the guard --------------------------------------------------------------


def install_scope_guard(
    session_factory: sessionmaker[Session], *, raise_on_violation: bool = True
) -> None:
    """Reject unscoped statements against owned tables on every session the factory makes.

    The listeners go on the factory's own ``Session`` subclass, so a guard on one
    factory never reaches another (tests install it on theirs) and the plain
    ``Session`` class is untouched. Installing twice on one factory is a no-op.

    ``raise_on_violation=False`` logs the violation at ERROR and lets the
    statement run. It exists so the guard's own tests can observe a violation;
    ``create_app`` and the test fixtures always use the default.
    """
    if getattr(session_factory.class_, _GUARD_MARKER, None) is not None:
        return
    guard = _ScopeGuard(raise_on_violation=raise_on_violation)
    event.listen(session_factory, "do_orm_execute", guard.on_execute)
    event.listen(session_factory, "before_flush", guard.on_flush)
    setattr(session_factory.class_, _GUARD_MARKER, guard)


class _ScopeGuard:
    def __init__(self, *, raise_on_violation: bool) -> None:
        self.raise_on_violation = raise_on_violation

    def on_execute(self, state: ORMExecuteState) -> None:
        if not (state.is_select or state.is_update or state.is_delete):
            return  # inserts carry user_id by construction; on_flush checks new rows
        if state.is_column_load or state.is_relationship_load:
            return  # loads by the identity of a row the session already holds
        options = state.execution_options
        if options.get(UNSCOPED_OPTION) or SCOPE_OPTION in options:
            return
        owned = owned_models_in(state.statement)
        if not owned:
            return
        kind = "update" if state.is_update else "delete" if state.is_delete else "select"
        names = ", ".join(model.__name__ for model in owned)
        self._violation(
            f"unscoped {kind} touching {names}: build it with scoped(user, {owned[0].__name__}), "
            "scoped_update(), scoped_delete(), or get_scoped() in place of Session.get(); "
            "wrap a deliberate cross-user statement in unscoped()"
        )

    def on_flush(
        self, session: Session, flush_context: UOWTransaction, instances: object | None
    ) -> None:
        for obj in session.new:
            if isinstance(obj, UserOwned) and not _has_owner(obj):
                self._violation(
                    f"new {type(obj).__name__} has no user_id: set user_id (or user) "
                    "before adding it to the session"
                )

    def _violation(self, message: str) -> None:
        if self.raise_on_violation:
            raise UnscopedQueryError(message)
        log.error("scope guard: %s", message)


def _has_owner(obj: UserOwned) -> bool:
    # An attribute never set is absent from the instance dict; an explicit None is present.
    state: InstanceState[UserOwned] = inspect(obj, raiseerr=True)
    values = state.dict
    return values.get("user_id") is not None or values.get("user") is not None
