"""The ``users`` table (spec section 8, ADR 0005).

One row of kind ``local`` exists in v1, created at first start. The table is not
user-owned itself; everything else hangs off it.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from netkeeper.models.base import Base, UTCDateTime, utcnow


class UserKind(enum.StrEnum):
    LOCAL = "local"
    HOSTED = "hosted"


def _enum_values(kind: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in kind]


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    # Stored as VARCHAR plus a CHECK constraint, which renders the same on SQLite and
    # PostgreSQL; a native enum type would not.
    kind: Mapped[UserKind] = mapped_column(
        Enum(
            UserKind,
            name="user_kind",
            native_enum=False,
            length=16,
            create_constraint=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=UserKind.LOCAL,
    )
    display_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
