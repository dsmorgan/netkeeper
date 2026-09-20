"""``settings_kv``: runtime-adjustable settings, counters, and flags per user (spec 8.4)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from netkeeper.models.base import Base, TimestampMixin, UserOwned

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None


class SettingKV(UserOwned, TimestampMixin, Base):
    __tablename__ = "settings_kv"
    __table_args__ = (UniqueConstraint("user_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    value: Mapped[JsonValue] = mapped_column(JSON, nullable=False)
