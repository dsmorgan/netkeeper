"""``settings_kv``: runtime-adjustable values per user (spec 8.4 and 15).

The first consumer of :mod:`netkeeper.scoping`. Values are JSON and a key is
unique per user. Seeding from ``config.toml`` on first start is a later item;
nothing here reads the config.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult
from sqlalchemy.orm import Session

from netkeeper.models import JsonValue, SettingKV, User
from netkeeper.scoping import scoped, scoped_delete


def get_setting(session: Session, user: User, key: str, default: JsonValue = None) -> JsonValue:
    """The value stored under ``key`` for ``user``, or ``default`` when there is none."""
    row = _find(session, user, key)
    return default if row is None else row.value


def set_setting(session: Session, user: User, key: str, value: JsonValue) -> SettingKV:
    """Store ``value`` under ``key`` for ``user``, creating or updating the row.

    Flushes, so the row has an id and the ``(user_id, key)`` unique constraint has
    been checked when this returns.
    """
    row = _find(session, user, key)
    if row is None:
        row = SettingKV(user_id=user.id, key=key, value=value)
        session.add(row)
    else:
        row.value = value
    session.flush()
    return row


def delete_setting(session: Session, user: User, key: str) -> bool:
    """Delete ``key`` for ``user``; True when a row was deleted."""
    statement = scoped_delete(user, SettingKV).where(SettingKV.key == key)
    # Session.execute() is typed as the plain Result in SQLAlchemy 2.0; DML gets a CursorResult.
    result = cast(CursorResult[Any], session.execute(statement))
    return result.rowcount > 0


def _find(session: Session, user: User, key: str) -> SettingKV | None:
    return session.scalars(scoped(user, SettingKV).where(SettingKV.key == key)).one_or_none()
