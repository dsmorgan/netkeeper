"""Declarative base, portable column types, and the mixins every table builds on.

Spec section 8 and ADR 0005: datetimes are stored naive UTC and returned aware
(:class:`UTCDateTime`); every user-owned table carries a non-null, indexed
``user_id`` (:class:`UserOwned`); constraint names follow one convention so
Alembic batch mode on SQLite can find and drop them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:
    from sqlalchemy.engine import Dialect

    from netkeeper.models.user import User

# Alembic's batch mode recreates SQLite tables from reflection, and it can only drop
# a constraint it can name. Every constraint gets a deterministic name from here.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """The current time, timezone-aware, in UTC."""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """A ``DateTime`` column that stores naive UTC and returns aware UTC.

    Binding a naive datetime raises :class:`ValueError`: a naive value has no
    offset, so storing it as UTC would silently shift local times.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTCDateTime needs a timezone-aware datetime, got a naive one")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            return value.astimezone(UTC)
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    """Declarative base for every netkeeper table."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """``created_at`` and ``updated_at``, set in Python so the values are aware UTC."""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, sort_order=100
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow, sort_order=101
    )


class UserOwned:
    """Marks a table as belonging to one user (ADR 0005).

    ``user_id`` is non-null and indexed; deleting the user deletes the rows.
    Uniqueness constraints on the table must include ``user_id``.
    """

    @declared_attr
    def user_id(cls) -> Mapped[int]:
        return mapped_column(
            ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True, sort_order=-90
        )

    @declared_attr
    def user(cls) -> Mapped[User]:
        return relationship("User")
