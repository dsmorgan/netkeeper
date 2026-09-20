"""``settings_kv``: runtime-adjustable values per user (spec 8.4 and 15).

The first consumer of :mod:`netkeeper.scoping`. Values are JSON and a key is
unique per user. Seeding from ``config.toml`` on first start is a later item;
nothing here reads the config.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, Insert
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from netkeeper.models import JsonValue, SettingKV, User
from netkeeper.models.base import utcnow
from netkeeper.scoping import SCOPE_OPTION, scoped, scoped_delete


def get_setting(session: Session, user: User, key: str, default: JsonValue = None) -> JsonValue:
    """The value stored under ``key`` for ``user``, or ``default`` when there is none."""
    row = _find(session, user, key)
    return default if row is None else row.value


def set_setting(session: Session, user: User, key: str, value: JsonValue) -> SettingKV:
    """Store ``value`` under ``key`` for ``user``, creating or updating the row.

    One ``INSERT ... ON CONFLICT DO UPDATE`` on the ``(user_id, key)`` unique, so
    two writers racing on the same key both succeed and the last one wins, where
    a select-then-insert would hand one of them an ``IntegrityError``. Returns
    the row as the database now has it.
    """
    session.execute(_upsert(session, user, key, value))
    row = _find(session, user, key, refresh=True)
    if row is None:
        raise RuntimeError(f"setting {key!r} for user {user.id} vanished after its upsert")
    return row


def delete_setting(session: Session, user: User, key: str) -> bool:
    """Delete ``key`` for ``user``; True when a row was deleted."""
    statement = scoped_delete(user, SettingKV).where(SettingKV.key == key)
    # Session.execute() is typed as the plain Result in SQLAlchemy 2.0; DML gets a CursorResult.
    result = cast(CursorResult[Any], session.execute(statement))
    return result.rowcount > 0


def _upsert(session: Session, user: User, key: str, value: JsonValue) -> Insert:
    """The dialect's upsert. Marked with the scope option: the guard does not inspect
    inserts, but the mark keeps the convention visible in one place."""
    dialect = session.get_bind().dialect.name
    changes = {"value": value, "updated_at": utcnow()}
    conflict = ["user_id", "key"]
    statement: Insert
    if dialect == "sqlite":
        statement = sqlite.insert(SettingKV).on_conflict_do_update(
            index_elements=conflict, set_=changes
        )
    elif dialect == "postgresql":
        statement = postgresql.insert(SettingKV).on_conflict_do_update(
            index_elements=conflict, set_=changes
        )
    else:
        raise NotImplementedError(f"settings_kv upsert is not implemented for {dialect}")
    return statement.values(user_id=user.id, key=key, value=value).execution_options(
        **{SCOPE_OPTION: user.id}
    )


def _find(session: Session, user: User, key: str, *, refresh: bool = False) -> SettingKV | None:
    statement = scoped(user, SettingKV).where(SettingKV.key == key)
    if refresh:
        # After a Core upsert the identity map may hold the row with its old value.
        statement = statement.execution_options(populate_existing=True)
    return session.scalars(statement).one_or_none()
